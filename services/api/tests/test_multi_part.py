from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.intelligence.base import GenerationResult, MultiGenerationResult, RewriteResult
from app.intelligence.dev import DevIntelligence
from app.intelligence.questions import question_part_count, split_question_parts
from app.retrieval.answer import AskRequest, ask
from app.retrieval.search import RetrievedPassage, SearchOutcome


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        internal_low_threshold=0.34,
        internal_high_threshold=0.52,
        public_low_threshold=0.55,
        public_high_threshold=0.72,
    )


def _passage(text: str, title: str = "Intel Core i5-9400") -> RetrievedPassage:
    return RetrievedPassage(
        passage_id=uuid4(),
        parent_id=None,
        child_text=text,
        parent_text=text,
        section_path="Specifications",
        page_ref=None,
        document_id=uuid4(),
        document_title=title,
        document_version=1,
        doc_type="product_description",
        effective_from=None,
        plan_scope=[],
        version_scope=[],
        source_id=uuid4(),
        source_title=title,
        source_tier="official_doc",
        visibility="internal",
        rerank_score=0.9,
        coverage=1.0,
    )


def test_compound_detection_does_not_split_ordinary_conjunctions() -> None:
    assert question_part_count("What do the terms and conditions say?") == 1
    assert question_part_count("What about refunds; cancellations; pricing?") == 1
    assert question_part_count(
        "When was the i5 9400 released? How many cores does it have?"
    ) == 2
    assert question_part_count(
        "When was the i5 9400 released and how many cores does it have?"
    ) == 2
    assert split_question_parts(
        "When was the i5 9400 released? How many cores does it have?"
    ) == ["When was the i5 9400 released?", "How many cores does it have?"]
    assert split_question_parts(
        "When was the i5 9400 released and how many cores does it have?"
    ) == ["When was the i5 9400 released", "how many cores does it have?"]
    # A full stop or newline is also a request boundary when the next sentence
    # starts with an interrogative.
    assert question_part_count(
        "When was the i5 9400 released. How many cores does it have"
    ) == 2
    assert split_question_parts(
        "When was the i5 9400 released. How many cores does it have"
    ) == ["When was the i5 9400 released.", "How many cores does it have"]
    assert question_part_count(
        "When was the i5 9400 released\nHow many cores does it have"
    ) == 2
    assert split_question_parts(
        "When was the i5 9400 released\nHow many cores does it have"
    ) == ["When was the i5 9400 released", "How many cores does it have"]


def test_boolean_answer_requires_the_question_attribute_and_matching_polarity() -> None:
    from app.retrieval.search import claim_supported_by_sentence, lexical_faithfulness

    question = "Does the i5 9400 CPU support hyper-threading?"
    ht_row = "| **Intel Hyper-Threading Technology** | No | Strictly 1 thread per physical core |"
    summary_row = "| **Cores / Threads** | 6 / 6 (No Hyper-Threading) |"
    other_no_row = "| **Intel TSX-NI** | No | Transactional Synchronization Extensions disabled |"

    assert claim_supported_by_sentence("No", ht_row, question)
    assert claim_supported_by_sentence("No.", ht_row, question)
    assert claim_supported_by_sentence("No, it does not support hyper-threading", ht_row, question)
    assert claim_supported_by_sentence("No, it does not support hyper-threading", summary_row, question)
    assert not claim_supported_by_sentence("No", other_no_row, question)
    assert not claim_supported_by_sentence("Yes", ht_row, question)
    assert lexical_faithfulness("No", [_passage(ht_row)], question=question) == 1.0
    assert lexical_faithfulness("No", [_passage(other_no_row)], question=question) == 0.0


def test_short_values_are_supported_only_by_the_same_cited_row() -> None:
    from app.retrieval.search import claim_supported_by_sentence

    cores = "| **Cores / Threads** | 6 / 6 (No Hyper-Threading) |"
    launch = "| **Launch Date** | January 7, 2019 |"

    assert claim_supported_by_sentence("6 cores", cores)
    assert claim_supported_by_sentence("6 cores.", cores)
    assert claim_supported_by_sentence("Launched on January 7, 2019", launch)
    assert claim_supported_by_sentence("Launched on January 7, 2019.", launch)
    assert claim_supported_by_sentence("January 7, 2019", launch)
    assert not claim_supported_by_sentence("8 cores", cores)
    assert not claim_supported_by_sentence("January 8, 2019", launch)
    # A bare value has no field context and can collide with another table row.
    assert not claim_supported_by_sentence("6", cores)


