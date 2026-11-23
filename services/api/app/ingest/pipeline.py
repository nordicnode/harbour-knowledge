from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from ..config import get_settings
from ..db import fetch_all, fetch_one, fetch_val, tenant_conn
from ..errors import AppError
from ..intelligence import EvidencePassage, TopicOption, get_intelligence
from ..intelligence.local_embeddings import EmbeddingInputTooLong
from .chunker import chunk_markdown, estimate_tokens, split_oversized_chunk
from .parsers import ParsedDocument, fetch_url, parse_bytes, upload_format
from .storage import read_bytes, remove_snapshot, store_snapshot

log = logging.getLogger(__name__)

PIPELINE_VERSION = "v1"
# Bump when passage construction changes, so an explicit re-ingest rebuilds
# stale indexes even when the normalized content hash remains unchanged.
INDEXING_VERSION = "3"
REVIEW_INTERVAL = {
    "pricing": 30,
    "policy": 30,
    "procedure": 90,
    "troubleshooting": 90,
}

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"(?<!\d)(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
SECRET_RE = re.compile(
    r"(?i)\b(sk-[a-z0-9_-]{12,}|bearer\s+[a-z0-9._-]{12,}|"
    r"(?:api[_-]?key|secret|password|token)\s*[:=]\s*[^\s,;]{6,})"
)
URL_CREDENTIAL_RE = re.compile(r"https?://[^\s/@]+:[^\s/@]+@")


def _normalise_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def quote_in_text(quote: str, text: str) -> bool:
    return _normalise_ws(quote).lower() in _normalise_ws(text).lower()


def redact(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}

    def replace(pattern: re.Pattern[str], label: str):
        nonlocal text
        text, n = pattern.subn(label, text)
        if n:
            counts[label.strip("[]")] = counts.get(label.strip("[]"), 0) + n

    replace(URL_CREDENTIAL_RE, "https://[REDACTED_CREDENTIALS]@")
    replace(SECRET_RE, "[REDACTED_SECRET]")
    replace(CARD_RE, "[REDACTED_NUMBER]")
    replace(EMAIL_RE, "[REDACTED_EMAIL]")
    replace(PHONE_RE, "[REDACTED_PHONE]")
    return text, counts


def normalise(markdown: str) -> str:
    """Normalize line endings without guessing which source content is noise."""
    return markdown.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def _question_for(title: str, doc_type: str) -> str:
    base = re.sub(r"\([^)]*\)|\[[^\]]*\]|\bv\d+\b|\b20\d{2}\b|\b(final|draft|old|new)\b", " ", title, flags=re.I)
    base = re.sub(r"[_\-]+", " ", base).strip(" .:-")
    base = re.sub(r"\.(pdf|docx|md|txt|html)$", "", base, flags=re.I).strip()
    if doc_type == "procedure":
        return "How do I " + (base[:1].lower() + base[1:]) + "?"
    return "What is Harbour's " + base.lower() + "?"


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.7f}" for value in vector) + "]"


async def enqueue_job(conn: AsyncConnection, tenant_id: UUID, source_id: UUID, key: str) -> UUID:
    row = await fetch_one(
        conn,
        """insert into ingest_jobs (tenant_id, source_id, idempotency_key, status, stage)
           values (%s, %s, %s, 'queued', 'intake')
           on conflict (tenant_id, idempotency_key)
           do update set status = 'queued', stage = 'intake', error = null,
                         stats = '{}'::jsonb, attempts = 0, started_at = null,
                         finished_at = null, updated_at = now()
           returning id""",
        (tenant_id, source_id, key),
    )
    assert row is not None
    return row["id"]


async def _set_job(
    conn: AsyncConnection,
    job_id: UUID,
    status: str,
    stage: str | None = None,
    error: str | None = None,
    stats: dict | None = None,
) -> None:
    await conn.execute(
        """update ingest_jobs
           set status = %s,
               stage = coalesce(%s, stage),
               error = %s,
               stats = coalesce(%s, stats),
               started_at = case when %s = 'running' then coalesce(started_at, now()) else started_at end,
               finished_at = case when %s in ('succeeded','failed','dead_letter') then now() else finished_at end,
               updated_at = now()
           where id = %s""",
        (status, stage, error, Jsonb(stats) if stats is not None else None, status, status, job_id),
    )


