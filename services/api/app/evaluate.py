"""Evaluate a synthetic corpus without committing changes to the application database.

Run from services/api: python -m app.evaluate --split development --output evaluation/baseline.json
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .answers_service import approve_answer, create_answer
from .config import ROOT, get_settings
from .deps import Session
from .ingest.pipeline import enqueue_job, quote_in_text, run_ingest
from .ingest.storage import save_bytes
from .retrieval.answer import AskRequest, ask

CORPUS = ROOT / "services" / "api" / "evaluation" / "corpus.json"


class _RollbackEvaluation(Exception):
    """Force rollback of the synthetic tenant and all evaluation writes."""


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(percentile * len(ordered)) - 1)], 1)


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


async def _fetchone(conn, sql: str, params=()):
    return await (await conn.execute(sql, params)).fetchone()


async def _ingest_corpus(conn, tenant_id: UUID, corpus: dict) -> tuple[dict[str, UUID], list[dict]]:
    source_ids = {}
    timings = []
    for source in corpus["sources"]:
        source_id = uuid4()
        data = source["text"].encode("utf-8")
        path = save_bytes(tenant_id, source_id, source["filename"], data)
        digest = hashlib.sha256(data).hexdigest()
        await conn.execute(
            """insert into sources
                 (id, tenant_id, kind, title, storage_path, mime_type, size_bytes, hash_sha256,
                  source_tier, visibility, status, metadata, plan_scope, product_version_scope)
               values (%s, %s, %s, %s, %s, %s, %s, %s, 'official_doc', %s, 'processing', %s, %s, %s)""",
            (source_id, tenant_id, "markdown" if source["filename"].endswith(".md") else "text",
             source["title"], path, "text/plain", len(data), digest, source["visibility"],
             Jsonb({"filename": source["filename"]}), source.get("plan_scope", []),
             source.get("version_scope", [])),
        )
        job_id = await enqueue_job(conn, tenant_id, source_id, f"evaluation:{source_id}")
        started = time.perf_counter()
        await run_ingest(tenant_id, source_id, job_id, auto_propose=False)
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        job = await _fetchone(conn, "select status, stage, error from ingest_jobs where id = %s", (job_id,))
        if not job or job["status"] != "succeeded":
            raise RuntimeError(f"Evaluation ingestion failed for {source['id']}: {job}")
        source_ids[source["id"]] = source_id
        timings.append({"source": source["id"], "elapsed_ms": elapsed})
    return source_ids, timings


async def _create_answers(conn, session: Session, corpus: dict, sources: dict[str, UUID]) -> dict[str, UUID]:
    answers = {}
    for item in corpus["canonical_answers"]:
        rows = await (await conn.execute(
            """select p.id, p.text from passages p join documents d on d.id = p.document_id
               where d.source_id = %s and p.level = 'chunk' and p.status = 'current'
               order by p.chunk_index""",
            (sources[item["source"]],),
        )).fetchall()
        passage = next((row for row in rows if quote_in_text(item["quote"], row["text"])), None)
        if passage is None:
            raise RuntimeError(f"Expected quote missing from source {item['source']}")
        answer = await create_answer(
            conn, session, question=item["question"], answer_md=item["answer"], topic_slug=None,
            plan_scope=item.get("plan_scope", []), version_scope=item.get("version_scope", []),
            visibility=item["visibility"], evidence=[{"passage_id": passage["id"], "quote": item["quote"]}],
        )
        await approve_answer(conn, session, answer["id"])
        answers[item["id"]] = answer["id"]
    return answers


async def _run_case(conn, session: Session, settings, case: dict, sources: dict[str, UUID], answers: dict[str, UUID]) -> dict:
    request = AskRequest(
        question=case["question"], channel=case["channel"], plan=case.get("plan"),
        product_version=case.get("product_version"),
        as_of=date.fromisoformat(case["as_of"]) if case.get("as_of") else None,
    )
    # Ask creates logs, gaps, and possibly drafts. Roll each case back independently
    # so earlier questions cannot influence later ones or historical scores.
    await conn.execute("savepoint eval_case")
    try:
        started = time.perf_counter()
        result = await ask(conn, session, settings, request)
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        log = await _fetchone(conn, "select retrieved_passage_ids from qa_logs where id = %s", (result["qa_log_id"],))
        ids = log["retrieved_passage_ids"] if log else []
        rows = await (await conn.execute(
            """select p.id, p.text, d.source_id, s.visibility from passages p
               join documents d on d.id = p.document_id join sources s on s.id = d.source_id
               where p.id = any(%s::uuid[])""", (ids,),
        )).fetchall() if ids else []
        by_id = {row["id"]: row for row in rows}
        source_labels = {value: key for key, value in sources.items()}
        relevant = case.get("relevant_evidence", [])
        ranks = []
        retrieved = []
        for rank, pid in enumerate(ids, start=1):
            row = by_id.get(pid)
            if not row:
                continue
            label = source_labels.get(row["source_id"], "unknown")
            retrieved.append({"rank": rank, "source": label, "visibility": row["visibility"]})
            if any(label == item["source"] and quote_in_text(item["quote"], row["text"]) for item in relevant):
                ranks.append(rank)
        citations = []
        for citation in result.get("citations", []):
            row = await _fetchone(conn, "select text from passages where id = %s", (citation["passage_id"],))
            quote = citation.get("quote") or ""
            citations.append({
                "source_title": citation.get("source_title"), "quote": quote,
                "reported_verified": citation.get("verified"),
                "quote_exists_in_passage": bool(row and quote and quote_in_text(quote, row["text"])),
            })
        expected_id = answers.get(case.get("expected_canonical"))
        return {
            "id": case["id"], "question": case["question"], "channel": case["channel"],
            "plan": case.get("plan"), "product_version": case.get("product_version"),
            "expected_behavior": case["expected_behavior"], "expected_answer": case.get("expected_answer"),
            "expected_canonical": case.get("expected_canonical"), "expected_evidence": relevant,
            "actual_mode": result["mode"], "abstained": result["abstained"],
            "dense_scan": result.get("dense_scan"),
            "abstention_reason": result.get("abstention_reason"), "answer": result.get("answer_md"),
            "canonical_match": (str(expected_id) == result.get("answer_id")) if expected_id else None,
            "relevant_first_rank": min(ranks) if ranks else None,
            "retrieved": retrieved, "citations": citations, "elapsed_ms": elapsed,
            "manual_review_required": "Check answer correctness, claim support, and time qualification; quote presence is not entailment.",
        }
    finally:
        await conn.execute("rollback to savepoint eval_case")
        await conn.execute("release savepoint eval_case")


def _summarize(results: list[dict]) -> dict:
    answerable = [r for r in results if r["expected_behavior"] == "answer"]
    unanswerable = [r for r in results if r["expected_behavior"] == "abstain"]
    relevant = [r for r in results if r["expected_evidence"]]
    canonical = [r for r in results if r["expected_canonical"]]
    quotes = [c for r in results for c in r["citations"]]
    return {
        "cases": len(results),
        "answerable_cases": len(answerable),
        "false_abstention_rate": _ratio(sum(r["abstained"] for r in answerable), len(answerable)),
        "abstention_cases": len(unanswerable),
        "false_answer_rate": _ratio(sum(not r["abstained"] for r in unanswerable), len(unanswerable)),
        "expected_canonical_cases": len(canonical),
        "expected_canonical_selected_rate": _ratio(sum(r["canonical_match"] is True for r in canonical), len(canonical)),
        "retrieval_labeled_cases": len(relevant),
        "evidence_hit_at_1": _ratio(sum(r["relevant_first_rank"] == 1 for r in relevant), len(relevant)),
        "evidence_hit_at_5": _ratio(sum(r["relevant_first_rank"] is not None and r["relevant_first_rank"] <= 5 for r in relevant), len(relevant)),
        "evidence_mrr": round(sum(1 / r["relevant_first_rank"] if r["relevant_first_rank"] else 0 for r in relevant) / len(relevant), 3) if relevant else None,
        "citation_quotes": len(quotes),
        "citation_quote_presence_rate": _ratio(sum(c["quote_exists_in_passage"] for c in quotes), len(quotes)),
        "dense_scan_modes": {
            mode: sum(1 for r in results if r.get("dense_scan") == mode)
            for mode in ("exact", "hnsw", "none")
        },
        "ask_wall_ms_p50": _percentile([r["elapsed_ms"] for r in results], 0.5),
        "ask_wall_ms_p95": _percentile([r["elapsed_ms"] for r in results], 0.95),
    }


async def evaluate(corpus: dict, split: str) -> dict:
    settings = get_settings()
    settings.validate_provider()
    tenant_id, user_id = uuid4(), uuid4()
    session = Session(tenant_id=tenant_id, tenant_slug=f"evaluation-{tenant_id}", tenant_name="Evaluation",
                      user_id=user_id, user_name="Evaluator", email="eval@example.invalid", role="admin", title="Evaluator")
    selected = [case for case in corpus["cases"] if case["split"] == split]
    if not selected:
        raise ValueError(f"No cases in split {split}")
    report = None
    # Keep temporary originals under the project storage directory and never use
    # the destructive seed/reset command. The DB transaction always rolls back.
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="evaluation-", dir=settings.storage_dir) as directory:
        original_storage = settings.storage_dir
        settings.storage_dir = Path(directory)
        try:
            async with await psycopg.AsyncConnection.connect(settings.database_url, row_factory=dict_row) as conn:
                try:
                    async with conn.transaction():
                        await conn.execute("select set_config('app.tenant_id', %s, true)", (str(tenant_id),))
                        await conn.execute("insert into tenants (id, slug, name) values (%s, %s, %s)",
                                           (tenant_id, session.tenant_slug, session.tenant_name))
                        await conn.execute("insert into users (id, tenant_id, email, name, role, title) values (%s, %s, %s, %s, %s, %s)",
                                           (user_id, tenant_id, session.email, session.user_name, session.role, session.title))

                        @asynccontextmanager
                        async def isolated_conn(_tenant_id):
                            if _tenant_id != tenant_id:
                                raise RuntimeError("Unexpected evaluation tenant")
                            yield conn

                        with patch("app.ingest.pipeline.tenant_conn", isolated_conn):
                            sources, ingestion = await _ingest_corpus(conn, tenant_id, corpus)
                        answers = await _create_answers(conn, session, corpus, sources)
                        results = [await _run_case(conn, session, settings, case, sources, answers) for case in selected]
                        report = {
                            "created_at_utc": datetime.now(timezone.utc).isoformat(),
                            "corpus_version": corpus["version"],
                            "corpus_sha256": hashlib.sha256(CORPUS.read_bytes()).hexdigest(),
                            "split": split,
                            "configuration": {
                                "ai_mode": settings.ai_mode, "embedding_provider": settings.embedding_provider,
                                "embedding_model": settings.active_embedding_model, "embedding_version": settings.embedding_version,
                                "embedding_dim": settings.embedding_dim, "model_ingest_small": settings.model_ingest_small,
                                "model_rewrite": settings.model_rewrite, "model_answer": settings.model_answer,
                                "model_rerank": settings.model_rerank or "lexical fallback",
                                "local_embedding_device": settings.local_embedding_device if settings.embedding_provider == "local" else None,
                                "dense_exact_max_chunks": settings.dense_exact_max_chunks,
                                "max_children_per_parent": settings.max_children_per_parent,
                                "internal_thresholds": [settings.internal_low_threshold, settings.internal_high_threshold],
                                "public_thresholds": [settings.public_low_threshold, settings.public_high_threshold],
                            },
                            "limitations": [
                                "Synthetic starter corpus only; not a representative accuracy estimate.",
                                "Evidence hit and MRR use the final returned passage ranking, not pre-rerank candidates.",
                                "Quote presence does not establish claim support. Answer correctness requires manual review.",
                                "Wall times include sequential cases with mixed cold and warm model state; no hardware resource measurements.",
                                "This run uses a rollback-isolated tenant and does not test HTTP upload, worker polling, authentication, or cross-tenant isolation.",
                            ],
                            "ingestion": ingestion, "summary": _summarize(results), "cases": results,
                        }
                        raise _RollbackEvaluation()
                except _RollbackEvaluation:
                    pass
        finally:
            settings.storage_dir = original_storage
    assert report is not None
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("development", "heldout"), default="development")
    parser.add_argument("--output", type=Path, help="Write JSON report to this project-relative path instead of stdout")
    args = parser.parse_args()
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    report = asyncio.run(evaluate(corpus, args.split))
    output = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        path = (Path.cwd() / args.output).resolve()
        if ROOT.resolve() not in path.parents:
            parser.error("Output must be inside the project")
        if path.exists():
            parser.error("Output already exists; choose a new path")
        path.write_text(output, encoding="utf-8")
        print(f"Evaluation report written to {path.relative_to(ROOT)}")
        print(json.dumps(report["summary"], indent=2))
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
