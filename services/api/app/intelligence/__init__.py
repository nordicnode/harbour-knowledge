from __future__ import annotations

from functools import lru_cache

from ..config import get_settings
from .base import (
    Classification,
    DraftResult,
    EvidencePassage,
    EvidenceQuote,
    GenerationResult,
    Intelligence,
    RewriteResult,
    TopicOption,
)
from .dev import DevIntelligence
from .hybrid import HybridIntelligence


@lru_cache
def get_intelligence() -> Intelligence:
    settings = get_settings()
    if settings.ai_mode == "dev":
        chat: Intelligence = DevIntelligence(settings.embedding_dim, settings.embedding_model)
    else:
        from .openai_compat import OpenAICompatIntelligence

        chat = OpenAICompatIntelligence(settings)

    if settings.embedding_provider == "local":
        from .local_embeddings import LocalEmbeddingIntelligence

        return HybridIntelligence(chat, LocalEmbeddingIntelligence(settings))
    if settings.embedding_provider == "openai":
        if settings.ai_mode == "dev":
            raise RuntimeError(
                "EMBEDDING_PROVIDER=openai requires AI_MODE=openai. "
                "Use local embeddings with dev chat, or configure the chat provider."
            )
        return chat
    if settings.ai_mode == "dev":
        return chat
    return HybridIntelligence(
        chat,
        DevIntelligence(settings.embedding_dim, settings.embedding_model),
    )


__all__ = [
    "Classification",
    "DraftResult",
    "EvidencePassage",
    "EvidenceQuote",
    "GenerationResult",
    "Intelligence",
    "RewriteResult",
    "TopicOption",
    "get_intelligence",
]
