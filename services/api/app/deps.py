from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from fastapi import Request
from psycopg import AsyncConnection

from .config import Settings, get_settings
from .db import fetch_one, resolve_tenant_id, tenant_conn
from .errors import AppError, permission


@dataclass(frozen=True)
class Session:
    tenant_id: UUID
    tenant_slug: str
    tenant_name: str
    user_id: UUID
    user_name: str
    email: str
    role: Literal["admin", "editor", "agent"]
    title: str


@dataclass
class Context:
    session: Session
    conn: AsyncConnection
    settings: Settings


async def get_context(request: Request):
    """Resolve the authenticated workspace context.

    Development uses an explicit stub: a workspace slug and a member email. It is
    enabled only when APP_ENV=development, so a production deployment fails
    closed until a real session verifier is wired in.
    """
    settings = get_settings()
    if settings.app_env != "development":
        raise AppError(
            "permission",
            "Authentication is not configured for this deployment. Wire the session verifier before serving traffic.",
            503,
        )

    slug = request.headers.get("x-tenant-slug") or settings.default_tenant_slug
    email = request.headers.get("x-user-email")

    tenant_id = await resolve_tenant_id(slug)
    if tenant_id is None:
        raise AppError("not_found", f"Workspace '{slug}' does not exist.", 404)

    async with tenant_conn(tenant_id) as conn:
        tenant = await fetch_one(conn, "select id, name, slug from tenants where id = %s", (tenant_id,))
        if email:
            user = await fetch_one(
                conn,
                "select id, name, email, role, title from users where tenant_id = %s and email = %s",
                (tenant_id, email),
            )
        else:
            user = await fetch_one(
                conn,
                """select id, name, email, role, title from users
                   where tenant_id = %s
                   order by case role when 'admin' then 0 when 'editor' then 1 else 2 end, created_at
                   limit 1""",
                (tenant_id,),
            )
        if user is None:
            raise AppError(
                "not_found",
                "This workspace has no members yet. Run the seed script or invite a member.",
                404,
            )

        session = Session(
            tenant_id=tenant_id,
            tenant_slug=tenant["slug"],
            tenant_name=tenant["name"],
            user_id=user["id"],
            user_name=user["name"],
            email=user["email"],
            role=user["role"],
            title=user["title"],
        )
        yield Context(session=session, conn=conn, settings=settings)


def require_editor(ctx: Context) -> None:
    if ctx.session.role not in ("admin", "editor"):
        raise permission("Only an editor or admin can approve knowledge changes.")


def require_admin(ctx: Context) -> None:
    if ctx.session.role != "admin":
        raise permission("Only an admin can change workspace settings.")
