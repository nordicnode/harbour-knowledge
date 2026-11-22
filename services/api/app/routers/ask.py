from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..deps import Context, get_context
from ..retrieval.answer import AskRequest, ask, record_feedback

router = APIRouter(tags=["answering"])


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None
    channel: Literal["web", "public", "widget", "discord"] = "web"
    plan: str | None = None
    product_version: str | None = None
    as_of: date | None = None
    tone: Literal["short", "friendly", "formal"] | None = None
    history: list[dict] = Field(default_factory=list)


class FeedbackBody(BaseModel):
    thumbs: Literal[-1, 0, 1] | None = None
    edited_answer_md: str | None = None
    note: str | None = None


@router.post("/ask")
async def ask_endpoint(body: AskBody, ctx: Context = Depends(get_context)):
    result = await ask(
        ctx.conn,
        ctx.session,
        ctx.settings,
        AskRequest(
            question=body.question,
            channel=body.channel,
            plan=body.plan,
            product_version=body.product_version,
            as_of=body.as_of,
            tone=body.tone,
            history=body.history,
        ),
    )
    return result


@router.post("/ask/{qa_log_id}/feedback")
async def feedback_endpoint(
    qa_log_id: UUID,
    body: FeedbackBody,
    ctx: Context = Depends(get_context),
):
    return await record_feedback(
        ctx.conn,
        ctx.session,
        qa_log_id,
        body.thumbs,
        body.edited_answer_md,
        body.note,
    )
