from __future__ import annotations

import difflib
import json
import logging
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
from ..intelligence import EvidencePassage, GenerationResult, get_intelligence
from ..intelligence.dev import match_tokens, tokenize
from ..ingest.pipeline import quote_in_text
from .search import (
    RetrievedPassage,
    band_for,
    confidence_composite,
    has_detectable_contradiction,
    hybrid_search,
    lexical_faithfulness,
    retrieval_strength,
    vector_literal,
)

log = logging.getLogger(__name__)
QUESTION_CANDIDATE_LIMIT = 30

# Function words do not distinguish two questions. Negations, numbers,
# qualifiers and named entities are still compared separately, so removing
# these cannot make "before 30 days" match "after 30 days".
QUESTION_STOPWORDS = frozenset("""
    a an and are as at be been being but by can could did do does doing for from
    had has have having how i if in into is it its me my of on or our ours should
    so than that the their them then there these they this those to us was we were
    what when where which while who whom why will with would you your
""".split())

# Hardware and unit acronyms usually describe the same subject rather than a
# different one: "i5 9400" and "i5 9400 CPU" ask the same question. Named
# products, plans, features and identifiers are not included here.
GENERIC_HARDWARE_TERMS = frozenset(
    "cpu gpu ram rom ssd hdd usb pc gb mb tb kb ghz mhz".split()
)

QUESTION_SYNONYMS = {
    "processor": "cpu",
    "processors": "cpu",
    "chip": "cpu",
    "chips": "cpu",
    "core": "core",
    "cores": "core",
    "thread": "thread",
    "threads": "thread",
}


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


def _supporting_quote(answer_md: str, passage_text: str, max_chars: int = 600) -> str | None:
    """Pick an exact passage excerpt with the strongest overlap with the answer.

    Fallback answers cite passage ids, not quote spans. Taking the first 600
    characters makes a citation look verified even when the excerpt has nothing
    to do with the claim. Instead, choose a sentence-like span that shares the
    answer's terms, then verify the returned text is literally in the passage.
    """
    answer_text = re.sub(r"\[[0-9]+\]|\[P:[^\]]+\]", "", answer_md or "")
    answer_terms = set(match_tokens(answer_text))
    if not answer_terms or not passage_text.strip():
        return None

    answer_sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", answer_text) if s.strip()]
    candidates: list[tuple[float, int, int]] = []
    for match in re.finditer(r"[^.!?\n]+[.!?]?", passage_text):
        candidate = match.group().strip()
        if not candidate:
            continue
        terms = set(match_tokens(candidate))
        if not terms:
            continue
        overlap = len(answer_terms & terms) / max(1, len(answer_terms))
        candidates.append((overlap, match.start(), match.end()))
    if not candidates:
        return None

    # If several answer claims are verbatim in this passage, keep their span
    # together so the citation does not show only one of the supported claims.
    exact = [
        (start, end) for _, start, end in candidates
        if any(quote_in_text(passage_text[start:end].strip(), sentence) for sentence in answer_sentences)
    ]
    if exact and max(end for _, end in exact) - min(start for start, _ in exact) <= max_chars:
        start, end = min(start for start, _ in exact), max(end for _, end in exact)
    else:
        score, start, end = max(candidates, key=lambda item: (item[0], item[2] - item[1]))
        if score <= 0:
            return None
    quote = passage_text[start : min(end, start + max_chars)].strip()
    if not quote or not quote_in_text(quote, passage_text):
        return None
    return quote


