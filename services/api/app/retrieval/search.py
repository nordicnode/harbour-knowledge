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
from ..intelligence.dev import (
    NON_GATING_TERMS,
    gating_terms,
    match_terms,
    term_matches,
    term_overlap,
)

RRF_K = 60
DENSE_LIMIT = 60
LEXICAL_LIMIT = 60
CANDIDATE_LIMIT = 40
RERANK_KEEP = 8


def _dense_scan_for_count(eligible: int, threshold: int) -> str:
    """Choose exact scan while it is cheap enough to guarantee recall.

    The threshold is a count of eligible current chunks, not a table size. A
    filtered query over a large table can still have a small eligible set, so
    the decision is made after the visibility and scope filters. A threshold of
    zero disables the exact path entirely.
    """
    if threshold <= 0:
        return "hnsw"
    return "exact" if eligible <= threshold else "hnsw"


def _select_diverse(
    scored: list[tuple[float, dict]],
    limit: int,
    max_children_per_parent: int,
) -> list[tuple[float, dict]]:
    """Keep ranked rows without letting one section fill the result list.

    A section often contains a rule and its exception, or several independent
    table rows. One child per parent could hide the second fact; two preserves
    it while avoiding a flood of overlapping siblings.
    """
    kept: list[tuple[float, dict]] = []
    counts: dict[object, int] = {}
    per_parent = max(1, max_children_per_parent)
    for score, row in scored:
        parent_key = row["parent_id"] or row["passage_id"]
        if counts.get(parent_key, 0) >= per_parent:
            continue
        counts[parent_key] = counts.get(parent_key, 0) + 1
        kept.append((score, row))
        if len(kept) >= limit:
            break
    return kept


# Weight of the fused dense+lexical agreement when ordering candidates.
#
# Ordering used to be the reranker's score alone. With no cross-encoder
# configured the reranker is lexical overlap, which returns near-identical
# scores for every passage of a document that mentions the subject, and then
# orders them arbitrarily. That discarded the strongest signal available: in a
# measured case the correct passage ranked first by dense retrieval and first by
# RRF while sharing no vocabulary with the question ("released" vs "Launched"),
# and the lexical reranker pushed it out of the top four. Fusion can only reorder
# candidates the two retrievers already agreed on, so it restores recall rather
# than inventing relevance.
FUSION_WEIGHT = 0.3


def vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(f"{value:.7f}" for value in vector) + "]"


def normalise_scope_value(value: str | None) -> str | None:
    """Plan and version identifiers are matched case-insensitively.

    Ingestion lowercases source scope, while a model rewriting a question may
    return "Team" or "V1". Comparing the raw strings silently excluded the only
    relevant documents, so every path normalises before filtering and reporting.
    """
    if value is None:
        return None
    cleaned = value.strip().lower().lstrip(":")
    return cleaned or None


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
    # Which dense path actually ran: "exact" cosine scan, "hnsw", or "none".
    dense_scan: str = "hnsw"
    index_warning: str | None = None


