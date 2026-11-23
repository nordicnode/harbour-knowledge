"""Opt-in PostgreSQL integration tests using an isolated temporary tenant.

Run with HARBOUR_TEST_POSTGRES=1 from services/api against a migrated database.

Stop the API first, or run these against a separate database: `queued_ingest_jobs`
is SECURITY DEFINER on purpose, so a live ingestion worker sees a test tenant's
queued job and races this suite for it (observed as "DID NOT RAISE" and a tenant
delete deadlock). Holding the worker's advisory lock also parks it:

    KEY=$(python3 -c 'print(0x48415242)')   # LEADER_LOCK_KEY
    psql "$MIGRATION_DATABASE_URL" -c "select pg_advisory_lock($KEY)" -c 'select pg_sleep(120)' &
"""
import asyncio
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg.rows import dict_row

from app.config import get_settings
from app.db import fetch_all, fetch_one, fetch_val
from app.deps import Context, Session
from app.ingest import pipeline
from app.intelligence.dev import DevIntelligence
from app.routers.sources import _job_key, _withdraw_sources, reingest_source

pytestmark = pytest.mark.skipif(
    os.environ.get("HARBOUR_TEST_POSTGRES") != "1",
    reason="set HARBOUR_TEST_POSTGRES=1 to run the isolated PostgreSQL integration tests",
)


@pytest_asyncio.fixture
async def workspace(monkeypatch):
    settings = get_settings()
    tenant_id, user_id, source_id = uuid4(), uuid4(), uuid4()
    original_storage = settings.storage_dir
    original_storage.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ingest-test-", dir=original_storage) as directory:
        monkeypatch.setattr(settings, "storage_dir", Path(directory))
        monkeypatch.setattr(pipeline, "get_intelligence", lambda: DevIntelligence(settings.embedding_dim))

        @asynccontextmanager
        async def tenant_connection(_tenant_id):
            assert _tenant_id == tenant_id
            async with await psycopg.AsyncConnection.connect(settings.database_url, row_factory=dict_row) as conn:
                async with conn.transaction():
                    await conn.execute("select set_config('app.tenant_id', %s, true)", (str(tenant_id),))
                    yield conn

        monkeypatch.setattr(pipeline, "tenant_conn", tenant_connection)
        session = Session(tenant_id, f"test-{tenant_id}", "Test", user_id, "Test", "test@example.invalid", "admin", "Admin")
        async with tenant_connection(tenant_id) as conn:
            await conn.execute("insert into tenants (id, slug, name) values (%s, %s, 'Test')", (tenant_id, session.tenant_slug))
            await conn.execute("insert into users (id, tenant_id, email, name, role) values (%s, %s, %s, 'Test', 'admin')", (user_id, tenant_id, session.email))
            await conn.execute("""insert into sources
                (id, tenant_id, kind, title, origin_url, hash_sha256, status)
                values (%s, %s, 'url', 'Test source', 'https://example.com/source.md', 'initial', 'processing')""",
                (source_id, tenant_id))
            job_id = await pipeline.enqueue_job(conn, tenant_id, source_id, _job_key(source_id, "initial"))
        try:
            yield tenant_id, source_id, job_id, tenant_connection, session, settings
        finally:
            async with tenant_connection(tenant_id) as conn:
                await conn.execute("delete from tenants where id = %s", (tenant_id,))


def mock_url(monkeypatch, text):
    async def fetch(*args):
        return text.encode(), "text/markdown", "https://example.com/source.md"
    monkeypatch.setattr(pipeline, "fetch_url", fetch)


async def state(workspace):
    tenant, source_id, job_id, connection, _, _ = workspace
    async with connection(tenant) as conn:
        source = await fetch_one(conn, "select * from sources where id = %s", (source_id,))
        job = await fetch_one(conn, "select * from ingest_jobs where id = %s", (job_id,))
        docs = await fetch_all(conn, "select id, version, status, content_md from documents where source_id = %s order by version", (source_id,))
        passages = await fetch_all(conn, "select p.id, p.status, p.text from passages p join documents d on d.id = p.document_id where d.source_id = %s order by p.id", (source_id,))
        return source, job, docs, passages


