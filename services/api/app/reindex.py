from __future__ import annotations

import asyncio
import logging
import sys

from .config import get_settings
from .db import close_pool, fetch_all, open_pool, resolve_tenant_id, tenant_conn
from .ingest.pipeline import _vector_literal
from .intelligence import get_intelligence

log = logging.getLogger(__name__)

BATCH_SIZE = 32


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
        async with tenant_conn(tenant_id) as conn:
            rows = await fetch_all(
                conn,
                """select p.id, p.text, p.contextual_blurb
                   from passages p
                   join documents d on d.id = p.document_id
                   where p.level = 'chunk'
                     and p.status = 'current'
                     and d.status = 'current'
                   order by p.document_id, p.chunk_index""",
            )
            if not rows:
                log.info("no current passages to re-index")
                return 0

            log.info(
                "re-indexing %s passages with %s (%s)",
                len(rows),
                settings.active_embedding_model,
                settings.embedding_provider,
            )
            done = 0
            for start in range(0, len(rows), BATCH_SIZE):
                batch = rows[start : start + BATCH_SIZE]
                vectors = await intelligence.embed(
                    [f"{row['contextual_blurb']}\n\n{row['text']}" for row in batch],
                    task="document",
                )
                for row, vector in zip(batch, vectors):
                    await conn.execute(
                        """update passages
                              set embedding = %s::vector,
                                  embedding_model = %s,
                                  embedding_version = %s
                            where id = %s""",
                        (
                            _vector_literal(vector),
                            settings.active_embedding_model,
                            settings.embedding_version,
                            row["id"],
                        ),
                    )
                done += len(batch)
                log.info("re-indexed %s/%s passages", done, len(rows))
            return done
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