FILTER_SQL = """
  p.status = 'current'
  and p.level = 'chunk'
  and d.status = 'current'
  and s.status = 'active'
  and s.deleted_at is null
  and s.visibility = any(%(visibilities)s::text[])
  and (%(plan)s::text is null or cardinality(d.plan_scope) = 0
       or lower(%(plan)s::text) = any(select lower(scope) from unnest(d.plan_scope) scope))
  and (%(version)s::text is null or cardinality(d.product_version_scope) = 0
       or lower(%(version)s::text) = any(select lower(scope) from unnest(d.product_version_scope) scope))
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


async def _dense_candidate_count(
    conn: AsyncConnection,
    joins: str,
    filters: str,
    params: dict,
    threshold: int,
) -> int:
    """Count eligible dense candidates, stopping just above the threshold.

    The count is deliberately bounded: once more than `threshold` rows match, the
    exact scan would be too expensive and the precise number no longer matters.
    This query does not compute distances, so its cost is a small fraction of the
    exact scan it is deciding against.
    """
    count_params = {
        key: params[key]
        for key in ("visibilities", "plan", "version", "as_of", "topic", "identity")
    }
    row = await fetch_one(
        conn,
        f"""
          select count(*) as eligible
          from (
            select 1
            {joins}
            where {filters}
              and p.embedding is not null
              and p.embedding_identity = %(identity)s
            limit %(probe_limit)s
          ) candidates
        """,
        {**count_params, "probe_limit": max(0, threshold) + 1},
    )
    return int(row["eligible"] or 0) if row else 0


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
    plan = normalise_scope_value(plan)
    product_version = normalise_scope_value(product_version)
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
    dense_scan = "none"
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
        elif settings.dense_exact_max_chunks > 0:
            eligible = await _dense_candidate_count(
                conn, joins, filters, params, settings.dense_exact_max_chunks
            )
            dense_scan = _dense_scan_for_count(eligible, settings.dense_exact_max_chunks)
        else:
            dense_scan = "hnsw"
    dense_index = "exact" if exact else ("hybrid" if use_dense else "lexical_only")

    if not use_dense:
        dense_cte = "dense as (select null::uuid as id, null::bigint as rn where false),"
    elif dense_scan == "exact":
        # MATERIALIZED prevents the planner from satisfying the distance order
        # from the HNSW index. Above the threshold this branch is never built.
        dense_cte = f"""
        exact_candidates as materialized (
          select p.id, p.embedding
          {joins}
          where {filters}
            and p.embedding is not null
            and p.embedding_identity = %(identity)s
        ),
        dense as (
          select id, rn
          from (
            select id, row_number() over (order by embedding <=> %(emb)s::vector) as rn
            from exact_candidates
          ) ranked
          where rn <= %(dense_limit)s
        ),
    """
    else:
        dense_cte = f"""
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
            dense_index=dense_index, dense_scan=dense_scan, index_warning=index_warning,
        )

    intelligence = get_intelligence()
    documents = [
        f"{row['document_title']} — {row['section_path']}\n{row['child_text']}" for row in rows
    ]
    scores = await intelligence.rerank(query, documents)

    # Comparison terms are lightly stemmed so "expense"/"expenses" and
    # "mean"/"means" are not treated as different subjects. Stored vectors keep
    # the unstemmed tokens; only scoring uses this view.
    query_terms = match_terms(query)
    scored: list[tuple[float, dict]] = []
    top_rrf = max((float(row["rrf"]) for row in rows), default=0.0) or 1.0
    for row, score in zip(rows, scores):
        text_terms = match_terms(f"{row['document_title']} {row['section_path']} {row['child_text']}")
        coverage = len(term_overlap(query_terms, text_terms)) / max(1, len(query_terms))
        # Fused score: the reranker stays dominant, and agreement between dense
        # and lexical retrieval breaks the near-ties it cannot separate.
        fused = (1.0 - FUSION_WEIGHT) * float(score) + FUSION_WEIGHT * (float(row["rrf"]) / top_rrf)
        scored.append((fused, row | {
            "coverage": coverage,
            "terms": text_terms,
            "rerank_raw": float(score),
            "rrf_normalised": float(row["rrf"]) / top_rrf,
        }))
    scored.sort(key=lambda item: item[0], reverse=True)

    kept: list[RetrievedPassage] = []
    for score, row in _select_diverse(
        scored, RERANK_KEEP, settings.max_children_per_parent
    ):
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
                extra={
                    "rerank_raw": row["rerank_raw"],
                    "rrf_normalised": row["rrf_normalised"],
                },
            )
        )
    coverage_union: set[str] = set()
    for item in kept[:5]:
        coverage_union |= match_terms(f"{item.document_title} {item.section_path} {item.child_text}")
    query_coverage = len(term_overlap(query_terms, coverage_union)) / max(1, len(query_terms))

    return SearchOutcome(
        passages=kept,
        candidates_scanned=len(rows),
        query_coverage=min(1.0, query_coverage),
        dense_index=dense_index,
        dense_scan=dense_scan,
        index_warning=index_warning,
    )


def retrieval_strength(outcome: SearchOutcome) -> float:
    """Composed retrieval signal. Raw cosine is never used directly.

    Reranker scores supply the ordering signal; coverage catches the case where
    one strong match hides that the question has parts with no evidence at all.

    The average is taken over the strongest passages that actually scored above
    noise. Averaging a fixed top three punished correctly filtered queries: with
    ``plan=v2`` only one passage is eligible, so the two filler passages' zero
    scores divided a good match by three and pushed a supported answer below the
    abstention band. A passage the reranker scored at zero is not evidence of
    support and must not be able to dilute a confident one.
    """
    if not outcome.passages:
        return 0.0
    ordered = sorted((p.rerank_score for p in outcome.passages), reverse=True)
    relevant = [score for score in ordered if score > 0.05][:3] or ordered[:1]
    mean_top = sum(relevant) / len(relevant)
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


