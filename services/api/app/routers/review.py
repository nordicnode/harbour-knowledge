from __future__ import annotations

from datetime import timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from psycopg.types.json import Jsonb

from ..answers_service import approve_answer, update_answer
from ..db import fetch_all, fetch_one, fetch_val
from ..deps import Context, get_context, require_editor
from ..errors import not_found, permission, validation

router = APIRouter(tags=["governance"])

UNDO_WINDOW_SECONDS = 15


class DecideBody(BaseModel):
    decision: Literal["approve", "reject", "defer"]
    note: str | None = None
    resolution: str | None = None


async def _build_undo_snapshot(ctx: Context, item: dict, payload: dict, decision: str) -> dict:
    """Capture the rows a decision will touch so a recent decision can be reverted."""
    undo: dict = {
        "decision": decision,
        "kind": item["kind"],
        "conflict": None,
        "answer": None,
        "cascaded_ids": [],
        "created_version": None,
        "created_review_item_id": None,
        "expected": {},
    }
    if decision == "defer":
        return undo

    kind = item["kind"]
    answer_id = payload.get("answer_id")

    if kind == "conflict":
        conflict = await fetch_val(
            ctx.conn,
            "select to_jsonb(c) from conflicts c where c.id = %s",
            (payload.get("conflict_id"),),
        )
        if conflict:
            undo["conflict"] = conflict
            undo["expected"]["conflict_status"] = "resolved" if decision == "approve" else "dismissed"

    answer_side_effect = bool(answer_id) and (
        (kind == "new_answer" and decision in ("approve", "reject"))
        or (kind == "edit_proposal" and decision == "approve")
        or (kind == "stale_answer" and decision == "approve")
        or (kind == "change_impact" and decision == "approve")
    )
    if answer_side_effect:
        answer = await fetch_val(
            ctx.conn,
            "select to_jsonb(ca) from canonical_answers ca where ca.id = %s",
            (answer_id,),
        )
        if answer:
            expected: dict = {
                "answer_status": answer["status"],
                "answer_version": answer["current_version"],
            }
            if kind == "new_answer":
                expected["answer_status"] = "approved" if decision == "approve" else "archived"
            elif kind == "edit_proposal":
                expected["answer_status"] = "approved"
                if payload.get("suggested_md"):
                    undo["created_version"] = int(answer["current_version"]) + 1
                    expected["answer_version"] = undo["created_version"]
            elif kind == "stale_answer":
                expected["answer_status"] = "needs_recheck"
            undo["answer"] = answer
            undo["expected"] = expected

            if decision == "approve" and kind in ("new_answer", "edit_proposal"):
                cascaded = await fetch_all(
                    ctx.conn,
                    """select id from review_items
                       where status = 'pending'
                         and kind in ('new_answer','edit_proposal')
                         and payload->>'answer_id' = %s and id <> %s""",
                    (answer_id, item["id"]),
                )
                undo["cascaded_ids"] = [str(row["id"]) for row in cascaded]

    return undo


async def _enrich(ctx: Context, item: dict) -> dict:
    payload = item["payload"] or {}
    item["summary"] = payload.get("summary") or payload.get("question") or item["kind"].replace("_", " ")
    item["conflict"] = None
    item["answer"] = None
    item["suggested_md"] = payload.get("suggested_md")

    if item["kind"] == "conflict" or payload.get("conflict_id"):
        conflict = await fetch_one(
            ctx.conn,
            """select c.*, pa.text as quote_a, pb.text as quote_b,
                      da.title as document_a, db.title as document_b,
                      da.effective_from as effective_from_a, db.effective_from as effective_from_b,
                      sa.title as source_a, sb.title as source_b,
                      sa.visibility as visibility_a, sb.visibility as visibility_b,
                      sa.source_tier as source_tier_a, sb.source_tier as source_tier_b,
                      ca.question as answer_a_question, cb.question as answer_b_question
               from conflicts c
               left join passages pa on pa.id = c.passage_id_a
               left join passages pb on pb.id = c.passage_id_b
               left join documents da on da.id = pa.document_id
               left join documents db on db.id = pb.document_id
               left join sources sa on sa.id = da.source_id
               left join sources sb on sb.id = db.source_id
               left join canonical_answers ca on ca.id = c.answer_id_a
               left join canonical_answers cb on cb.id = c.answer_id_b
               where c.id = %s""",
            (payload.get("conflict_id"),),
        )
        item["conflict"] = conflict

    if payload.get("answer_id"):
        answer = await fetch_one(
            ctx.conn,
            """select ca.*, t.name as topic_name,
                      (select count(*) from answer_evidence ae
                        where ae.answer_id = ca.id and ae.answer_version = ca.current_version) as evidence_count
               from canonical_answers ca left join topics t on t.id = ca.topic_id
               where ca.id = %s""",
            (payload.get("answer_id"),),
        )
        item["answer"] = answer

    if item["kind"] == "new_answer" and not payload.get("answer_id"):
        passages = await fetch_all(
            ctx.conn,
            """select p.id, p.text, p.section_path, d.title as document_title, s.title as source_title
               from passages p join documents d on d.id = p.document_id join sources s on s.id = d.source_id
               where p.id = any(%s::uuid[])""",
            (payload.get("evidence_passage_ids") or [],),
        )
        item["suggested_evidence"] = passages

    return item


