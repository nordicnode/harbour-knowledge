from __future__ import annotations

import asyncio
import logging
import math
import sys
from uuid import UUID

from .config import Settings, get_settings
from .db import (
    close_pool,
    fetch_all,
    fetch_val,
    open_pool,
    resolve_tenant_id,
    tenant_conn,
)
from .errors import AppError
from .ingest.pipeline import _vector_literal
from .intelligence import Intelligence, get_intelligence

log = logging.getLogger(__name__)

BATCH_SIZE = 32


def _embedding_text(text: str, blurb: str) -> str:
    """Match ingestion: only add context when there is some."""
    return f"{blurb}\n\n{text}" if blurb else text


def _validate_vectors(vectors: list[list[float]], expected: int, settings: Settings) -> None:
    if len(vectors) != expected:
        raise RuntimeError(
            f"The embedding provider returned {len(vectors)} vectors for {expected} passages."
        )
    for vector in vectors:
        if len(vector) != settings.embedding_dim:
            raise RuntimeError(
                f"The embedding provider returned {len(vector)} dimensions, "
                f"but EMBEDDING_DIM is {settings.embedding_dim}."
            )
        if not all(math.isfinite(value) for value in vector):
            raise RuntimeError("The embedding provider returned a non-finite value.")


async def _stage_missing_batches(
    tenant_id: UUID, identity: str, settings: Settings, intelligence: Intelligence
) -> int:
    """Embed current passages into staging in short, committed batches.

    Each batch is its own transaction, so an interrupted run keeps the batches
    it finished and the live `passages` embeddings are untouched.
    """
    processed = 0
    while True:
        async with tenant_conn(tenant_id) as conn:
            rows = await fetch_all(
                conn,
                """select p.id, p.text, p.contextual_blurb,
                          d.title as document_title, s.title as source_title
                   from passages p
                   join documents d on d.id = p.document_id
                   join sources s on s.id = d.source_id
                   where p.level = 'chunk'
                     and p.status = 'current'
                     and d.status = 'current'
                     and not exists (
                       select 1 from embedding_reindex_staging st
                       where st.passage_id = p.id and st.identity = %s
                     )
                   order by p.id
                   limit %s""",
                (identity, BATCH_SIZE),
            )
        if not rows:
            break

        try:
            vectors = await intelligence.embed(
                [_embedding_text(row["text"], row["contextual_blurb"] or "") for row in rows],
                task="document",
            )
        except AppError as exc:
            index = getattr(exc, "index", None)
            if isinstance(index, int) and 0 <= index < len(rows):
                row = rows[index]
                raise RuntimeError(
                    f"Passage {row['id']} in “{row['source_title']}” cannot be embedded: "
                    f"{exc.message} Re-ingest that source so the passage can be split."
                ) from exc
            raise
        _validate_vectors(vectors, len(rows), settings)

        async with tenant_conn(tenant_id) as conn:
            for row, vector in zip(rows, vectors):
                await conn.execute(
                    """insert into embedding_reindex_staging
                         (passage_id, tenant_id, identity, embedding)
                       values (%s, %s, %s, %s::vector)
                       on conflict (passage_id) do update
                         set tenant_id = excluded.tenant_id,
                             identity = excluded.identity,
                             embedding = excluded.embedding,
                             created_at = now()""",
                    (row["id"], tenant_id, identity, _vector_literal(vector)),
                )
        processed += len(rows)
        log.info("staged %s passages", processed)
    return processed


async def reindex_tenant(
    tenant_id: UUID, settings: Settings, intelligence: Intelligence
) -> int:
    """Rebuild one tenant's vectors and switch them atomically.

    Staging rows from another identity are discarded first. Completed batches
    are keyed by the active identity, so a later run resumes instead of starting
    over, and concurrent ingestion does not invalidate the run. The final
    transaction verifies that every current passage is either already current
    or staged, then swaps all staged vectors into `passages` at once and clears
    the staging table.
    """
    identity = settings.embedding_identity

    async with tenant_conn(tenant_id) as conn:
        removed = await fetch_all(
            conn,
            "delete from embedding_reindex_staging where identity <> %s returning passage_id",
            (identity,),
        )
        staged = await fetch_val(
            conn,
            "select count(*) from embedding_reindex_staging where identity = %s",
            (identity,),
        )
        total = await fetch_val(
            conn,
            """select count(*)
               from passages p
               join documents d on d.id = p.document_id
               where p.level = 'chunk' and p.status = 'current' and d.status = 'current'""",
        )
    if removed:
        log.info("discarded %s staging rows from another embedding identity", len(removed))
    if staged:
        log.info("resuming with %s already staged passages", staged)
    log.info(
        "rebuilding %s current passages for identity %s with %s (%s)",
        total,
        identity,
        settings.active_embedding_model,
        settings.embedding_provider,
    )

    await _stage_missing_batches(tenant_id, identity, settings, intelligence)

    async with tenant_conn(tenant_id) as conn:
        missing = await fetch_val(
            conn,
            """select count(*)
               from passages p
               join documents d on d.id = p.document_id
               where p.level = 'chunk' and p.status = 'current' and d.status = 'current'
                 and p.embedding_identity is distinct from %s
                 and not exists (
                   select 1 from embedding_reindex_staging st
                   where st.passage_id = p.id and st.identity = %s
                 )""",
            (identity, identity),
        )
        if missing:
            raise RuntimeError(
                f"{missing} current passages still lack staged embeddings; re-run reindex."
            )

        rows = await fetch_all(
            conn,
            """update passages p
                  set embedding = st.embedding,
                      embedding_model = %s,
                      embedding_version = %s,
                      embedding_identity = st.identity
                 from embedding_reindex_staging st
                where st.passage_id = p.id
                  and st.identity = %s
                  and p.status = 'current'
                  and p.level = 'chunk'
                returning p.id""",
            (settings.active_embedding_model, settings.embedding_version, identity),
        )
        await conn.execute(
            "delete from embedding_reindex_staging where tenant_id = %s",
            (tenant_id,),
        )
    log.info("switched %s passages to embedding identity %s", len(rows), identity)
    return len(rows)


async def reindex(slug: str | None = None) -> int:
    settings = get_settings()
    settings.validate_provider()
    intelligence = get_intelligence()
    tenant_slug = slug or settings.default_tenant_slug

    await open_pool()
    try:
        tenant_id = await resolve_tenant_id(tenant_slug)
        if tenant_id is None:
            raise RuntimeError(f"Workspace '{tenant_slug}' does not exist.")
        return await reindex_tenant(tenant_id, settings, intelligence)
    finally:
        await close_pool()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    slug = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        count = asyncio.run(reindex(slug))
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        log.error("re-index failed: %s", exc)
        raise SystemExit(1) from exc
    log.info("re-index complete: %s passages", count)


if __name__ == "__main__":
    main()
