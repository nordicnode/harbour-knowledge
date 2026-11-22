from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..db import fetch_all, fetch_one
from ..deps import Context, get_context, require_editor
from ..errors import not_found, validation
from ..ingest.pipeline import PIPELINE_VERSION, enqueue_job
from ..ingest.storage import save_bytes

router = APIRouter(tags=["sources"])

KIND_BY_EXT = {
    ".pdf": "pdf", ".docx": "docx", ".pptx": "pptx", ".xlsx": "xlsx",
    ".html": "html", ".htm": "html", ".epub": "epub", ".txt": "text",
    ".md": "markdown", ".markdown": "markdown", ".csv": "text", ".log": "text",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
}
TIERS = {"system_of_record", "official_doc", "wiki", "chat_export"}
SourceTier = Literal["system_of_record", "official_doc", "wiki", "chat_export"]
MAX_UPLOAD_CHUNK = 64 * 1024


class UrlSourceBody(BaseModel):
    """The JSON form of POST /v1/sources (used for URL ingestion)."""

    model_config = ConfigDict(extra="ignore")

    url: str
    visibility: Literal["internal", "public"] = "internal"
    source_tier: SourceTier = "official_doc"
    plan_scope: list[str] | str | None = None
    product_version_scope: list[str] | str | None = None
    title: str | None = None


class BulkDeleteBody(BaseModel):
    ids: list[UUID] = Field(min_length=1, max_length=200)


def _upload_limit_message(max_bytes: int) -> str:
    return f"Files are limited to {max(1, max_bytes // (1024 * 1024))} MB."


def _declared_upload_too_large(request: Request, max_bytes: int) -> bool:
    """Reject an oversized multipart body before Starlette parses it."""
    raw = request.headers.get("content-length")
    if not raw or not raw.isdigit():
        return False
    # Leave room for multipart boundaries and the small metadata fields.
    return int(raw) > max_bytes + (1024 * 1024)


