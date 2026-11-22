from __future__ import annotations

import difflib
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from ..config import Settings
from ..deps import Session
from ..errors import AppError, validation
from ..intelligence import EvidencePassage, get_intelligence
from ..ingest.pipeline import quote_in_text
from .search import (
    RetrievedPassage,
    band_for,
    confidence_composite,
    hybrid_search,
    lexical_faithfulness,
    retrieval_strength,
    vector_literal,
)

CANONICAL_MATCH_THRESHOLD = 0.36


@dataclass
class AskRequest:
    question: str
    channel: str = "web"
    plan: str | None = None
    product_version: str | None = None
    as_of: date | None = None
    tone: str | None = None
    history: list[dict] | None = None


@dataclass
class Citation:
    evidence_id: str
    passage_id: str
    quote: str
    source_title: str
    source_date: str | None
    section_path: str
    verified: bool

    def as_dict(self) -> dict:
        return asdict(self)


def normalise_question(question: str) -> str:
    text = question.lower().strip()
    text = re.sub(r"[^a-z0-9\s'?]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:300]


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


async def _historical_accuracy(conn: AsyncConnection, answer_id: UUID | None) -> float:
    """Smoothed thumbs history. One rating must not produce a perfect score."""
    if answer_id is None:
        return 0.65
    row = await (
        await conn.execute(
            """select count(*) filter (where feedback_thumbs = 1) as positive,
                      count(*) filter (where feedback_thumbs = -1) as negative
               from qa_logs where answer_id = %s and feedback_thumbs is not null""",
            (answer_id,),
        )
    ).fetchone()
    positive = (row or {}).get("positive", 0) or 0
    negative = (row or {}).get("negative", 0) or 0
    prior_weight = 2.0
    prior = 0.7 if answer_id else 0.65
    return (positive + prior * prior_weight) / (positive + negative + prior_weight)


async def _pending_conflicts(
    conn: AsyncConnection,
    passage_ids: list[UUID],
    channel: str,
) -> list[dict]:
    if not passage_ids:
        return []
    rows = await (
        await conn.execute(
            """select c.id, c.kind, c.score,
                      pa.id as passage_a, pb.id as passage_b,
                      da.title as doc_a, db.title as doc_b,
                      sa.visibility as vis_a, sb.visibility as vis_b
               from conflicts c
               left join passages pa on pa.id = c.passage_id_a
               left join passages pb on pb.id = c.passage_id_b
               left join documents da on da.id = pa.document_id
               left join documents db on db.id = pb.document_id
               left join sources sa on sa.id = da.source_id
               left join sources sb on sb.id = db.source_id
               where c.status = 'pending'
                 and (c.passage_id_a = any(%s::uuid[]) or c.passage_id_b = any(%s::uuid[]))
               limit 3""",
            (passage_ids, passage_ids),
        )
    ).fetchall()
    results = []
    for row in rows:
        both_public = row["vis_a"] == "public" and row["vis_b"] == "public"
        if channel == "public" and not both_public:
            # Never reveal or act on an internal-side conflict on a public surface.
            continue
        results.append({
            "id": str(row["id"]),
            "notice": f"“{row['doc_a']}” and “{row['doc_b']}” disagree on a related point.",
            "public": both_public,
            "passage_a": str(row["passage_a"]),
            "passage_b": str(row["passage_b"]),
            "document_a": row["doc_a"],
            "document_b": row["doc_b"],
        })
    return results


def _conflict_notice(conflicts: list[dict]) -> str | None:
    if not conflicts:
        return None
    return (
        conflicts[0]["notice"]
        + " The answer uses the current approved source; the disagreement remains on the review queue."
    )


def _conflict_sources_ignored(conflicts: list[dict], own_passage_ids: set[str]) -> list[dict]:
    ignored: list[dict] = []
    seen: set[str] = set()
    for conflict in conflicts:
        if not conflict.get("public"):
            continue
        if conflict["passage_a"] not in own_passage_ids:
            candidate = conflict["document_a"]
        elif conflict["passage_b"] not in own_passage_ids:
            candidate = conflict["document_b"]
        else:
            continue
        if candidate and candidate not in seen:
            seen.add(candidate)
            ignored.append({
                "title": candidate,
                "reason": "Conflicting source held back pending a human decision.",
            })
    return ignored


async def _canonical_match(
    conn: AsyncConnection,
    standalone: str,
    retrieved: list[RetrievedPassage],
    session: Session,
    settings: Settings,
    plan: str | None,
    product_version: str | None,
    channel: str,
) -> tuple[dict | None, float]:
    visibilities = ["public"] if channel == "public" else ["internal", "public"]
    score_by_passage = {p.passage_id: p.rerank_score for p in retrieved}
    ids = list(score_by_passage.keys())
    rows: list[dict] = []
    if ids:
        rows = list(
            await (
                await conn.execute(
                    """select ca.*, array_agg(ae.passage_id) as evidence_passage_ids
                       from canonical_answers ca
                       join answer_evidence ae
                         on ae.answer_id = ca.id and ae.answer_version = ca.current_version
                       where ca.status = 'approved'
                         and ca.visibility = any(%s::text[])
                         and ae.passage_id = any(%s::uuid[])
                         and (cardinality(ca.plan_scope) = 0 or %s::text is null
                              or %s::text = any(ca.plan_scope))
                         and (cardinality(ca.version_scope) = 0 or %s::text is null
                              or %s::text = any(ca.version_scope))
                       group by ca.id""",
                    (visibilities, ids, plan, plan, product_version, product_version),
                )
            ).fetchall()
        )
    exact = await (
        await conn.execute(
            """select ca.*, array_agg(ae.passage_id) as evidence_passage_ids
               from canonical_answers ca
               join answer_evidence ae
                 on ae.answer_id = ca.id and ae.answer_version = ca.current_version
               where ca.status = 'approved'
                 and ca.visibility = any(%s::text[])
                 and lower(trim(ca.question)) = lower(trim(%s))
                 and (cardinality(ca.plan_scope) = 0 or %s::text is null or %s::text = any(ca.plan_scope))
                 and (cardinality(ca.version_scope) = 0 or %s::text is null
                      or %s::text = any(ca.version_scope))
               group by ca.id""",
            (visibilities, standalone, plan, plan, product_version, product_version),
        )
    ).fetchone()

    best: dict | None = None
    best_score = 0.0
    for row in rows:
        score = max((score_by_passage.get(pid, 0.0) for pid in row["evidence_passage_ids"]), default=0.0)
        if score > best_score:
            best, best_score = row, score
    if exact is not None:
        best, best_score = exact, max(best_score, 1.0)
    if best is None or best_score < CANONICAL_MATCH_THRESHOLD:
        return None, best_score
    return best, best_score


async def _canonical_payload(
    conn: AsyncConnection,
    answer: dict,
    channel: str,
) -> tuple[list[Citation], list[RetrievedPassage], bool]:
    rows = await (
        await conn.execute(
            """select ae.id as evidence_id, ae.passage_id, ae.quote, ae.claim_ref,
                      p.text, p.status as passage_status, p.section_path,
                      d.title as document_title, d.effective_from,
                      s.title as source_title, s.visibility
               from answer_evidence ae
               join passages p on p.id = ae.passage_id
               join documents d on d.id = p.document_id
               join sources s on s.id = d.source_id
               where ae.answer_id = %s and ae.answer_version = %s
               order by ae.created_at""",
            (answer["id"], answer["current_version"]),
        )
    ).fetchall()

    citations: list[Citation] = []
    evidence: list[RetrievedPassage] = []
    stale = False
    for row in rows:
        if channel == "public" and row["visibility"] != "public":
            stale = True
            continue
        verified = row["passage_status"] == "current" and quote_in_text(row["quote"], row["text"])
        if not verified:
            stale = True
        citations.append(
            Citation(
                evidence_id=str(row["evidence_id"]),
                passage_id=str(row["passage_id"]),
                quote=row["quote"],
                source_title=row["source_title"],
                source_date=_iso(row["effective_from"]),
                section_path=row["section_path"] or "Overview",
                verified=verified,
            )
        )
        evidence.append(
            RetrievedPassage(
                passage_id=row["passage_id"],
                parent_id=None,
                child_text=row["text"],
                parent_text=row["text"],
                section_path=row["section_path"] or "Overview",
                page_ref=None,
                document_id=UUID(int=0),
                document_title=row["document_title"],
                document_version=answer["current_version"],
                doc_type="policy",
                effective_from=row["effective_from"],
                plan_scope=answer["plan_scope"],
                version_scope=answer["version_scope"],
                source_id=UUID(int=0),
                source_title=row["source_title"],
                source_tier="official_doc",
                visibility=row["visibility"],
                rerank_score=1.0,
                coverage=1.0,
            )
        )
    return citations, evidence, stale


async def _upsert_gap(
    conn: AsyncConnection,
    session: Session,
    question: str,
    channel: str,
) -> UUID:
    row = await (
        await conn.execute(
            """insert into gaps (tenant_id, normalised_question, representative_question, channels)
               values (%s, %s, %s, %s)
               on conflict (tenant_id, normalised_question) do update
                 set count = gaps.count + 1,
                     last_seen_at = now(),
                     representative_question = excluded.representative_question,
                     channels = array(select distinct unnest(gaps.channels || excluded.channels)),
                     updated_at = now()
               returning id""",
            (session.tenant_id, normalise_question(question), question.strip(), [channel]),
        )
    ).fetchone()
    assert row is not None
    return row["id"]


async def _searched_scope(
    conn: AsyncConnection,
    session: Session,
    channel: str,
    plan: str | None,
    product_version: str | None,
) -> str:
    visibilities = ["public"] if channel == "public" else ["internal", "public"]
    row = await (
        await conn.execute(
            """select
                 (select count(*) from sources where status = 'active' and visibility = any(%s::text[])) as sources,
                 (select count(*) from canonical_answers where status = 'approved'
                    and visibility = any(%s::text[])) as answers,
                 (select count(*) from passages p
                    join documents d on d.id = p.document_id
                    join sources s on s.id = d.source_id
                    where p.status = 'current' and d.status = 'current' and s.status = 'active'
                      and s.visibility = any(%s::text[])) as passages""",
            (visibilities, visibilities, visibilities),
        )
    ).fetchone()
    scope = [f"{row['sources']} sources", f"{row['passages']} current passages", f"{row['answers']} approved answers"]
    if plan:
        scope.append(f"plan: {plan}")
    if product_version:
        scope.append(f"version: {product_version}")
    if channel == "public":
        scope.insert(0, "public help index")
    return " · ".join(scope)


async def _log_qa(
    conn: AsyncConnection,
    session: Session,
    request: AskRequest,
    standalone: str,
    mode: str,
    answer_md: str | None,
    citations: list[dict],
    sources_ignored: list[dict],
    retrieved_ids: list[UUID],
    scores: list[float],
    composite: float | None,
    band: str | None,
    abstained: bool,
    gap_id: UUID | None,
    answer_id: UUID | None,
    answer_version: int | None,
    latency_ms: int,
    provider: str,
) -> UUID:
    row = await (
        await conn.execute(
            """insert into qa_logs
                 (tenant_id, user_id, question, standalone_query, channel, plan, product_version, as_of,
                  mode, answer_id, answer_version, answer_md, citations, sources_ignored,
                  retrieved_passage_ids, rerank_scores, confidence_composite, confidence_band,
                  abstained, gap_id, latency_ms, provider, model, correlation_id)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s, %s, %s, %s, gen_random_uuid())
               returning id""",
            (
                session.tenant_id, session.user_id, request.question, standalone, request.channel,
                request.plan, request.product_version, request.as_of, mode, answer_id, answer_version,
                answer_md, Jsonb(citations), Jsonb(sources_ignored), retrieved_ids, scores,
                composite, band, abstained, gap_id, latency_ms, provider,
                get_intelligence().name,
            ),
        )
    ).fetchone()
    assert row is not None
    return row["id"]


async def _maybe_propose_fallback(
    conn: AsyncConnection,
    session: Session,
    standalone: str,
    answer_md: str,
    evidence: list[RetrievedPassage],
    citations: list[dict],
    plan: str | None,
    product_version: str | None,
) -> None:
    """Turn a good fallback into a ready-to-publish draft.

    A person still decides whether it becomes canonical, but they should not
    have to re-draft an answer Harbour already grounded in cited sources.
    """
    if not evidence or not citations:
        return

    existing = (
        await (
            await conn.execute(
                """select id from canonical_answers
                   where lower(question) = lower(%s) and status in ('draft','in_review')
                   order by created_at desc limit 1""",
                (standalone,),
            )
        ).fetchone()
    )
    answer_id = existing["id"] if existing else None

    if answer_id is None:
        row = (
            await (
                await conn.execute(
                    """insert into canonical_answers
                         (tenant_id, topic_id, question, answer_md, status, owner_id,
                          review_interval_days, next_review_at, plan_scope, version_scope,
                          visibility, current_version, created_by)
                       values (%s, %s, %s, %s, 'in_review', %s, 90, now() + interval '90 days',
                               %s, %s, 'internal', 1, %s)
                       returning id""",
                    (
                        session.tenant_id,
                        evidence[0].topic_id,
                        standalone,
                        answer_md,
                        session.user_id,
                        [plan] if plan else [],
                        [product_version] if product_version else [],
                        session.user_id,
                    ),
                )
            ).fetchone()
        )
        assert row is not None
        answer_id = row["id"]
        await conn.execute(
            """insert into answer_versions
                 (tenant_id, answer_id, version, answer_md, changed_by, change_reason)
               values (%s, %s, 1, %s, %s, 'Drafted automatically from a question Harbour could answer')""",
            (session.tenant_id, answer_id, answer_md, session.user_id),
        )
        for index, citation in enumerate(citations):
            quote = (citation.get("quote") or "").strip()
            if not quote:
                continue
            await conn.execute(
                """insert into answer_evidence
                     (tenant_id, answer_id, answer_version, passage_id, quote, claim_ref)
                   values (%s, %s, 1, %s, %s, %s)""",
                (session.tenant_id, answer_id, citation["passage_id"], quote, f"claim-{index + 1}"),
            )

    await conn.execute(
        """insert into review_items (tenant_id, kind, payload, priority, dedupe_key)
           values (%s, 'new_answer', %s, 60, %s)
           on conflict (tenant_id, dedupe_key)
             where dedupe_key is not null and status = 'pending'
             do update set payload = excluded.payload, priority = excluded.priority""",
        (
            session.tenant_id,
            Jsonb({
                "answer_id": str(answer_id),
                "question": standalone,
                "origin": "fallback_answer",
                "evidence_count": len(citations),
                "summary": "Harbour drafted this from your sources. Publish it if it reads right.",
            }),
            f"fallback:{normalise_question(standalone)}",
        ),
    )


async def ask(conn: AsyncConnection, session: Session, settings: Settings, request: AskRequest) -> dict:
    if not request.question.strip():
        raise validation("A question is required.")
    if len(request.question) > 2000:
        raise validation("Questions are limited to 2,000 characters.")

    started = time.perf_counter()
    # Agent-assist is the only internal surface. Help centre, widget and bot are
    # customer-facing and get the public visibility filter.
    audience = "internal" if request.channel == "web" else "public"
    intelligence = get_intelligence()
    rewrite = await intelligence.rewrite(request.question, request.history)
    standalone = rewrite.standalone_question.strip() or request.question.strip()
    plan = request.plan or rewrite.plan
    product_version = request.product_version or rewrite.product_version

    embedding = (await intelligence.embed([standalone], task="query"))[0]
    outcome = await hybrid_search(
        conn,
        standalone,
        embedding,
        channel=audience,
        plan=plan,
        product_version=product_version,
        as_of=request.as_of,
    )
    retrieval = retrieval_strength(outcome)
    evidence_ids = [p.passage_id for p in outcome.passages]

    canonical, canonical_score = await _canonical_match(
        conn, standalone, outcome.passages, session, settings, plan, product_version, audience
    )

    conflicts: list[dict] = []
    conflict_notice = None
    sources_ignored: list[dict] = []
    if canonical:
        citations, canonical_evidence, stale = await _canonical_payload(conn, canonical, audience)
        citation_dicts = [c.as_dict() for c in citations]
        verified = sum(1 for c in citations if c.verified)
        faithfulness = verified / max(1, len(citations))
        historical = await _historical_accuracy(conn, canonical["id"])
        own_ids = {c.passage_id for c in citations}
        conflicts = await _pending_conflicts(conn, [UUID(pid) for pid in own_ids], audience)
        conflict_notice = _conflict_notice(conflicts)
        sources_ignored.extend(_conflict_sources_ignored(conflicts, own_ids))
        canonical_retrieval = min(1.0, 0.55 + 0.45 * min(1.0, canonical_score))
        composite = confidence_composite(canonical_retrieval, faithfulness, historical)
        band = band_for(composite, audience, settings)

        if audience == "public" and (stale or not citations or composite < settings.public_low_threshold):
            reason = "The approved answer's evidence changed since approval and is no longer safe to serve publicly."
            sources_ignored.append({"title": canonical["question"], "reason": reason})
            canonical = None
        elif stale:
            sources_ignored.append({
                "title": canonical["question"],
                "reason": "Evidence changed since approval. This answer is due for recheck.",
            })
            band = "medium"

        if canonical:
            answer_md = canonical["answer_md"]
            if conflict_notice:
                answer_md = f"> **Sources disagree.** {conflict_notice}\n\n{answer_md}"
            latency_ms = int((time.perf_counter() - started) * 1000)
            qa_log_id = await _log_qa(
                conn, session, request, standalone, "canonical", answer_md, citation_dicts,
                sources_ignored, evidence_ids, [p.rerank_score for p in outcome.passages],
                composite, band, False, None, canonical["id"], canonical["current_version"],
                latency_ms, intelligence.name,
            )
            return {
                "question": request.question,
                "standalone_query": standalone,
                "channel": request.channel,
                "mode": "canonical",
                "answer_md": answer_md,
                "citations": citation_dicts,
                "confidence_band": band,
                "confidence": {
                    "composite": round(composite, 3),
                    "retrieval": round(canonical_retrieval, 3),
                    "faithfulness": round(faithfulness, 3),
                    "historical": round(historical, 3),
                },
                "sources_ignored": sources_ignored,
                "conflict_notice": conflict_notice,
                "abstained": False,
                "searched_scope": await _searched_scope(conn, session, audience, plan, product_version),
                "next_steps": [],
                "gap_id": None,
                "answer_id": str(canonical["id"]),
                "answer_version": canonical["current_version"],
                "qa_log_id": str(qa_log_id),
                "latency_ms": latency_ms,
                "provider": intelligence.name,
            }

    low, high = (
        (settings.public_low_threshold, settings.public_high_threshold)
        if audience == "public"
        else (settings.internal_low_threshold, settings.internal_high_threshold)
    )

    abstain_reason = None
    if audience == "public":
        abstain_reason = "No approved public answer covers this question for the selected scope."
    elif not outcome.passages:
        abstain_reason = "No current passage in the knowledge base matched this question."
    elif retrieval < low:
        abstain_reason = "The retrieved evidence was too weak to answer safely."
    elif canonical is None and conflicts and retrieval < high:
        abstain_reason = "Sources disagree and the disagreement is not yet resolved."

    if abstain_reason is None and outcome.passages:
        evidence = [
            EvidencePassage(
                evidence_id=str(p.passage_id),
                text=p.parent_text,
                title=p.document_title,
                section_path=p.section_path,
                source_date=_iso(p.effective_from),
                source_tier=p.source_tier,
                visibility=p.visibility,
                plan_scope=p.plan_scope,
                version_scope=p.version_scope,
                score=p.rerank_score,
            )
            for p in outcome.passages[:6]
        ]
        top_conflicts = await _pending_conflicts(
            conn, [p.passage_id for p in outcome.passages[:3]], audience
        )
        generation = await intelligence.generate_answer(
            standalone, evidence, _conflict_notice(top_conflicts)
        )
        valid_ids = {e.evidence_id for e in evidence}
        cited = [cid for cid in generation.cited_ids if cid in valid_ids]
        cited_passages = [p for p in outcome.passages if str(p.passage_id) in cited]
        if generation.abstain or not generation.answer_md or not cited_passages:
            abstain_reason = "The answer could not be grounded in the retrieved evidence."
        else:
            faithfulness = lexical_faithfulness(generation.answer_md, cited_passages)
            if faithfulness < 0.35:
                retry = await intelligence.generate_answer(standalone, evidence, conflict_notice)
                retry_cited = [cid for cid in retry.cited_ids if cid in valid_ids]
                retry_passages = [p for p in outcome.passages if str(p.passage_id) in retry_cited]
                retry_faithfulness = lexical_faithfulness(retry.answer_md, retry_passages)
                if retry_faithfulness > faithfulness and retry_passages:
                    generation, cited_passages, faithfulness = retry, retry_passages, retry_faithfulness
                else:
                    abstain_reason = "The generated answer did not verify against its evidence."

            if abstain_reason is None and audience == "public":
                abstain_reason = "Unapproved evidence cannot answer customers directly."

            # Only conflicts actually cited by the generated answer may affect it.
            cited_ids = {str(p.passage_id) for p in cited_passages}
            conflicts = await _pending_conflicts(
                conn, [UUID(pid) for pid in cited_ids], audience
            )
            conflict_notice = _conflict_notice(conflicts)
            sources_ignored.extend(_conflict_sources_ignored(conflicts, cited_ids))

            if abstain_reason is None:
                historical = await _historical_accuracy(conn, None)
                composite = confidence_composite(retrieval, faithfulness, historical)
                band = band_for(composite, audience, settings)
                if composite < low:
                    abstain_reason = "Confidence stayed below the abstention threshold."
                elif band == "medium" and audience == "public":
                    abstain_reason = "Confidence was not high enough for a customer-facing answer."
                else:
                    answer_md = generation.answer_md
                    label = (
                        "> **Draft from unapproved sources.** This is not yet a canonical answer. "
                        "Check the citations before sending it to a customer."
                    )
                    if band == "medium":
                        label += "\n>\n> **Review recommended.** The retrieved evidence only partly covers this question."
                    answer_md = f"{label}\n\n{answer_md}"
                    if conflict_notice:
                        answer_md = f"> **Sources disagree.** {conflict_notice}\n\n{answer_md}"
                    citations = [
                        Citation(
                            evidence_id=str(p.passage_id),
                            passage_id=str(p.passage_id),
                            quote=p.child_text[:600],
                            source_title=p.source_title,
                            source_date=_iso(p.effective_from),
                            section_path=p.section_path,
                            verified=True,
                        ).as_dict()
                        for p in cited_passages
                    ]
                    await _maybe_propose_fallback(
                        conn, session, standalone, generation.answer_md, cited_passages, citations,
                        plan, product_version,
                    )
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    qa_log_id = await _log_qa(
                        conn, session, request, standalone, "fallback", answer_md, citations,
                        sources_ignored, evidence_ids, [p.rerank_score for p in outcome.passages],
                        composite, band, False, None, None, None, latency_ms, intelligence.name,
                    )
                    return {
                        "question": request.question,
                        "standalone_query": standalone,
                        "channel": request.channel,
                        "mode": "fallback",
                        "answer_md": answer_md,
                        "citations": citations,
                        "confidence_band": band,
                        "confidence": {
                            "composite": round(composite, 3),
                            "retrieval": round(retrieval, 3),
                            "faithfulness": round(faithfulness, 3),
                            "historical": round(historical, 3),
                        },
                        "sources_ignored": sources_ignored,
                        "conflict_notice": conflict_notice,
                        "abstained": False,
                        "searched_scope": await _searched_scope(conn, session, audience, plan, product_version),
                        "next_steps": [],
                        "gap_id": None,
                        "answer_id": None,
                        "answer_version": None,
                        "qa_log_id": str(qa_log_id),
                        "latency_ms": latency_ms,
                        "provider": intelligence.name,
                    }

    # Abstention is a result, not an error. Log it and grow the gap record.
    if not conflicts and outcome.passages:
        conflicts = await _pending_conflicts(conn, [p.passage_id for p in outcome.passages[:3]], audience)
        conflict_notice = _conflict_notice(conflicts)
    gap_id = await _upsert_gap(conn, session, standalone, request.channel)
    scope = await _searched_scope(conn, session, audience, plan, product_version)
    next_steps = (
        ["Ask a teammate in your support channel.", "Draft a canonical answer from the sources below."]
        if audience == "internal"
        else ["Contact support for a human answer."]
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    qa_log_id = await _log_qa(
        conn, session, request, standalone, "abstained", None, [], sources_ignored,
        evidence_ids, [p.rerank_score for p in outcome.passages], None, None, True, gap_id,
        None, None, latency_ms, intelligence.name,
    )
    return {
        "question": request.question,
        "standalone_query": standalone,
        "channel": request.channel,
        "mode": "abstained",
        "answer_md": None,
        "citations": [],
        "confidence_band": "low",
        "confidence": None,
        "sources_ignored": sources_ignored,
        "conflict_notice": conflict_notice,
        "abstained": True,
        "abstention_reason": abstain_reason,
        "searched_scope": scope,
        "next_steps": next_steps,
        "gap_id": str(gap_id),
        "answer_id": None,
        "answer_version": None,
        "qa_log_id": str(qa_log_id),
        "latency_ms": latency_ms,
        "provider": intelligence.name,
    }


async def record_feedback(
    conn: AsyncConnection,
    session: Session,
    qa_log_id: UUID,
    thumbs: int | None,
    edited_answer_md: str | None,
    note: str | None,
) -> dict:
    log_row = await (
        await conn.execute("select * from qa_logs where id = %s", (qa_log_id,))
    ).fetchone()
    if log_row is None:
        raise AppError("not_found", "That answer log does not exist.", 404)

    distance = None
    if edited_answer_md and log_row["answer_md"]:
        distance = 1 - difflib.SequenceMatcher(
            None, log_row["answer_md"].split(), edited_answer_md.split()
        ).ratio()
    await conn.execute(
        """update qa_logs
           set feedback_thumbs = coalesce(%s, feedback_thumbs),
               edited_answer_md = coalesce(%s, edited_answer_md),
               edit_distance = coalesce(%s, edit_distance)
           where id = %s""",
        (thumbs, edited_answer_md, distance, qa_log_id),
    )

    review_item = None
    if edited_answer_md and distance is not None and distance > 0.12:
        row = await (
            await conn.execute(
                """insert into review_items (tenant_id, kind, payload, priority, dedupe_key)
                   values (%s, 'edit_proposal', %s, 65, %s)
                   on conflict (tenant_id, dedupe_key)
                     where dedupe_key is not null and status = 'pending' do nothing
                   returning id""",
                (
                    session.tenant_id,
                    json.dumps({
                        "qa_log_id": str(qa_log_id),
                        "question": log_row["question"],
                        "answer_id": str(log_row["answer_id"]) if log_row["answer_id"] else None,
                        "answer_version": log_row["answer_version"],
                        "original_md": log_row["answer_md"],
                        "suggested_md": edited_answer_md,
                        "note": note,
                        "edit_distance": round(distance, 3),
                        "summary": "An agent improved this answer. Publish the new version if it reads right.",
                    }),
                    f"edit:{qa_log_id}",
                ),
            )
        ).fetchone()
        review_item = str(row["id"]) if row else None
    elif thumbs == -1 and log_row["answer_id"]:
        row = await (
            await conn.execute(
                """insert into review_items (tenant_id, kind, payload, priority, dedupe_key)
                   values (%s, 'stale_answer', %s, 45, %s)
                   on conflict (tenant_id, dedupe_key)
                     where dedupe_key is not null and status = 'pending' do nothing
                   returning id""",
                (
                    session.tenant_id,
                    json.dumps({
                        "answer_id": str(log_row["answer_id"]),
                        "question": log_row["question"],
                        "note": note,
                        "reason": "An agent marked this answer unhelpful.",
                        "summary": "An agent marked this answer unhelpful. Check whether it still holds.",
                    }),
                    f"unhelpful:{log_row['answer_id']}",
                ),
            )
        ).fetchone()
        review_item = str(row["id"]) if row else None
    return {"ok": True, "review_item_id": review_item}
