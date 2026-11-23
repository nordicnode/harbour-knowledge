from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Sequence
from uuid import UUID

from psycopg import AsyncConnection

from ..config import get_settings
from ..db import fetch_one
from ..intelligence import get_intelligence
from ..intelligence.dev import match_tokens

RRF_K = 60
DENSE_LIMIT = 60
LEXICAL_LIMIT = 60
CANDIDATE_LIMIT = 40
RERANK_KEEP = 8


def vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(f"{value:.7f}" for value in vector) + "]"


@dataclass
class RetrievedPassage:
    passage_id: UUID
    parent_id: UUID | None
    child_text: str
    parent_text: str
    section_path: str
    page_ref: str | None
    document_id: UUID
    document_title: str
    document_version: int
    doc_type: str
    effective_from: date | None
    plan_scope: list[str]
    version_scope: list[str]
    source_id: UUID
    source_title: str
    source_tier: str
    visibility: str
    topic_id: UUID | None = None
    topic_name: str | None = None
    rrf_score: float = 0.0
    rerank_score: float = 0.0
    coverage: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def prompt_text(self) -> str:
        return f"{self.document_title} › {self.section_path}\n\n{self.parent_text}"


@dataclass
class SearchOutcome:
    passages: list[RetrievedPassage]
    candidates_scanned: int
    query_coverage: float
    # "hybrid" when dense and lexical candidates were fused, "lexical_only"
    # when the dense index is unavailable for the active embedding identity,
    # or "exact" for literal-token lookup.
    dense_index: str = "hybrid"
    index_warning: str | None = None


FILTER_SQL = """
  p.status = 'current'
  and p.level = 'chunk'
  and d.status = 'current'
  and s.status = 'active'
  and s.deleted_at is null
  and s.visibility = any(%(visibilities)s::text[])
  and (%(plan)s::text is null or cardinality(d.plan_scope) = 0 or %(plan)s::text = any(d.plan_scope))
  and (%(version)s::text is null or cardinality(d.product_version_scope) = 0
       or %(version)s::text = any(d.product_version_scope))
  and (d.effective_from is null or d.effective_from <= coalesce(%(as_of)s::date, current_date))
  and (d.effective_to is null or d.effective_to > coalesce(%(as_of)s::date, current_date))
  and (%(topic)s::text is null or t.slug = %(topic)s)
"""

BASE_JOINS = """
  from passages p
  join documents d on d.id = p.document_id
  join sources s on s.id = d.source_id
  left join topics t on t.id = d.topic_id
"""


async def _dense_index_state(conn: AsyncConnection, identity: str) -> tuple[bool, bool]:
    """Report whether current vectors match the active identity.

    The check intentionally ignores request scope: a plan-filtered query with no
    matches is not evidence that the whole index is stale. RLS still limits the
    rows to the current tenant.
    """
    row = await fetch_one(
        conn,
        """select
             exists(select 1 from passages p
                    join documents d on d.id = p.document_id
                    join sources s on s.id = d.source_id
                    where p.status = 'current' and p.level = 'chunk'
                      and d.status = 'current' and s.status = 'active' and s.deleted_at is null
                      and p.embedding is not null and p.embedding_identity = %s) as compatible,
             exists(select 1 from passages p
                    join documents d on d.id = p.document_id
                    join sources s on s.id = d.source_id
                    where p.status = 'current' and p.level = 'chunk'
                      and d.status = 'current' and s.status = 'active' and s.deleted_at is null
                      and p.embedding is not null) as any_embedded""",
        (identity,),
    )
    return bool(row and row["compatible"]), bool(row and row["any_embedded"])


