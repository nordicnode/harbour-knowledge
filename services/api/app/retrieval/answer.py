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
from ..intelligence.dev import match_tokens, term_matches, tokenize
from ..intelligence.questions import (
    normalise_subquestions,
    question_part_count,
    split_question_parts,
)
from ..ingest.pipeline import quote_in_text
from .search import (
    RetrievedPassage,
    SearchOutcome,
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


def _quote_spans(text: str) -> list[tuple[int, int]]:
    """Sentence-like spans that never lose or reorder a character.

    A period inside a URL or an abbreviation ends a span early, which left
    citations starting mid-URL ("html)). Launched on January 7, 2019"). Ending a
    span after a closing parenthesis that is followed by a new capitalised
    sentence keeps the excerpt starting at a real word without altering it.
    """
    boundaries = [0]
    for match in re.finditer(r"(?<=[.!?])\s+|\n+|(?<=\))\s+(?=[A-Z(\[])", text):
        boundaries.append(match.end())
    boundaries.append(len(text))
    spans = [(start, end) for start, end in zip(boundaries, boundaries[1:])]
    return [(start, end) for start, end in spans if text[start:end].strip()]


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
    for start, end in _quote_spans(passage_text):
        candidate = passage_text[start:end].strip()
        if not candidate:
            continue
        terms = set(match_tokens(candidate))
        if not terms:
            continue
        overlap = len(answer_terms & terms) / max(1, len(answer_terms))
        candidates.append((overlap, start, end))
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


ROW_STOPWORDS = frozenset("""a an the is are was were be been what which who how does do did
    many much of for in on at to from with its it this that these those have has had
    i we you they he she product model number specification specs value please tell
    give show find need want get""".split())

# A narrow attribute mapping for exact table-row lookup. "When was it released?"
# and "What is the launch date?" ask for the same row; this does not add
# synonyms for arbitrary prose.
ATTRIBUTE_SYNONYMS = {
    "release": "launch",
    "released": "launch",
    "releases": "launch",
    "launch": "launch",
    "launched": "launch",
    "launches": "launch",
    "announced": "launch",
    "announcement": "launch",
    "introduced": "launch",
    "introduction": "launch",
}


def _model_ids(text: str) -> set[str]:
    """Normalised product identifiers such as "i5 9400" or "i7-12700F"."""
    return {
        re.sub(r"[^a-z0-9]", "", match.group().lower())
        for match in re.finditer(r"\b[a-z]+\d+[\s-]*\d+[a-z]?\b", text, re.I)
    }


def _attribute_rows(text: str) -> list[tuple[str, str, str]]:
    """Return (line, key, value) for every two-column table row."""
    rows: list[tuple[str, str, str]] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) != 2:
            continue
        key = re.sub(r"[*`_]", "", cells[0]).strip()
        value = re.sub(r"[*`_]", "", cells[1]).strip()
        if not key or not value:
            continue
        if re.fullmatch(r"[:\-\s]+", key) or re.fullmatch(r"[:\-\s]+", value):
            continue
        rows.append((stripped, key, value))
    return rows