async def _topics(conn: AsyncConnection) -> list[TopicOption]:
    rows = await fetch_all(conn, "select slug, name, coalesce(description,'') as description from topics order by name")
    return [TopicOption(**row) for row in rows]


async def _read_source(
    conn: AsyncConnection, tenant_id: UUID, source: dict, new_snapshots: list[str],
) -> tuple[bytes, ParsedDocument]:
    settings = get_settings()
    if source["kind"] == "url":
        if not source["origin_url"]:
            raise AppError("ingest_failed", "This URL source has no origin URL.")
        data, content_type, final_url = await fetch_url(
            source["origin_url"],
            settings.max_upload_bytes,
            settings.url_fetch_timeout_seconds,
        )
        storage_path = store_snapshot(tenant_id, source["id"], data, content_type)
        new_snapshots.append(storage_path)
        digest = hashlib.sha256(data).hexdigest()
        await conn.execute(
            """update sources
               set storage_path = %s, hash_sha256 = %s, mime_type = %s, size_bytes = %s,
                   origin_url = %s, metadata = metadata || %s::jsonb, updated_at = now()
               where id = %s""",
            (
                storage_path, digest, content_type, len(data), final_url,
                json.dumps({"snapshot_at": datetime.now(timezone.utc).isoformat()}),
                source["id"],
            ),
        )
        parsed = parse_bytes(final_url.rsplit("/", 1)[-1] or "page.html", content_type, data, source["title"])
        return data, parsed

    if not source["storage_path"]:
        raise AppError("ingest_failed", "The uploaded file is missing from storage.")
    data = read_bytes(tenant_id, source["id"], source["storage_path"])
    filename = (source.get("metadata") or {}).get("filename") or source["title"]
    # Validate again for sources queued before upload format restrictions.
    # This also prevents misleading MIME headers from changing the parser.
    kind = upload_format(filename)
    parsed = parse_bytes(filename, source["mime_type"], data, source["title"], kind=kind)
    return data, parsed


async def _previous_document(conn: AsyncConnection, source_id: UUID) -> dict | None:
    return await fetch_one(
        conn,
        """select * from documents where source_id = %s and status = 'current'
           order by version desc limit 1""",
        (source_id,),
    )


async def _supersede(conn: AsyncConnection, document: dict, today: date) -> None:
    await conn.execute(
        "update documents set status = 'superseded', effective_to = coalesce(effective_to, %s), updated_at = now() where id = %s",
        (today, document["id"]),
    )
    await conn.execute("update passages set status = 'superseded' where document_id = %s", (document["id"],))


async def _mark_answers_for_recheck(conn: AsyncConnection, document_id: UUID) -> int:
    """Take approved answers out of the ready set when their evidence is superseded.

    Evidence stays pinned to the old version for audit; the answer must be
    edited and re-approved against current passages before it can serve again.
    Runs inside the replacement transaction, so a failed re-ingest reverts it.
    """
    rows = await fetch_all(
        conn,
        """with affected as (
             select distinct ae.answer_id
             from answer_evidence ae
             join passages p on p.id = ae.passage_id
             join canonical_answers ca
               on ca.id = ae.answer_id and ca.current_version = ae.answer_version
             where p.document_id = %s and ca.status = 'approved'
           )
           update canonical_answers ca
           set status = 'needs_recheck', updated_at = now()
           where ca.id in (select answer_id from affected)
           returning ca.id""",
        (document_id,),
    )
    return len(rows)


async def _insert_document(
    conn: AsyncConnection,
    tenant_id: UUID,
    source: dict,
    parsed: ParsedDocument,
    content_md: str,
    classification,
    topics: list[TopicOption],
) -> dict:
    previous = await _previous_document(conn, source["id"])
    today = date.today()
    if previous:
        await _supersede(conn, previous, today)
        await _mark_answers_for_recheck(conn, previous["id"])
    version = (previous["version"] + 1) if previous else 1
    topic_id = None
    if classification.topic_slugs:
        topic_id = await fetch_val(
            conn, "select id from topics where slug = %s limit 1", (classification.topic_slugs[0],)
        )
    content_hash = hashlib.sha256(content_md.encode("utf-8")).hexdigest()
    row = await fetch_one(
        conn,
        """insert into documents
             (tenant_id, source_id, version, effective_from, supersedes_id, status, title, content_md,
              content_hash, doc_type, topic_id, parser, parse_quality, metadata, plan_scope,
              product_version_scope)
           values (%s, %s, %s, %s, %s, 'current', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           returning *""",
        (
            tenant_id, source["id"], version, today, previous["id"] if previous else None,
            parsed.title, content_md, content_hash, classification.doc_type, topic_id,
            parsed.parser, None,
            json.dumps({
                "tags": classification.tags,
                "entities": classification.entities,
                "summary": classification.summary,
                "page_count": parsed.page_count,
                "extraction": parsed.metadata,
                "indexing_version": INDEXING_VERSION,
            }),
            source.get("plan_scope") or [],
            source.get("product_version_scope") or [],
        ),
    )
    assert row is not None
    return row


