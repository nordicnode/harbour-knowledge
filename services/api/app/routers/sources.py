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


@router.post("/sources", status_code=202)
async def create_source(request: Request, ctx: Context = Depends(get_context)):
    require_editor(ctx)
    content_type = request.headers.get("content-type", "")
    file = None
    url: str | None = None
    visibility: Literal["internal", "public"] = "internal"
    source_tier = "official_doc"
    plan_scope: list[str] = []
    version_scope: list[str] = []

    if "application/json" in content_type:
        body = await request.json()
        url = body.get("url")
        visibility = body.get("visibility", "internal")
        source_tier = body.get("source_tier", "official_doc")
        plan_scope = _list_value(body.get("plan_scope"))
        version_scope = _list_value(body.get("product_version_scope"))
        title_override = body.get("title")
        filename = None
        mime_type = None
        data = b""
    else:
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
        data = await file.read() if hasattr(file, "read") else b""
        filename = getattr(file, "filename", None) if hasattr(file, "read") else None
        mime_type = getattr(file, "content_type", None) if hasattr(file, "read") else None
        if file is not None and not hasattr(file, "read"):
            file = None

    if visibility not in ("internal", "public"):
        raise validation("visibility must be internal or public.")
    if source_tier not in TIERS:
        raise validation("Unknown source tier.")

    if file is not None:
        if not data:
            raise validation("The uploaded file is empty.")
        if len(data) > ctx.settings.max_upload_bytes:
            raise validation("Files are limited to 15 MB.")
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


@router.get("/sources/{source_id}")
async def get_source(source_id: UUID, ctx: Context = Depends(get_context)):
    source = await fetch_one(ctx.conn, "select * from sources where id = %s", (source_id,))
    if source is None:
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
    source = await fetch_one(ctx.conn, "select * from sources where id = %s", (source_id,))
    if source is None or source["deleted_at"] is not None:
        raise not_found("That source does not exist.")
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
        """insert into audit_log (tenant_id, actor_type, actor_id, action, object_type, object_id, detail)
           values (%s, 'user', %s, 'source.deleted', 'source', %s, '{}'::jsonb)""",
        (ctx.session.tenant_id, ctx.session.user_id, source_id),
    )
    return {"ok": True}