async def hybrid_search(
    conn: AsyncConnection,
    query: str,
    embedding: list[float] | None,
    *,
    channel: str,
    plan: str | None = None,
    product_version: str | None = None,
    as_of: date | None = None,
    exact: bool = False,
    topic_slug: str | None = None,
    visibility_override: list[str] | None = None,
) -> SearchOutcome:
    visibilities = visibility_override or (["public"] if channel == "public" else ["internal", "public"])
    settings = get_settings()
    identity = settings.embedding_identity
    params = {
        "q": query,
        "visibilities": visibilities,
        "plan": plan,
        "version": product_version,
        "as_of": as_of,
        "topic": topic_slug,
        "emb": vector_literal(embedding) if embedding else None,
        "identity": identity,
        "dense_limit": DENSE_LIMIT,
        "lexical_limit": LEXICAL_LIMIT,
        "candidate_limit": CANDIDATE_LIMIT,
    }
    filters = FILTER_SQL
    joins = BASE_JOINS

    use_dense = bool(embedding) and not exact
    index_warning = None
    if use_dense:
        compatible, any_embedded = await _dense_index_state(conn, identity)
        if not compatible:
            use_dense = False
            index_warning = (
                "Stored embeddings were built with a different embedding configuration. "
                "Search used keyword matching only until the index is rebuilt with `make reindex`."
                if any_embedded
                else "No passages have embeddings for the active configuration. "
                "Search used keyword matching only until the index is built with `make reindex`."
            )
    dense_index = "exact" if exact else ("hybrid" if use_dense else "lexical_only")

    dense_cte = "dense as (select null::uuid as id, null::bigint as rn where false)," if not use_dense else f"""
        dense as (
          select p.id, row_number() over (order by p.embedding <=> %(emb)s::vector) as rn
          {joins}
          where {filters}
            and p.embedding is not null
            and p.embedding_identity = %(identity)s
          order by p.embedding <=> %(emb)s::vector
          limit %(dense_limit)s
        ),
    """

    # `exact` means literal tokens, not English stems. The indexed bm25_text is
    # built with the english configuration, so exact mode evaluates the simple
    # configuration against the passage text instead of reusing that index.
    if exact:
        lexical_document = "to_tsvector('simple', p.text)"
        lexical_query = "websearch_to_tsquery('simple', %(q)s)"
    else:
        lexical_document = "p.bm25_text"
        lexical_query = "websearch_to_tsquery('english', %(q)s)"

    sql = f"""
      with
      {dense_cte}
      lexical_matches as (
        select p.id, ts_rank_cd({lexical_document}, {lexical_query}) as rank_score
        {joins}
        where {filters}
          and {lexical_document} @@ {lexical_query}
        order by rank_score desc
        limit %(lexical_limit)s
      ),
      lexical as (
        select id, row_number() over (order by rank_score desc) as rn
        from lexical_matches
      )
      select
        p.id as passage_id, p.parent_id, p.text as child_text,
        coalesce(parent.text, p.text) as parent_text,
        p.section_path, p.page_ref,
        d.id as document_id, d.title as document_title, d.version as document_version,
        d.doc_type, d.effective_from, d.plan_scope, d.product_version_scope,
        d.topic_id, t.name as topic_name,
        s.id as source_id, s.title as source_title, s.source_tier, s.visibility,
        coalesce(1.0 / ({RRF_K} + dense.rn), 0) + coalesce(1.0 / ({RRF_K} + lexical.rn), 0) as rrf
      {joins}
      left join passages parent on parent.id = p.parent_id
      left join dense on dense.id = p.id
      left join lexical on lexical.id = p.id
      where (dense.id is not null or lexical.id is not null)
        and p.status = 'current'
        and d.status = 'current'
        and s.status = 'active'
      order by rrf desc
      limit %(candidate_limit)s
    """
    cur = await conn.execute(sql, params)
    rows = list(await cur.fetchall())
    if not rows:
        return SearchOutcome(
            passages=[], candidates_scanned=0, query_coverage=0.0,
            dense_index=dense_index, index_warning=index_warning,
        )

    intelligence = get_intelligence()
    documents = [
        f"{row['document_title']} — {row['section_path']}\n{row['child_text']}" for row in rows
    ]
    scores = await intelligence.rerank(query, documents)

    query_terms = set(match_tokens(query))
    scored: list[tuple[float, dict]] = []
    for row, score in zip(rows, scores):
        text_terms = set(match_tokens(f"{row['document_title']} {row['section_path']} {row['child_text']}"))
        coverage = len(query_terms & text_terms) / max(1, len(query_terms))
        scored.append((score, row | {"coverage": coverage}))
    scored.sort(key=lambda item: item[0], reverse=True)

    kept: list[RetrievedPassage] = []
    seen_parents: set[UUID] = set()
    for score, row in scored:
        parent_key = row["parent_id"] or row["passage_id"]
        if parent_key in seen_parents:
            continue
        seen_parents.add(parent_key)
        kept.append(
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
                topic_id=row["topic_id"],
                topic_name=row["topic_name"],
                rrf_score=float(row["rrf"]),
                rerank_score=float(score),
                coverage=float(row["coverage"]),
            )
        )
        if len(kept) >= RERANK_KEEP:
            break

    coverage_union: set[str] = set()
    for item in kept[:5]:
        coverage_union |= set(match_tokens(f"{item.document_title} {item.section_path} {item.child_text}"))
    query_coverage = len(query_terms & coverage_union) / max(1, len(query_terms))

    return SearchOutcome(
        passages=kept,
        candidates_scanned=len(rows),
        query_coverage=min(1.0, query_coverage),
        dense_index=dense_index,
        index_warning=index_warning,
    )