async def requeue(workspace):
    tenant, source_id, job_id, connection, session, settings = workspace
    async with connection(tenant) as conn:
        result = await reingest_source(source_id, Context(session, conn, settings))
        # URL snapshot hashes change; a replacement may have a fresh job key.
        job_id = UUID(result["job_id"])
    return (tenant, source_id, job_id, connection, session, settings)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["provider", "sql", "invalid_vectors"])
async def test_failed_replacement_preserves_previous_version(workspace, monkeypatch, failure):
    tenant, source_id, job_id, connection, session, settings = workspace
    mock_url(monkeypatch, "# Reference\n\nThe service supports 10 users.")
    await pipeline.run_ingest(tenant, source_id, job_id, auto_propose=False)
    source, job, docs, passages = await state(workspace)
    assert job["status"] == "succeeded"
    old_path = source["storage_path"]
    old_bytes = (settings.storage_dir / old_path).read_bytes()
    assert old_bytes and len(docs) == 1 and passages

    workspace = await requeue(workspace)
    tenant, source_id, job_id, connection, session, settings = workspace
    mock_url(monkeypatch, "# Reference\n\nThe service supports 25 users.")
    original_detect_conflicts = pipeline.detect_conflicts
    if failure == "invalid_vectors":
        class Incomplete(DevIntelligence):
            async def embed(self, texts, task="document"):
                return []
        monkeypatch.setattr(pipeline, "get_intelligence", lambda: Incomplete(settings.embedding_dim))
    else:
        async def fail(conn, *args):
            if failure == "sql":
                await conn.execute("select 1 / 0")
            raise RuntimeError("provider failed")
        monkeypatch.setattr(pipeline, "detect_conflicts", fail)

    await pipeline.run_ingest(tenant, source_id, job_id, auto_propose=False)
    new_source, job, new_docs, new_passages = await state(workspace)
    assert job["status"] == "failed" and job["finished_at"] is not None
    assert job["stage"] == ("contextualise_embed_index" if failure == "invalid_vectors" else "conflict_scan")
    assert job["error"]
    assert new_source["status"] == "active" and new_source["storage_path"] == old_path
    assert (settings.storage_dir / old_path).read_bytes() == old_bytes
    assert new_docs == docs and new_passages == passages
    assert all(p["status"] == "current" for p in new_passages)
    assert len(list((settings.storage_dir / str(tenant)).iterdir())) == 1

    # Once the underlying problem is fixed, retry must switch complete versions.
    monkeypatch.setattr(pipeline, "get_intelligence", lambda: DevIntelligence(settings.embedding_dim))
    monkeypatch.setattr(pipeline, "detect_conflicts", original_detect_conflicts)
    workspace = await requeue(workspace)
    await pipeline.run_ingest(tenant, source_id, workspace[2], auto_propose=False)
    source, job, docs, passages = await state(workspace)
    assert job["status"] == "succeeded" and source["status"] == "active"
    assert [(d["version"], d["status"]) for d in docs] == [(1, "superseded"), (2, "current")]
    assert source["storage_path"] != old_path and (settings.storage_dir / old_path).read_bytes() == old_bytes
    assert any(p["status"] == "current" and "25 users" in p["text"] for p in passages)


