from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from ..deps import Context, get_context
from ..errors import validation
from ..intelligence import get_intelligence
from ..retrieval.search import hybrid_search

router = APIRouter(tags=["search"])


@router.get("/search")
async def search_endpoint(
    q: str,
    plan: str | None = None,
    product_version: str | None = None,
    topic: str | None = None,
    as_of: date | None = None,
    exact: bool = False,
    ctx: Context = Depends(get_context),
):
    if len(q.strip()) < 2:
        raise validation("Enter at least two characters to search.")
    intelligence = get_intelligence()
    embedding = None
    if not exact:
        embedding = (await intelligence.embed([q], task="query"))[0]
    outcome = await hybrid_search(
        ctx.conn,
        q,
        embedding,
        channel="internal",
        plan=plan,
        product_version=product_version,
        as_of=as_of,
        exact=exact,
        topic_slug=topic,
    )
    results = [
        {
            "passage_id": str(p.passage_id),
            "parent_id": str(p.parent_id) if p.parent_id else None,
            "text": p.parent_text,
            "matched_text": p.child_text,
            "section_path": p.section_path,
            "document_id": str(p.document_id),
            "source_id": str(p.source_id),
            "document_title": p.document_title,
            "source_title": p.source_title,
            "source_tier": p.source_tier,
            "visibility": p.visibility,
            "topic_id": str(p.topic_id) if p.topic_id else None,
            "topic_name": p.topic_name,
            "document_version": p.document_version,
            "score": round(p.rerank_score, 4),
            "coverage": round(p.coverage, 3),
        }
        for p in outcome.passages
        if p.rerank_score > 0.01
    ]
    return {
        "query": q,
        "results": results,
        "candidates_scanned": outcome.candidates_scanned,
        "coverage": round(outcome.query_coverage, 3),
        "exact": exact,
        "retrieval_mode": outcome.dense_index,
        "index_warning": outcome.index_warning,
    }
