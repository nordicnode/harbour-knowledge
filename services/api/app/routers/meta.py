from __future__ import annotations

from fastapi import APIRouter, Depends

from ..db import fetch_val
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
