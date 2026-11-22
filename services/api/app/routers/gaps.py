from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..db import fetch_all, fetch_one
from ..deps import Context, get_context, require_editor
from ..errors import not_found, validation

router = APIRouter(tags=["governance"])


class GapStatusBody(BaseModel):
    status: Literal["open", "answered", "wont_answer"]
    linked_answer_id: UUID | None = None
    note: str | None = None


@router.get("/gaps")
async def list_gaps(status: str = "open", ctx: Context = Depends(get_context)):
    rows = await fetch_all(
        ctx.conn,
        """select g.*, ca.question as linked_question
           from gaps g left join canonical_answers ca on ca.id = g.linked_answer_id
           where g.status = %s
           order by g.count desc, g.last_seen_at desc""",
        (status,),
    )
    totals = await fetch_all(
        ctx.conn,
        "select status, count(*) as count from gaps group by status",
    )
    return {"gaps": rows, "totals": totals}


@router.post("/gaps/{gap_id}/status")
async def set_gap_status(gap_id: UUID, body: GapStatusBody, ctx: Context = Depends(get_context)):
    require_editor(ctx)
    gap = await fetch_one(ctx.conn, "select * from gaps where id = %s", (gap_id,))
    if gap is None:
        raise not_found("That gap does not exist.")
    if body.status == "answered":
        if not body.linked_answer_id:
            raise validation("Link the canonical answer that closes this gap.")
        answer = await fetch_one(
            ctx.conn,
            "select id, status from canonical_answers where id = %s",
            (body.linked_answer_id,),
        )
        if answer is None or answer["status"] != "approved":
            raise validation("Only an approved canonical answer can close a gap.")
    row = await fetch_one(
        ctx.conn,
        """update gaps set status = %s, linked_answer_id = %s, updated_at = now()
           where id = %s returning *""",
        (body.status, body.linked_answer_id if body.status == "answered" else None, gap_id),
    )
    await ctx.conn.execute(
        """insert into audit_log (tenant_id, actor_type, actor_id, action, object_type, object_id, detail)
           values (%s, 'user', %s, 'gap.status_changed', 'gap', %s, %s)""",
        (ctx.session.tenant_id, ctx.session.user_id, gap_id, {"status": body.status, "note": body.note}),
    )
    return row
