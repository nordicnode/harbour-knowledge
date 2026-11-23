from __future__ import annotations

from fastapi import APIRouter, Depends

from ..db import fetch_one, fetch_val
from ..deps import Context, get_context
from ..intelligence import get_intelligence

router = APIRouter(tags=["meta"])


@router.get("/meta")
async def meta(ctx: Context = Depends(get_context)):
    settings = ctx.settings
    pending_review = await fetch_val(
        ctx.conn,
        "select count(*) from review_items where status = 'pending'",
    )
    identity = settings.embedding_identity
    index = await fetch_one(
        ctx.conn,
        """select
             count(*) filter (where p.embedding is not null and p.embedding_identity = %s) as compatible,
             count(*) filter (
               where p.embedding is not null and p.embedding_identity is distinct from %s
             ) as stale,
             count(*) as total
           from passages p
           join documents d on d.id = p.document_id
           join sources s on s.id = d.source_id
           where p.level = 'chunk' and p.status = 'current' and d.status = 'current'
             and s.status = 'active' and s.deleted_at is null""",
        (identity, identity),
    ) or {"compatible": 0, "stale": 0, "total": 0}
    return {
        "workspace": {
            "slug": ctx.session.tenant_slug,
            "name": ctx.session.tenant_name,
        },
        "member": {
            "name": ctx.session.user_name,
            "email": ctx.session.email,
            "role": ctx.session.role,
            "title": ctx.session.title,
        },
        "ai": {
            "mode": settings.ai_mode,
            "provider": get_intelligence().name,
            "embedding_model": settings.active_embedding_model,
            "embedding_provider": settings.embedding_provider,
            "embedding_version": settings.embedding_version,
            "embedding_dim": settings.embedding_dim,
            "embedding_identity": identity,
            "index": {
                "compatible_passages": int(index["compatible"] or 0),
                "stale_passages": int(index["stale"] or 0),
                "total_passages": int(index["total"] or 0),
                "needs_reindex": int(index["stale"] or 0) > 0,
            },
        },
        "thresholds": {
            "internal": {
                "low": settings.internal_low_threshold,
                "high": settings.internal_high_threshold,
            },
            "public": {
                "low": settings.public_low_threshold,
                "high": settings.public_high_threshold,
            },
        },
        "review": {"pending": int(pending_review or 0)},
        "channels": ["web", "public", "widget", "discord"],
    }
