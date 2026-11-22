from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from .db import fetch_all, fetch_one, fetch_val
from .deps import Session
from .errors import AppError, permission, validation
from .ingest.pipeline import quote_in_text


async def _topic_id(conn: AsyncConnection, slug: str | None) -> UUID | None:
    if not slug:
        return None
    topic_id = await fetch_val(conn, "select id from topics where slug = %s", (slug,))
    if topic_id is None:
        raise validation(f"Unknown topic '{slug}'.")
    return topic_id


async def validate_evidence(
    conn: AsyncConnection,
    tenant_id: UUID,
    evidence: list[dict],
    visibility: str,
) -> list[dict]:
    if not evidence:
        raise validation("An approved answer needs at least one evidence passage.")
    cleaned: list[dict] = []
    for item in evidence:
        passage_id = item.get("passage_id")
        quote = (item.get("quote") or "").strip()
        if not passage_id or not quote:
            raise validation("Every evidence item needs a passage_id and a quote.")
        row = await fetch_one(
            conn,
            """select p.id, p.text, p.status, d.title as document_title, d.source_id,
                      s.visibility as source_visibility
               from passages p
               join documents d on d.id = p.document_id
               join sources s on s.id = d.source_id
               where p.id = %s""",
            (passage_id,),
        )
        if row is None:
            raise validation("One of the selected passages no longer exists.")
        if row["status"] != "current":
            raise validation("A superseded passage cannot support an approved answer. Re-check the source.")
        if not quote_in_text(quote, row["text"]):
            raise validation("A quote does not appear verbatim in its passage. Citations must be stored evidence.")
        if visibility == "public" and row["source_visibility"] != "public":
            raise validation("A public answer cannot cite an internal source.")
        cleaned.append({
            "passage_id": row["id"],
            "quote": quote,
            "claim_ref": item.get("claim_ref") or f"claim-{len(cleaned) + 1}",
        })
    return cleaned


async def create_answer(
    conn: AsyncConnection,
    session: Session,
    *,
    question: str,
    answer_md: str,
    topic_slug: str | None,
    plan_scope: list[str],
    version_scope: list[str],
    visibility: str,
    evidence: list[dict],
    owner_id: UUID | None = None,
    review_interval_days: int = 180,
    status: str = "in_review",
) -> dict:
    if not question.strip():
        raise validation("A canonical question is required.")
    if not answer_md.strip():
        raise validation("A draft answer needs some text.")
    if status not in ("draft", "in_review"):
        raise validation("New answers start as draft or in_review.")

    topic_id = await _topic_id(conn, topic_slug)
    cleaned = await validate_evidence(conn, session.tenant_id, evidence, visibility) if evidence else []
    if status == "in_review" and not cleaned:
        raise validation("Move an answer to review only when at least one evidence quote is attached.")

    row = await fetch_one(
        conn,
        """insert into canonical_answers
             (tenant_id, topic_id, question, answer_md, status, owner_id, review_interval_days,
              next_review_at, plan_scope, version_scope, visibility, current_version, created_by)
           values (%s, %s, %s, %s, %s, %s, %s, now() + %s * interval '1 day',
                   %s, %s, %s, 1, %s)
           returning *""",
        (
            session.tenant_id, topic_id, question.strip(), answer_md.strip(), status,
            owner_id or session.user_id, review_interval_days, review_interval_days,
            sorted(plan_scope), sorted(version_scope), visibility, session.user_id,
        ),
    )
    assert row is not None
    await conn.execute(
        """insert into answer_versions
             (tenant_id, answer_id, version, answer_md, changed_by, change_reason)
           values (%s, %s, 1, %s, %s, 'Initial draft')""",
        (session.tenant_id, row["id"], answer_md.strip(), session.user_id),
    )
    for quote in cleaned:
        await conn.execute(
            """insert into answer_evidence
                 (tenant_id, answer_id, answer_version, passage_id, quote, claim_ref)
               values (%s, %s, 1, %s, %s, %s)""",
            (session.tenant_id, row["id"], quote["passage_id"], quote["quote"], quote["claim_ref"]),
        )
    if status == "in_review":
        await conn.execute(
            """insert into review_items (tenant_id, kind, payload, priority, dedupe_key)
               values (%s, 'new_answer', %s, 60, %s)
               on conflict (tenant_id, dedupe_key)
                 where dedupe_key is not null and status = 'pending' do nothing""",
            (
                session.tenant_id,
                Jsonb({
                    "answer_id": str(row["id"]),
                    "question": row["question"],
                    "visibility": visibility,
                    "evidence_count": len(cleaned),
                    "summary": "Harbour drafted this from your sources. Publish it if it reads right.",
                }),
                f"new_answer:{row['id']}",
            ),
        )
    await conn.execute(
        """insert into audit_log (tenant_id, actor_type, actor_id, action, object_type, object_id, detail)
           values (%s, 'user', %s, 'answer.created', 'canonical_answer', %s, %s)""",
        (session.tenant_id, session.user_id, row["id"], Jsonb({"status": status})),
    )
    return row