async def _insert_passages(
    conn: AsyncConnection,
    tenant_id: UUID,
    document: dict,
    chunk_set,
    source: dict,
) -> list[dict]:
    intelligence = get_intelligence()
    settings = get_settings()
    parent_ids: list[UUID] = []

    for section in chunk_set.sections:
        row = await fetch_one(
            conn,
            """insert into passages
                 (tenant_id, document_id, document_version, level, text, text_hash, chunk_index,
                  section_path, page_ref, contextual_blurb, bm25_text, token_count, status)
               values (%s, %s, %s, 'section', %s, %s, %s, %s, %s, '', to_tsvector('english', %s), %s, 'current')
               returning id""",
            (
                tenant_id, document["id"], document["version"], section.text,
                hashlib.sha256(section.text.encode()).hexdigest(), len(parent_ids),
                section.section_path, section.page_ref, section.text, section.token_count,
            ),
        )
        assert row is not None
        parent_ids.append(row["id"])

    inserted: list[dict] = []
    by_section: dict[int, list] = {}
    for chunk in chunk_set.chunks:
        by_section.setdefault(chunk.section_index, []).append(chunk)

    async def contextualise(chunk, text: str) -> str:
        return await intelligence.contextualise(
            document["title"], chunk.section_path, text,
            document["plan_scope"], document["product_version_scope"],
        )

    chunk_index = 0
    local_embeddings = getattr(intelligence, "embeddings", intelligence)
    checker = getattr(local_embeddings, "check_embedding_lengths", None)
    for section_index, chunks in by_section.items():
        # Split over-limit source material before sending it to the chat model;
        # no extra embeddings or regenerated context are needed for rejected text.
        candidates = [(chunk, chunk.text, 0) for chunk in chunks]
        if checker is not None:
            while True:
                try:
                    await checker([text for _, text, _ in candidates])
                    break
                except EmbeddingInputTooLong as exc:
                    chunk, text, depth = candidates[exc.index]
                    if depth >= 32:
                        raise AppError("ingest_failed", "A passage could not be split within the embedding limit.")
                    candidates[exc.index:exc.index + 1] = [
                        (chunk, part, depth + 1) for part in split_oversized_chunk(text)
                    ]
        pending = [(chunk, text, await contextualise(chunk, text), depth) for chunk, text, depth in candidates]
        def embedding_inputs():
            return [f"{blurb}\n\n{text}" if blurb else text for _, text, blurb, _ in pending]

        while True:
            try:
                if checker is not None:
                    await checker(embedding_inputs())
                else:
                    vectors = await intelligence.embed(embedding_inputs(), task="document")
                break
            except EmbeddingInputTooLong as exc:
                chunk, text, blurb, depth = pending[exc.index]
                # Prefer source evidence to a generated blurb when only the
                # latter pushes an otherwise valid passage over the limit.
                if blurb:
                    pending[exc.index] = (chunk, text, "", depth)
                    continue
                if depth >= 32:
                    raise AppError("ingest_failed", "A passage could not be split within the embedding limit.")
                pieces = split_oversized_chunk(text)
                replacement = [(chunk, part, await contextualise(chunk, part), depth + 1) for part in pieces]
                pending[exc.index:exc.index + 1] = replacement
        if checker is not None:
            vectors = await intelligence.embed(embedding_inputs(), task="document")
        if len(vectors) != len(pending) or any(
            len(vector) != settings.embedding_dim or not all(math.isfinite(value) for value in vector)
            for vector in vectors
        ):
            raise AppError("ingest_failed", "The embedding provider returned invalid or incomplete vectors.")
        for (chunk, text, blurb, _), vector in zip(pending, vectors):
            bm25_source = f"{chunk.section_path}\n{blurb}\n{text}"
            row = await fetch_one(
                    conn,
                    """insert into passages
                         (tenant_id, document_id, document_version, parent_id, level, text, text_hash,
                          chunk_index, section_path, page_ref, contextual_blurb, embedding, embedding_model,
                          embedding_version, embedding_identity, bm25_text, token_count, tags, status)
                       values (%s, %s, %s, %s, 'chunk', %s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s,
                               to_tsvector('english', %s), %s, %s, 'current')
                       returning id""",
                    (
                        tenant_id, document["id"], document["version"], parent_ids[section_index],
                        text, hashlib.sha256(text.encode()).hexdigest(), chunk_index,
                        chunk.section_path, chunk.page_ref, blurb, _vector_literal(vector),
                        settings.active_embedding_model, settings.embedding_version,
                        settings.embedding_identity, bm25_source,
                        estimate_tokens(text), document["metadata"].get("tags", []),
                    ),
                )
            assert row is not None
            inserted.append({"id": row["id"], "text": text, "section_path": chunk.section_path})
            chunk_index += 1
    return inserted


