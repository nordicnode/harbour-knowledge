from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import get_settings

log = logging.getLogger(__name__)

_pool: AsyncConnectionPool | None = None


def get_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("Database pool is not open")
    return _pool


async def open_pool() -> None:
    global _pool
    settings = get_settings()
    _pool = AsyncConnectionPool(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        open=False,
        kwargs={"row_factory": dict_row},
    )
    await _pool.open(wait=True, timeout=15)
    log.info("database pool open")


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def tenant_conn(tenant_id: UUID | str) -> AsyncIterator[AsyncConnection]:
    """A transaction with RLS scoped to one tenant.

    Every app query runs through here. If the setting is missing, RLS returns
    zero rows rather than leaking another tenant's data.
    """
    async with get_pool().connection() as conn:
        async with conn.transaction():
            await conn.execute("select set_config('app.tenant_id', %s, true)", (str(tenant_id),))
            yield conn


async def resolve_tenant_id(slug: str) -> UUID | None:
    async with get_pool().connection() as conn:
        cur = await conn.execute("select tenant_id_for_slug(%s) as id", (slug,))
        row = await cur.fetchone()
        return row["id"] if row else None


async def fetch_one(conn: AsyncConnection, query: str, params: tuple | dict = ()) -> dict | None:
    cur = await conn.execute(query, params)
    return await cur.fetchone()


async def fetch_all(conn: AsyncConnection, query: str, params: tuple | dict = ()) -> list[dict]:
    cur = await conn.execute(query, params)
    return list(await cur.fetchall())


async def fetch_val(conn: AsyncConnection, query: str, params: tuple | dict = ()):
    row = await fetch_one(conn, query, params)
    if row is None:
        return None
    return next(iter(row.values()))
