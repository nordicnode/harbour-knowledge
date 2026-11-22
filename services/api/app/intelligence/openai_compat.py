from __future__ import annotations

import json
import logging
import re
from typing import Any, Sequence

import httpx
from openai import APIConnectionError, APIStatusError, AsyncOpenAI, RateLimitError

from ..config import Settings
from ..errors import provider_rejected, provider_unavailable, rate_limited
from .base import (
    Classification,
    DraftResult,
    EvidencePassage,
    EvidenceQuote,
    GenerationResult,
    RewriteResult,
    TopicOption,
)
from .dev import DOC_TYPES, DevIntelligence

log = logging.getLogger(__name__)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text


class OpenAICompatIntelligence:
    """One implementation against Chat Completions semantics.

    base_url, key, and per-role models are configuration. No vendor SDK or model
    name appears in business logic. Reranking goes through an optional
    OpenAI-compatible /rerank endpoint and falls back to lexical scoring.
    """

    name = "openai"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = AsyncOpenAI(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
            timeout=60.0,
            max_retries=1,
        )
        self.embedding_client = AsyncOpenAI(
            base_url=settings.openai_base_url,
            api_key=settings.openai_embedding_api_key or settings.openai_api_key,
            timeout=60.0,
            max_retries=1,
        )
        self._http = httpx.AsyncClient(timeout=60.0)
        self._lexical = DevIntelligence(settings.embedding_dim, settings.embedding_model)

    # -- embeddings ---------------------------------------------------------
    async def embed(self, texts: Sequence[str], task: str = "document") -> list[list[float]]:
        model = self.settings.model_embed
        if not model:
            raise provider_unavailable("MODEL_EMBED is not configured.")
        payload: dict[str, object] = {"model": model, "input": list(texts)}
        # The pgvector column is fixed at 1024 dimensions. OpenAI's default is
        # 1536, so request the index dimension explicitly unless the endpoint
        # is known to reject the optional parameter.
        if self.settings.openai_embedding_dimensions:
            payload["dimensions"] = self.settings.embedding_dim
        try:
            response = await self.embedding_client.embeddings.create(**payload)
        except RateLimitError as exc:
            raise rate_limited(str(exc)) from exc
        except APIConnectionError as exc:
            raise provider_unavailable(f"Could not reach the embedding provider: {exc}") from exc
        except APIStatusError as exc:
            raise provider_rejected(f"Embedding provider rejected the request ({exc.status_code}).") from exc
        vectors = [item.embedding for item in response.data]
        if vectors and len(vectors[0]) != self.settings.embedding_dim:
            raise provider_rejected(
                f"Embedding model returned {len(vectors[0])} dimensions but the index expects "
                f"{self.settings.embedding_dim}. Changing embedders requires a full re-index."
            )
        return vectors

    # -- reranking ----------------------------------------------------------
    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        model = self.settings.model_rerank
        if not (self.settings.rerank_base_url and model):
            return await self._lexical.rerank(query, documents)
        url = self.settings.rerank_base_url.rstrip("/") + "/rerank"
        headers = {"content-type": "application/json"}
        if self.settings.rerank_api_key:
            headers["authorization"] = f"Bearer {self.settings.rerank_api_key}"
        payload = {"model": model, "query": query, "documents": list(documents), "top_n": len(documents)}
        try:
            response = await self._http.post(url, json=payload, headers=headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("rerank provider unavailable, using lexical fallback: %s", exc)
            return await self._lexical.rerank(query, documents)
        data = response.json()
        results = data.get("results") or data.get("data") or []
        scores = [0.0] * len(documents)
        for item in results:
            index = item.get("index")
            score = item.get("relevance_score", item.get("score", 0.0))
            if isinstance(index, int) and 0 <= index < len(scores):
                scores[index] = float(score)
        return scores

    # -- chat helpers -------------------------------------------------------
    async def _chat_json(
        self,
        role: str,
        name: str,
        schema: dict[str, Any],
        system: str,
        user: str,
    ) -> dict[str, Any]:
        model = self.settings.model_for(role)
        if not model:
            raise provider_unavailable(f"No model is configured for role '{role}'.")
        base_messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_error: Exception | None = None
        for attempt in range(2):
            messages = base_messages
            if attempt:
                messages = base_messages + [
                    {"role": "assistant", "content": "I will return only valid JSON."},
                    {"role": "user", "content": "Return only the JSON object, with every required field."},
                ]
            try:
                response = await self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": name, "strict": True, "schema": schema},
                    },
                )
                content = response.choices[0].message.content or ""
                return json.loads(_strip_fences(content))
            except RateLimitError as exc:
                raise rate_limited(str(exc)) from exc
            except APIConnectionError as exc:
                raise provider_unavailable(f"Could not reach the AI provider: {exc}") from exc
            except (json.JSONDecodeError, APIStatusError, IndexError, TypeError) as exc:
                last_error = exc
                log.warning("structured completion failed (%s), retrying", exc)
        raise provider_rejected(f"The provider did not return usable structured output: {last_error}")

    async def _chat_text(self, role: str, system: str, user: str, max_tokens: int = 220) -> str:
        model = self.settings.model_for(role)
        if not model:
            raise provider_unavailable(f"No model is configured for role '{role}'.")
        try:
            response = await self.client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0.2,
                max_tokens=max_tokens,
            )
            return (response.choices[0].message.content or "").strip()
        except RateLimitError as exc:
            raise rate_limited(str(exc)) from exc
        except APIConnectionError as exc:
            raise provider_unavailable(f"Could not reach the AI provider: {exc}") from exc
        except APIStatusError as exc:
            raise provider_rejected(f"The provider rejected the request ({exc.status_code}).") from exc

    # -- query understanding ------------------------------------------------
    async def rewrite(self, question: str, history: Sequence[dict] | None = None) -> RewriteResult:
        history_text = ""
        if history:
            history_text = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in history[-6:])
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "standalone_question", "intent", "plan", "product_version", "error_codes", "entities",
            ],
            "properties": {
                "standalone_question": {"type": "string"},
                "intent": {
                    "type": "string",
                    "enum": ["factoid", "procedure", "troubleshooting", "comparison", "synthesis", "out_of_scope"],
                },
                "plan": {"type": "string"},
                "product_version": {"type": "string"},
                "error_codes": {"type": "array", "items": {"type": "string"}},
                "entities": {"type": "array", "items": {"type": "string"}},
            },
        }
        data = await self._chat_json(
            "rewrite",
            "standalone_query",
            schema,
            "Rewrite a support question into a standalone search query. Resolve pronouns from history. "
            "Extract plan and product version only when explicit. Return empty strings for absent values.",
            f"Question: {question}\n\nHistory:\n{history_text or '(none)'}",
        )
        return RewriteResult(
            standalone_question=data.get("standalone_question") or question,
            intent=data.get("intent") or "factoid",
            plan=(data.get("plan") or None),
            product_version=(data.get("product_version") or None),
            error_codes=list(data.get("error_codes") or []),
            entities=list(data.get("entities") or []),
        )

    async def contextualise(
        self,
        title: str,
        section_path: str,
        text: str,
        plan_scope: Sequence[str],
        version_scope: Sequence[str],
    ) -> str:
        scope = f"Plans: {', '.join(plan_scope) or 'all'}. Versions: {', '.join(version_scope) or 'all'}."
        return await self._chat_text(
            "ingest_small",
            "You situate a chunk of a support document for retrieval. Write 2 short sentences naming the "
            "document, section, product, plan and version it concerns. No preamble.",
            f"Document: {title}\nSection: {section_path}\n{scope}\n\nChunk:\n{text[:2500]}",
            max_tokens=140,
        )

    async def classify(
        self,
        title: str,
        text: str,
        topics: Sequence[TopicOption],
    ) -> Classification:
        topic_list = "\n".join(f"- {t.slug}: {t.name} — {t.description}" for t in topics)
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["doc_type", "topic_slugs", "tags", "entities", "summary"],
            "properties": {
                "doc_type": {"type": "string", "enum": list(DOC_TYPES)},
                "topic_slugs": {"type": "array", "items": {"type": "string"}},
                "tags": {"type": "array", "items": {"type": "string"}},
                "entities": {"type": "array", "items": {"type": "string"}},
                "summary": {"type": "string"},
            },
        }
        data = await self._chat_json(
            "ingest_small",
            "classification",
            schema,
            "Classify a support document. Use only the closed document types and the provided topic slugs. "
            "Propose at most two topics and eight lowercase tags.",
            f"Title: {title}\n\nTopics:\n{topic_list}\n\nExcerpt:\n{text[:6000]}",
        )
        allowed = {t.slug for t in topics}
        return Classification(
            doc_type=data.get("doc_type") if data.get("doc_type") in DOC_TYPES else "faq",
            topic_slugs=[slug for slug in data.get("topic_slugs", []) if slug in allowed][:2],
            tags=list(data.get("tags", []))[:12],
            entities=list(data.get("entities", []))[:20],
            summary=data.get("summary", ""),
        )

    # -- generation ---------------------------------------------------------
    def _evidence_block(self, evidence: Sequence[EvidencePassage]) -> str:
        blocks = []
        for item in evidence:
            blocks.append(
                f"[P:{item.evidence_id}]\nTitle: {item.title}\nSection: {item.section_path or '-'}\n"
                f"Scope: plans={item.plan_scope or 'all'}, versions={item.version_scope or 'all'}, "
                f"visibility={item.visibility}\nText:\n{item.text}"
            )
        return "\n\n---\n\n".join(blocks)

    async def draft_answer(self, question: str, evidence: Sequence[EvidencePassage]) -> DraftResult:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["answer_md", "evidence"],
            "properties": {
                "answer_md": {"type": "string"},
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["passage_id", "quote", "claim_ref"],
                        "properties": {
                            "passage_id": {"type": "string"},
                            "quote": {"type": "string"},
                            "claim_ref": {"type": "string"},
                        },
                    },
                },
            },
        }
        data = await self._chat_json(
            "answer",
            "drafted_answer",
            schema,
            "You draft a canonical support answer from evidence only. Every sentence must be supported by a "
            "verbatim quote from the supplied passages. Do not add facts, prices, dates or steps not present. "
            "Use Markdown. Attach one evidence quote per claim, copying the quote exactly from the passage.",
            f"Question: {question}\n\nEvidence:\n{self._evidence_block(evidence)}",
        )
        quotes = [
            EvidenceQuote(
                passage_id=str(item.get("passage_id", "")),
                quote=str(item.get("quote", "")),
                claim_ref=str(item.get("claim_ref", "")),
            )
            for item in data.get("evidence", [])
        ]
        return DraftResult(answer_md=str(data.get("answer_md", "")), evidence=quotes, confidence=0.7)

    async def generate_answer(
        self,
        question: str,
        evidence: Sequence[EvidencePassage],
        conflict_notice: str | None = None,
    ) -> GenerationResult:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["answer_md", "cited_ids", "abstain"],
            "properties": {
                "answer_md": {"type": "string"},
                "cited_ids": {"type": "array", "items": {"type": "string"}},
                "abstain": {"type": "boolean"},
            },
        }
        conflict_text = f"\n\nA conflict was detected. Surface it honestly:\n{conflict_notice}" if conflict_notice else ""
        data = await self._chat_json(
            "answer",
            "grounded_answer",
            schema,
            "Answer a support question using only the supplied evidence. Cite passages inline as [P:<id>]. "
            "Never cite an id that is not supplied. If the evidence is insufficient, set abstain=true and return "
            "an empty answer. Never blend contradictory passages into a single claim."
            + conflict_text,
            f"Question: {question}\n\nEvidence:\n{self._evidence_block(evidence)}",
        )
        supplied = {item.evidence_id for item in evidence}
        cited = [cid for cid in data.get("cited_ids", []) if cid in supplied]
        return GenerationResult(
            answer_md=str(data.get("answer_md", "")),
            cited_ids=cited,
            abstain=bool(data.get("abstain")) or not data.get("answer_md"),
        )