NUMBER_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\$\s?([\d,]+(?:\.\d+)?)"), "usd"),
    (re.compile(r"\b([\d,]+(?:\.\d+)?)\s*(?:%|percent)\b", re.I), "percent"),
    (re.compile(r"\b([\d,]+)\s*(day|days)\b", re.I), "days"),
    (re.compile(r"\b([\d,]+)\s*(month|months)\b", re.I), "months"),
    (re.compile(r"\b([\d,]+)\s*(?:user|users|seat|seats)\b", re.I), "seats"),
]


GENERIC_CLAIM_WORDS = {
    "plan", "plans", "per", "user", "users", "month", "months", "cost", "costs",
    "price", "prices", "priced", "include", "includes", "including", "our", "your",
    "harbour", "the", "and", "are", "is", "was", "were", "of", "to", "for", "with",
    "from", "that", "this", "it", "its", "can", "be", "at", "or", "above", "below",
    "under", "over", "than", "any", "all", "more", "less", "there", "their", "they",
}


def _claims(text: str) -> list[tuple[str, float, set[str], set[str]]]:
    """Numeric claims plus their subject anchor, so Team's price is never
    compared with Business's price just because both say "per user, per month".
    """
    claims: list[tuple[str, float, set[str], set[str]]] = []
    for pattern, unit in NUMBER_PATTERNS:
        for match in pattern.finditer(text):
            raw = match.group(1).replace(",", "")
            try:
                value = float(raw)
            except ValueError:
                continue
            start = max(0, match.start() - 120)
            end = min(len(text), match.end() + 120)
            context = {
                token for token in re.findall(r"[a-z0-9]+", text[start:end].lower())
                if len(token) > 2 and not token.isdigit()
            }
            before = text[max(0, match.start() - 90):match.start()].lower()
            subjects = {
                token for token in re.findall(r"[a-z0-9]+", before)
                if len(token) > 2 and not token.isdigit() and token not in GENERIC_CLAIM_WORDS
            }
            if not subjects:
                subjects = context - GENERIC_CLAIM_WORDS
            claims.append((unit, value, context, subjects))
    return claims


