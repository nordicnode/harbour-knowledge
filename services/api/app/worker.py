from __future__ import annotations

import asyncio
import logging

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from .config import get_settings
from .db import fetch_all, fetch_one, get_pool
from .ingest.pipeline import run_ingest

log = logging.getLogger(__name__)

POLL_SECONDS = 1.0
LEADER_RETRY_SECONDS = 2.0
RECOVERY_INTERVAL_SECONDS = 60.0
MAX_RECOVERY_ATTEMPTS = 3
# Arbitrary, stable application-wide key. Only one API process may run jobs.
LEADER_LOCK_KEY = 0x48415242  # "HARB"
# pg_locks represents a 64-bit advisory key as (classid, objid, objsubid=1).
LEADER_LOCK_CLASSID = (LEADER_LOCK_KEY >> 32) & 0xFFFFFFFF
LEADER_LOCK_OBJID = LEADER_LOCK_KEY & 0xFFFFFFFF


def _connection_usable(conn: AsyncConnection) -> bool:
    """A closed or broken session has already lost its advisory lock."""
    return not conn.closed and not conn.broken


async def _leader_holds_lock(conn: AsyncConnection) -> bool:
    """Prove the session still owns the lock, not just that it looks open.

    A terminated backend is not marked broken by psycopg until a statement
    runs, so `conn.broken` alone leaves a window where a former leader keeps
    claiming work. This round-trip also detects an explicit unlock by anyone
    else with access to the session.
    """
    if not _connection_usable(conn):
        return False
    row = await fetch_one(
        conn,
        """select exists (
             select 1 from pg_locks
             where locktype = 'advisory'
               and pid = pg_backend_pid()
               and classid = %s::oid
               and objid = %s::oid
               and objsubid = 1
               and granted
           ) as held""",
        (LEADER_LOCK_CLASSID, LEADER_LOCK_OBJID),
    )
    return bool(row and row["held"])


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _recover_stale_jobs(conn: AsyncConnection) -> None:
    """Requeue abandoned jobs, or fail them once the retry budget is spent."""
    # The leader connection is autocommit so the session-level advisory lock is
    # never held in an idle transaction. Recovery still needs to be one atomic
    # transaction, since it may both requeue and fail jobs.
    async with conn.transaction():
        rows = await fetch_all(
            conn,
            "select * from requeue_stale_ingest_jobs(interval '30 minutes', 10, %s)",
            (MAX_RECOVERY_ATTEMPTS,),
        )
    requeued = sum(1 for row in rows if row["outcome"] == "queued")
    exhausted = len(rows) - requeued
    if requeued:
        log.warning("requeued %d stale ingest job(s)", requeued)
    if exhausted:
        log.error("failed %d ingest job(s) after the recovery retry limit", exhausted)


async def _run_leader(lock_conn: AsyncConnection, stop: asyncio.Event) -> None:
    if not _connection_usable(lock_conn):
        raise ConnectionError("leader connection closed before work started")

    next_recovery = 0.0
    while not stop.is_set():
        try:
            still_leader = await _leader_holds_lock(lock_conn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ConnectionError("leader connection lost; re-electing a worker") from exc
        if not still_leader:
            raise ConnectionError("leader lock lost; re-electing a worker")

        loop_time = asyncio.get_running_loop().time()
        if loop_time >= next_recovery:
            try:
                await _recover_stale_jobs(lock_conn)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad recovery must not stop the leader
                log.exception("stale job recovery failed: %s", exc)
                if not _connection_usable(lock_conn):
                    raise ConnectionError(
                        "leader connection lost during recovery; re-electing a worker"
                    ) from exc
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
    settings = get_settings()
    standby_logged = False
    while not stop.is_set():
        try:
            # Use a dedicated autocommit session for the advisory lock. Pooled
            # connections are shared with request transactions, and setting the
            # pool's session autocommit would leak into those request paths.
            async with await AsyncConnection.connect(
                settings.database_url,
                autocommit=True,
                row_factory=dict_row,
            ) as lock_conn:
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
                        if _connection_usable(lock_conn):
                            try:
                                await lock_conn.execute(
                                    "select pg_advisory_unlock(%s::bigint)",
                                    (LEADER_LOCK_KEY,),
                                )
                            except Exception as exc:  # noqa: BLE001 - closing the session also releases it
                                log.warning("could not release worker lock cleanly: %s", exc)
        except asyncio.CancelledError:
            raise
        except ConnectionError as exc:
            log.warning("worker leadership interrupted; re-electing: %s", exc)
        except Exception as exc:  # noqa: BLE001 - keep the worker alive
            log.exception("worker loop error: %s", exc)
        if not stop.is_set():
            await _wait(stop, LEADER_RETRY_SECONDS)
    log.info("ingestion worker stopped")