@pytest.mark.asyncio
async def test_oversized_passages_are_split_and_code_fails_safely(workspace, monkeypatch):
    from app.intelligence.local_embeddings import EmbeddingInputTooLong

    tenant, source_id, job_id, connection, _, settings = workspace

    class Limited(DevIntelligence):
        async def contextualise(self, *args):
            return "context " * 20

        async def embed(self, texts, task="document"):
            for index, text in enumerate(texts):
                if len(text) > 90:
                    raise EmbeddingInputTooLong(index, len(text), 90)
            return await super().embed(texts, task)

    monkeypatch.setattr(pipeline, "get_intelligence", lambda: Limited(settings.embedding_dim))
    rows = [f"| Model {number} | {number} cores |" for number in range(12)]
    prose = "Alpha phrase. " * 30 + "Tail fact remains."
    table = "| Model | Specification |\n| --- | --- |\n" + "\n".join(rows)
    mock_url(monkeypatch, "# Reference\n\n" + prose + "\n\n" + table)
    await pipeline.run_ingest(tenant, source_id, job_id, auto_propose=False)
    source, job, docs, passages = await state(workspace)
    assert job["status"] == "succeeded"
    async with connection(tenant) as conn:
        chunks = await fetch_all(conn, "select text, contextual_blurb, chunk_index from passages where document_id = %s and level = 'chunk' order by chunk_index", (docs[0]["id"],))
    assert len(chunks) > 2
    assert all(len((row["contextual_blurb"] + "\n\n" if row["contextual_blurb"] else "") + row["text"]) <= 90 for row in chunks)
    assert any("Tail fact remains." in row["text"] for row in chunks)
    assert all(any(item in row["text"] and "| Model | Specification |" in row["text"] for row in chunks) for item in rows)
    assert [row["chunk_index"] for row in chunks] == list(range(len(chunks)))

    workspace = await requeue(workspace)
    mock_url(monkeypatch, "# Reference\n\n```\n" + "long code line\n" * 30 + "```")
    await pipeline.run_ingest(tenant, source_id, workspace[2], auto_propose=False)
    source, job, new_docs, new_passages = await state(workspace)
    assert job["status"] == "failed" and "code block" in job["error"]
    assert source["status"] == "active" and new_docs == docs and new_passages == passages

    workspace = await requeue(workspace)
    mock_url(monkeypatch, "# Reference\n\n" + "unpunctuated condition " * 30)
    await pipeline.run_ingest(tenant, source_id, workspace[2], auto_propose=False)
    source, job, new_docs, new_passages = await state(workspace)
    assert job["status"] == "failed" and "cannot be split safely" in job["error"]
    assert source["status"] == "active" and new_docs == docs and new_passages == passages


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("HARBOUR_TEST_LOCAL_EMBEDDING") != "1", reason="requires the configured local embedding model")
async def test_real_local_model_indexes_large_table_without_truncation(workspace, monkeypatch):
    from app.intelligence.hybrid import HybridIntelligence
    from app.intelligence.local_embeddings import LocalEmbeddingIntelligence

    tenant, source_id, job_id, connection, _, settings = workspace
    local = LocalEmbeddingIntelligence(settings)
    monkeypatch.setattr(pipeline, "get_intelligence", lambda: HybridIntelligence(DevIntelligence(settings.embedding_dim), local))
    rows = [f"| Model-{index:03} | {index} cores |" for index in range(80)]
    table = "| Model | Specification |\n| --- | --- |\n" + "\n".join(rows)
    mock_url(monkeypatch, "# Reference\n\n" + table)
    await pipeline.run_ingest(tenant, source_id, job_id, auto_propose=False)
    source, job, docs, _ = await state(workspace)
    assert source["status"] == "active" and job["status"] == "succeeded", job["error"]
    async with connection(tenant) as conn:
        chunks = await fetch_all(conn, "select text, contextual_blurb from passages where document_id = %s and level = 'chunk' order by chunk_index", (docs[0]["id"],))
    assert len(chunks) > 1
    assert all(any(row in chunk["text"] for chunk in chunks) for row in rows)
    assert all("| Model | Specification |" in chunk["text"] for chunk in chunks)
    # The same checked inputs can be re-embedded without silent truncation.
    vectors = await local.embed([f"{chunk['contextual_blurb']}\n\n{chunk['text']}" if chunk["contextual_blurb"] else chunk["text"] for chunk in chunks])
    assert len(vectors) == len(chunks)


