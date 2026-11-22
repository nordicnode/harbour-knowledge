from __future__ import annotations

import asyncio
import logging

from .db import fetch_all, get_pool
from .ingest.pipeline import run_ingest

log = logging.getLogger(__name__)

POLL_SECONDS = 1.0


async def worker_loop(stop: asyncio.Event) -> None:
    """Poll committed ingest jobs and run them.

    This is deliberately a small in-process worker for the MVP. It reads only
    committed rows, so a request can enqueue a job and return without the worker
    racing an open request transaction. In production the same loop runs as its
    own process (see README).
    """
    log.info("ingestion worker started")
    while not stop.is_set():
        try:
            async with get_pool().connection() as conn:
                rows = await fetch_all(conn, "select * from queued_ingest_jobs(5)")
            for row in rows:
                if stop.is_set():
                    break
                await run_ingest(row["tenant_id"], row["source_id"], row["job_id"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the worker alive
            log.exception("worker loop error: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
        except asyncio.TimeoutError:
            pass
    log.info("ingestion worker stopped")
