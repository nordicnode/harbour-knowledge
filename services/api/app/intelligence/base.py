from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence


@dataclass
class TopicOption:
    slug: str
    name: str
    description: str = ""


@dataclass
class EvidencePassage:
    evidence_id: str
    text: str
    title: str
    section_path: str | None = None
    source_date: str | None = None
    source_tier: str = "official_doc"
    visibility: str = "internal"
    plan_scope: list[str] = field(default_factory=list)
    version_scope: list[str] = field(default_factory=list)
    score: float = 0.0


@dataclass
class RewriteResult:
    standalone_question: str
    intent: str = "factoid"
    plan: str | None = None
    product_version: str | None = None
    error_codes: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    # One entry per independent request. A single question still has one entry;
    # consumers must not infer that an empty list means "the same question".
    sub_questions: list[str] = field(default_factory=list)


@dataclass
class Classification:
    doc_type: str
    topic_slugs: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class EvidenceQuote:
    passage_id: str
    quote: str
    claim_ref: str | None = None


@dataclass
class DraftResult:
    answer_md: str
    evidence: list[EvidenceQuote]
    confidence: float = 0.6


@dataclass
class GenerationResult:
    answer_md: str
    cited_ids: list[str]
    abstain: bool = False


@dataclass
class MultiGenerationResult:
    """One answer per requested part, in request order."""

    answers: list[GenerationResult] = field(default_factory=list)


class Intelligence(Protocol):
    """The only AI surface business logic is allowed to call.

    Implementations: deterministic offline development mode, and any
    OpenAI-compatible Chat Completions + Embeddings endpoint.
    """

    name: str

    async def embed(self, texts: Sequence[str], task: str = "document") -> list[list[float]]: ...

    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]: ...

    async def rewrite(self, question: str, history: Sequence[dict] | None = None) -> RewriteResult: ...

    async def equivalent_questions(self, question: str, candidates: Sequence[str]) -> list[int]: ...

    async def contextualise(
        self,
        title: str,
        section_path: str,
        text: str,
        plan_scope: Sequence[str],
        version_scope: Sequence[str],
    ) -> str: ...

    async def classify(
        self,
        title: str,
        text: str,
        topics: Sequence[TopicOption],
    ) -> Classification: ...

    async def draft_answer(
        self,
        question: str,
        evidence: Sequence[EvidencePassage],
    ) -> DraftResult: ...

    async def generate_answer(
        self,
        question: str,
        evidence: Sequence[EvidencePassage],
        conflict_notice: str | None = None,
    ) -> GenerationResult: ...

    async def generate_multi_answer(
        self,
        questions: Sequence[str],
        evidence: Sequence[EvidencePassage],
        conflict_notice: str | None = None,
    ) -> MultiGenerationResult: ...
