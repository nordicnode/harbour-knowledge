from __future__ import annotations

import asyncio
import logging

from .db import fetch_all, fetch_one, get_pool
from .ingest.pipeline import run_ingest

log = logging.getLogger(__name__)

POLL_SECONDS = 1.0
LEADER_RETRY_SECONDS = 2.0
RECOVERY_INTERVAL_SECONDS = 60.0
# Arbitrary, stable application-wide key. Only one API process may run jobs.
LEADER_LOCK_KEY = 0x48415242  # "HARB"


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _recover_stale_jobs(conn) -> None:
    """Requeue jobs whose worker process disappeared mid-run."""
    rows = await fetch_all(
        conn,
        "select * from requeue_stale_ingest_jobs(interval '30 minutes')",
    )
    if rows:
        log.warning("requeued %d stale ingest job(s)", len(rows))


async def _run_leader(lock_conn, stop: asyncio.Event) -> None:
    next_recovery = 0.0
    while not stop.is_set():
        loop_time = asyncio.get_running_loop().time()
        if loop_time >= next_recovery:
            try:
                await _recover_stale_jobs(lock_conn)
            except Exception as exc:  # noqa: BLE001 - recovery must not stop the leader
                log.exception("stale job recovery failed: %s", exc)
            next_recovery = loop_time + RECOVERY_INTERVAL_SECONDS

        async with get_pool().connection() as conn:
            rows = await fetch_all(conn, "select * from queued_ingest_jobs(5)")
        for row in rows:
            if stop.is_set():
                break
            await run_ingest(row["tenant_id"], row["source_id"], row["job_id"])
        await _wait(stop, POLL_SECONDS)


async def worker_loop(stop: asyncio.Event) -> None:
    """Run ingestion jobs in exactly one process.

    A session-level advisory lock elects the leader. Other API workers stay on
    standby and retry the lock, so a deployment with multiple Uvicorn workers
    does not process the same queued job twice. If the leader crashes, its
    session lock is released and the next standby takes over.
    """
    log.info("ingestion worker started")
    standby_logged = False
    while not stop.is_set():
        try:
            async with get_pool().connection() as lock_conn:
                await lock_conn.set_autocommit(True)
                row = await fetch_one(
                    lock_conn,
                    "select pg_try_advisory_lock(%s::bigint) as acquired",
                    (LEADER_LOCK_KEY,),
                )
                if not row or not row["acquired"]:
                    if not standby_logged:
                        log.info("worker standby: another process holds the ingestion lock")
                        standby_logged = True
                else:
                    standby_logged = False
                    log.info("worker leader: holding ingestion lock")
                    try:
                        await _run_leader(lock_conn, stop)
                    finally:
                        try:
                            await lock_conn.execute(
                                "select pg_advisory_unlock(%s::bigint)",
                                (LEADER_LOCK_KEY,),
                            )
                        except Exception as exc:  # noqa: BLE001 - closing the session also releases it
                            log.warning("could not release worker lock cleanly: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the worker alive
            log.exception("worker loop error: %s", exc)
        if not stop.is_set():
            await _wait(stop, LEADER_RETRY_SECONDS)
    log.info("ingestion worker stopped")
