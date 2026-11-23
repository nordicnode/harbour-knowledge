import json
from uuid import uuid4

import pytest

from app.deps import Session
from app.evaluate import CORPUS, _percentile, _run_case, _summarize


def test_corpus_has_unique_cases_and_separate_splits() -> None:
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    ids = [case["id"] for case in corpus["cases"]]
    assert len(ids) == len(set(ids))
    assert {case["split"] for case in corpus["cases"]} == {"development", "heldout"}
    sources = {source["id"] for source in corpus["sources"]}
    for case in corpus["cases"]:
        assert case["expected_behavior"] in {"answer", "abstain"}
        assert all(evidence["source"] in sources for evidence in case["relevant_evidence"])


def test_metrics_report_absent_denominators_and_final_ranks() -> None:
    result = _summarize([
        {"expected_behavior": "answer", "abstained": True, "expected_evidence": [{}],
         "expected_canonical": None, "relevant_first_rank": 2, "citations": [], "elapsed_ms": 10.0},
        {"expected_behavior": "answer", "abstained": False, "expected_evidence": [{}],
         "expected_canonical": "a", "canonical_match": False, "relevant_first_rank": None,
         "citations": [{"quote_exists_in_passage": True}], "elapsed_ms": 20.0},
    ])
    assert result["false_abstention_rate"] == 0.5
    assert result["false_answer_rate"] is None
    assert result["expected_canonical_selected_rate"] == 0.0
    assert result["evidence_hit_at_5"] == 0.5
    assert result["evidence_mrr"] == 0.25
    assert result["citation_quote_presence_rate"] == 1.0
    assert result["ask_wall_ms_p95"] == 20.0
    assert _percentile([], 0.95) is None


@pytest.mark.asyncio
async def test_case_rolls_back_on_ask_failure(monkeypatch) -> None:
    class Connection:
        def __init__(self):
            self.commands = []

        async def execute(self, sql, params=()):
            self.commands.append(sql)

    async def fail(*args):
        raise RuntimeError("provider failed")

    monkeypatch.setattr("app.evaluate.ask", fail)
    conn = Connection()
    tenant, user = uuid4(), uuid4()
    session = Session(tenant, "eval", "Eval", user, "User", "test@example.invalid", "admin", "Admin")
    with pytest.raises(RuntimeError, match="provider failed"):
        await _run_case(conn, session, None, {"question": "Test?", "channel": "web"}, {}, {})
    assert conn.commands == ["savepoint eval_case", "rollback to savepoint eval_case", "release savepoint eval_case"]