async def _read_upload(file, max_bytes: int) -> bytes:
    """Read an upload in bounded chunks instead of materialising it blindly."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(MAX_UPLOAD_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise validation(_upload_limit_message(max_bytes))
        chunks.append(chunk)
    return b"".join(chunks)


def _list_value(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    else:
        items = str(value).split(",")
    return sorted({item.strip().lower() for item in items if str(item).strip()})


def _kind_for(filename: str, mime: str | None) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in KIND_BY_EXT:
        return KIND_BY_EXT[suffix]
    mime = (mime or "").lower()
    if "pdf" in mime:
        return "pdf"
    if "wordprocessingml" in mime:
        return "docx"
    if "presentationml" in mime:
        return "pptx"
    if "html" in mime:
        return "html"
    if mime.startswith("text/"):
        return "text"
    raise validation(f"Unsupported file type '{suffix or mime}'. Upload a PDF, DOCX, PPTX, HTML, or text file.")


def _job_key(source_id: UUID, digest: str) -> str:
    return f"ingest:{source_id}:{digest}:{PIPELINE_VERSION}"


async def _close_review_items_for_source(
    ctx: Context, source_id: UUID, source_title: str
) -> tuple[int, int]:
    """Close decisions that the removed source made moot.

    Review rows stay for audit, but they leave the pending queue. Conflicts
    involving a passage from the source are dismissed: the pair can no longer
    be reconciled because one side is gone. Answer drafts that cite the source
    are closed too, since approval would reject their tombstoned evidence.
    """
    note = f"Closed automatically: “{source_title}” was removed."
    dismissed = await fetch_all(
        ctx.conn,
        """with source_passages as (
             select p.id from passages p
             join documents d on d.id = p.document_id
             where d.source_id = %s
           )
           update conflicts c
           set status = 'dismissed', resolution = %s, resolved_at = now(), updated_at = now()
           where c.tenant_id = %s and c.status = 'pending'
             and (c.passage_id_a in (select id from source_passages)
                  or c.passage_id_b in (select id from source_passages))
           returning c.id""",
        (source_id, f"Source removed: {source_title}", ctx.session.tenant_id),
    )
    closed = await fetch_all(
        ctx.conn,
        """with source_passages as (
             select p.id from passages p
             join documents d on d.id = p.document_id
             where d.source_id = %s
           ),
           source_conflicts as (
             select c.id from conflicts c
             where c.tenant_id = %s
               and (c.passage_id_a in (select id from source_passages)
                    or c.passage_id_b in (select id from source_passages))
           )
           update review_items ri
           set status = 'rejected', decision_note = %s, decided_at = now(),
               undo_payload = null, undo_expires_at = null
           where ri.tenant_id = %s and ri.status = 'pending'
             and (
               ri.payload->>'source_id' = %s
               or ri.payload->>'new_passage_id' in (select id::text from source_passages)
               or ri.payload->>'existing_passage_id' in (select id::text from source_passages)
               or exists (
                 select 1 from source_passages sp
                 where coalesce(ri.payload->'evidence_passage_ids', '[]'::jsonb) ? sp.id::text
               )
               or ri.payload->>'conflict_id' in (select id::text from source_conflicts)
               or (
                 ri.kind in ('new_answer', 'edit_proposal')
                 and exists (
                   select 1
                   from answer_evidence ae
                   join source_passages sp on sp.id = ae.passage_id
                   join canonical_answers ca on ca.id = ae.answer_id
                   where ca.tenant_id = ri.tenant_id
                     and ca.id::text = ri.payload->>'answer_id'
                     and ae.answer_version = ca.current_version
                 )
               )
             )
           returning ri.id""",
        (
            source_id,
            ctx.session.tenant_id,
            note,
            ctx.session.tenant_id,
            str(source_id),
        ),
    )
    return len(closed), len(dismissed)


async def _mark_answers_for_recheck(ctx: Context, source_id: UUID) -> int:
    """Take approved answers out of the ready set when their evidence is removed.

    The answer is kept for audit and can be edited and re-approved, but it must
    not keep advertising itself as ready after the source behind it is gone.
    """
    rows = await fetch_all(
        ctx.conn,
        """with affected as (
             select distinct ae.answer_id
             from answer_evidence ae
             join passages p on p.id = ae.passage_id
             join documents d on d.id = p.document_id
             join canonical_answers ca
               on ca.id = ae.answer_id and ca.current_version = ae.answer_version
             where d.source_id = %s
               and ca.tenant_id = %s
               and ca.status = 'approved'
           )
           update canonical_answers ca
           set status = 'needs_recheck', updated_at = now()
           where ca.id in (select answer_id from affected)
           returning ca.id""",
        (source_id, ctx.session.tenant_id),
    )
    return len(rows)


async def _withdraw_source(ctx: Context, source_id: UUID) -> bool:
    """Withdraw one source and its artifacts, waiting for an in-flight ingest."""
    await ctx.conn.execute(
        "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (str(source_id),),
    )
    source = await fetch_one(ctx.conn, "select * from sources where id = %s", (source_id,))
    if source is None or source["deleted_at"] is not None:
        return False
    closed_review_items, dismissed_conflicts = await _close_review_items_for_source(
        ctx, source_id, source["title"]
    )
    answers_needs_recheck = await _mark_answers_for_recheck(ctx, source_id)
    await ctx.conn.execute(
        """update passages set status = 'tombstoned'
           where document_id in (select id from documents where source_id = %s)""",
        (source_id,),
    )
    await ctx.conn.execute(
        "update documents set status = 'withdrawn', updated_at = now() where source_id = %s",
        (source_id,),
    )
    await ctx.conn.execute(
        "update sources set status = 'withdrawn', deleted_at = now(), updated_at = now() where id = %s",
        (source_id,),
    )
    await ctx.conn.execute(
        """update ingest_jobs
           set status = 'failed', stage = 'withdrawn', error = 'Source removed',
               finished_at = now(), updated_at = now()
           where source_id = %s and status in ('queued','running')""",
        (source_id,),
    )
    await ctx.conn.execute(
        """insert into audit_log (tenant_id, actor_type, actor_id, action, object_type, object_id, detail)
           values (%s, 'user', %s, 'source.deleted', 'source', %s, %s)""",
        (
            ctx.session.tenant_id,
            ctx.session.user_id,
            source_id,
            Jsonb({
                "closed_review_items": closed_review_items,
                "dismissed_conflicts": dismissed_conflicts,
                "answers_needs_recheck": answers_needs_recheck,
            }),
        ),
    )
    return True


@router.post("/sources", status_code=202)
async def create_source(request: Request, ctx: Context = Depends(get_context)):
    require_editor(ctx)
    content_type = request.headers.get("content-type", "")
    file = None
    url: str | None = None
    visibility: Literal["internal", "public"] = "internal"
    source_tier: str = "official_doc"
    plan_scope: list[str] = []
    version_scope: list[str] = []
    filename: str | None = None
    mime_type: str | None = None
    data = b""

    if "application/json" in content_type:
        try:
            body = UrlSourceBody.model_validate(await request.json())
        except (ValidationError, ValueError) as exc:
            raise validation("Send a JSON object with a url and valid source fields.") from exc
        url = body.url
        visibility = body.visibility
        source_tier = body.source_tier
        plan_scope = _list_value(body.plan_scope)
        version_scope = _list_value(body.product_version_scope)
        title_override = body.title
    else:
        if _declared_upload_too_large(request, ctx.settings.max_upload_bytes):
            raise validation(_upload_limit_message(ctx.settings.max_upload_bytes))
        form = await request.form()
        file = form.get("file")
        url = form.get("url")
        visibility = form.get("visibility", "internal")
        source_tier = form.get("source_tier", "official_doc")
        plan_scope = _list_value(form.getlist("plan_scope") or form.get("plan_scope"))
        version_scope = _list_value(
            form.getlist("product_version_scope") or form.get("product_version_scope")
        )
        title_override = form.get("title")
        has_file = file is not None and hasattr(file, "read")
        if has_file:
            filename = getattr(file, "filename", None)
            mime_type = getattr(file, "content_type", None)
            data = await _read_upload(file, ctx.settings.max_upload_bytes)
        else:
            file = None

    if visibility not in ("internal", "public"):
        raise validation("visibility must be internal or public.")
    if source_tier not in TIERS:
        raise validation("Unknown source tier.")

    if file is not None:
        if not data:
            raise validation("The uploaded file is empty.")
        if len(data) > ctx.settings.max_upload_bytes:
            raise validation(_upload_limit_message(ctx.settings.max_upload_bytes))
        filename = filename or "upload"
        kind = _kind_for(filename, mime_type or mimetypes.guess_type(filename)[0])
        digest = hashlib.sha256(data).hexdigest()
        title = (title_override or Path(filename).stem).replace("_", " ").replace("-", " ").strip()
        origin_url = None
    elif url:
        parsed = urlparse(str(url))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise validation("Enter a valid http(s) URL.")
        url = str(url)
        kind = "url"
        digest = hashlib.sha256(f"url:{url}".encode()).hexdigest()
        title = title_override or (parsed.hostname + parsed.path).rstrip("/")
        filename = Path(parsed.path).name or "page.html"
        mime_type = None
        data = b""
        origin_url = url
    else:
        raise validation("Upload a file or provide a URL.")

    source_id = uuid4()

    duplicate = None
    if kind != "url":
        duplicate = await fetch_one(
            ctx.conn,
            """select id, title from sources
               where hash_sha256 = %s and alias_of_id is null and deleted_at is null
                 and status in ('active','processing')
               order by created_at limit 1""",
            (digest,),
        )

    if duplicate:
        row = await fetch_one(
            ctx.conn,
            """insert into sources
                 (id, tenant_id, kind, title, origin_url, mime_type, size_bytes, hash_sha256,
                  source_tier, visibility, status, alias_of_id, ingested_at, metadata, plan_scope,
                  product_version_scope)
               values (%s, %s, %s, %s, null, %s, %s, %s, %s, %s, 'active', %s, now(), %s, %s, %s)
               returning *""",
            (
                source_id, ctx.session.tenant_id, kind, title, mime_type, len(data), digest,
                source_tier, visibility, duplicate["id"],
                Jsonb({"filename": filename, "duplicate_of": duplicate["title"]}),
                plan_scope, version_scope,
            ),
        )
        job = await fetch_one(
            ctx.conn,
            """insert into ingest_jobs (tenant_id, source_id, status, stage, idempotency_key, stats, finished_at)
               values (%s, %s, 'succeeded', 'alias', %s, %s, now()) returning id""",
            (
                ctx.session.tenant_id, source_id, f"alias:{source_id}",
                Jsonb({"duplicate_of": str(duplicate["id"])}),
            ),
        )
        return {
            "id": str(source_id),
            "title": title,
            "status": "active",
            "duplicate_of": str(duplicate["id"]),
            "job_id": str(job["id"]) if job else None,
        }

    storage_path = None
    if data:
        storage_path = save_bytes(ctx.session.tenant_id, source_id, filename or "upload", data)

    source = await fetch_one(
        ctx.conn,
        """insert into sources
             (id, tenant_id, kind, title, origin_url, storage_path, mime_type, size_bytes, hash_sha256,
              source_tier, visibility, status, metadata, plan_scope, product_version_scope)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'processing', %s, %s, %s)
           returning *""",
        (
            source_id, ctx.session.tenant_id, kind, title, origin_url, storage_path, mime_type,
            len(data) if data else None, digest, source_tier, visibility,
            Jsonb({"filename": filename, "uploaded_by": str(ctx.session.user_id)}),
            plan_scope, version_scope,
        ),
    )
    assert source is not None
    job_id = await enqueue_job(ctx.conn, ctx.session.tenant_id, source_id, _job_key(source_id, digest))
    # The worker polls committed jobs, so the request never races a background
    # task against its own open transaction.
    return {"id": str(source_id), "title": source["title"], "status": "processing", "job_id": str(job_id)}


@router.get("/sources")
async def list_sources(ctx: Context = Depends(get_context)):
    rows = await fetch_all(
        ctx.conn,
        """select s.*, d.id as document_id, d.version as document_version, d.doc_type,
                  d.topic_id, t.name as topic_name,
                  (select count(*) from passages p where p.document_id = d.id and p.level = 'chunk') as passage_count,
                  j.status as job_status, j.stage as job_stage, j.error as job_error,
                  (select count(*) from sources a where a.alias_of_id = s.id) as alias_count
           from sources s
           left join lateral (
             select * from documents where source_id = s.id and status = 'current' limit 1
           ) d on true
           left join topics t on t.id = d.topic_id
           left join lateral (
             select * from ingest_jobs where source_id = s.id order by created_at desc limit 1
           ) j on true
           where s.alias_of_id is null and s.deleted_at is null
           order by s.created_at desc""",
    )
    return {"sources": rows}


@router.post("/sources/bulk-delete")
async def bulk_delete_sources(body: BulkDeleteBody, ctx: Context = Depends(get_context)):
    require_editor(ctx)
    deleted = 0
    missing: list[str] = []
    # Sorted ids give concurrent bulk requests the same advisory-lock order.
    for source_id in sorted(set(body.ids), key=str):
        if await _withdraw_source(ctx, source_id):
            deleted += 1
        else:
            missing.append(str(source_id))
    return {"deleted": deleted, "missing": missing}


@router.get("/sources/{source_id}")
async def get_source(source_id: UUID, ctx: Context = Depends(get_context)):
    source = await fetch_one(ctx.conn, "select * from sources where id = %s", (source_id,))
    if source is None or source["deleted_at"] is not None:
        raise not_found("That source does not exist.")
    document = await fetch_one(
        ctx.conn,
        """select d.*, t.name as topic_name from documents d
           left join topics t on t.id = d.topic_id
           where d.source_id = %s order by d.version desc limit 1""",
        (source_id,),
    )
    passages = []
    if document:
        passages = await fetch_all(
            ctx.conn,
            """select id, level, text, section_path, page_ref, chunk_index, status, token_count,
                      contextual_blurb
               from passages where document_id = %s order by level, chunk_index""",
            (document["id"],),
        )
    jobs = await fetch_all(
        ctx.conn,
        "select id, status, stage, error, attempts, stats, created_at, finished_at from ingest_jobs where source_id = %s order by created_at desc",
        (source_id,),
    )
    alias_of = None
    if source["alias_of_id"]:
        alias_of = await fetch_one(
            ctx.conn, "select id, title from sources where id = %s", (source["alias_of_id"],)
        )
    content = document["content_md"] if document else ""
    return {
        "source": source,
        "document": {**document, "content_md": content[:60000]} if document else None,
        "content_truncated": len(content) > 60000,
        "passages": passages,
        "jobs": jobs,
        "alias_of": alias_of,
    }


@router.post("/sources/{source_id}/reingest", status_code=202)
async def reingest_source(source_id: UUID, ctx: Context = Depends(get_context)):
    require_editor(ctx)
    source = await fetch_one(ctx.conn, "select * from sources where id = %s", (source_id,))
    if source is None or source["deleted_at"] is not None:
        raise not_found("That source does not exist.")
    if source["alias_of_id"]:
        raise validation("This source is an alias of another upload.")
    job_id = await enqueue_job(
        ctx.conn, ctx.session.tenant_id, source_id, _job_key(source_id, source["hash_sha256"])
    )
    if source["kind"] == "url":
        await ctx.conn.execute(
            "update sources set metadata = metadata || %s::jsonb, status = 'processing' where id = %s",
            (Jsonb({"refetch_requested_at": "now"}), source_id),
        )
    return {"id": str(source_id), "job_id": str(job_id), "status": "queued"}


@router.delete("/sources/{source_id}")
async def delete_source(source_id: UUID, ctx: Context = Depends(get_context)):
    require_editor(ctx)
    if not await _withdraw_source(ctx, source_id):
        raise not_found("That source does not exist.")
    return {"ok": True}