async def detect_conflicts(
    conn: AsyncConnection,
    tenant_id: UUID,
    document: dict,
    new_passages: list[dict],
) -> list[dict]:
    if document["doc_type"] not in ("policy", "pricing") or not document["topic_id"]:
        return []
    existing = await fetch_all(
        conn,
        """select p.id, p.text, d.title as document_title, d.doc_type, s.title as source_title
           from passages p
           join documents d on d.id = p.document_id
           join sources s on s.id = d.source_id
           where p.level = 'chunk' and p.status = 'current'
             and d.status = 'current' and d.topic_id = %s and d.id <> %s
           limit 400""",
        (document["topic_id"], document["id"]),
    )
    created: list[dict] = []
    for new in new_passages:
        new_claims = _claims(new["text"])
        if not new_claims:
            continue
        for old in existing:
            old_claims = _claims(old["text"])
            if not old_claims:
                continue
            best: tuple[float, list[str]] | None = None
            for unit_a, value_a, context_a, subject_a in new_claims:
                for unit_b, value_b, context_b, subject_b in old_claims:
                    if unit_a != unit_b or value_a == value_b:
                        continue
                    # Different subjects are different claims: Team $12 is not a
                    # contradiction of Business $24.
                    if not (subject_a & subject_b):
                        continue
                    shared = context_a & context_b
                    if len(shared) < 3:
                        continue
                    jaccard = len(shared) / max(1, len(context_a | context_b))
                    if jaccard < 0.12:
                        continue
                    if best is None or jaccard > best[0]:
                        best = (
                            jaccard,
                            [
                                f"{unit_a}: {value_a:g} vs {value_b:g}",
                                "shared context: " + ", ".join(sorted(shared)[:8]),
                            ],
                        )
            if best is None:
                continue
            first, second = sorted([str(new["id"]), str(old["id"])])
            duplicate = await fetch_one(
                conn,
                """select id from conflicts
                   where tenant_id = %s and status = 'pending'
                     and ((passage_id_a = %s and passage_id_b = %s)
                       or (passage_id_a = %s and passage_id_b = %s))""",
                (tenant_id, first, second, second, first),
            )
            if duplicate:
                continue
            score = min(0.95, 0.55 + best[0])
            conflict = await fetch_one(
                conn,
                """insert into conflicts
                     (tenant_id, passage_id_a, passage_id_b, kind, detector, score)
                   values (%s, %s, %s, 'contradiction', 'rule', %s) returning id""",
                (tenant_id, first, second, score),
            )
            assert conflict is not None
            await fetch_one(
                conn,
                """insert into review_items (tenant_id, kind, payload, priority, dedupe_key)
                   values (%s, 'conflict', %s, 85, %s)
                   on conflict (tenant_id, dedupe_key) where dedupe_key is not null and status = 'pending'
                   do nothing returning id""",
                (
                    tenant_id,
                    json.dumps({
                        "conflict_id": str(conflict["id"]),
                        "summary": f"Two current sources disagree: {best[1][0]}",
                        "new_passage_id": str(new["id"]),
                        "existing_passage_id": str(old["id"]),
                        "new_document": document["title"],
                        "existing_document": old["document_title"],
                        "detail": best[1][1],
                    }),
                    f"conflict:{first}:{second}",
                ),
            )
            created.append({"conflict_id": str(conflict["id"]), "detail": best[1][0]})
    return created


async def propose_draft(
    conn: AsyncConnection,
    tenant_id: UUID,
    source: dict,
    document: dict,
    passages: list[dict],
    user_id: UUID | None,
) -> dict | None:
    if document["doc_type"] not in ("policy", "pricing", "procedure"):
        return None
    if not document["topic_id"] or not passages:
        return None
    existing = await fetch_val(
        conn,
        "select id from canonical_answers where topic_id = %s and status <> 'archived' limit 1",
        (document["topic_id"],),
    )
    if existing:
        return None

    intelligence = get_intelligence()
    question = _question_for(document["title"], document["doc_type"])
    evidence = [
        EvidencePassage(
            evidence_id=str(p["id"]),
            text=p["text"],
            title=document["title"],
            section_path=p["section_path"],
            source_date=str(document["effective_from"]) if document["effective_from"] else None,
            visibility=source["visibility"],
            plan_scope=document["plan_scope"],
            version_scope=document["product_version_scope"],
        )
        for p in passages[:4]
    ]
    try:
        draft = await intelligence.draft_answer(question, evidence)
    except AppError as exc:
        log.warning("draft proposal skipped: %s", exc.message)
        return None
    valid = []
    by_id = {str(p["id"]): p for p in passages}
    for quote in draft.evidence:
        passage = by_id.get(quote.passage_id)
        if passage and quote.quote and quote_in_text(quote.quote, passage["text"]):
            valid.append(quote)
    if not valid or not draft.answer_md.strip():
        return None

    interval = REVIEW_INTERVAL.get(document["doc_type"], 180)
    answer = await fetch_one(
        conn,
        """insert into canonical_answers
             (tenant_id, topic_id, question, answer_md, status, owner_id, review_interval_days,
              next_review_at, plan_scope, version_scope, visibility, current_version, created_by,
              human_confidence)
           values (%s, %s, %s, %s, 'in_review', %s, %s, now() + %s * interval '1 day',
                   %s, %s, %s, 1, %s, %s)
           returning *""",
        (
            tenant_id, document["topic_id"], question, draft.answer_md, user_id, interval, interval,
            document["plan_scope"], document["product_version_scope"], "internal",
            user_id, draft.confidence,
        ),
    )
    assert answer is not None
    await conn.execute(
        """insert into answer_versions
             (tenant_id, answer_id, version, answer_md, changed_by, change_reason)
           values (%s, %s, 1, %s, %s, 'Drafted from a newly ingested source')""",
        (tenant_id, answer["id"], draft.answer_md, user_id),
    )
    for quote in valid:
        await conn.execute(
            """insert into answer_evidence
                 (tenant_id, answer_id, answer_version, passage_id, quote, claim_ref)
               values (%s, %s, 1, %s, %s, %s)""",
            (tenant_id, answer["id"], quote.passage_id, quote.quote, quote.claim_ref),
        )
    await conn.execute(
        """insert into review_items (tenant_id, kind, payload, priority, dedupe_key)
           values (%s, 'new_answer', %s, 60, %s)
           on conflict (tenant_id, dedupe_key) where dedupe_key is not null and status = 'pending'
           do nothing""",
        (
            tenant_id,
            json.dumps({
                "answer_id": str(answer["id"]),
                "question": question,
                "source_id": str(source["id"]),
                "source_title": source["title"],
                "evidence_count": len(valid),
                "visibility": answer["visibility"],
                "summary": "Harbour drafted this from your sources. Publish it if it reads right.",
            }),
            f"new_answer:{answer['id']}",
        ),
    )
    return answer