@pytest.mark.asyncio
async def test_uploaded_text_ignores_conflicting_mime_during_ingestion(workspace, monkeypatch):
    import hashlib
    import httpx
    from app.deps import get_context
    from app.ingest.storage import read_bytes
    from app.main import app

    tenant, _, _, connection, session, settings = workspace
    async with connection(tenant) as conn:
        app.dependency_overrides[get_context] = lambda: Context(session, conn, settings)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/v1/sources", files={
                    "file": ("facts.txt", b"A useful fact.", "application/pdf"),
                })
                assert response.status_code == 202
                source_id, job_id = UUID(response.json()["id"]), UUID(response.json()["job_id"])
        finally:
            app.dependency_overrides.pop(get_context, None)
    await pipeline.run_ingest(tenant, source_id, job_id, auto_propose=False)
    async with connection(tenant) as conn:
        source = await fetch_one(conn, "select * from sources where id = %s", (source_id,))
        job = await fetch_one(conn, "select * from ingest_jobs where id = %s", (job_id,))
        document = await fetch_one(conn, "select * from documents where source_id = %s", (source_id,))
        assert source["status"] == "active" and job["status"] == "succeeded"
        assert document["parser"] == "text" and "A useful fact." in document["content_md"]
        assert read_bytes(tenant, source_id, source["storage_path"]) == b"A useful fact."
        assert source["hash_sha256"] == hashlib.sha256(b"A useful fact.").hexdigest()


@pytest.mark.asyncio
async def test_preserved_markdown_is_indexed_after_redaction(workspace, monkeypatch):
    tenant, source_id, job_id, connection, _, settings = workspace
    content = "# Limit\n\n> Only annual plans qualify.\n\nLimit:\n40\n\n```python\n# config\n  value = 40\n```\n\nContact user@example.invalid."
    mock_url(monkeypatch, content)
    seen = []

    class RecordingIntelligence(DevIntelligence):
        async def classify(self, title, text, topics):
            seen.append(text)
            return await super().classify(title, text, topics)

    monkeypatch.setattr(pipeline, "get_intelligence", lambda: RecordingIntelligence(settings.embedding_dim))
    await pipeline.run_ingest(tenant, source_id, job_id, auto_propose=False)
    source, job, docs, passages = await state(workspace)
    assert source["status"] == "active" and job["status"] == "succeeded"
    assert seen and "user@example.invalid" not in seen[0]
    assert "[REDACTED_EMAIL]" in seen[0]
    assert "> Only annual plans qualify." in docs[0]["content_md"]
    assert "Limit:\n40" in docs[0]["content_md"]
    assert any("> Only annual plans qualify." in passage["text"] for passage in passages)
    assert any("```python\n# config\n  value = 40\n```" in passage["text"] for passage in passages)
    assert not any("user@example.invalid" in passage["text"] for passage in passages)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["html", "docx"])
async def test_structured_documents_are_indexed_under_correct_heading(workspace, monkeypatch, kind):
    tenant, source_id, job_id, connection, _, _ = workspace
    if kind == "html":
        data = (b"<h1>Annual rules</h1><ul><li><p>Annual plans qualify.</p></li></ul>"
                b"<table><tr><th>Plan</th><th>Limit</th></tr><tr><td>Annual</td><td>40</td></tr></table>"
                b"<h1>Monthly rules</h1><p>Monthly plans do not qualify.</p>")
        content_type = "text/html"
    else:
        import io
        from docx import Document

        document = Document()
        document.add_heading("Annual rules", 1)
        document.add_paragraph("Annual plans qualify.")
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text, table.cell(0, 1).text = "Plan", "Limit"
        table.cell(1, 0).text, table.cell(1, 1).text = "Annual", "40"
        document.add_heading("Monthly rules", 1)
        document.add_paragraph("Monthly plans do not qualify.")
        output = io.BytesIO()
        document.save(output)
        data = output.getvalue()
        content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    async def fetch(*args):
        return data, content_type, f"https://example.com/rules.{kind}"

    monkeypatch.setattr(pipeline, "fetch_url", fetch)
    await pipeline.run_ingest(tenant, source_id, job_id, auto_propose=False)
    source, job, docs, _ = await state(workspace)
    assert source["status"] == "active" and job["status"] == "succeeded"
    assert docs[0]["content_md"].count("Annual plans qualify.") == 1
    async with connection(tenant) as conn:
        rows = await fetch_all(conn, "select text, section_path from passages where document_id = %s and level = 'chunk'", (docs[0]["id"],))
    assert next(row for row in rows if "| Annual | 40 |" in row["text"])["section_path"] == "Annual rules"
    assert next(row for row in rows if "Monthly plans do not qualify." in row["text"])["section_path"] == "Monthly rules"