def retrieval_strength(outcome: SearchOutcome) -> float:
    """Composed retrieval signal. Raw cosine is never used directly.

    Reranker scores supply the ordering signal; coverage catches the case where
    one strong match hides that the question has parts with no evidence at all.
    """
    if not outcome.passages:
        return 0.0
    top = sorted((p.rerank_score for p in outcome.passages), reverse=True)[:3]
    mean_top = sum(top) / len(top)
    return max(0.0, min(1.0, 0.62 * mean_top + 0.38 * outcome.query_coverage))


def confidence_composite(retrieval: float, faithfulness: float, historical: float) -> float:
    return max(0.0, min(1.0, 0.55 * retrieval + 0.30 * faithfulness + 0.15 * historical))


def band_for(composite: float, channel: str, settings) -> str:
    low, high = (
        (settings.public_low_threshold, settings.public_high_threshold)
        if channel == "public"
        else (settings.internal_low_threshold, settings.internal_high_threshold)
    )
    if composite >= high:
        return "high"
    if composite >= low:
        return "medium"
    return "low"


def _grounding_sentences(text: str) -> list[str]:
    """Split claims without discarding words, negation, numbers, or conditions."""
    import re

    text = re.sub(r"\[[0-9]+\]|\[P:[^\]]+\]", "", text or "")
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"(?m)^\s*(?:[-*]\s+|\d+[.)]\s+)", "", text)
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip()]


def _grounding_normalize(text: str) -> str:
    import re

    return re.sub(r"\s+", " ", text).strip().casefold().rstrip(".!?")


def _contradiction_signature(text: str) -> tuple[frozenset[str], frozenset[str], bool]:
    """Identify a narrow class of opposite-polarity or numeric statements.

    Equal content words are deliberately required: broad overlap can confuse
    different subjects or conditions. This is a safeguard, not an NLI model.
    """
    import re

    text = _grounding_normalize(text).replace("’", "'")
    text = re.sub(r"\bcan't\b", "can not", text)
    text = re.sub(r"\bwon't\b", "will not", text)
    text = re.sub(r"\b(can)not\b", r"\1 not", text)
    text = re.sub(r"\b([a-z]+)n't\b", r"\1 not", text)
    text = re.sub(r"\bnon[- ]([a-z]+)\b", r"not \1", text)
    words = re.findall(r"[a-z]+|\d+(?:[.,]\d+)*", text)
    negative = any(word in {"not", "no", "never"} for word in words)
    # Normalize common antonyms, without assuming that all antonyms are simple
    # negations (e.g. 'expired' and 'valid' may have other meanings).
    opposites = {"ineligible": "eligible", "unavailable": "available", "disabled": "enabled"}
    normalized = []
    for word in words:
        if word in opposites:
            negative = True
            word = opposites[word]
        normalized.append(word)
    numbers = frozenset(word for word in normalized if word[0].isdigit())
    ignored = {"not", "no", "never", "is", "are", "was", "were", "be", "been", "can", "could", "do", "does", "did", "the", "a", "an"}
    content = frozenset(word for word in normalized if word not in ignored and not word[0].isdigit())
    return content, numbers, negative


def has_detectable_contradiction(answer_md: str, context: Sequence[RetrievedPassage]) -> bool:
    """Detect narrowly matched opposite-polarity or numeric source statements."""
    sentences = [sentence for passage in context for sentence in _grounding_sentences(passage.child_text)]
    for claim in _grounding_sentences(answer_md):
        content, numbers, negative = _contradiction_signature(claim)
        if len(content) < 2:
            continue
        for sentence in sentences:
            other_content, other_numbers, other_negative = _contradiction_signature(sentence)
            if content != other_content:
                continue
            if negative != other_negative and numbers == other_numbers:
                return True
            if negative == other_negative and numbers and other_numbers and numbers != other_numbers:
                return True
    return False


def lexical_faithfulness(
    answer_md: str,
    cited: Sequence[RetrievedPassage],
    context: Sequence[RetrievedPassage] | None = None,
) -> float:
    """Fraction of claims matching complete cited child sentences verbatim.

    This conservative extractive check rejects paraphrases it cannot verify;
    it is not a semantic entailment guarantee. Context is checked for a
    narrow set of explicit contradictory claims before a match is accepted.
    """
    claims = _grounding_sentences(answer_md)
    if not claims:
        return 0.0
    cited_sentences = [sentence for passage in cited for sentence in _grounding_sentences(passage.child_text)]
    checked_context = context if context is not None else cited
    supported = 0
    for claim in claims:
        normalized = _grounding_normalize(claim)
        if not normalized or not any(normalized == _grounding_normalize(sentence) for sentence in cited_sentences):
            continue
        if not has_detectable_contradiction(claim, checked_context):
            supported += 1
    return supported / len(claims)