async def _fail(
    conn: AsyncConnection,
    job_id: UUID,
    source_id: UUID,
    stage: str,
    exc: Exception,
) -> None:
    # Provider and SQL exceptions may contain credentials, prompts, or source
    # text. Keep detailed diagnostics in the exception type and stage, not in
    # persisted job errors or logs.
    if isinstance(exc, AppError) and exc.code in {"validation", "ingest_failed"}:
        message, _ = redact(exc.message)
    elif isinstance(exc, AppError):
        message = f"Ingestion failed: {exc.code}."
    else:
        message = f"Ingestion failed ({type(exc).__name__})."
    log.error("ingestion failed at %s: %s", stage, message)
    await _set_job(conn, job_id, "failed", stage, message[:2000])
    await conn.execute(
        """update sources set status = case
               when status = 'quarantined' then status
               when exists (select 1 from documents where source_id = %s and status = 'current')
                 then 'active' else 'failed' end,
               updated_at = now()
           where id = %s and deleted_at is null and status <> 'withdrawn'""",
        (source_id, source_id),
    )
    if "ocr" in message.lower():
        await conn.execute(
            """insert into review_items (tenant_id, kind, payload, priority, dedupe_key)
               select tenant_id, 'low_ocr', %s::jsonb, 40, %s from sources where id = %s
               on conflict (tenant_id, dedupe_key)
                 where dedupe_key is not null and status = 'pending' do nothing""",
            (
                json.dumps({"stage": stage, "error": message[:800], "source_id": str(source_id)}),
                f"low_ocr:{source_id}",
                source_id,
            ),
        )