def _table_fact_fallback(question: str, passages: list[RetrievedPassage]) -> GenerationResult | None:
    """Return an exact, unambiguous attribute-table row for an identified model.

    Never infer a value or a citation from the model's answer. The question must
    name an explicit model, the table must identify that same model, and every
    matching attribute row across retrieved tables must agree.
    """
    def model_ids(text: str) -> set[str]:
        return {re.sub(r"[^a-z0-9]", "", match.group().lower()) for match in
                re.finditer(r"\b[a-z]+\d+[\s-]*\d+[a-z]?\b", text, re.I)}

    models = model_ids(question)
    if len(models) != 1 or not passages:
        return None
    model = next(iter(models))
    # Refuse to use a lower-ranked table when a similarly relevant top result
    # belongs to another document; table rows often omit their subject.
    top_document = passages[0].document_id
    remaining = re.sub(r"\b[a-z]+\d+[\s-]*\d+[a-z]?\b", " ", question, flags=re.I)
    terms = set(re.findall(r"[a-z0-9]+", remaining.lower())) - {
        "how", "many", "much", "what", "is", "are", "does", "do", "the", "a", "an", "of",
        "for", "have", "has", "its", "cpu", "processor", "product", "model", "number",
    }
    if not terms:
        return None
    matches: list[tuple[RetrievedPassage, str, str]] = []
    for passage in passages:
        lines = [line.strip() for line in passage.child_text.splitlines() if line.strip()]
        if len(lines) < 3:
            continue
        cells = [[cell.strip() for cell in line.strip("|").split("|")] for line in lines if line.startswith("|") and line.endswith("|")]
        if len(cells) != len(lines) or any(len(row) != 2 for row in cells):
            continue
        plain = lambda value: re.sub(r"[*`_]", "", value).strip().lower()
        if plain(cells[0][0]) not in {"attribute", "property", "field"} or plain(cells[0][1]) not in {"specification", "value"}:
            continue
        identity_rows = [row for row in cells[2:] if plain(row[0]) in {"product name", "model", "model name"}]
        if len(identity_rows) != 1 or model_ids(identity_rows[0][1]) != {model}:
            continue
        if any(model_ids(p.document_title) and model_ids(p.document_title) != {model}
               for p in passages if p.document_id == passage.document_id):
            continue
        for line, row in zip(lines[2:], cells[2:]):
            key_terms = set(re.findall(r"[a-z0-9]+", plain(row[0])))
            if terms.issubset(key_terms) and row[1].strip():
                matches.append((passage, line, plain(row[1])))
    if not matches or not any(p.document_id == top_document for p, _, _ in matches):
        return None
    if len({value for _, _, value in matches}) != 1:
        return None
    passage, row, _ = next(match for match in matches if match[0].document_id == top_document)
    return GenerationResult(answer_md=row, cited_ids=[str(passage.passage_id)])


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


def _question_safety_key(question: str) -> tuple:
    """Keep critical qualifiers intact even if a model claims equivalence."""
    text = question.casefold().replace("’", "'")
    text = re.sub(r"\bcan't\b", "can not", text)
    text = re.sub(r"\bwon't\b", "will not", text)
    text = re.sub(r"\b([a-z]+)n't\b", r"\1 not", text)
    text = re.sub(r"\bcannot\b", "can not", text)
    numbers = tuple(re.findall(r"\b\d+(?:[.,]\d+)*\b", text))
    words = set(re.findall(r"[a-z0-9]+", text))
    intent = None
    if "old" in words or "age" in words:
        intent = "age"
    elif "born" in words or "birth" in words:
        intent = "birth"
    elif "refund" in words or "refunded" in words:
        if words & {"request", "apply", "claim", "obtain"}:
            intent = "refund_request"
        elif words & {"window", "long", "deadline", "period"}:
            intent = "refund_window"
    qualifiers = frozenset(re.findall(
        r"\b(?:not|no|never|before|after|within|without|except|unless|only|"
        r"at least|at most|more than|less than|annual|monthly|yearly|"
        r"team|business|enterprise|today|current|previous|historical|was|were|will|would)\b", text,
    ))
    # Capitalized names and identifiers protect entity substitutions; generic
    # initial question words are not entities.
    entities = frozenset(re.sub(r"'s$", "", token.casefold()) for token in re.findall(
        r"\b(?:[A-Z][a-z]+(?:'[sS])?|[A-Z]{2,}[A-Z0-9_-]*|[a-zA-Z]+[-_]\d+)\b", question
    ) if token.casefold() not in {"what", "how", "where", "when", "why", "which", "who", "is", "does", "can", "do"}
    and token.casefold() not in GENERIC_HARDWARE_TERMS)
    return numbers, qualifiers, entities, intent


