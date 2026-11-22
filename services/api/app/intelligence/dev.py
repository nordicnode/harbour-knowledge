from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Sequence

from .base import (
    Classification,
    DraftResult,
    EvidencePassage,
    EvidenceQuote,
    GenerationResult,
    RewriteResult,
    TopicOption,
)

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can", "could",
    "did", "do", "does", "for", "from", "had", "has", "have", "how", "i", "if", "in",
    "into", "is", "it", "its", "me", "my", "of", "on", "or", "our", "should", "so",
    "that", "the", "their", "them", "then", "there", "these", "they", "this", "to",
    "was", "we", "were", "what", "when", "where", "which", "who", "why", "will",
    "with", "would", "you", "your", "about", "after", "before", "over", "under",
}

PLANS = ("team", "business", "enterprise")
ERROR_CODE_RE = re.compile(r"\b[A-Z]{1,4}[-_]?\d{3,5}\b")
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*")
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9“\"'(])")

DOC_TYPES = (
    "policy", "pricing", "procedure", "faq", "known_issue", "bug_workaround",
    "troubleshooting", "product_description", "internal_note",
)


def tokenize(text: str, keep_stop: bool = False) -> list[str]:
    tokens = TOKEN_RE.findall(text.lower())
    if keep_stop:
        return tokens
    return [t for t in tokens if t not in STOPWORDS and len(t) > 1]


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class DevIntelligence:
    """Deterministic, offline intelligence for development and tests.

    It is lexical rather than truly semantic: embeddings are signed hashed
    token vectors, reranking blends token overlap with cosine, and answers are
    extractive so every citation is verbatim by construction. This keeps the
    full governed pipeline runnable with no API key and no network. Set
    AI_MODE=openai to use a real provider.
    """

    name = "dev"

    def __init__(self, embedding_dim: int = 1024, embedding_model: str = "dev-hashed-v1"):
        self.embedding_dim = embedding_dim
        self.embedding_model = embedding_model

    # -- embeddings ---------------------------------------------------------
    async def embed(self, texts: Sequence[str], task: str = "document") -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        tokens = tokenize(text)
        if not tokens:
            return [0.0] * self.embedding_dim
        weights: Counter[str] = Counter(tokens)
        features: list[tuple[str, float]] = [(t, 1.0) for t in weights]
        for left, right in zip(tokens, tokens[1:]):
            features.append((f"{left} {right}", 0.55))
        vector = [0.0] * self.embedding_dim
        for feature, weight in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            number = int.from_bytes(digest, "big")
            index = number % self.embedding_dim
            sign = 1.0 if (number >> 63) & 1 else -1.0
            vector[index] += sign * weight * (1.0 + math.log1p(weights.get(feature, 1)))
        norm = math.sqrt(sum(v * v for v in vector))
        if norm > 0:
            vector = [v / norm for v in vector]
        return vector

    # -- reranking ----------------------------------------------------------
    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        q_tokens = set(tokenize(query))
        if not q_tokens:
            return [0.0 for _ in documents]
        q_pairs = {f"{a} {b}" for a, b in zip(tokenize(query), tokenize(query)[1:])}
        q_vec = self._embed_one(query)
        scores: list[float] = []
        for doc in documents:
            d_tokens = set(tokenize(doc))
            d_pairs = {f"{a} {b}" for a, b in zip(tokenize(doc), tokenize(doc)[1:])}
            overlap = len(q_tokens & d_tokens) / len(q_tokens)
            phrase = 1.0 if q_pairs & d_pairs else 0.0
            d_vec = self._embed_one(doc)
            cosine = _clamp(sum(a * b for a, b in zip(q_vec, d_vec)))
            exact_bonus = 0.08 if any(code.lower() in doc.lower() for code in ERROR_CODE_RE.findall(query)) else 0.0
            scores.append(_clamp(0.52 * overlap + 0.30 * cosine + 0.10 * phrase + exact_bonus))
        return scores

    # -- query understanding ------------------------------------------------
    async def rewrite(self, question: str, history: Sequence[dict] | None = None) -> RewriteResult:
        standalone = question.strip()
        if history and len(tokenize(standalone)) <= 3:
            previous = next(
                (m.get("content", "") for m in reversed(history) if m.get("role") == "user"),
                "",
            )
            if previous:
                standalone = f"{previous.strip()} {standalone}"

        lowered = standalone.lower()
        plan = next((p for p in PLANS if re.search(rf"\b{p}\b", lowered)), None)
        version = None
        version_match = re.search(r"\b(?:v|version\s*)(\d+)\b", lowered)
        if version_match:
            version = f"v{version_match.group(1)}"
        error_codes = ERROR_CODE_RE.findall(standalone)

        if re.search(r"\b(how do i|how to|steps?|walk me through|process for)\b", lowered):
            intent = "procedure"
        elif re.search(r"\b(error|not working|fails?|broken|troubleshoot|fix)\b", lowered):
            intent = "troubleshooting"
        elif re.search(r"\b(vs\.?|versus|compare|difference between)\b", lowered):
            intent = "comparison"
        elif re.search(r"\b(summar|overview|everything about|all the)\b", lowered):
            intent = "synthesis"
        elif re.search(r"\b(what is|what are|does|do you|can i|is there|who is)\b", lowered):
            intent = "factoid"
        else:
            intent = "factoid"

        entities: list[str] = []
        for candidate in ("harbour", "okta", "xero", "quickbooks", "slack", "sso", "saml", "api"):
            if re.search(rf"\b{candidate}\b", lowered):
                entities.append(candidate)

        return RewriteResult(
            standalone_question=standalone,
            intent=intent,
            plan=plan.title() if plan else None,
            product_version=version,
            error_codes=error_codes,
            entities=entities,
        )

    async def contextualise(
        self,
        title: str,
        section_path: str,
        text: str,
        plan_scope: Sequence[str],
        version_scope: Sequence[str],
    ) -> str:
        scope: list[str] = []
        if plan_scope:
            scope.append("plan: " + ", ".join(plan_scope))
        if version_scope:
            scope.append("version: " + ", ".join(version_scope))
        first = SENTENCE_RE.split(text.strip().replace("\n", " "))[0][:180]
        scope_text = f" ({'; '.join(scope)})" if scope else ""
        return f"From {title} › {section_path or 'Overview'}{scope_text}. {first}"

    # -- classification -----------------------------------------------------
    async def classify(
        self,
        title: str,
        text: str,
        topics: Sequence[TopicOption],
    ) -> Classification:
        haystack = f"{title}\n{text}".lower()

        if re.search(r"\binternal\b|\bdo not share\b|\bconfidential\b", haystack[:600]):
            doc_type = "internal_note"
        elif re.search(r"\brefund|policy|terms|legal|eligible|approval\b", haystack):
            doc_type = "policy"
        elif re.search(r"\bpricing|price|per user|\$\d", haystack):
            doc_type = "pricing"
        elif re.search(r"\bknown issue\b", haystack):
            doc_type = "known_issue"
        elif re.search(r"\bworkaround\b|\bbug\b", haystack):
            doc_type = "bug_workaround"
        elif re.search(r"\bhow to\b|\bsetting up\b|^change\b", title, re.I) or re.search(r"^1\.\s", text, re.M):
            doc_type = "procedure"
        elif re.search(r"\berror\b|\btroubleshoot|not sync|failed\b", haystack):
            doc_type = "troubleshooting"
        elif re.search(r"\bfaq\b|frequently asked", haystack):
            doc_type = "faq"
        else:
            doc_type = "product_description"

        lowered_tokens = set(tokenize(text))
        topic_scores: list[tuple[float, str]] = []
        for topic in topics:
            words = set(tokenize(f"{topic.name} {topic.slug.replace('-', ' ')} {topic.description}"))
            if not words:
                continue
            matches = len(words & lowered_tokens)
            if matches:
                topic_scores.append((matches / len(words), topic.slug))
        topic_slugs = [slug for _, slug in sorted(topic_scores, reverse=True)[:2]]

        counts = Counter(tokenize(text))
        tags = [word for word, _ in counts.most_common(8)]
        entities = sorted(set(ERROR_CODE_RE.findall(text)))
        for plan in PLANS:
            if re.search(rf"\b{plan}\b", text.lower()):
                entities.append(plan.title())
        entities.append("Harbour")
        summary = SENTENCE_RE.split(text.strip().replace("\n", " "))[0][:240]

        return Classification(
            doc_type=doc_type,
            topic_slugs=topic_slugs,
            tags=tags,
            entities=entities,
            summary=summary,
        )

    # -- generation ---------------------------------------------------------
    def _rank_sentences(self, question: str, evidence: Sequence[EvidencePassage]) -> list[tuple[int, int, str, str]]:
        q_tokens = set(tokenize(question))
        ranked: list[tuple[float, int, int, str, str]] = []
        for e_index, passage in enumerate(evidence):
            clean = re.sub(r"<!--.*?-->", " ", passage.text)
            sentences = [s.strip() for s in SENTENCE_RE.split(clean) if len(s.strip()) > 20]
            if not sentences and clean.strip():
                sentences = [clean.strip()]
            for s_index, sentence in enumerate(sentences):
                s_tokens = set(tokenize(sentence))
                overlap = len(q_tokens & s_tokens) / max(1, len(q_tokens))
                detail = 0.08 if re.search(r"\d", sentence) else 0.0
                position = 0.05 if s_index < 3 else 0.0
                score = overlap * 1.0 + detail + position - 0.15 * (e_index / max(1, len(evidence)))
                ranked.append((score, e_index, s_index, sentence, passage.evidence_id))
        ranked.sort(reverse=True)
        return [(e, s, sentence, pid) for _, e, s, sentence, pid in ranked]

    async def draft_answer(self, question: str, evidence: Sequence[EvidencePassage]) -> DraftResult:
        if not evidence:
            return DraftResult(answer_md="", evidence=[], confidence=0.0)
        ranked = self._rank_sentences(question, evidence)[:3]
        chosen = sorted(ranked, key=lambda item: (item[0], item[1]))
        seen: set[str] = set()
        quotes: list[EvidenceQuote] = []
        parts: list[str] = []
        for _, _, sentence, passage_id in chosen:
            if sentence in seen:
                continue
            seen.add(sentence)
            parts.append(sentence)
            quotes.append(
                EvidenceQuote(
                    passage_id=passage_id,
                    quote=sentence,
                    claim_ref=f"claim-{len(quotes) + 1}",
                )
            )
        answer = " ".join(parts).strip()
        if not answer:
            first = evidence[0]
            answer = re.sub(r"<!--.*?-->", " ", first.text).strip()[:600]
            quotes = [EvidenceQuote(first.evidence_id, answer, "claim-1")]
        return DraftResult(answer_md=answer, evidence=quotes, confidence=0.6)

    async def generate_answer(
        self,
        question: str,
        evidence: Sequence[EvidencePassage],
        conflict_notice: str | None = None,
    ) -> GenerationResult:
        if not evidence:
            return GenerationResult(answer_md="", cited_ids=[], abstain=True)

        ranked = self._rank_sentences(question, evidence)
        chosen: list[tuple[int, int, str, str]] = []
        used_ids: list[str] = []
        for e_index, _s, sentence, pid in ranked:
            if any(pid == existing for existing in used_ids) and len(chosen) >= 2:
                continue
            chosen.append((e_index, _s, sentence, pid))
            if pid not in used_ids:
                used_ids.append(pid)
            if len(chosen) >= 2:
                break
        chosen.sort(key=lambda item: (item[0], item[1]))

        lines: list[str] = []
        if conflict_notice:
            lines.append(f"**Heads up:** {conflict_notice}")
            lines.append("")
        for index, (_e, _s, sentence, pid) in enumerate(chosen, start=1):
            lines.append(f"{sentence} [{index}]")
        if len(used_ids) > len(chosen):
            used_ids = used_ids[: len(chosen)]
        return GenerationResult(answer_md="\n\n".join(lines), cited_ids=used_ids, abstain=False)
