from __future__ import annotations

from fastapi import APIRouter, Depends

from ..db import fetch_all, fetch_one
from ..deps import Context, get_context

router = APIRouter(tags=["overview"])


@router.get("/overview")
async def overview(ctx: Context = Depends(get_context)):
    stats = await fetch_one(
        ctx.conn,
        """select
             (select count(*) from review_items where status = 'pending') as pending_review,
             (select count(*) from review_items where status = 'pending'
                and created_at < now() - interval '7 days') as pending_over_7d,
             (select count(*) from conflicts where status = 'pending') as conflicts_pending,
             (select count(*) from gaps where status = 'open') as open_gaps,
             (select coalesce(sum(count), 0) from gaps where status = 'open') as gap_questions,
             (select count(*) from canonical_answers where status = 'approved') as approved_answers,
             (select count(*) from canonical_answers where status = 'in_review') as in_review,
             (select count(*) from canonical_answers where status = 'approved'
                and next_review_at is not null and next_review_at < now()) as answers_overdue,
             (select count(*) from sources where status = 'active' and deleted_at is null
                and alias_of_id is null) as sources,
             (select count(*) from sources where status = 'failed' and deleted_at is null) as failed_sources,
             (select count(*) from passages p
                join documents d on d.id = p.document_id
                join sources s on s.id = d.source_id
                where p.status = 'current' and d.status = 'current' and s.status = 'active') as passages,
             (select count(*) from qa_logs where created_at > now() - interval '7 days') as questions_7d,
             (select count(*) from qa_logs where abstained and created_at > now() - interval '7 days') as abstentions_7d,
             (select count(*) from qa_logs where mode = 'fallback'
                and created_at > now() - interval '7 days') as fallback_7d""",
    )
    assert stats is not None
    questions = stats["questions_7d"] or 0
    stats["abstention_rate"] = round(stats["abstentions_7d"] / questions, 3) if questions else 0
    stats["fallback_rate"] = round(stats["fallback_7d"] / questions, 3) if questions else 0
    stats["approved_coverage"] = (
        round(stats["approved_answers"] / (stats["approved_answers"] + stats["open_gaps"]), 3)
        if (stats["approved_answers"] + stats["open_gaps"])
        else 1.0
    )
    stats["stale_pct"] = (
        round(stats["answers_overdue"] / stats["approved_answers"], 3) if stats["approved_answers"] else 0
    )

    items = await fetch_all(
        ctx.conn,
        """select id, kind, priority, payload, created_at from review_items
           where status = 'pending' order by priority desc, created_at limit 8""",
    )
    gaps = await fetch_all(
        ctx.conn,
        """select id, representative_question, count, channels, last_seen_at from gaps
           where status = 'open' order by count desc, last_seen_at desc limit 5""",
    )
    recent = await fetch_all(
        ctx.conn,
        """select question, channel, mode, abstained, confidence_band, created_at
           from qa_logs order by created_at desc limit 8""",
    )
    return {"stats": stats, "needs_attention": items, "gaps": gaps, "recent_questions": recent}