def _question_content_terms(question: str) -> frozenset[str]:
    """Content words that must agree before a deterministic reuse.

    Numbers, entities, negation and qualifiers are checked by
    `_question_safety_key`; this function only covers ordinary wording.
    """
    text = question.casefold().replace("’", "'")
    text = re.sub(r"\b(can)not\b", r"\1 not", text)
    text = re.sub(r"\b([a-z]+)n't\b", r"\1 not", text)
    terms: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", text):
        token = QUESTION_SYNONYMS.get(token, token)
        if token.isdigit() or token in QUESTION_STOPWORDS or token in GENERIC_HARDWARE_TERMS:
            continue
        terms.add(token)
    return frozenset(terms)


def _question_search_terms(question: str) -> str:
    # OR-query as a bounded candidate generator, never an equivalence decision.
    tokens = list(dict.fromkeys(re.findall(r"[a-z0-9]+", question.casefold())))[:30]
    return " OR ".join(tokens)


async def _equivalent_question_rows(question: str, rows: list[dict]) -> list[dict]:
    """Accept exact questions or model-confirmed candidates with matching safeguards."""
    def exact_key(value: str) -> str:
        return re.sub(r"\s+", " ", value.casefold().replace("’", "'")).strip(" ?.!\t\r\n")

    exact = [row for row in rows if exact_key(row["question"]) == exact_key(question)]
    if exact:
        return exact
    key = _question_safety_key(question)
    content = _question_content_terms(question)
    safe = []
    deterministic = []
    for row in rows:
        candidate_key = _question_safety_key(row["question"])
        if candidate_key[:3] != key[:3]:
            continue
        if key[3] and candidate_key[3] and key[3] != candidate_key[3]:
            continue
        safe.append(row)
        # Identical content words after removing function words and generic
        # hardware acronyms is stronger evidence than a model opinion. This is
        # what lets "i5 9400" and "i5 9400 CPU" share one answer without
        # weakening the entity, number, negation and qualifier checks above.
        if (
            len(content) >= 2
            and candidate_key[3] == key[3]
            and _question_content_terms(row["question"]) == content
        ):
            deterministic.append(row)
    if deterministic:
        return deterministic
    if not safe:
        return []
    try:
        intelligence = get_intelligence()
        indices = await intelligence.equivalent_questions(question, [row["question"] for row in safe])
        forward = [safe[index] for index in set(indices) if type(index) is int and 0 <= index < len(safe)]
        # Require the same judgement in both directions. Too many matches are
        # ambiguous and should not trigger automatic reuse.
        if len(forward) > 3:
            return []
        matches = []
        for row in forward:
            if 0 in await intelligence.equivalent_questions(row["question"], [question]):
                matches.append(row)
        return matches
    except AppError as exc:
        log.warning("Question equivalence unavailable: %s", exc.code)
        return []


