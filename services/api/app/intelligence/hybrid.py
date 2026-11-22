from __future__ import annotations

from typing import Sequence

from .base import (
    Classification,
    DraftResult,
    EvidencePassage,
    GenerationResult,
    Intelligence,
    RewriteResult,
    TopicOption,
)


class HybridIntelligence:
    """Use one provider for chat work and another for embeddings.

    This is the practical split for a local GPU: a remote model can write and
    classify, while a local Sentence-Transformers model handles the embedding
    index without an API round trip or per-token embedding charge.
    """

    def __init__(self, chat: Intelligence, embeddings: Intelligence):
        self.chat = chat
        self.embeddings = embeddings
        self.name = f"{chat.name}+{embeddings.name}"

    async def embed(self, texts: Sequence[str], task: str = "document") -> list[list[float]]:
        return await self.embeddings.embed(texts, task)

    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        return await self.chat.rerank(query, documents)

    async def rewrite(self, question: str, history: Sequence[dict] | None = None) -> RewriteResult:
        return await self.chat.rewrite(question, history)

    async def contextualise(
        self,
        title: str,
        section_path: str,
        text: str,
        plan_scope: Sequence[str],
        version_scope: Sequence[str],
    ) -> str:
        return await self.chat.contextualise(title, section_path, text, plan_scope, version_scope)

    async def classify(
        self,
        title: str,
        text: str,
        topics: Sequence[TopicOption],
    ) -> Classification:
        return await self.chat.classify(title, text, topics)

    async def draft_answer(self, question: str, evidence: Sequence[EvidencePassage]) -> DraftResult:
        return await self.chat.draft_answer(question, evidence)

    async def generate_answer(
        self,
        question: str,
        evidence: Sequence[EvidencePassage],
        conflict_notice: str | None = None,
    ) -> GenerationResult:
        return await self.chat.generate_answer(question, evidence, conflict_notice)
