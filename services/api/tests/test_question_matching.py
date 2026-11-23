from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.intelligence.dev import DevIntelligence
from app.retrieval.answer import _canonical_match, _equivalent_question_rows, _question_safety_key


@pytest.mark.asyncio
async def test_offline_equivalence_is_conservative() -> None:
    intelligence = DevIntelligence()
    candidates = ["How old is Mikey?", "When was Mikey born?", "How old is Alice?", "How old was Mikey in 2020?"]
    assert await intelligence.equivalent_questions("What is Mikey's age?", candidates) == [0]


@pytest.mark.asyncio
async def test_equivalence_preserves_entities_negation_dates_and_scope(monkeypatch) -> None:
    import app.retrieval.answer as answer_module

    # A model saying yes to everything must not override deterministic guards.
    matcher = AsyncMock(side_effect=lambda q, c: list(range(len(c))))
    monkeypatch.setattr(answer_module, "get_intelligence", lambda: SimpleNamespace(equivalent_questions=matcher))
    rows = [{"question": q} for q in (
        "How old is Mikey?", "How old is Alice?", "How old was Mikey in 2020?", "How old is Mikey not?",
    )]
    assert await _equivalent_question_rows("What is Mikey's age?", rows) == [rows[0]]
    assert _question_safety_key("Does the Team plan allow refunds?") != _question_safety_key("Does the Business plan allow refunds?")
    assert _question_safety_key("Can I request a refund before 30 days?") != _question_safety_key("Can I request a refund after 30 days?")
    assert _question_safety_key("How old is Mikey?") != _question_safety_key("How old was Mikey?")
    refund_rows = [{"question": "What is the refund window?"}, {"question": "How do I request a refund?"}]
    assert await _equivalent_question_rows("How can I request a refund?", refund_rows) == [refund_rows[1]]


@pytest.mark.asyncio
async def test_generic_hardware_suffix_is_not_a_new_question(monkeypatch) -> None:
    import app.retrieval.answer as answer_module

    matcher = AsyncMock(return_value=[])
    monkeypatch.setattr(answer_module, "get_intelligence", lambda: SimpleNamespace(equivalent_questions=matcher))
    without_suffix = [{"question": "How many cores does the i5 9400 have?"}]
    with_suffix = [{"question": "How many cores does the i5 9400 CPU have?"}]

    assert await _equivalent_question_rows(with_suffix[0]["question"], without_suffix) == without_suffix
    assert await _equivalent_question_rows(without_suffix[0]["question"], with_suffix) == with_suffix
    # Deterministic content matching must not depend on a model opinion.
    matcher.assert_not_awaited()


@pytest.mark.asyncio
async def test_deterministic_reuse_still_requires_the_same_requested_fact(monkeypatch) -> None:
    import app.retrieval.answer as answer_module

    matcher = AsyncMock(return_value=[])
    monkeypatch.setattr(answer_module, "get_intelligence", lambda: SimpleNamespace(equivalent_questions=matcher))

    assert await _equivalent_question_rows(
        "How many cores does the i5 9400 have?",
        [{"question": "How many cores does the i5 9400 need?"}],
    ) == []
    assert await _equivalent_question_rows(
        "Does the i5 9400 support 128 GB?",
        [{"question": "Does the i5 9400 support 64 GB?"}],
    ) == []


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, rows):
        self.rows = rows
        self.params = None

    async def execute(self, sql, params):
        self.params = params
        return Cursor(self.rows)


@pytest.mark.asyncio
async def test_canonical_match_uses_question_not_shared_evidence(monkeypatch) -> None:
    import app.retrieval.answer as answer_module

    rows = [
        {"id": uuid4(), "question": "What is the refund window?", "plan_scope": [], "version_scope": []},
        {"id": uuid4(), "question": "How do I request a refund?", "plan_scope": [], "version_scope": []},
    ]
    monkeypatch.setattr(answer_module, "get_intelligence", lambda: SimpleNamespace(
        equivalent_questions=AsyncMock(return_value=[0]),
    ))
    conn = Connection(rows)
    answer, score = await _canonical_match(conn, "How can I request a refund?", [], None, None, None, None, "web")
    assert answer["id"] == rows[1]["id"]
    assert score == 1.0
    assert conn.params[0] == ["internal", "public"]


@pytest.mark.asyncio
async def test_equivalence_requires_confirmation_in_both_directions(monkeypatch) -> None:
    import app.retrieval.answer as answer_module

    matcher = AsyncMock(side_effect=[[0], []])
    monkeypatch.setattr(answer_module, "get_intelligence", lambda: SimpleNamespace(equivalent_questions=matcher))
    assert await _equivalent_question_rows("What is Mikey's age?", [{"question": "How old is Mikey?"}]) == []


@pytest.mark.asyncio
async def test_canonical_match_chooses_one_when_duplicate_answers_agree(monkeypatch) -> None:
    import app.retrieval.answer as answer_module

    older = {
        "id": uuid4(),
        "question": "How many cores does the i5 9400 CPU have?",
        "plan_scope": [],
        "version_scope": [],
        "answer_md": "6 / 6",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    newer = {
        "id": uuid4(),
        "question": "How many cores does the i5 9400 have?",
        "plan_scope": [],
        "version_scope": [],
        "answer_md": "6 / 6",
        "created_at": datetime(2026, 2, 1, tzinfo=timezone.utc),
    }
    monkeypatch.setattr(answer_module, "get_intelligence", lambda: SimpleNamespace(
        equivalent_questions=AsyncMock(return_value=[0, 1]),
    ))
    question = "How many cores does the i5 9400 processor have?"
    answer, score = await _canonical_match(
        Connection([older, newer]), question, [], None, None, None, None, "web"
    )
    assert answer["id"] == older["id"] and score == 1.0

    newer["answer_md"] = "8 / 8"
    answer, score = await _canonical_match(
        Connection([older, newer]), question, [], None, None, None, None, "web"
    )
    assert answer is None and score == 0.0


@pytest.mark.asyncio
async def test_canonical_match_refuses_ambiguous_equivalent_answers(monkeypatch) -> None:
    import app.retrieval.answer as answer_module

    rows = [{"id": uuid4(), "question": "How old is Mikey?", "plan_scope": [], "version_scope": []},
            {"id": uuid4(), "question": "What is Mikey's age?", "plan_scope": [], "version_scope": []}]
    monkeypatch.setattr(answer_module, "get_intelligence", lambda: DevIntelligence())
    answer, score = await _canonical_match(Connection(rows), "What is the age of Mikey?", [], None, None, None, None, "web")
    assert answer is None and score == 0.0