async def update_answer(
    conn: AsyncConnection,
    session: Session,
    answer: dict,
    *,
    answer_md: str,
    evidence: list[dict],
    change_reason: str | None,
    visibility: str | None = None,
    plan_scope: list[str] | None = None,
    version_scope: list[str] | None = None,
    topic_slug: str | None = None,
    owner_id: UUID | None = None,
    review_interval_days: int | None = None,
) -> dict:
    new_visibility = visibility or answer["visibility"]
    cleaned = await validate_evidence(conn, session.tenant_id, evidence, new_visibility)
    version = answer["current_version"] + 1
    topic_id = await _topic_id(conn, topic_slug) if topic_slug else answer["topic_id"]
    interval = review_interval_days or answer["review_interval_days"]

    await conn.execute(
        """insert into answer_versions
             (tenant_id, answer_id, version, answer_md, changed_by, change_reason, supersedes_version)
           values (%s, %s, %s, %s, %s, %s, %s)""",
        (
            session.tenant_id, answer["id"], version, answer_md.strip(), session.user_id,
            change_reason or "Edited in the knowledge workspace", answer["current_version"],
        ),
    )
    for quote in cleaned:
        await conn.execute(
            """insert into answer_evidence
                 (tenant_id, answer_id, answer_version, passage_id, quote, claim_ref)
               values (%s, %s, %s, %s, %s, %s)""",
            (session.tenant_id, answer["id"], version, quote["passage_id"], quote["quote"], quote["claim_ref"]),
        )
    row = await fetch_one(
        conn,
        """update canonical_answers
           set answer_md = %s, current_version = %s, status = 'in_review', visibility = %s,
               plan_scope = %s, version_scope = %s, topic_id = %s, owner_id = %s,
               review_interval_days = %s, next_review_at = now() + %s * interval '1 day',
               approved_by = null, approved_at = null, updated_at = now()
           where id = %s
           returning *""",
        (
            answer_md.strip(), version, new_visibility, sorted(plan_scope or answer["plan_scope"]),
            sorted(version_scope or answer["version_scope"]), topic_id,
            owner_id or answer["owner_id"], interval, interval, answer["id"],
        ),
    )
    assert row is not None
    await conn.execute(
        """insert into review_items (tenant_id, kind, payload, priority, dedupe_key)
           values (%s, 'edit_proposal', %s, 60, %s)
           on conflict (tenant_id, dedupe_key)
             where dedupe_key is not null and status = 'pending' do nothing""",
        (
            session.tenant_id,
            Jsonb({
                "answer_id": str(answer["id"]),
                "question": row["question"],
                "answer_version": version,
                "summary": "An edited version is waiting to go live.",
            }),
            f"edit_answer:{answer['id']}:{version}",
        ),
    )
    await conn.execute(
        """insert into audit_log (tenant_id, actor_type, actor_id, action, object_type, object_id, detail)
           values (%s, 'user', %s, 'answer.version_created', 'canonical_answer', %s, %s)""",
        (session.tenant_id, session.user_id, answer["id"], Jsonb({"version": version})),
    )
    return row


async def approve_answer(conn: AsyncConnection, session: Session, answer_id: UUID) -> dict:
    answer = await fetch_one(conn, "select * from canonical_answers where id = %s", (answer_id,))
    if answer is None:
        raise AppError("not_found", "That canonical answer does not exist.", 404)
    evidence = await fetch_all(
        conn,
        """select ae.passage_id, ae.quote, p.status
           from answer_evidence ae join passages p on p.id = ae.passage_id
           where ae.answer_id = %s and ae.answer_version = %s""",
        (answer_id, answer["current_version"]),
    )
    if not evidence:
        raise validation("An answer cannot be approved without stored evidence.")
    stale = [row for row in evidence if row["status"] != "current"]
    if stale:
        raise validation("Some evidence is no longer current. Re-check the evidence before approving.")
    if answer["visibility"] == "public":
        internal = await fetch_val(
            conn,
            """select count(*) from answer_evidence ae
               join passages p on p.id = ae.passage_id
               join documents d on d.id = p.document_id
               join sources s on s.id = d.source_id
               where ae.answer_id = %s and ae.answer_version = %s and s.visibility <> 'public'""",
            (answer_id, answer["current_version"]),
        )
        if internal:
            raise validation("A public answer cannot cite an internal source.")

    interval = answer["review_interval_days"]
    row = await fetch_one(
        conn,
        """update canonical_answers
           set status = 'approved', approved_by = %s, approved_at = now(),
               next_review_at = now() + %s * interval '1 day', updated_at = now()
           where id = %s returning *""",
        (session.user_id, interval, answer_id),
    )
    assert row is not None
    await conn.execute(
        """update review_items
           set status = 'approved', decided_by = %s, decided_at = now(),
               decision_note = coalesce(decision_note, 'Approved with the canonical answer')
           where status = 'pending' and kind in ('new_answer','edit_proposal')
             and (payload->>'answer_id') = %s""",
        (session.user_id, str(answer_id)),
    )
    await conn.execute(
        """insert into audit_log (tenant_id, actor_type, actor_id, action, object_type, object_id, detail)
           values (%s, 'user', %s, 'answer.approved', 'canonical_answer', %s, %s)""",
        (session.tenant_id, session.user_id, answer_id, Jsonb({"version": row["current_version"]})),
    )
    return row