def test_table_row_rescue_maps_release_to_launch_date() -> None:
    from app.retrieval.answer import _table_row_fact

    passage = _passage(
        "| Attribute | Specification |\n| :--- | :--- |\n"
        "| **Product Name** | Intel Core i5-9400 |\n"
        "| **Launch Date** | January 7, 2019 |\n"
        "| **Cores / Threads** | 6 / 6 (No Hyper-Threading) |"
    )
    result = _table_row_fact("When was the i5 9400 released?", [passage])
    assert result is not None
    assert "Launch Date" in result.answer_md
    assert "January 7, 2019" in result.answer_md
    assert result.cited_ids == [str(passage.passage_id)]


def test_rewrite_returns_every_compound_part_and_resolves_the_subject() -> None:
    import asyncio

    from app.intelligence.openai_compat import OpenAICompatIntelligence

    provider = object.__new__(OpenAICompatIntelligence)
    provider.settings = SimpleNamespace(embedding_dim=1024, embedding_model="dev-hashed-v1")
    provider._lexical = DevIntelligence()
    provider._chat_json = AsyncMock(return_value={
        "standalone_question": (
            "When was the i5 9400 released? How many cores does the i5 9400 have?"
        ),
        "sub_questions": [
            "When was the i5 9400 released?",
            "How many cores does the i5 9400 have?",
        ],
        "intent": "factoid",
        "plan": "",
        "product_version": "",
        "error_codes": [],
        "entities": ["i5 9400"],
    })

    question = "When was the i5 9400 released? How many cores does it have?"
    assert provider._needs_model_rewrite(question, None)
    result = asyncio.run(provider.rewrite(question))
    assert result.sub_questions == [
        "When was the i5 9400 released?",
        "How many cores does the i5 9400 have?",
    ]
    assert "How many cores does the i5 9400 have?" in result.standalone_question
    provider._chat_json.assert_awaited_once()


def test_rewrite_retries_and_then_splits_when_a_part_is_omitted() -> None:
    import asyncio

    from app.intelligence.openai_compat import OpenAICompatIntelligence

    provider = object.__new__(OpenAICompatIntelligence)
    provider.settings = SimpleNamespace(embedding_dim=1024, embedding_model="dev-hashed-v1")
    provider._lexical = DevIntelligence()
    provider._chat_json = AsyncMock(side_effect=[
        {
            "standalone_question": "How many cores does the i5 9400 have?",
            "sub_questions": ["How many cores does the i5 9400 have?"],
            "intent": "factoid",
            "plan": "",
            "product_version": "",
            "error_codes": [],
            "entities": [],
        },
        {
            "standalone_question": "How many cores does the i5 9400 have?",
            "sub_questions": ["How many cores does the i5 9400 have?"],
            "intent": "factoid",
            "plan": "",
            "product_version": "",
            "error_codes": [],
            "entities": [],
        },
    ])

    result = asyncio.run(provider.rewrite(
        "When was the i5 9400 released? How many cores does it have?"
    ))
    assert provider._chat_json.await_count == 2
    assert result.sub_questions == [
        "When was the i5 9400 released?",
        "How many cores does it have?",
    ]