@router.get("/review-items")
async def list_review_items(
    kind: str | None = None,
    ctx: Context = Depends(get_context),
):
    params: list = []
    kind_clause = ""
    if kind:
        kind_clause = "and kind = %s"
        params.append(kind)
    rows = await fetch_all(
        ctx.conn,
        f"""select * from review_items
            where status = 'pending' {kind_clause}
            order by priority desc, created_at""",
        tuple(params),
    )
    enriched = [await _enrich(ctx, row) for row in rows]
    counts = await fetch_all(
        ctx.conn,
        """select kind, count(*) as count from review_items
           where status = 'pending' group by kind order by count desc""",
    )
    return {"items": enriched, "counts": counts}


@router.post("/review-items/{item_id}/decide")
async def decide(item_id: UUID, body: DecideBody, ctx: Context = Depends(get_context)):
    require_editor(ctx)
    item = await fetch_one(ctx.conn, "select * from review_items where id = %s", (item_id,))
    if item is None:
        raise not_found("That review item does not exist.")
    if item["status"] != "pending":
        raise validation("That review item has already been decided.")

    payload = item["payload"] or {}
    decision = body.decision
    note = body.note
    result: dict = {"ok": True}
    decision_at = await fetch_val(ctx.conn, "select now()")
    undo_expires_at = decision_at + timedelta(seconds=UNDO_WINDOW_SECONDS)
    undo = await _build_undo_snapshot(ctx, item, payload, decision)

    if decision == "defer":
        await ctx.conn.execute(
            """update review_items set status = 'deferred', decided_by = %s, decided_at = %s,
                      decision_note = %s, undo_payload = %s, undo_expires_at = %s where id = %s""",
            (ctx.session.user_id, decision_at, note, Jsonb(undo), undo_expires_at, item_id),
        )
        return {"ok": True, "status": "deferred", "undo_expires_at": undo_expires_at.isoformat()}

    if item["kind"] == "conflict":
        conflict = await fetch_one(ctx.conn, "select * from conflicts where id = %s", (payload.get("conflict_id"),))
        if conflict is None:
            raise not_found("The conflict behind this item no longer exists.")
        if decision == "approve":
            resolution = body.resolution or note
            if not resolution:
                raise validation("Record which answer wins, or why the pair is compatible.")
            await ctx.conn.execute(
                """update conflicts set status = 'resolved', resolution = %s, resolved_by = %s,
                          resolved_at = now(), updated_at = now() where id = %s""",
                (resolution, ctx.session.user_id, conflict["id"]),
            )
            result["conflict_status"] = "resolved"
        else:
            await ctx.conn.execute(
                """update conflicts set status = 'dismissed', resolution = %s, resolved_by = %s,
                          resolved_at = now(), updated_at = now() where id = %s""",
                (note or "Dismissed as a false positive", ctx.session.user_id, conflict["id"]),
            )
            result["conflict_status"] = "dismissed"

    elif item["kind"] == "new_answer" and payload.get("answer_id"):
        answer_id = UUID(payload["answer_id"])
        if decision == "approve":
            approved = await approve_answer(ctx.conn, ctx.session, answer_id)
            result["answer"] = {"id": str(approved["id"]), "status": approved["status"]}
        else:
            await ctx.conn.execute(
                """update canonical_answers set status = 'archived', updated_at = now() where id = %s""",
                (answer_id,),
            )
            result["answer"] = {"id": str(answer_id), "status": "archived"}

    elif item["kind"] == "edit_proposal" and payload.get("answer_id"):
        answer = await fetch_one(
            ctx.conn, "select * from canonical_answers where id = %s", (UUID(payload["answer_id"]),)
        )
        if answer is None:
            raise not_found("The answer behind this proposal no longer exists.")
        if decision == "approve":
            # Workspace edits are already saved as a new immutable version and
            # only need re-approval. Agent-suggested edits carry suggested_md
            # and must be applied before approval.
            if payload.get("suggested_md"):
                evidence = await fetch_all(
                    ctx.conn,
                    """select passage_id, quote, claim_ref from answer_evidence
                       where answer_id = %s and answer_version = %s""",
                    (answer["id"], answer["current_version"]),
                )
                answer = await update_answer(
                    ctx.conn,
                    ctx.session,
                    answer,
                    answer_md=payload["suggested_md"],
                    evidence=[dict(row) for row in evidence],
                    change_reason="Approved agent edit",
                )
            approved = await approve_answer(ctx.conn, ctx.session, answer["id"])
            result["answer"] = {"id": str(approved["id"]), "status": approved["status"]}
        else:
            result["answer"] = {"id": str(answer["id"]), "status": answer["status"]}

    elif item["kind"] == "stale_answer" and payload.get("answer_id"):
        if decision == "approve":
            await ctx.conn.execute(
                "update canonical_answers set status = 'needs_recheck', updated_at = now() where id = %s",
                (UUID(payload["answer_id"]),),
            )
            result["answer"] = {"id": payload["answer_id"], "status": "needs_recheck"}
    elif item["kind"] == "change_impact" and payload.get("answer_id"):
        if decision == "approve":
            # A change-impact item asks an owner to confirm the affected answer.
            # Confirming resets its review clock; it does not unpublish the answer.
            row = await fetch_one(
                ctx.conn,
                """update canonical_answers
                   set next_review_at = now() + review_interval_days * interval '1 day', updated_at = now()
                   where id = %s returning id, status""",
                (UUID(payload["answer_id"]),),
            )
            result["answer"] = {"id": str(row["id"]), "status": row["status"]} if row else None
    elif item["kind"] == "new_answer" and not payload.get("answer_id"):
        if decision == "approve":
            raise validation(
                "This proposal has no drafted answer yet. Open it, attach evidence, and save a canonical draft."
            )

    if undo.get("created_version") and payload.get("answer_id"):
        created_id = await fetch_val(
            ctx.conn,
            """select id from review_items
               where dedupe_key = %s and created_at >= %s
               order by created_at desc limit 1""",
            (f"edit_answer:{payload['answer_id']}:{undo['created_version']}", decision_at),
        )
        undo["created_review_item_id"] = str(created_id) if created_id else None

    final_status = "approved" if decision == "approve" else "rejected"
    await ctx.conn.execute(
        """update review_items set status = %s, decided_by = %s, decided_at = %s,
                  decision_note = %s, undo_payload = %s, undo_expires_at = %s where id = %s""",
        (
            final_status, ctx.session.user_id, decision_at, note,
            Jsonb(undo), undo_expires_at, item_id,
        ),
    )
    await ctx.conn.execute(
        """insert into audit_log (tenant_id, actor_type, actor_id, action, object_type, object_id, detail)
           values (%s, 'user', %s, 'review.decided', 'review_item', %s, %s)""",
        (
            ctx.session.tenant_id, ctx.session.user_id, item_id,
            Jsonb({"decision": decision, "kind": item["kind"], "note": note}),
        ),
    )
    result["undo_expires_at"] = undo_expires_at.isoformat()
    return result