def _table_row_fact(question: str, passages: list[RetrievedPassage]) -> GenerationResult | None:
    """Extract one exact attribute row for an identified product.

    A deterministic rescue, not a second opinion. The question must name exactly
    one model, the row must be a two-column attribute row whose key matches an
    asked attribute, every matching row in the retrieved evidence must carry the
    same value, and the match must include the top-ranked passage. The returned
    text is copied from the passage, so it is verbatim by construction and needs
    no model call. Rows that disagree are left to a person instead of guessed.
    """
    models = _model_ids(question)
    if len(models) != 1 or not passages:
        return None
    model = next(iter(models))
    tokens = match_tokens(question)
    # Tokens that name the product's identifier are the subject, not the asked
    # attribute. Any *other* identifier in the question ("L4", "E4021") is a
    # constraint the evidence has to satisfy, so answering an L4 question from an
    # L3 row is not allowed.
    identifiers = {
        token for token in tokens
        if re.search(r"\d", token) and token not in model
    }
    words = {
        ATTRIBUTE_SYNONYMS.get(token, token)
        for token in tokens
        if token not in ROW_STOPWORDS and not re.search(r"\d", token)
    }
    if not words:
        return None

    matches: list[tuple[RetrievedPassage, str, str]] = []
    for passage in passages:
        # The document has to be about the named product, not merely mention it.
        if model not in _model_ids(passage.document_title):
            continue
        for line, key, value in _attribute_rows(passage.child_text):
            key_terms = set(match_tokens(key))
            if not key_terms:
                continue
            row_terms = key_terms | set(match_tokens(value))
            if identifiers and not all(
                any(term_matches(identifier, term) for term in row_terms) for identifier in identifiers
            ):
                continue
            if not any(term_matches(word, term) for word in words for term in key_terms):
                continue
            matches.append((passage, line, value.casefold()))
    if not matches:
        return None

    top_document = passages[0].document_id
    if not any(passage.document_id == top_document for passage, _, _ in matches):
        return None
    if len({value for _, _, value in matches}) != 1:
        return None
    passage, line, _ = next(m for m in matches if m[0].document_id == top_document)
    return GenerationResult(answer_md=line, cited_ids=[str(passage.passage_id)])


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


async def _document_chunks(conn: AsyncConnection, passage: RetrievedPassage, limit: int = 120) -> list[RetrievedPassage]:
    """Current child passages of one document, for exact attribute lookup.

    Deliberately scoped to the top-ranked document: the question is about the
    product that document is about, so reading the rest of it is cheaper than a
    second model call. These passages are only used to find a verbatim row that
    is already stated in the source; they never author an answer on their own.
    """
    if conn is None:
        return []
    rows = await (
        await conn.execute(
            """select p.id as passage_id, p.parent_id, p.text as child_text,
                      coalesce(parent.text, p.text) as parent_text,
                      p.section_path, p.page_ref,
                      d.id as document_id, d.title as document_title, d.version as document_version,
                      d.doc_type, d.effective_from, d.plan_scope, d.product_version_scope,
                      s.id as source_id, s.title as source_title, s.source_tier, s.visibility
               from passages p
               join documents d on d.id = p.document_id
               join sources s on s.id = d.source_id
               left join passages parent on parent.id = p.parent_id
               where p.document_id = %s and p.level = 'chunk' and p.status = 'current'
                 and d.status = 'current' and s.status = 'active' and s.deleted_at is null
               order by p.chunk_index
               limit %s""",
            (passage.document_id, limit),
        )
    ).fetchall()
    return [
        RetrievedPassage(
            passage_id=row["passage_id"],
            parent_id=row["parent_id"],
            child_text=row["child_text"],
            parent_text=row["parent_text"],
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
        )
        for row in rows
    ]


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