async def _canonical_match(
    conn: AsyncConnection,
    standalone: str,
    retrieved: list[RetrievedPassage],
    session: Session,
    settings: Settings,
    plan: str | None,
    product_version: str | None,
    channel: str,
    as_of: date | None = None,
) -> tuple[dict | None, float]:
    # Canonical versions currently lack effective-date semantics.
    if as_of is not None:
        return None, 0.0
    visibilities = ["public"] if channel == "public" else ["internal", "public"]
    ids = [p.passage_id for p in retrieved]
    # Retrieved passages only shortlist candidates; their relevance is not
    # evidence that the canonical *question* is equivalent.
    rows = list(await (await conn.execute(
        """select ca.* from canonical_answers ca
           where ca.status = 'approved' and ca.visibility = any(%s::text[])
             and exists (select 1 from answer_evidence ae
                         where ae.answer_id = ca.id and ae.answer_version = ca.current_version)
             and not exists (
                 select 1 from answer_evidence ae
                 join passages p on p.id = ae.passage_id
                 join documents d on d.id = p.document_id
                 join sources s on s.id = d.source_id
                 where ae.answer_id = ca.id and ae.answer_version = ca.current_version
                   and (p.status <> 'current' or d.status <> 'current' or s.status <> 'active'
                     or s.deleted_at is not null or not (s.visibility = any(%s::text[]))
                     or (cardinality(d.plan_scope) > 0 and (%s::text is null or
                         not (lower(%s::text) = any(select lower(x) from unnest(d.plan_scope) x))))
                     or (cardinality(d.product_version_scope) > 0 and (%s::text is null or
                         not (lower(%s::text) = any(select lower(x) from unnest(d.product_version_scope) x))))
                     or (d.effective_from is not null and d.effective_from > current_date)
                     or (d.effective_to is not null and d.effective_to <= current_date))
             )
             and (cardinality(ca.plan_scope) = 0 or (%s::text is not null and
                  lower(%s::text) = any(select lower(x) from unnest(ca.plan_scope) x)))
             and (cardinality(ca.version_scope) = 0 or (%s::text is not null and
                  lower(%s::text) = any(select lower(x) from unnest(ca.version_scope) x)))
             and (lower(trim(ca.question)) = lower(trim(%s)) or exists (
                 select 1 from answer_evidence ae where ae.answer_id = ca.id
                   and ae.answer_version = ca.current_version and ae.passage_id = any(%s::uuid[])
             ) or to_tsvector('english', ca.question) @@ websearch_to_tsquery('english', %s))
           order by (lower(trim(ca.question)) = lower(trim(%s))) desc,
             exists (select 1 from answer_evidence ae where ae.answer_id = ca.id
                     and ae.answer_version = ca.current_version and ae.passage_id = any(%s::uuid[])) desc,
             ts_rank(to_tsvector('english', ca.question), websearch_to_tsquery('english', %s)) desc,
             ca.created_at, ca.id
           limit %s""",
        (visibilities, visibilities, plan, plan, product_version, product_version,
         plan, plan, product_version, product_version, standalone, ids,
         _question_search_terms(standalone), standalone, ids, _question_search_terms(standalone), QUESTION_CANDIDATE_LIMIT),
    )).fetchall())
    matches = await _equivalent_question_rows(standalone, rows)
    if not matches:
        return None, 0.0
    # Prefer the matching explicit scope over a general answer, but abstain
    # when equally specific answers are ambiguous rather than choosing one.
    def specificity(row: dict) -> int:
        return int(bool(row["plan_scope"])) + int(bool(row["version_scope"]))

    best = max(specificity(row) for row in matches)
    winners = [row for row in matches if specificity(row) == best]
    if len(winners) > 1:
        # Different phrasings can produce duplicate approved answers. Serving
        # one is safe only when they say the same thing; otherwise abstain
        # rather than picking an arbitrary conflicting answer.
        if not all((row.get("answer_md") or "").strip() for row in winners):
            return None, 0.0
        answers = {
            re.sub(r"\s+", " ", (row.get("answer_md") or "").casefold()).strip()
            for row in winners
        }
        if len(answers) != 1:
            return None, 0.0
        winners.sort(key=lambda row: (
            row.get("created_at") or datetime.max.replace(tzinfo=timezone.utc),
            str(row.get("id") or ""),
        ))
        winners = winners[:1]
    if len(winners) != 1:
        return None, 0.0
    return winners[0], 1.0