@router.post("/review-items/{item_id}/undo")
async def undo_review_decision(item_id: UUID, ctx: Context = Depends(get_context)):
    """Revert a decision made by this user within the short undo window."""
    require_editor(ctx)
    item = await fetch_one(
        ctx.conn,
        "select * from review_items where id = %s for update",
        (item_id,),
    )
    if item is None:
        raise not_found("That review item does not exist.")
    if item["status"] == "pending":
        raise validation("That item is already back in the queue.")
    if item["decided_by"] != ctx.session.user_id:
        raise permission("Only the person who made the decision can undo it.")

    snapshot = item["undo_payload"] or {}
    if not snapshot:
        raise validation("There is no undo snapshot for this decision.")
    valid = await fetch_val(
        ctx.conn,
        "select coalesce(%s > now(), false)",
        (item["undo_expires_at"],),
    )
    if not valid:
        raise validation("The undo window has closed.")

    expected = snapshot.get("expected") or {}
    conflict_before = snapshot.get("conflict")
    answer_before = snapshot.get("answer")

    if conflict_before:
        current = await fetch_one(
            ctx.conn,
            "select id, status from conflicts where id = %s for update",
            (conflict_before["id"],),
        )
        if current is None:
            raise validation("The conflict behind this decision no longer exists.")
        if expected.get("conflict_status") and current["status"] != expected["conflict_status"]:
            raise validation("This conflict changed after the decision, so it cannot be undone safely.")

    if answer_before:
        current = await fetch_one(
            ctx.conn,
            "select id, status, current_version from canonical_answers where id = %s for update",
            (answer_before["id"],),
        )
        if current is None:
            raise validation("The answer behind this decision no longer exists.")
        if expected.get("answer_status") and current["status"] != expected["answer_status"]:
            raise validation("This answer changed after the decision, so it cannot be undone safely.")
        if (
            expected.get("answer_version") is not None
            and int(current["current_version"]) != int(expected["answer_version"])
        ):
            raise validation("A newer answer version exists, so this decision cannot be undone safely.")

    created_version = snapshot.get("created_version")
    if created_version and answer_before:
        await ctx.conn.execute(
            "delete from answer_evidence where answer_id = %s and answer_version = %s",
            (answer_before["id"], created_version),
        )
        await ctx.conn.execute(
            "delete from answer_versions where answer_id = %s and version = %s",
            (answer_before["id"], created_version),
        )

    created_item_id = snapshot.get("created_review_item_id")
    if created_item_id:
        await ctx.conn.execute(
            """delete from review_items
               where id = %s and status in ('approved','rejected') and decided_by = %s""",
            (UUID(created_item_id), ctx.session.user_id),
        )

    cascaded = [UUID(value) for value in (snapshot.get("cascaded_ids") or [])]
    if cascaded:
        await ctx.conn.execute(
            """update review_items
               set status = 'pending', decided_by = null, decided_at = null,
                   decision_note = null, undo_payload = null, undo_expires_at = null
               where id = any(%s::uuid[]) and status <> 'pending'""",
            (cascaded,),
        )

    if conflict_before:
        await ctx.conn.execute(
            """update conflicts c
               set status = s.status, resolution = s.resolution, resolved_by = s.resolved_by,
                   resolved_at = s.resolved_at, updated_at = s.updated_at
               from jsonb_populate_record(null::conflicts, %s::jsonb) s
               where c.id = %s""",
            (Jsonb(conflict_before), conflict_before["id"]),
        )

    if answer_before:
        await ctx.conn.execute(
            """update canonical_answers ca
               set topic_id = s.topic_id, question = s.question, answer_md = s.answer_md,
                   status = s.status, owner_id = s.owner_id,
                   review_interval_days = s.review_interval_days, next_review_at = s.next_review_at,
                   plan_scope = s.plan_scope, version_scope = s.version_scope,
                   visibility = s.visibility, human_confidence = s.human_confidence,
                   current_version = s.current_version, created_by = s.created_by,
                   approved_by = s.approved_by, approved_at = s.approved_at, updated_at = s.updated_at
               from jsonb_populate_record(null::canonical_answers, %s::jsonb) s
               where ca.id = %s""",
            (Jsonb(answer_before), answer_before["id"]),
        )

    await ctx.conn.execute(
        """update review_items
           set status = 'pending', decided_by = null, decided_at = null,
               decision_note = null, undo_payload = null, undo_expires_at = null
           where id = %s""",
        (item_id,),
    )
    await ctx.conn.execute(
        """insert into audit_log (tenant_id, actor_type, actor_id, action, object_type, object_id, detail)
           values (%s, 'user', %s, 'review.undone', 'review_item', %s, %s)""",
        (
            ctx.session.tenant_id, ctx.session.user_id, item_id,
            Jsonb({"decision": snapshot.get("decision"), "kind": item["kind"]}),
        ),
    )
    return {"ok": True, "status": "pending", "item_id": str(item_id)}


@router.get("/conflicts")
async def list_conflicts(status: str = "pending", ctx: Context = Depends(get_context)):
    rows = await fetch_all(
        ctx.conn,
        """select c.*, pa.text as quote_a, pb.text as quote_b,
                  da.title as document_a, db.title as document_b,
                  sa.title as source_a, sb.title as source_b,
                  sa.source_tier as source_tier_a, sb.source_tier as source_tier_b
           from conflicts c
           left join passages pa on pa.id = c.passage_id_a
           left join passages pb on pb.id = c.passage_id_b
           left join documents da on da.id = pa.document_id
           left join documents db on db.id = pb.document_id
           left join sources sa on sa.id = da.source_id
           left join sources sb on sb.id = db.source_id
           where c.status = %s
           order by c.created_at desc""",
        (status,),
    )
    return {"conflicts": rows}