async def test_compound_ask_answers_every_part_without_a_single_canonical_shortcut(monkeypatch) -> None:
    import app.retrieval.answer as answer_module

    release = _passage("Launched on January 7, 2019.")
    cores = _passage(
        "| Attribute | Specification |\n| :--- | :--- |\n"
        "| **Cores / Threads** | 6 / 6 (No Hyper-Threading) |"
    )
    release_id = str(release.passage_id)
    cores_id = str(cores.passage_id)
    question = "When was the i5 9400 released? How many cores does it have?"
    intelligence = SimpleNamespace(
        name="test",
        rewrite=AsyncMock(return_value=RewriteResult(
            standalone_question=(
                "When was the i5 9400 released? How many cores does the i5 9400 have?"
            ),
            sub_questions=[
                "When was the i5 9400 released?",
                "How many cores does the i5 9400 have?",
            ],
        )),
        embed=AsyncMock(return_value=[[1.0]]),
        generate_multi_answer=AsyncMock(return_value=MultiGenerationResult([
            GenerationResult(
                f"Launched on January 7, 2019. [P:{release_id}]", [release_id]
            ),
            GenerationResult(
                f"| **Cores / Threads** | 6 / 6 (No Hyper-Threading) | [P:{cores_id}]",
                [cores_id],
            ),
        ])),
    )

    async def search(conn, q, embedding, **kwargs):
        return SearchOutcome([release] if "released" in q else [cores], 1, 1.0)

    monkeypatch.setattr(answer_module, "get_intelligence", lambda: intelligence)
    monkeypatch.setattr(answer_module, "hybrid_search", search)
    monkeypatch.setattr(answer_module, "_canonical_match", AsyncMock(return_value=(
        {"id": uuid4(), "question": "How many cores does the i5 9400 have?",
         "answer_md": "6 / 6", "current_version": 1}, 1.0,
    )))
    monkeypatch.setattr(answer_module, "_pending_conflicts", AsyncMock(return_value=[]))
    monkeypatch.setattr(answer_module, "_historical_accuracy", AsyncMock(return_value=0.7))
    monkeypatch.setattr(answer_module, "_log_qa", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(answer_module, "_searched_scope", AsyncMock(return_value="test"))
    monkeypatch.setattr(answer_module, "_upsert_gap", AsyncMock(return_value=uuid4()))

    result = await ask(None, None, _settings(), AskRequest(question))

    assert not result["abstained"]
    assert result["mode"] == "fallback"
    assert "January 7, 2019" in result["answer_md"]
    assert "6 / 6" in result["answer_md"]
    assert "**When was the i5 9400 released?**" in result["answer_md"]
    assert "**How many cores does the i5 9400 have?**" in result["answer_md"]
    assert len(result["citations"]) == 2
    assert all(citation["verified"] for citation in result["citations"])
    assert result["standalone_query"].count("?") == 2
    answer_module._canonical_match.assert_not_awaited()
    questions = intelligence.generate_multi_answer.await_args.args[0]
    assert questions == [
        "When was the i5 9400 released?",
        "How many cores does the i5 9400 have?",
    ]


def test_multi_generation_retries_missing_parts_and_keeps_completed_ones() -> None:
    import asyncio

    from app.intelligence.base import EvidencePassage
    from app.intelligence.openai_compat import OpenAICompatIntelligence

    provider = object.__new__(OpenAICompatIntelligence)
    provider.settings = SimpleNamespace(
        embedding_dim=1024,
        embedding_model="dev-hashed-v1",
    )
    provider._lexical = DevIntelligence()
    provider._chat_json = AsyncMock(side_effect=[
        {
            "parts": [
                {"part": 1, "answer_md": "Launched on January 7, 2019. [P:E1]", "cited_ids": ["E1"]},
            ],
            "abstain": False,
        },
        {
            "parts": [
                {"part": 1, "answer_md": "", "cited_ids": []},
                {"part": 2, "answer_md": "", "cited_ids": []},
            ],
            "abstain": False,
        },
    ])
    provider.generate_answer = AsyncMock(return_value=GenerationResult(
        "6 cores. [P:E1]", ["e1"]
    ))
    evidence = [EvidencePassage(evidence_id="e1", text="6 cores", title="CPU")]

    result = asyncio.run(provider.generate_multi_answer(
        ["When was it released?", "How many cores does it have?"], evidence
    ))

    assert provider._chat_json.await_count == 2
    assert result.answers[0].answer_md == "Launched on January 7, 2019. [P:e1]"
    assert result.answers[1].answer_md == "6 cores. [P:E1]"
    provider.generate_answer.assert_awaited_once()


async def test_compound_ask_falls_back_to_an_individual_part_answer(monkeypatch) -> None:
    import app.retrieval.answer as answer_module

    release = _passage("Launched on January 7, 2019.")
    cores = _passage(
        "| Attribute | Specification |\n| :--- | :--- |\n"
        "| **Cores / Threads** | 6 / 6 (No Hyper-Threading) |"
    )
    release_id = str(release.passage_id)
    cores_id = str(cores.passage_id)
    intelligence = SimpleNamespace(
        name="test",
        rewrite=AsyncMock(return_value=RewriteResult(
            standalone_question=(
                "When was the i5 9400 released? How many cores does the i5 9400 have?"
            ),
            sub_questions=[
                "When was the i5 9400 released?",
                "How many cores does the i5 9400 have?",
            ],
        )),
        embed=AsyncMock(return_value=[[1.0], [1.0]]),
        generate_multi_answer=AsyncMock(return_value=MultiGenerationResult([
            GenerationResult(
                f"Launched on January 7, 2019. [P:{release_id}]", [release_id]
            ),
        ])),
        generate_answer=AsyncMock(return_value=GenerationResult(
            f"| **Cores / Threads** | 6 / 6 (No Hyper-Threading) | [P:{cores_id}]",
            [cores_id],
        )),
    )

    async def search(conn, q, embedding, **kwargs):
        return SearchOutcome([release] if "released" in q else [cores], 1, 1.0)

    monkeypatch.setattr(answer_module, "get_intelligence", lambda: intelligence)
    monkeypatch.setattr(answer_module, "hybrid_search", search)
    monkeypatch.setattr(answer_module, "_pending_conflicts", AsyncMock(return_value=[]))
    monkeypatch.setattr(answer_module, "_historical_accuracy", AsyncMock(return_value=0.7))
    monkeypatch.setattr(answer_module, "_log_qa", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(answer_module, "_searched_scope", AsyncMock(return_value="test"))
    monkeypatch.setattr(answer_module, "_upsert_gap", AsyncMock(return_value=uuid4()))

    result = await ask(
        None, None, _settings(),
        AskRequest("When was the i5 9400 released? How many cores does it have?"),
    )

    assert not result["abstained"]
    assert "January 7, 2019" in result["answer_md"]
    assert "6 / 6" in result["answer_md"]
    assert "I couldn't find current evidence" not in result["answer_md"]
    intelligence.generate_answer.assert_awaited_once()


async def test_compound_ask_marks_a_part_without_evidence(monkeypatch) -> None:
    import app.retrieval.answer as answer_module

    release = _passage("Launched on January 7, 2019.")
    release_id = str(release.passage_id)
    intelligence = SimpleNamespace(
        name="test",
        rewrite=AsyncMock(return_value=RewriteResult(
            standalone_question=(
                "When was the i5 9400 released? What is its L4 cache size?"
            ),
            sub_questions=[
                "When was the i5 9400 released?",
                "What is the L4 cache size of the i5 9400?",
            ],
        )),
        embed=AsyncMock(return_value=[[1.0]]),
        generate_multi_answer=AsyncMock(return_value=MultiGenerationResult([
            GenerationResult(
                f"Launched on January 7, 2019. [P:{release_id}]", [release_id]
            ),
        ])),
    )

    async def search(conn, q, embedding, **kwargs):
        return SearchOutcome([release], 1, 1.0) if "released" in q else SearchOutcome([], 0, 0.0)

    monkeypatch.setattr(answer_module, "get_intelligence", lambda: intelligence)
    monkeypatch.setattr(answer_module, "hybrid_search", search)
    monkeypatch.setattr(answer_module, "_pending_conflicts", AsyncMock(return_value=[]))
    monkeypatch.setattr(answer_module, "_historical_accuracy", AsyncMock(return_value=0.7))
    monkeypatch.setattr(answer_module, "_log_qa", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(answer_module, "_searched_scope", AsyncMock(return_value="test"))
    gap = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(answer_module, "_upsert_gap", gap)

    result = await ask(
        None, None, _settings(),
        AskRequest("When was the i5 9400 released? What is its L4 cache size?"),
    )

    assert not result["abstained"]
    assert result["mode"] == "fallback"
    assert result["confidence_band"] == "medium"
    assert "January 7, 2019" in result["answer_md"]
    assert "I couldn't find current evidence to answer this part." in result["answer_md"]
    assert result["gap_id"]
    gap.assert_awaited_once()
    gap.assert_awaited_with(
        None, None, "What is the L4 cache size of the i5 9400?", "web"
    )


async def test_public_compound_question_abstains_without_a_full_approved_answer(monkeypatch) -> None:
    import app.retrieval.answer as answer_module

    intelligence = SimpleNamespace(
        name="test",
        rewrite=AsyncMock(return_value=RewriteResult(
            standalone_question=(
                "When was the i5 9400 released? How many cores does the i5 9400 have?"
            ),
            sub_questions=[
                "When was the i5 9400 released?",
                "How many cores does the i5 9400 have?",
            ],
        )),
        embed=AsyncMock(return_value=[[1.0]]),
        generate_multi_answer=AsyncMock(),
    )
    monkeypatch.setattr(answer_module, "get_intelligence", lambda: intelligence)
    monkeypatch.setattr(answer_module, "hybrid_search", AsyncMock())
    monkeypatch.setattr(answer_module, "_log_qa", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(answer_module, "_searched_scope", AsyncMock(return_value="public"))
    gap = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(answer_module, "_upsert_gap", gap)

    result = await ask(
        None, None, _settings(),
        AskRequest(
            "When was the i5 9400 released? How many cores does it have?",
            channel="public",
        ),
    )

    assert result["abstained"]
    assert result["abstention_reason"] == (
        "No approved public answer covers every part of this question."
    )
    assert gap.await_count == 2
    intelligence.generate_multi_answer.assert_not_awaited()