async def _canonical_payload(
    conn: AsyncConnection,
    answer: dict,
    channel: str,
) -> tuple[list[Citation], list[RetrievedPassage], bool]:
    rows = await (
        await conn.execute(
            """select ae.id as evidence_id, ae.passage_id, ae.quote, ae.claim_ref,
                      p.text, p.status as passage_status, p.section_path, p.parent_id, p.page_ref,
                      p.document_id, d.title as document_title, d.effective_from,
                      d.version as document_version, d.doc_type,
                      d.plan_scope, d.product_version_scope,
                      s.id as source_id, s.title as source_title, s.source_tier, s.visibility
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
                parent_id=row["parent_id"],
                child_text=row["text"],
                parent_text=row["text"],
                section_path=row["section_path"] or "Overview",
                page_ref=row["page_ref"],
                document_id=row["document_id"],
                document_title=row["document_title"],
                document_version=row["document_version"],
                doc_type=row["doc_type"],
                effective_from=row["effective_from"],
                plan_scope=row["plan_scope"] or [],
                version_scope=row["product_version_scope"] or [],
                source_id=row["source_id"],
                source_title=row["source_title"],
                source_tier=row["source_tier"],
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
    citations = [
        citation for citation in citations
        if citation.get("verified") and (citation.get("quote") or "").strip()
    ]
    if not citations:
        return

    # Serialize draft matching and creation per tenant. Different phrasings
    # otherwise race through the equivalence check and create two drafts.
    await conn.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                       (f"fallback-question:{session.tenant_id}",))
    citation_ids = [UUID(citation["passage_id"]) for citation in citations]
    rows = list(await (await conn.execute(
        """select ca.id, ca.question, ca.status from canonical_answers ca
           where ca.status in ('draft','in_review','approved')
             and ca.visibility = 'internal'
             and (select array_agg(lower(x) order by lower(x)) from unnest(ca.plan_scope) x)
                 is not distinct from (select array_agg(lower(x) order by lower(x)) from unnest(%s::text[]) x)
             and (select array_agg(lower(x) order by lower(x)) from unnest(ca.version_scope) x)
                 is not distinct from (select array_agg(lower(x) order by lower(x)) from unnest(%s::text[]) x)
             and (lower(trim(ca.question)) = lower(trim(%s)) or exists (
                 select 1 from answer_evidence ae where ae.answer_id = ca.id
                   and ae.answer_version = ca.current_version and ae.passage_id = any(%s::uuid[])
             ) or to_tsvector('english', ca.question) @@ websearch_to_tsquery('english', %s))
           order by (lower(trim(ca.question)) = lower(trim(%s))) desc,
             exists (select 1 from answer_evidence ae where ae.answer_id = ca.id
                     and ae.answer_version = ca.current_version and ae.passage_id = any(%s::uuid[])) desc,
             ts_rank(to_tsvector('english', ca.question), websearch_to_tsquery('english', %s)) desc,
             ca.created_at, ca.id
           limit %s""",
        ([plan] if plan else [], [product_version] if product_version else [], standalone,
         citation_ids, _question_search_terms(standalone), standalone, citation_ids,
         _question_search_terms(standalone), QUESTION_CANDIDATE_LIMIT),
    )).fetchall())
    existing = await _equivalent_question_rows(standalone, rows)
    if existing:
        # Never mutate an existing review item, answer, or its evidence merely
        # because a paraphrase was asked; one review remains authoritative.
        return
    answer_id = None

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
            f"new_answer:{answer_id}",
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
        conn, standalone, outcome.passages, session, settings, plan, product_version, audience, request.as_of
    )

    conflicts: list[dict] = []
    conflict_notice = None
    sources_ignored: list[dict] = []
    unsafe_canonical = False
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

        if conflicts or has_detectable_contradiction(canonical["answer_md"], canonical_evidence + outcome.passages[:6]):
            sources_ignored.append({
                "title": canonical["question"],
                "reason": "Evidence contradicts this answer or has an unresolved conflict; review is required.",
            })
            unsafe_canonical = True
            canonical = None
        elif audience == "public" and (stale or not citations or composite < settings.public_low_threshold):
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
                "retrieval_mode": outcome.dense_index,
                "index_warning": outcome.index_warning,
            }

    low, high = (
        (settings.public_low_threshold, settings.public_high_threshold)
        if audience == "public"
        else (settings.internal_low_threshold, settings.internal_high_threshold)
    )

    abstain_reason = None
    if unsafe_canonical:
        abstain_reason = "The approved answer has conflicting evidence and needs review."
    elif audience == "public":
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
                text=p.child_text,
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
        if not generation.abstain and generation.answer_md and not cited_passages:
            # A small model can produce a malformed citation even when the
            # answer is present. Retry once; never infer or guess an ID.
            generation = await intelligence.generate_answer(standalone, evidence, _conflict_notice(top_conflicts))
            cited = [cid for cid in generation.cited_ids if cid in valid_ids]
            cited_passages = [p for p in outcome.passages if str(p.passage_id) in cited]
        if not generation.abstain and generation.answer_md and not cited_passages:
            # A structured table can support a precise extract without trusting
            # the model's uncited number or guessing which passage it meant.
            table_answer = _table_fact_fallback(standalone, outcome.passages[:6])
            if table_answer:
                generation = table_answer
                cited_passages = [p for p in outcome.passages[:6] if str(p.passage_id) in generation.cited_ids]
        if generation.abstain or not generation.answer_md or not cited_passages:
            abstain_reason = "The answer could not be grounded in the retrieved evidence."
        else:
            faithfulness = lexical_faithfulness(generation.answer_md, cited_passages, outcome.passages[:6])
            if faithfulness < 1.0:
                retry = await intelligence.generate_answer(standalone, evidence, _conflict_notice(top_conflicts))
                retry_cited = [cid for cid in retry.cited_ids if cid in valid_ids]
                retry_passages = [p for p in outcome.passages if str(p.passage_id) in retry_cited]
                retry_faithfulness = lexical_faithfulness(retry.answer_md, retry_passages, outcome.passages[:6])
                if not retry.abstain and retry.answer_md.strip() and retry_faithfulness == 1.0 and retry_passages:
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
            if conflicts and abstain_reason is None:
                abstain_reason = "Cited sources have an unresolved conflict and need review."

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
                        "Check the citations before sending it to a customer. "
                        "Time-sensitive facts may be out of date."
                    )
                    if band == "medium":
                        label += "\n>\n> **Review recommended.** The retrieved evidence only partly covers this question."
                    answer_md = f"{label}\n\n{answer_md}"
                    if conflict_notice:
                        answer_md = f"> **Sources disagree.** {conflict_notice}\n\n{answer_md}"
                    citations = []
                    for p in cited_passages:
                        quote = _supporting_quote(generation.answer_md, p.child_text)
                        excerpt = quote or p.child_text[:300].strip()
                        if not excerpt:
                            continue
                        citations.append(
                            Citation(
                                evidence_id=str(p.passage_id),
                                passage_id=str(p.passage_id),
                                quote=excerpt,
                                source_title=p.source_title,
                                source_date=_iso(p.effective_from),
                                section_path=p.section_path,
                                verified=bool(quote and quote_in_text(quote, p.child_text)),
                            ).as_dict()
                        )
                    citation_numbers = {
                        citation["passage_id"]: index for index, citation in enumerate(citations, start=1)
                    }
                    # Only expose inline references that map to an actual returned citation.
                    answer_md = re.sub(
                        r"\[P:([^\]]+)\]",
                        lambda match: f"[{citation_numbers[match.group(1)]}]"
                        if match.group(1) in citation_numbers else "",
                        answer_md,
                    )
                    if request.as_of is None:
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
                        "retrieval_mode": outcome.dense_index,
                        "index_warning": outcome.index_warning,
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
        "retrieval_mode": outcome.dense_index,
        "index_warning": outcome.index_warning,
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