@pytest.mark.asyncio
async def test_pdf_page_provenance_and_index_version_reingest(workspace, monkeypatch):
    from fpdf import FPDF

    tenant, source_id, job_id, connection, _, _ = workspace
    pdf = FPDF()
    pdf.set_font("helvetica", size=12)
    for content in ("First page fact.", "", "Third page fact."):
        pdf.add_page()
        if content:
            pdf.cell(text=content)
    data = bytes(pdf.output())

    async def fetch(*args):
        return data, "application/pdf", "https://example.com/reference.pdf"
    monkeypatch.setattr(pipeline, "fetch_url", fetch)
    await pipeline.run_ingest(tenant, source_id, job_id, auto_propose=False)
    async with connection(tenant) as conn:
        doc = await fetch_one(conn, "select id, metadata, parse_quality from documents where source_id = %s and status = 'current'", (source_id,))
        assert doc["parse_quality"] is None
        assert doc["metadata"]["extraction"]["pages_without_extractable_text"] == [2]
        assert doc["metadata"]["indexing_version"] == pipeline.INDEXING_VERSION
        rows = await fetch_all(conn, "select text, page_ref from passages where document_id = %s and level = 'chunk' order by chunk_index", (doc["id"],))
        assert [(row["text"], row["page_ref"]) for row in rows] == [("First page fact.", "1"), ("Third page fact.", "3")]
        # Mimic an index built by the earlier pipeline without modifying any
        # existing workspace source or its history.
        await conn.execute("update documents set metadata = metadata - 'indexing_version' where id = %s", (doc["id"],))
    workspace = await requeue(workspace)
    await pipeline.run_ingest(tenant, source_id, workspace[2], auto_propose=False)
    _, job, docs, _ = await state(workspace)
    assert job["status"] == "succeeded" and job["stage"] == "finish"
    assert [(doc["version"], doc["status"]) for doc in docs] == [(1, "superseded"), (2, "current")]
    workspace = await requeue(workspace)
    await pipeline.run_ingest(tenant, source_id, workspace[2], auto_propose=False)
    _, job, docs, _ = await state(workspace)
    assert job["status"] == "succeeded" and job["stage"] == "unchanged"
    assert len(docs) == 2


@pytest.mark.asyncio
async def test_initial_failure_and_successful_retry(workspace, monkeypatch):
    tenant, source_id, job_id, connection, session, settings = workspace
    mock_url(monkeypatch, "# Reference\n\nSome searchable content.")
    original = pipeline.detect_conflicts
    async def fail(*args):
        raise RuntimeError("token=secret-should-not-be-persisted")
    monkeypatch.setattr(pipeline, "detect_conflicts", fail)
    await pipeline.run_ingest(tenant, source_id, job_id, auto_propose=False)
    source, job, docs, passages = await state(workspace)
    assert source["status"] == "failed" and job["status"] == "failed"
    assert "secret-should-not-be-persisted" not in job["error"]
    assert not docs and not passages and source["storage_path"] is None
    workspace = await requeue(workspace)
    assert workspace[2] == job_id
    monkeypatch.setattr(pipeline, "detect_conflicts", original)
    await pipeline.run_ingest(tenant, source_id, job_id, auto_propose=False)
    source, job, docs, passages = await state(workspace)
    assert source["status"] == "active" and job["status"] == "succeeded"
    assert len(docs) == 1 and passages and job["attempts"] == 0


