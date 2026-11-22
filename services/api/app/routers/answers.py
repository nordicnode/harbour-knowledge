from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..answers_service import approve_answer, create_answer, update_answer, validate_evidence
from ..db import fetch_all, fetch_one
from ..deps import Context, get_context, require_editor
from ..errors import not_found, validation
from ..intelligence import EvidencePassage, get_intelligence
from ..ingest.pipeline import quote_in_text

router = APIRouter(tags=["answers"])


class EvidenceInput(BaseModel):
    passage_id: UUID
    quote: str
    claim_ref: str | None = None


class AnswerCreate(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    answer_md: str = ""
    topic_slug: str | None = None
    plan_scope: list[str] = Field(default_factory=list)
    version_scope: list[str] = Field(default_factory=list)
    visibility: Literal["internal", "public"] = "internal"
    evidence: list[EvidenceInput] = Field(default_factory=list)
    owner_id: UUID | None = None
    review_interval_days: int = Field(default=180, ge=1, le=730)


class AnswerPatch(BaseModel):
    answer_md: str = Field(min_length=1)
    change_reason: str | None = None
    topic_slug: str | None = None
    plan_scope: list[str] | None = None
    version_scope: list[str] | None = None
    visibility: Literal["internal", "public"] | None = None
    owner_id: UUID | None = None
    review_interval_days: int | None = Field(default=None, ge=1, le=730)
    evidence: list[EvidenceInput] | None = None


@router.get("/answers")
async def list_answers(
    status: str | None = None,
    topic: str | None = None,
    ctx: Context = Depends(get_context),
):
    where = ["1=1"]
    params: list = []
    if status:
        where.append("ca.status = %s")
        params.append(status)
    if topic:
        where.append("t.slug = %s")
        params.append(topic)
    rows = await fetch_all(
        ctx.conn,
        f"""select ca.*, t.name as topic_name, t.slug as topic_slug,
                   u.name as owner_name,
                   (select count(*) from answer_evidence ae
                     where ae.answer_id = ca.id and ae.answer_version = ca.current_version) as evidence_count,
                   (select count(*) from answer_evidence ae
                     join passages p on p.id = ae.passage_id
                     where ae.answer_id = ca.id and ae.answer_version = ca.current_version
                       and p.status <> 'current') as stale_evidence,
                   (ca.next_review_at is not null and ca.next_review_at < now()) as overdue
            from canonical_answers ca
            left join topics t on t.id = ca.topic_id
            left join users u on u.id = ca.owner_id
            where {' and '.join(where)}
            order by
              case ca.status when 'needs_recheck' then 0 when 'in_review' then 1 when 'approved' then 2
                   when 'draft' then 3 else 4 end,
              ca.updated_at desc""",
        tuple(params),
    )
    return {"answers": rows}


@router.get("/answers/{answer_id}")
async def get_answer(answer_id: UUID, ctx: Context = Depends(get_context)):
    answer = await fetch_one(
        ctx.conn,
        """select ca.*, t.name as topic_name, t.slug as topic_slug, u.name as owner_name
           from canonical_answers ca
           left join topics t on t.id = ca.topic_id
           left join users u on u.id = ca.owner_id
           where ca.id = %s""",
        (answer_id,),
    )
    if answer is None:
        raise not_found("That canonical answer does not exist.")
    evidence = await fetch_all(
        ctx.conn,
        """select ae.id, ae.passage_id, ae.quote, ae.claim_ref, p.text, p.section_path, p.status,
                  p.document_id, d.title as document_title, d.effective_from,
                  s.id as source_id, s.title as source_title, s.visibility as source_visibility
           from answer_evidence ae
           join passages p on p.id = ae.passage_id
           join documents d on d.id = p.document_id
           join sources s on s.id = d.source_id
           where ae.answer_id = %s and ae.answer_version = %s
           order by ae.created_at""",
        (answer_id, answer["current_version"]),
    )
    for row in evidence:
        row["quote_verified"] = row["status"] == "current" and quote_in_text(row["quote"], row["text"])
    versions = await fetch_all(
        ctx.conn,
        """select av.version, av.answer_md, av.change_reason, av.created_at,
                  u.name as changed_by_name
           from answer_versions av left join users u on u.id = av.changed_by
           where av.answer_id = %s order by av.version desc""",
        (answer_id,),
    )
    items = await fetch_all(
        ctx.conn,
        """select id, kind, status, priority, payload, created_at from review_items
           where status = 'pending' and payload->>'answer_id' = %s
           order by created_at""",
        (str(answer_id),),
    )
    return {"answer": answer, "evidence": evidence, "versions": versions, "review_items": items}


@router.post("/answers", status_code=201)
async def create_answer_endpoint(body: AnswerCreate, ctx: Context = Depends(get_context)):
    evidence = [item.model_dump() for item in body.evidence]
    answer_md = body.answer_md.strip()

    if not answer_md:
        if not evidence:
            raise validation("Give the answer some text, or select source passages to draft from.")
        passages = await fetch_all(
            ctx.conn,
            """select p.id, p.text, p.section_path, d.title as document_title, d.effective_from,
                      s.visibility, s.source_tier
               from passages p
               join documents d on d.id = p.document_id
               join sources s on s.id = d.source_id
               where p.id = any(%s::uuid[]) and p.status = 'current'""",
            ([item["passage_id"] for item in evidence],),
        )
        if not passages:
            raise validation("None of the selected passages are current.")
        source_evidence = [
            EvidencePassage(
                evidence_id=str(row["id"]),
                text=row["text"],
                title=row["document_title"],
                section_path=row["section_path"],
                source_date=str(row["effective_from"]) if row["effective_from"] else None,
                source_tier=row["source_tier"],
                visibility=row["visibility"],
                plan_scope=body.plan_scope,
                version_scope=body.version_scope,
            )
            for row in passages
        ]
        draft = await get_intelligence().draft_answer(body.question, source_evidence)
        answer_md = draft.answer_md
        evidence = [
            {"passage_id": quote.passage_id, "quote": quote.quote, "claim_ref": quote.claim_ref}
            for quote in draft.evidence
        ]

    answer = await create_answer(
        ctx.conn,
        ctx.session,
        question=body.question,
        answer_md=answer_md,
        topic_slug=body.topic_slug,
        plan_scope=body.plan_scope,
        version_scope=body.version_scope,
        visibility=body.visibility,
        evidence=evidence,
        owner_id=body.owner_id,
        review_interval_days=body.review_interval_days,
        status="in_review" if evidence else "draft",
    )
    return answer


@router.patch("/answers/{answer_id}")
async def patch_answer(answer_id: UUID, body: AnswerPatch, ctx: Context = Depends(get_context)):
    require_editor(ctx)
    answer = await fetch_one(ctx.conn, "select * from canonical_answers where id = %s", (answer_id,))
    if answer is None:
        raise not_found("That canonical answer does not exist.")

    if body.evidence is None:
        evidence = await fetch_all(
            ctx.conn,
            """select passage_id, quote, claim_ref from answer_evidence
               where answer_id = %s and answer_version = %s""",
            (answer_id, answer["current_version"]),
        )
    else:
        evidence = [item.model_dump() for item in body.evidence]

    updated = await update_answer(
        ctx.conn,
        ctx.session,
        answer,
        answer_md=body.answer_md,
        evidence=evidence,
        change_reason=body.change_reason,
        visibility=body.visibility,
        plan_scope=body.plan_scope,
        version_scope=body.version_scope,
        topic_slug=body.topic_slug,
        owner_id=body.owner_id,
        review_interval_days=body.review_interval_days,
    )
    return updated


@router.post("/answers/{answer_id}/approve")
async def approve_endpoint(answer_id: UUID, ctx: Context = Depends(get_context)):
    require_editor(ctx)
    return await approve_answer(ctx.conn, ctx.session, answer_id)


@router.post("/answers/{answer_id}/archive")
async def archive_endpoint(answer_id: UUID, ctx: Context = Depends(get_context)):
    require_editor(ctx)
    answer = await fetch_one(ctx.conn, "select * from canonical_answers where id = %s", (answer_id,))
    if answer is None:
        raise not_found("That canonical answer does not exist.")
    row = await fetch_one(
        ctx.conn,
        """update canonical_answers set status = 'archived', updated_at = now()
           where id = %s returning *""",
        (answer_id,),
    )
    await ctx.conn.execute(
        """update review_items set status = 'deferred', decided_by = %s, decided_at = now(),
                  decision_note = coalesce(decision_note, 'Answer archived')
           where status = 'pending' and payload->>'answer_id' = %s""",
        (ctx.session.user_id, str(answer_id)),
    )
    await ctx.conn.execute(
        """insert into audit_log (tenant_id, actor_type, actor_id, action, object_type, object_id, detail)
           values (%s, 'user', %s, 'answer.archived', 'canonical_answer', %s, '{}'::jsonb)""",
        (ctx.session.tenant_id, ctx.session.user_id, answer_id),
    )
    return row