async def run_ingest(
    tenant_id: UUID,
    source_id: UUID,
    job_id: UUID,
    *,
    auto_propose: bool = True,
    user_id: UUID | None = None,
) -> None:
    stage = "parse"
    new_snapshots: list[str] = []
    # Commit the claim independently: if the worker is killed, stale-job
    # recovery can see the running attempt and apply its retry limit.
    async with tenant_conn(tenant_id) as conn:
        await conn.execute(
            "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (str(source_id),),
        )
        claimed = await fetch_one(
            conn,
            """update ingest_jobs set status = 'running', stage = 'parse',
                      started_at = now(), finished_at = null, error = null, updated_at = now()
               where id = %s and source_id = %s and status = 'queued'
               returning started_at, attempts""",
            (job_id, source_id),
        )
    if claimed is None:
        return

    async with tenant_conn(tenant_id) as conn:
        # Keep the source lock and job row lock outside the savepoint, so
        # rollback and failure reporting remain serialized with withdrawal.
        await conn.execute(
            "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (str(source_id),),
        )
        current = await fetch_one(
            conn,
            """select id from ingest_jobs where id = %s and source_id = %s
               and status = 'running' and started_at = %s and attempts = %s for update""",
            (job_id, source_id, claimed["started_at"], claimed["attempts"]),
        )
        if current is None:
            return
        await conn.execute("savepoint ingest_attempt")
        try:
            await _set_job(conn, job_id, "running", "parse")
            source = await fetch_one(conn, "select * from sources where id = %s", (source_id,))
            if source is None:
                await _set_job(
                    conn, job_id, "failed", "intake",
                    "The source was not visible when ingestion started. Re-queue the job.",
                )
                await conn.execute("release savepoint ingest_attempt")
                return
            if source["status"] == "withdrawn" or source["deleted_at"] is not None:
                await _set_job(conn, job_id, "failed", "intake", "This source was withdrawn.")
                await conn.execute("release savepoint ingest_attempt")
                return

            _, parsed = await _read_source(conn, tenant_id, source, new_snapshots)

            stage = "normalise"
            await _set_job(conn, job_id, "running", stage)
            normalised = normalise(parsed.markdown)
            if not normalised.strip():
                raise AppError("ingest_failed", "No readable text remained after normalisation.")
            redacted, redactions = redact(normalised)
            parsed.markdown = redacted

            previous = await _previous_document(conn, source_id)
            content_hash = hashlib.sha256(redacted.encode("utf-8")).hexdigest()
            if (
                previous
                and previous["content_hash"] == content_hash
                and (previous.get("metadata") or {}).get("indexing_version") == INDEXING_VERSION
            ):
                await conn.execute(
                    "update sources set status = 'active', ingested_at = coalesce(ingested_at, now()), updated_at = now() where id = %s",
                    (source_id,),
                )
                await _set_job(conn, job_id, "succeeded", "unchanged", None, {"unchanged": True})
                await conn.execute("release savepoint ingest_attempt")
                return

            stage = "classify"
            await _set_job(conn, job_id, "running", stage)
            topics = await _topics(conn)
            classification = await get_intelligence().classify(parsed.title, parsed.markdown, topics)

            stage = "segment"
            await _set_job(conn, job_id, "running", stage)
            document = await _insert_document(conn, tenant_id, source, parsed, redacted, classification, topics)
            chunk_set = chunk_markdown(
                parsed.markdown, classification.doc_type, page_markers=parsed.parser == "pypdf"
            )
            if not chunk_set.chunks:
                raise AppError("ingest_failed", "The document produced no passages.")

            stage = "contextualise_embed_index"
            await _set_job(conn, job_id, "running", stage)
            passages = await _insert_passages(conn, tenant_id, document, chunk_set, source)

            stage = "deduplicate"
            await _set_job(conn, job_id, "running", stage)
            # Exact duplicate passages inside this document are collapsed by hash;
            # cross-document near-duplicate clustering arrives in phase 3.

            stage = "conflict_scan"
            await _set_job(conn, job_id, "running", stage)
            conflicts = await detect_conflicts(conn, tenant_id, document, passages)

            if auto_propose:
                stage = "propose"
                await _set_job(conn, job_id, "running", stage)
                await propose_draft(conn, tenant_id, source, document, passages, user_id)

            stage = "finish"
            # A source can be removed while a long ingest is still running. Do
            # not let the worker resurrect it after the delete has committed.
            finished = await fetch_one(
                conn,
                """update sources set status = 'active', ingested_at = now(), updated_at = now()
                   where id = %s and deleted_at is null and status <> 'withdrawn'
                   returning id""",
                (source_id,),
            )
            if finished is None:
                stage = "withdrawn"
                raise AppError("ingest_failed", "The source was removed during ingestion.")
            stats = {
                "sections": len(chunk_set.sections),
                "passages": len(passages),
                "redactions": redactions,
                "conflicts": len(conflicts),
                "document_id": str(document["id"]),
                "document_version": document["version"],
                "doc_type": classification.doc_type,
                "topics": classification.topic_slugs,
            }
            await _set_job(conn, job_id, "succeeded", "finish", None, stats)
            await conn.execute(
                """insert into audit_log (tenant_id, actor_type, actor_id, action, object_type, object_id, detail)
                   values (%s, %s, %s, 'source.ingested', 'source', %s, %s)""",
                (tenant_id, "user" if user_id else "system", user_id, source_id, Jsonb(stats)),
            )
            await conn.execute("release savepoint ingest_attempt")
        except Exception as exc:  # noqa: BLE001 - job boundary records every failure
            # Also clears PostgreSQL's failed-transaction state after SQL errors.
            await conn.execute("rollback to savepoint ingest_attempt")
            await conn.execute("release savepoint ingest_attempt")
            for snapshot in new_snapshots:
                try:
                    remove_snapshot(snapshot)
                except OSError:
                    log.warning("could not remove failed ingest snapshot", exc_info=True)
            await _fail(conn, job_id, source_id, stage, exc)