@pytest.mark.asyncio
async def test_interruption_and_recovery_skips_locked_job(workspace, monkeypatch):
    tenant, source_id, job_id, connection, _, _ = workspace
    mock_url(monkeypatch, "# Reference\n\nSome searchable content.")
    async def interrupted(*args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(pipeline, "detect_conflicts", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await pipeline.run_ingest(tenant, source_id, job_id, auto_propose=False)
    source, job, docs, passages = await state(workspace)
    assert job["status"] == "running" and not docs and not passages
    # Keep the cutoff far beyond ordinary real jobs; recovery writes are rolled
    # back and thus cannot alter the user's actual queue.
    async with connection(tenant) as conn:
        await conn.execute("update ingest_jobs set started_at = now() - interval '200 years' where id = %s", (job_id,))
    async with connection(tenant) as locked:
        await locked.execute("select id from ingest_jobs where id = %s for update", (job_id,))
        async with connection(tenant) as recovery:
            await recovery.execute("savepoint test_recovery")
            rows = await fetch_all(recovery, "select * from requeue_stale_ingest_jobs(interval '100 years', 10, 3)")
            assert not any(row["job_id"] == job_id for row in rows)
            await recovery.execute("rollback to savepoint test_recovery")
            await recovery.execute("release savepoint test_recovery")
    async with connection(tenant) as recovery:
        await recovery.execute("savepoint test_recovery")
        rows = await fetch_all(recovery, "select * from requeue_stale_ingest_jobs(interval '100 years', 10, 3)")
        assert any(row["job_id"] == job_id and row["outcome"] == "queued" for row in rows)
        await recovery.execute("rollback to savepoint test_recovery")
        await recovery.execute("release savepoint test_recovery")
        await recovery.execute("update ingest_jobs set attempts = 3 where id = %s", (job_id,))
        await recovery.execute("savepoint test_exhaustion")
        rows = await fetch_all(recovery, "select * from requeue_stale_ingest_jobs(interval '100 years', 10, 3)")
        assert any(row["job_id"] == job_id and row["outcome"] == "failed" for row in rows)
        await recovery.execute("rollback to savepoint test_exhaustion")
        await recovery.execute("release savepoint test_exhaustion")


@pytest.mark.asyncio
async def test_withdrawal_waits_for_ingest_and_cannot_be_undone(workspace, monkeypatch):
    tenant, source_id, job_id, connection, session, settings = workspace
    mock_url(monkeypatch, "# Reference\n\nOriginal searchable content.")
    await pipeline.run_ingest(tenant, source_id, job_id, auto_propose=False)
    workspace = await requeue(workspace)
    tenant, source_id, job_id, connection, session, settings = workspace
    mock_url(monkeypatch, "# Reference\n\nReplacement searchable content.")
    entered, proceed = asyncio.Event(), asyncio.Event()
    original = pipeline.detect_conflicts
    async def pause(*args):
        entered.set()
        await proceed.wait()
        return await original(*args)
    monkeypatch.setattr(pipeline, "detect_conflicts", pause)
    ingest = asyncio.create_task(pipeline.run_ingest(tenant, source_id, job_id, auto_propose=False))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        source, job, docs, passages = await state(workspace)
        assert source["status"] == "active" and job["status"] == "running"
        assert len(docs) == 1 and docs[0]["status"] == "current"
        assert any("Original searchable content" in p["text"] and p["status"] == "current" for p in passages)
        async def withdraw():
            async with connection(tenant) as conn:
                return await _withdraw_sources(Context(session, conn, settings), [source_id])
        deletion = asyncio.create_task(withdraw())
        await asyncio.sleep(0.1)
        assert not deletion.done()
        proceed.set()
        await asyncio.wait_for(ingest, 10)
        assert await asyncio.wait_for(deletion, 10) == (1, [])
        source, job, docs, passages = await state(workspace)
        assert source["status"] == "withdrawn" and source["deleted_at"] is not None
        assert all(doc["status"] == "withdrawn" for doc in docs)
        assert all(p["status"] == "tombstoned" for p in passages)
        await pipeline.run_ingest(tenant, source_id, job_id, auto_propose=False)
        assert (await state(workspace))[0]["status"] == "withdrawn"
    finally:
        proceed.set()
        await asyncio.gather(ingest, return_exceptions=True)


@pytest.mark.asyncio
async def test_replacement_marks_answers_citing_superseded_evidence(workspace, monkeypatch):
    tenant, source_id, job_id, connection, session, settings = workspace
    mock_url(monkeypatch, "# Reference\n\nThe service supports 10 users.")
    await pipeline.run_ingest(tenant, source_id, job_id, auto_propose=False)
    async with connection(tenant) as conn:
        document = await fetch_one(
            conn,
            "select id from documents where source_id = %s and status = 'current'",
            (source_id,),
        )
        passage = await fetch_one(
            conn,
            "select id, text from passages where document_id = %s and level = 'chunk' limit 1",
            (document["id"],),
        )
        answer = await fetch_one(
            conn,
            """insert into canonical_answers
                 (tenant_id, question, answer_md, status, owner_id, review_interval_days,
                  next_review_at, visibility, current_version, created_by, approved_by, approved_at)
               values (%s, 'How many users does it support?', %s, 'approved', %s, 90,
                       now() + interval '90 days', 'internal', 1, %s, %s, now())
               returning id""",
            (tenant, passage["text"], session.user_id, session.user_id, session.user_id),
        )
        await conn.execute(
            """insert into answer_versions (tenant_id, answer_id, version, answer_md, changed_by)
               values (%s, %s, 1, %s, %s)""",
            (tenant, answer["id"], passage["text"], session.user_id),
        )
        await conn.execute(
            """insert into answer_evidence (tenant_id, answer_id, answer_version, passage_id, quote)
               values (%s, %s, 1, %s, %s)""",
            (tenant, answer["id"], passage["id"], passage["text"]),
        )

    workspace = await requeue(workspace)
    mock_url(monkeypatch, "# Reference\n\nThe service supports 25 users.")
    await pipeline.run_ingest(tenant, source_id, workspace[2], auto_propose=False)
    async with connection(tenant) as conn:
        answer_status = await fetch_val(
            conn, "select status from canonical_answers where id = %s", (answer["id"],)
        )
        evidence = await fetch_one(
            conn,
            "select passage_id, quote from answer_evidence where answer_id = %s",
            (answer["id"],),
        )
        old_passage = await fetch_val(
            conn, "select status from passages where id = %s", (passage["id"],)
        )
    assert answer_status == "needs_recheck"
    assert evidence["passage_id"] == passage["id"]
    assert evidence["quote"] == passage["text"]
    assert old_passage == "superseded"


@pytest.mark.asyncio
async def test_staged_reindex_switches_atomically_and_resumes(workspace, monkeypatch):
    from app import reindex as reindex_module
    from app.intelligence.dev import DevIntelligence
    from app.retrieval import search as search_module

    tenant, source_id, job_id, connection, _, settings = workspace
    mock_url(
        monkeypatch,
        "# Reference\n\n## Usage\n\nThe service supports 10 users.\n\n"
        "## Limits\n\nThe API allows 60 requests per minute.\n\n"
        "## Support\n\nContact support at support@example.com.",
    )
    await pipeline.run_ingest(tenant, source_id, job_id, auto_propose=False)
    identity = settings.embedding_identity
    async with connection(tenant) as conn:
        rows = await fetch_all(
            conn,
            """select p.id, p.embedding_identity
               from passages p join documents d on d.id = p.document_id
               where d.source_id = %s and p.level = 'chunk'
               order by p.id""",
            (source_id,),
        )
    assert len(rows) >= 2
    assert {row["embedding_identity"] for row in rows} == {identity}

    # Simulate an index from an earlier embedding configuration. Dense search
    # must not compare it with the active query vectors, and the fallback must
    # be reported rather than silent.
    async with connection(tenant) as conn:
        await conn.execute(
            "update passages set embedding_identity = 'p1-legacy' where id = any(%s::uuid[])",
            ([row["id"] for row in rows],),
        )
    intelligence = DevIntelligence(settings.embedding_dim)
    monkeypatch.setattr(search_module, "get_intelligence", lambda: intelligence)
    query_vector = (await intelligence.embed(["service supports users"], task="query"))[0]
    async with connection(tenant) as conn:
        outcome = await search_module.hybrid_search(
            conn, "service supports users", query_vector, channel="internal"
        )
    assert outcome.dense_index == "lexical_only"
    assert outcome.index_warning and "different embedding configuration" in outcome.index_warning
    assert outcome.passages

    # Interrupt after the first committed staging batch. Live embeddings must
    # still belong to the old identity, while finished batches stay staged so
    # a later run can resume instead of starting over.
    monkeypatch.setattr(reindex_module, "tenant_conn", connection)
    monkeypatch.setattr(reindex_module, "BATCH_SIZE", 1)

    class Flaky(DevIntelligence):
        calls = 0

        async def embed(self, texts, task="document"):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("interrupted")
            return await super().embed(texts, task)

    with pytest.raises(RuntimeError, match="interrupted"):
        await reindex_module.reindex_tenant(tenant, settings, Flaky(settings.embedding_dim))
    async with connection(tenant) as conn:
        live_legacy = await fetch_val(
            conn,
            "select count(*) from passages where id = any(%s::uuid[]) and embedding_identity = 'p1-legacy'",
            ([row["id"] for row in rows],),
        )
        staged = await fetch_val(conn, "select count(*) from embedding_reindex_staging")
    assert live_legacy == len(rows)
    assert 0 < staged < len(rows)

    updated = await reindex_module.reindex_tenant(
        tenant, settings, DevIntelligence(settings.embedding_dim)
    )
    assert updated == len(rows)
    async with connection(tenant) as conn:
        mismatched = await fetch_val(
            conn,
            """select count(*) from passages
               where id = any(%s::uuid[])
                 and (embedding is null or embedding_identity is distinct from %s)""",
            ([row["id"] for row in rows], identity),
        )
        staged = await fetch_val(conn, "select count(*) from embedding_reindex_staging")
    assert mismatched == 0 and staged == 0

    async with connection(tenant) as conn:
        outcome = await search_module.hybrid_search(
            conn, "service supports users", query_vector, channel="internal"
        )
    assert outcome.dense_index == "hybrid" and outcome.index_warning is None
    assert outcome.passages


@pytest.mark.asyncio
async def test_plan_scope_filter_is_case_insensitive(workspace, monkeypatch):
    """A model returning "Team" must still match sources scoped to "team"."""
    from app.retrieval import search as search_module

    tenant, source_id, job_id, connection, _, settings = workspace
    async with connection(tenant) as conn:
        await conn.execute(
            "update sources set plan_scope = %s, visibility = 'public' where id = %s",
            (["team"], source_id),
        )
    mock_url(monkeypatch, "# Team plan\n\nThe Team plan supports 10 users.")
    await pipeline.run_ingest(tenant, source_id, job_id, auto_propose=False)
    intelligence = DevIntelligence(settings.embedding_dim)
    monkeypatch.setattr(search_module, "get_intelligence", lambda: intelligence)
    query = "How many users does the Team plan support"
    query_vector = (await intelligence.embed([query], task="query"))[0]

    async with connection(tenant) as conn:
        for plan, expected in (("team", True), ("Team", True), ("TEAM", True), ("business", False)):
            outcome = await search_module.hybrid_search(
                conn, query, query_vector, channel="internal", plan=plan
            )
            assert bool(outcome.passages) is expected, plan

    # An unscoped query still sees a general document, and canonical matching
    # uses the same normalised value.
    async with connection(tenant) as conn:
        outcome = await search_module.hybrid_search(conn, query, query_vector, channel="internal")
    assert outcome.passages