def _merge_search_outcomes(outcomes: list[SearchOutcome], limit: int = 8) -> SearchOutcome:
    """Keep each part's strongest evidence without letting one part fill the prompt."""
    if not outcomes:
        return SearchOutcome([], 0, 0.0, dense_index="hybrid", dense_scan="none")
    per_part = max(2, limit // len(outcomes))
    ordered: list[RetrievedPassage] = []
    seen: set[UUID] = set()
    for outcome in outcomes:
        for passage in outcome.passages[:per_part]:
            if passage.passage_id in seen:
                continue
            seen.add(passage.passage_id)
            ordered.append(passage)
    remaining = sorted(
        (p for outcome in outcomes for p in outcome.passages if p.passage_id not in seen),
        key=lambda p: p.rerank_score,
        reverse=True,
    )
    for passage in remaining:
        if len(ordered) >= limit:
            break
        seen.add(passage.passage_id)
        ordered.append(passage)
    warnings = [o.index_warning for o in outcomes if o.index_warning]
    dense_modes = {o.dense_index for o in outcomes}
    dense_scans = {o.dense_scan for o in outcomes}
    return SearchOutcome(
        passages=ordered[:limit],
        candidates_scanned=sum(o.candidates_scanned for o in outcomes),
        query_coverage=sum(o.query_coverage for o in outcomes) / len(outcomes),
        dense_index=outcomes[0].dense_index if len(dense_modes) == 1 else "hybrid",
        dense_scan=outcomes[0].dense_scan if len(dense_scans) == 1 else "mixed",
        index_warning="; ".join(dict.fromkeys(warnings)) or None,
    )


def _evidence_passages(passages: list[RetrievedPassage]) -> list[EvidencePassage]:
    return [
        EvidencePassage(
            evidence_id=str(passage.passage_id),
            text=passage.child_text,
            title=passage.document_title,
            section_path=passage.section_path,
            source_date=_iso(passage.effective_from),
            source_tier=passage.source_tier,
            visibility=passage.visibility,
            plan_scope=passage.plan_scope,
            version_scope=passage.version_scope,
            score=passage.rerank_score,
        )
        for passage in passages
    ]


def _table_rescue(question: str, passages: list[RetrievedPassage]) -> GenerationResult | None:
    for extractor in (_table_fact_fallback, _table_row_fact):
        found = extractor(question, passages)
        if found:
            return found
    return None


def _citations_for_answer(
    answer_md: str,
    passages: list[RetrievedPassage],
    question: str,
) -> list[Citation]:
    asked_models = _model_ids(question)
    citations: list[Citation] = []
    seen: set[str] = set()
    for passage in passages:
        passage_id = str(passage.passage_id)
        if passage_id in seen:
            continue
        seen.add(passage_id)
        quote = _supporting_quote(answer_md, passage.child_text)
        if quote and asked_models:
            quote_models = _model_ids(quote)
            if quote_models and not (quote_models & asked_models):
                continue
        # A cited passage with no excerpt that supports the claim is not
        # evidence for it. Showing a truncated, unrelated paragraph as a
        # citation made the answer look verified when it was not.
        if not quote:
            continue
        excerpt = quote
        if not excerpt:
            continue
        citations.append(
            Citation(
                evidence_id=passage_id,
                passage_id=passage_id,
                quote=excerpt,
                source_title=passage.source_title,
                source_date=_iso(passage.effective_from),
                section_path=passage.section_path,
                verified=bool(quote and quote_in_text(quote, passage.child_text)),
            )
        )
    return citations


async def _abstain_multi(
    conn: AsyncConnection,
    session: Session,
    request: AskRequest,
    questions: list[str],
    reason: str,
    audience: str,
    plan: str | None,
    product_version: str | None,
    started: float,
    merged: SearchOutcome | None = None,
    sources_ignored: list[dict] | None = None,
) -> dict:
    intelligence = get_intelligence()
    gap_ids = [await _upsert_gap(conn, session, question, request.channel) for question in questions]
    gap_id = gap_ids[0] if gap_ids else None
    retrieved_ids = [p.passage_id for p in merged.passages] if merged else []
    scores = [p.rerank_score for p in merged.passages] if merged else []
    latency_ms = int((time.perf_counter() - started) * 1000)
    qa_log_id = await _log_qa(
        conn, session, request, " ".join(questions), "abstained", None, [],
        sources_ignored or [], retrieved_ids, scores, None, None, True, gap_id,
        None, None, latency_ms, intelligence.name,
    )
    next_steps = (
        ["Ask a teammate in your support channel.", "Draft a canonical answer from the sources below."]
        if audience == "internal"
        else ["Contact support for a human answer."]
    )
    return {
        "question": request.question,
        "standalone_query": " ".join(questions),
        "channel": request.channel,
        "mode": "abstained",
        "answer_md": None,
        "citations": [],
        "confidence_band": "low",
        "confidence": None,
        "sources_ignored": sources_ignored or [],
        "conflict_notice": None,
        "abstained": True,
        "abstention_reason": reason,
        "searched_scope": await _searched_scope(conn, session, audience, plan, product_version),
        "next_steps": next_steps,
        "gap_id": str(gap_id) if gap_id else None,
        "answer_id": None,
        "answer_version": None,
        "qa_log_id": str(qa_log_id),
        "latency_ms": latency_ms,
        "provider": intelligence.name,
        "retrieval_mode": merged.dense_index if merged else "hybrid",
        "dense_scan": merged.dense_scan if merged else "none",
        "index_warning": merged.index_warning if merged else None,
    }


async def _ask_multi_part(
    conn: AsyncConnection,
    session: Session,
    settings: Settings,
    request: AskRequest,
    parts: list[str],
    plan: str | None,
    product_version: str | None,
    audience: str,
    started: float,
) -> dict:
    intelligence = get_intelligence()
    if audience == "public":
        # A customer-facing answer needs one approved, public answer covering the
        # whole request. Joining independent fallback drafts would expose
        # unapproved source text and make a partial answer look complete.
        return await _abstain_multi(
            conn, session, request, parts,
            "No approved public answer covers every part of this question.",
            audience, plan, product_version, started,
        )

    low = settings.internal_low_threshold
    # Embed every part in one request. Remote embedding providers charge a round
    # trip per call, and a compound question otherwise paid it once per part.
    embeddings = await intelligence.embed(list(parts), task="query")
    if len(embeddings) != len(parts):
        embeddings = [(await intelligence.embed([part], task="query"))[0] for part in parts]

    outcomes: list[SearchOutcome] = []
    strengths: list[float] = []
    for part, embedding in zip(parts, embeddings):
        outcome = await hybrid_search(
            conn,
            part,
            embedding,
            channel=audience,
            plan=plan,
            product_version=product_version,
            as_of=request.as_of,
        )
        outcomes.append(outcome)
        strengths.append(retrieval_strength(outcome))

    attempted = [
        index for index, strength in enumerate(strengths)
        if outcomes[index].passages and strength >= low
    ]
    missing = [index for index in range(len(parts)) if index not in attempted]
    if not attempted:
        return await _abstain_multi(
            conn, session, request, parts,
            "No part of this question could be grounded in the current evidence.",
            audience, plan, product_version, started, _merge_search_outcomes(outcomes),
        )

    merged = _merge_search_outcomes([outcomes[index] for index in attempted])
    if not merged.passages:
        return await _abstain_multi(
            conn, session, request, parts,
            "No part of this question could be grounded in the current evidence.",
            audience, plan, product_version, started, merged,
        )

    evidence = _evidence_passages(merged.passages)
    questions = [parts[index] for index in attempted]
    generator = getattr(intelligence, "generate_multi_answer", None)
    if callable(generator):
        generated = list((await generator(questions, evidence, None)).answers)
    else:
        generated = [
            await intelligence.generate_answer(question, evidence, None)
            for question in questions
        ]
    generated += [GenerationResult("", [], True)] * (len(questions) - len(generated))

    by_id = {str(passage.passage_id): passage for passage in merged.passages}
    resolved: dict[int, dict] = {}

    def resolve_generation(part_index: int, generation: GenerationResult) -> dict | None:
        """Validate one generated part and fall back to a deterministic row."""
        answer_md = (generation.answer_md or "").strip()
        cited_ids = list(dict.fromkeys(
            cid for cid in (generation.cited_ids or []) if cid in by_id
        ))
        cited_passages = [by_id[cid] for cid in cited_ids]
        if generation.abstain or not answer_md or not cited_passages:
            rescue = _table_rescue(parts[part_index], merged.passages)
            if rescue:
                answer_md = (rescue.answer_md or "").strip()
                cited_ids = list(dict.fromkeys(
                    cid for cid in (rescue.cited_ids or []) if cid in by_id
                ))
                cited_passages = [by_id[cid] for cid in cited_ids]
        if not answer_md or not cited_passages:
            return None
        faithfulness = lexical_faithfulness(answer_md, cited_passages, merged.passages)
        if faithfulness < 1.0:
            rescue = _table_rescue(parts[part_index], merged.passages)
            if rescue:
                answer_md = (rescue.answer_md or "").strip()
                cited_ids = list(dict.fromkeys(
                    cid for cid in (rescue.cited_ids or []) if cid in by_id
                ))
                cited_passages = [by_id[cid] for cid in cited_ids]
                faithfulness = lexical_faithfulness(answer_md, cited_passages, merged.passages)
        if faithfulness < 1.0:
            return None
        citations = _citations_for_answer(answer_md, cited_passages, parts[part_index])
        if not citations:
            return None
        return {
            "answer_md": answer_md,
            "citations": citations,
            "faithfulness": faithfulness,
        }

    for offset, part_index in enumerate(attempted):
        result = resolve_generation(part_index, generated[offset])
        if result:
            resolved[part_index] = result

    # A combined generation can leave one part unanswered even after its retry.
    # Ask that part on its own before reporting it as missing; the single-part
    # prompt no longer has to share attention with every other question.
    individual = getattr(intelligence, "generate_answer", None)
    if callable(individual):
        for part_index in attempted:
            if part_index in resolved:
                continue
            try:
                generation = await individual(parts[part_index], evidence, None)
            except AppError as exc:
                # A failed individual retry must not discard the parts that did
                # resolve; report this part as missing instead.
                log.warning("individual compound part failed: %s", exc)
                continue
            result = resolve_generation(part_index, generation)
            if result:
                resolved[part_index] = result

    missing = sorted(index for index in range(len(parts)) if index not in resolved)
    if not resolved:
        return await _abstain_multi(
            conn, session, request, parts,
            "No part of this question could be grounded in the retrieved evidence.",
            audience, plan, product_version, started, merged,
        )

    cited_ids_all = [
        UUID(citation.passage_id)
        for result in resolved.values()
        for citation in result["citations"]
    ]
    cited_conflicts = await _pending_conflicts(conn, cited_ids_all, audience)
    if cited_conflicts:
        ignored = _conflict_sources_ignored(
            cited_conflicts, {str(passage_id) for passage_id in cited_ids_all}
        )
        return await _abstain_multi(
            conn, session, request, parts,
            "Cited sources have an unresolved conflict and need review.",
            audience, plan, product_version, started, merged, ignored,
        )

    historical = await _historical_accuracy(conn, None)
    retrieval = min(strengths[index] for index in resolved)
    faithfulness = min(result["faithfulness"] for result in resolved.values())
    composite = min(
        confidence_composite(strengths[index], result["faithfulness"], historical)
        for index, result in resolved.items()
    )
    if composite < low:
        return await _abstain_multi(
            conn, session, request, parts,
            "Confidence stayed below the abstention threshold.",
            audience, plan, product_version, started, merged,
        )
    band = band_for(composite, audience, settings)
    if missing:
        band = "medium" if band == "high" else band

    global_citations: list[dict] = []
    sections: list[str] = []
    for index, part in enumerate(parts):
        if index not in resolved:
            sections.append(
                f"**{part}**\n\nI couldn't find current evidence to answer this part."
            )
            continue
        result = resolved[index]
        markers: dict[str, int] = {}
        for citation in result["citations"]:
            if citation.passage_id in markers:
                continue
            global_citations.append(citation.as_dict())
            markers[citation.passage_id] = len(global_citations)
        part_answer = re.sub(
            r"\[P:([^\]]+)\]",
            lambda match: f"[{markers[match.group(1)]}]"
            if match.group(1) in markers else "",
            result["answer_md"],
        )
        sections.append(f"**{part}**\n\n{part_answer}")

    label = (
        "> **Draft from unapproved sources.** This is not yet a canonical answer. "
        "Check the citations before sending it to a customer. "
        "Time-sensitive facts may be out of date."
    )
    if missing:
        label += (
            "\n>\n> **Incomplete.** At least one part had no verifiable evidence. "
            "The missing part is marked below."
        )
    if band == "medium":
        label += (
            "\n>\n> **Review recommended.** The retrieved evidence only partly covers this request."
        )
    answer_md = f"{label}\n\n" + "\n\n".join(sections)

    gap_ids = [
        await _upsert_gap(conn, session, parts[index], request.channel)
        for index in missing
    ]
    gap_id = gap_ids[0] if gap_ids else None
    latency_ms = int((time.perf_counter() - started) * 1000)
    qa_log_id = await _log_qa(
        conn, session, request, " ".join(parts), "fallback", answer_md, global_citations,
        [], [p.passage_id for p in merged.passages],
        [p.rerank_score for p in merged.passages], composite, band, False, gap_id,
        None, None, latency_ms, intelligence.name,
    )
    return {
        "question": request.question,
        "standalone_query": " ".join(parts),
        "channel": request.channel,
        "mode": "fallback",
        "answer_md": answer_md,
        "citations": global_citations,
        "confidence_band": band,
        "confidence": {
            "composite": round(composite, 3),
            "retrieval": round(retrieval, 3),
            "faithfulness": round(faithfulness, 3),
            "historical": round(historical, 3),
        },
        "sources_ignored": [],
        "conflict_notice": None,
        "abstained": False,
        "searched_scope": await _searched_scope(conn, session, audience, plan, product_version),
        "next_steps": [],
        "gap_id": str(gap_id) if gap_id else None,
        "answer_id": None,
        "answer_version": None,
        "qa_log_id": str(qa_log_id),
        "latency_ms": latency_ms,
        "provider": intelligence.name,
        "retrieval_mode": merged.dense_index,
        "dense_scan": merged.dense_scan,
        "index_warning": merged.index_warning,
    }


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

    parts = normalise_subquestions(getattr(rewrite, "sub_questions", None))
    if not parts:
        parts = [standalone]
    if len(parts) == 1 and question_part_count(request.question) > 1:
        # The provider ignored the decomposition instruction. Split only the
        # explicit separators; a collapsed one-part rewrite cannot be recovered
        # here, which is why the rewrite itself retries before this point.
        fallback_parts = split_question_parts(standalone or request.question)
        if len(fallback_parts) > 1:
            parts = fallback_parts
    if len(parts) > 1:
        return await _ask_multi_part(
            conn, session, settings, request, parts, plan, product_version, audience, started
        )
    standalone = parts[0] if parts else standalone

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
                "dense_scan": outcome.dense_scan,
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

        def extract_known_fact() -> tuple[GenerationResult | None, list[RetrievedPassage]]:
            """Deterministic, verbatim extraction of a stated attribute.

            Used when the model produced no usable citation, or when its answer
            did not verify. It never invents a value: the returned text is copied
            from a passage, and ambiguous or disagreeing rows are refused.
            """
            by_id = {str(p.passage_id): p for p in candidates}
            for extractor in (_table_fact_fallback, _table_row_fact):
                found = extractor(standalone, candidates)
                if not found:
                    continue
                found_passages = [by_id[pid] for pid in found.cited_ids if pid in by_id]
                if found_passages:
                    return found, found_passages
            return None, []

        # An attribute row often sits in a section the retriever ranked below the
        # passages that merely mention the product. Widen the deterministic
        # extraction to the whole top-ranked document before paying for another
        # model call; the extractors still refuse an unnamed or ambiguous value.
        candidates = list(outcome.passages[:6])
        if outcome.passages:
            candidates = await _document_chunks(conn, outcome.passages[0]) + candidates

        if not generation.abstain and generation.answer_md and not cited_passages:
            # A small model can produce a malformed citation even when the
            # answer is present. Recover an exact stated fact if the evidence
            # contains one; never infer or guess an ID.
            rescued, rescued_passages = extract_known_fact()
            if rescued:
                generation, cited_passages = rescued, rescued_passages
        if not generation.abstain and generation.answer_md and not cited_passages:
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
                # A copied attribute row is exact, so prefer it to paying for a
                # second model call that may paraphrase again.
                rescued, rescued_passages = extract_known_fact()
                if rescued:
                    generation, cited_passages, faithfulness = rescued, rescued_passages, 1.0
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
                    asked_models = _model_ids(standalone)
                    for p in cited_passages:
                        quote = _supporting_quote(generation.answer_md, p.child_text)
                        # A question that names a product does not accept another
                        # product's row as evidence: quoting "i5-9400F ... 65 W" for
                        # an i5-9400 TDP question reads as a fact about the asked
                        # part, and only the shared unit makes it look right.
                        if quote and asked_models:
                            quote_models = _model_ids(quote)
                            if quote_models and not (quote_models & asked_models):
                                continue
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
                        "dense_scan": outcome.dense_scan,
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
        "dense_scan": outcome.dense_scan,
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