def _grounding_pieces(text: str) -> list[str]:
    """Sentences plus individual table rows and short values.

    A cited sentence list does not contain "65 W" on its own, so a one-value
    answer could never verify against it. Table rows are complete evidence units
    and are kept intact.
    """
    pieces: list[str] = []
    for block in (text or "").splitlines():
        stripped = block.strip()
        if not stripped:
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            pieces.append(stripped)
    pieces.extend(_grounding_sentences(text))
    return pieces


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


def _markup_normalize(text: str) -> str:
    """Compare text by its words, ignoring presentation.

    A table cell is rendered in the stored passage as ``| **TDP** | 65 W |`` and
    an answer may quote it as "TDP: 65 W". Removing table pipes, emphasis,
    citation markers and URLs cannot invent a number, a condition or a word, so
    it is safe to treat the two as the same evidence.
    """
    import re

    text = re.sub(r"\[P:[^\]]+\]|\[[0-9]+\]", " ", text or "")
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[*_`>#|]+", " ", text)
    text = re.sub(r"[^0-9a-z%.+-]+", " ", text.casefold())
    return re.sub(r"\s+", " ", text).strip()


def _short_table_value_supported(claim: str, sentence: str) -> bool:
    """Accept a compact field/value answer against one cited table row.

    A model often answers "6 cores" or "Launched on January 7, 2019" instead
    of reciting the whole row. Requiring every meaningful claim token and every
    claim number to occur in the same row keeps the field/value association; a
    bare digit still cannot match an unrelated row. This deliberately applies
    only to table rows, so a prose claim that drops a condition is still
    rejected by the stricter sentence rules.
    """
    row = sentence.strip()
    if not (row.startswith("|") and row.endswith("|")):
        return False
    def clean(token: str) -> str:
        return token.rstrip(".,;:!?)]")

    claim_tokens = [cleaned for token in _markup_normalize(claim).split() if (cleaned := clean(token))]
    if not (0 < len(claim_tokens) <= 12):
        return False
    row_tokens = [cleaned for token in _markup_normalize(row).split() if (cleaned := clean(token))]
    ignorable = {
        "a", "an", "the", "it", "its", "is", "are", "was", "were", "be",
        "been", "has", "have", "had", "does", "do", "did", "this", "that",
        "these", "those", "of", "for", "on", "in", "to", "with", "and", "or",
        "also", "approximately", "about",
    }
    claim_numbers = {token for token in claim_tokens if token[0].isdigit()}
    row_numbers = {token for token in row_tokens if token[0].isdigit()}
    claim_words = {
        token for token in claim_tokens
        if not token[0].isdigit() and token not in ignorable
    }
    row_words = {token for token in row_tokens if not token[0].isdigit()}
    return bool(
        claim_numbers
        and claim_numbers == row_numbers
        and claim_words
        and all(term_overlap({word}, row_words) for word in claim_words)
    )


_REFERENCE_WORDS = frozenset(
    "it its they them their this that these those he she him his her there".split()
)


def _short_table_boolean_supported(claim: str, sentence: str, question: str) -> bool:
    """Accept a yes/no answer when the cited row states the same polarity.

    A model often answers a boolean attribute question with just "No" while
    citing the row ``| Intel Hyper-Threading Technology | No | ... |``. The
    generic sentence checks reject that because "No" has no content word to
    match. Requiring the row to overlap the question's attribute, and the
    claim's polarity to match the row's explicit yes/no value, keeps an
    unrelated "No" row from supporting the answer.
    """
    import re

    if not question:
        return False
    row = sentence.strip()
    if not (row.startswith("|") and row.endswith("|")):
        return False
    claim_tokens = _markup_normalize(claim).split()
    if not (0 < len(claim_tokens) <= 12):
        return False
    content, numbers, negative = _contradiction_signature(claim)
    if numbers:
        return False

    question_terms = {
        token for token in gating_terms(question)
        if not any(character.isdigit() for character in token)
    }
    row_text = _markup_normalize(row)
    row_terms = set(row_text.split())
    # The row may keep "hyper-threading" as one token while the claim's
    # signature splits it into two words. Compare the component words too.
    row_terms.update(
        part for token in list(row_terms) for part in re.split(r"[-/]", token) if part
    )
    if not question_terms or not any(
        term_matches(term, row_term) for term in question_terms for row_term in row_terms
    ):
        return False
    if negative and not re.search(r"\bno\b", row_text):
        return False
    if not negative and not re.search(r"\byes\b", row_text):
        return False

    # Every extra fact in the claim must also be present in the row. Question
    # verbs such as "support" are not facts and are ignored here.
    return all(
        term in NON_GATING_TERMS
        or term in _REFERENCE_WORDS
        or any(term_matches(term, row_term) for row_term in row_terms)
        for term in content
    )


def claim_supported_by_sentence(claim: str, sentence: str, question: str = "") -> bool:
    """Decide whether one claim is supported by one cited sentence.

    Five standards are accepted:

    * **Verbatim** - the claim equals the sentence after normalisation. This is
      the strict path and proves the words were copied.
    * **Short table value** - a compact field/value answer matches one cited
      table row, with every meaningful token and number present in that row.
      This covers "6 cores" and "Launched on January 7, 2019" without letting a
      bare digit match an unrelated row.
    * **Boolean table value** - "Yes" or "No" matches an explicit yes/no cell
      in a row whose attribute overlaps the question, so a one-word answer to
      "Does it support hyper-threading?" is grounded without accepting a
      different row that also says "No".
    * **Near-extractive** - the claim keeps nearly every content word of the
      sentence and introduces almost none of its own, with identical numbers,
      units and negation polarity. This tolerates dropped trailing punctuation,
      markdown wrappers, a reordering, or a repeated subject, which are the
      common ways a small local model reformats a copied sentence.
    * **Markup-only difference** - the claim's words appear as a contiguous span
      once table pipes and emphasis are removed, and it carries at least two
      content words. This is how a table cell quoted as "TDP: 65 W" is
      recognised without trusting the model's own citation.

    It intentionally rejects a claim that drops a condition ("only Enterprise",
    "within 30 days") or adds a clause: those are the cases where quote presence
    most often gets mistaken for support.
    """
    if _grounding_normalize(claim) == _grounding_normalize(sentence):
        return True
    if _short_table_value_supported(claim, sentence):
        return True
    if _short_table_boolean_supported(claim, sentence, question):
        return True

    claim_content, claim_numbers, claim_negative = _contradiction_signature(claim)
    sentence_content, sentence_numbers, sentence_negative = _contradiction_signature(sentence)
    if not claim_content or not sentence_content:
        return False
    if claim_numbers != sentence_numbers or claim_negative != sentence_negative:
        return False

    shared = term_overlap(claim_content, sentence_content)
    claim_coverage = len(shared) / len(claim_content)
    sentence_coverage = len(shared) / len(sentence_content)
    if claim_coverage >= 0.95 and sentence_coverage >= 0.7:
        return True

    # A single word can appear by coincidence ("6" is in many table rows), so a
    # markup-only match needs enough substance to be evidence of a real span.
    claim_words = _markup_normalize(claim).split()
    if len(claim_words) < 2:
        return False
    return f" {_markup_normalize(claim)} " in f" {_markup_normalize(sentence)} "


def any_claim_supported(answer_md: str, sentence: str, question: str = "") -> bool:
    """True when at least one claim in the answer is supported by this excerpt."""
    return any(
        claim_supported_by_sentence(claim, sentence, question)
        for claim in _grounding_sentences(answer_md)
    )


def lexical_faithfulness(
    answer_md: str,
    cited: Sequence[RetrievedPassage],
    context: Sequence[RetrievedPassage] | None = None,
    question: str = "",
) -> float:
    """Fraction of claims matched by a complete cited child sentence.

    Matching is verbatim or near-extractive; see `claim_supported_by_sentence`.
    This is not a semantic entailment guarantee, and it is reported as
    faithfulness, never as a probability that the answer is correct. Context is
    checked for a narrow set of explicit contradictory claims first.
    """
    claims = _grounding_sentences(answer_md)
    if not claims:
        return 0.0
    cited_pieces = [piece for passage in cited for piece in _grounding_pieces(passage.child_text)]
    checked_context = context if context is not None else cited
    supported = 0
    for claim in claims:
        normalized = _grounding_normalize(claim)
        if not normalized or not any(
            claim_supported_by_sentence(claim, piece, question) for piece in cited_pieces
        ):
            continue
        if not has_detectable_contradiction(claim, checked_context):
            supported += 1
    return supported / len(claims)
