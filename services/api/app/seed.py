"""Reset the development workspace and load a realistic messy support corpus.

Usage: cd services/api && uv run python -m app.seed
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .answers_service import approve_answer, create_answer
from .config import ROOT, get_settings
from .db import close_pool, fetch_all, fetch_one, open_pool, tenant_conn
from .deps import Session
from .ingest.pipeline import PIPELINE_VERSION, detect_conflicts, enqueue_job, run_ingest
from .ingest.storage import save_bytes
from .retrieval.answer import AskRequest, ask, record_feedback

sys.path.insert(0, str(ROOT))
from seed.make_pdf import build as build_pdf  # noqa: E402

TENANT_ID = UUID("7b1f6b3e-0c1a-4f2b-9a3d-2f6c8e1d4a50")
ADMIN_ID = UUID("a1000000-0000-4000-8000-000000000001")
EDITOR_ID = UUID("a1000000-0000-4000-8000-000000000002")
AGENT_ID = UUID("a1000000-0000-4000-8000-000000000003")
CORPUS = ROOT / "seed" / "corpus"

TOPICS = [
    ("billing", "Billing & refunds", "Invoices, plans, payments and refunds.", None, 30),
    ("refunds", "Refunds", "Refund eligibility, windows and exceptions.", "billing", 30),
    ("pricing", "Plans & pricing", "Plan names, prices and what each plan includes.", "billing", 30),
    ("invoices", "Invoices & billing details", "Invoice delivery, payment methods and billing contacts.", "billing", 90),
    ("product", "Product & cards", "How the product works, including cards and spend.", None, 180),
    ("card-sync", "Card sync", "Corporate card connections and sync errors.", "product", 90),
    ("access", "Access & security", "Sign-in, SSO and workspace permissions.", None, 90),
    ("sso", "SSO", "SAML single sign-on availability and setup.", "access", 90),
    ("approvals", "Expense approvals", "Approval rules and who can approve spend.", "product", 90),
    ("integrations", "Integrations", "Connections to other tools.", "product", 180),
]


def _job_key(source_id: UUID, digest: str) -> str:
    return f"ingest:{source_id}:{digest}:{PIPELINE_VERSION}"


def _session(user_id: UUID, name: str, email: str, role: str, title: str) -> Session:
    return Session(
        tenant_id=TENANT_ID,
        tenant_slug="harbour",
        tenant_name="Harbour Support",
        user_id=user_id,
        user_name=name,
        email=email,
        role=role,  # type: ignore[arg-type]
        title=title,
    )


ADMIN = _session(ADMIN_ID, "Maya Chen", "maya@harbour.example", "admin", "Head of Support")
EDITOR = _session(EDITOR_ID, "Jonas Reed", "jonas@harbour.example", "editor", "Senior Support Specialist")
AGENT = _session(AGENT_ID, "Priya Nair", "priya@harbour.example", "agent", "Support Agent")


async def reset_workspace(settings) -> None:
    async with await psycopg.AsyncConnection.connect(
        settings.migration_database_url, row_factory=dict_row
    ) as conn:
        await conn.execute("delete from tenants where slug = 'harbour'")
        await conn.execute(
            """insert into tenants (id, slug, name, settings)
               values (%s, 'harbour', 'Harbour Support', %s)""",
            (TENANT_ID, Jsonb({"default_channel": "web", "timezone": "Europe/London"})),
        )
        await conn.execute(
            """insert into users (id, tenant_id, email, name, role, title) values
                 (%s, %s, 'maya@harbour.example', 'Maya Chen', 'admin', 'Head of Support'),
                 (%s, %s, 'jonas@harbour.example', 'Jonas Reed', 'editor', 'Senior Support Specialist'),
                 (%s, %s, 'priya@harbour.example', 'Priya Nair', 'agent', 'Support Agent')""",
            (ADMIN_ID, TENANT_ID, EDITOR_ID, TENANT_ID, AGENT_ID, TENANT_ID),
        )
        topic_ids: dict[str, UUID] = {}
        for slug, name, description, _parent, interval in TOPICS:
            row = await (
                await conn.execute(
                    """insert into topics (tenant_id, slug, name, description, review_interval_days, owner_id)
                       values (%s, %s, %s, %s, %s, %s) returning id""",
                    (TENANT_ID, slug, name, description, interval, EDITOR_ID),
                )
            ).fetchone()
            topic_ids[slug] = row["id"]
        for slug, _name, _description, parent, _interval in TOPICS:
            if parent:
                await conn.execute(
                    "update topics set parent_id = %s where id = %s",
                    (topic_ids[parent], topic_ids[slug]),
                )
    print("· workspace reset and topics created")


async def ingest_file(
    filename: str,
    *,
    kind: str,
    tier: str,
    visibility: str,
    topic_slug: str,
    plan_scope: list[str] | None = None,
    version_scope: list[str] | None = None,
    title: str | None = None,
) -> UUID:
    path = CORPUS / filename
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    source_id = uuid4()
    storage_path = save_bytes(TENANT_ID, source_id, filename, data)
    async with tenant_conn(TENANT_ID) as conn:
        await conn.execute(
            """insert into sources
                 (id, tenant_id, kind, title, storage_path, mime_type, size_bytes, hash_sha256,
                  source_tier, visibility, status, metadata, plan_scope, product_version_scope)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'processing', %s, %s, %s)""",
            (
                source_id, TENANT_ID, kind, title or path.stem.replace("-", " ").replace("_", " "),
                storage_path, "application/pdf" if kind == "pdf" else None, len(data), digest, tier,
                visibility, Jsonb({"filename": filename, "seeded": True}), plan_scope or [],
                version_scope or [],
            ),
        )
        job_id = await enqueue_job(conn, TENANT_ID, source_id, _job_key(source_id, digest))
    await run_ingest(TENANT_ID, source_id, job_id, auto_propose=False, user_id=EDITOR_ID)
    async with tenant_conn(TENANT_ID) as conn:
        source = await fetch_one(conn, "select title, status from sources where id = %s", (source_id,))
        if source is None or source["status"] != "active":
            raise RuntimeError(f"Seed ingestion failed for {filename}: {source}")
        topic_id = await fetch_one(conn, "select id from topics where slug = %s", (topic_slug,))
        await conn.execute(
            """update documents set topic_id = %s
               where source_id = %s and status = 'current'""",
            (topic_id["id"], source_id),
        )
    print(f"· ingested {filename}")
    return source_id


async def find_quote(conn, needle: str) -> dict:
    row = await fetch_one(
        conn,
        """select p.id, p.text, p.section_path, d.title as document_title, d.effective_from,
                  s.visibility
           from passages p
           join documents d on d.id = p.document_id
           join sources s on s.id = d.source_id
           where p.level = 'chunk' and p.status = 'current' and d.status = 'current'
             and position(lower(%s) in regexp_replace(lower(p.text), '\\s+', ' ', 'g')) > 0
           order by p.chunk_index
           limit 1""",
        (needle,),
    )
    if row is None:
        raise RuntimeError(f"Seed quote not found in any passage: {needle!r}")
    return row


async def make_answer(
    conn,
    session: Session,
    *,
    question: str,
    answer_md: str,
    quotes: list[str],
    topic_slug: str,
    visibility: str = "public",
    plan_scope: list[str] | None = None,
    version_scope: list[str] | None = None,
    interval: int = 90,
    approve: bool = True,
) -> dict:
    evidence = []
    for quote in quotes:
        passage = await find_quote(conn, quote)
        evidence.append({"passage_id": passage["id"], "quote": quote, "claim_ref": f"claim-{len(evidence) + 1}"})
    answer = await create_answer(
        conn,
        session,
        question=question,
        answer_md=answer_md,
        topic_slug=topic_slug,
        plan_scope=plan_scope or [],
        version_scope=version_scope or [],
        visibility=visibility,
        evidence=evidence,
        owner_id=EDITOR_ID,
        review_interval_days=interval,
        status="in_review",
    )
    if approve:
        answer = await approve_answer(conn, session, answer["id"])
    return answer


async def ask_once(session: Session, settings, *, question: str, channel: str = "web", plan=None, version=None) -> dict:
    async with tenant_conn(TENANT_ID) as conn:
        return await ask(
            conn,
            session,
            settings,
            AskRequest(
                question=question,
                channel=channel,
                plan=plan,
                product_version=version,
            ),
        )


async def main() -> None:
    settings = get_settings()
    settings.validate_provider()
    build_pdf()
    await open_pool()
    try:
        await reset_workspace(settings)

        refund_2025 = await ingest_file(
            "refund-policy-2025.md", kind="markdown", tier="official_doc",
            visibility="public", topic_slug="refunds", title="Refund policy (2025)",
        )
        old_refund_pdf = await ingest_file(
            "old-refund-policy.pdf", kind="pdf", tier="official_doc",
            visibility="public", topic_slug="refunds", title="Refund policy (2024)",
        )
        pricing_2025 = await ingest_file(
            "pricing-2025.md", kind="markdown", tier="official_doc",
            visibility="public", topic_slug="pricing", title="Plans and pricing (2025)",
        )
        await ingest_file(
            "pricing-2024.txt", kind="text", tier="wiki",
            visibility="public", topic_slug="pricing", title="Pricing sheet (2024)",
        )
        await ingest_file(
            "sso-setup.md", kind="markdown", tier="official_doc",
            visibility="public", topic_slug="sso", plan_scope=["enterprise"],
            title="Setting up SSO",
        )
        await ingest_file(
            "billing-email.md", kind="markdown", tier="wiki",
            visibility="public", topic_slug="invoices", title="Change the billing email",
        )
        await ingest_file(
            "card-sync-E4021.md", kind="markdown", tier="wiki",
            visibility="public", topic_slug="card-sync", title="Card sync error E4021",
        )
        await ingest_file(
            "approvals-v1.md", kind="markdown", tier="official_doc",
            visibility="public", topic_slug="approvals", version_scope=["v1"],
            title="Expense approvals (version 1)",
        )
        await ingest_file(
            "approvals-v2.md", kind="markdown", tier="official_doc",
            visibility="public", topic_slug="approvals", version_scope=["v2"],
            title="Expense approvals (version 2)",
        )
        await ingest_file(
            "internal-refunds.txt", kind="text", tier="system_of_record",
            visibility="internal", topic_slug="refunds", title="Internal refund handling notes",
        )
        await ingest_file(
            "ticket-export.txt", kind="ticket_export", tier="chat_export",
            visibility="internal", topic_slug="invoices", title="Ticket export — March 2025",
        )

        async with tenant_conn(TENANT_ID) as conn:
            # Re-run conflict detection now that seeded topics are fixed, so the
            # planted 14-day vs 30-day and $24 vs $19 contradictions are found.
            for source_id in (refund_2025, old_refund_pdf, pricing_2025):
                document = await fetch_one(
                    conn,
                    "select * from documents where source_id = %s and status = 'current'",
                    (source_id,),
                )
                if document:
                    passages = await fetch_all(
                        conn,
                        "select id, text from passages where document_id = %s and level = 'chunk' and status = 'current'",
                        (document["id"],),
                    )
                    await detect_conflicts(conn, TENANT_ID, document, passages)

            async with tenant_conn(TENANT_ID) as conn:
                refund_window = await make_answer(
                    conn, EDITOR,
                    question="What is the refund window for annual plans?",
                    answer_md=(
                        "Annual subscriptions can be refunded within 14 days of the initial purchase. "
                        "After the 14-day window, annual plans are non-refundable."
                    ),
                    quotes=[
                        "Annual subscriptions can be refunded within 14 days of the initial purchase.",
                        "After the 14-day window, annual plans are non-refundable.",
                    ],
                    topic_slug="refunds",
                    interval=30,
                )
                await make_answer(
                    conn, EDITOR,
                    question="How do I request a refund?",
                    answer_md=(
                        "Annual subscriptions can be refunded within 14 days of the initial purchase. "
                        "Contact support from the workspace owner account and include the invoice number. "
                        "Approved refunds are processed within five business days."
                    ),
                    quotes=[
                        "Annual subscriptions can be refunded within 14 days of the initial purchase.",
                        "Contact support from the workspace owner account and include the invoice number.",
                        "We process approved refunds within five business days.",
                    ],
                    topic_slug="refunds",
                    interval=30,
                )
                await make_answer(
                    conn, EDITOR,
                    question="What does the Business plan cost?",
                    answer_md=(
                        "Business costs $24 per user per month. It adds approvals, custom spend policies "
                        "and priority support."
                    ),
                    quotes=[
                        "Business costs $24 per user per month. It adds approvals, custom spend policies and priority support.",
                    ],
                    topic_slug="pricing",
                    interval=30,
                )
                await make_answer(
                    conn, EDITOR,
                    question="Does Harbour support SSO?",
                    answer_md=(
                        "SSO is available on the Enterprise plan only. Team and Business workspaces "
                        "cannot enable SAML single sign-on."
                    ),
                    quotes=["SSO is available on the Enterprise plan only."],
                    topic_slug="sso",
                    plan_scope=["enterprise"],
                    interval=90,
                )
                await make_answer(
                    conn, EDITOR,
                    question="How do I change the billing email?",
                    answer_md=(
                        "Only a workspace admin can change the billing email. Open Settings, then Billing, "
                        "select Billing contact and choose Edit. Invoices and failed-payment notices go to "
                        "the new address from the next billing cycle."
                    ),
                    quotes=[
                        "Only a workspace admin can change the billing email.",
                        "Invoices and failed-payment notices go to the new address from the next billing cycle.",
                    ],
                    topic_slug="invoices",
                    interval=90,
                )
                await make_answer(
                    conn, EDITOR,
                    question="What should I do when card sync fails with E4021?",
                    answer_md=(
                        "E4021 means Harbour could not sync a corporate card with the card issuer. "
                        "Reconnect the card from Settings, then Cards, and complete the issuer login. "
                        "If the error remains after two attempts, escalate to the Cards team."
                    ),
                    quotes=[
                        "E4021 means Harbour could not sync a corporate card with the card issuer.",
                        "Escalate to the Cards team if the issuer reports the card as active but Harbour still shows E4021 after two reconnect attempts.",
                    ],
                    topic_slug="card-sync",
                    interval=90,
                )
                await make_answer(
                    conn, EDITOR,
                    question="Do I need approval for team expenses?",
                    answer_md=(
                        "In version 1, expenses under $50 are approved automatically. Anything at or above $50 "
                        "needs a manager approval before it is submitted to finance."
                    ),
                    quotes=[
                        "In version 1, expenses under $50 are approved automatically.",
                        "Anything at or above $50 needs a manager approval before it is submitted to finance.",
                    ],
                    topic_slug="approvals",
                    version_scope=["v1"],
                    interval=90,
                )
                await make_answer(
                    conn, EDITOR,
                    question="Do I need approval for team expenses?",
                    answer_md=(
                        "In version 2, every expense needs a manager approval. The $50 automatic approval rule "
                        "has been removed."
                    ),
                    quotes=[
                        "In version 2, every expense needs a manager approval.",
                        "The $50 automatic approval rule has been removed.",
                    ],
                    topic_slug="approvals",
                    version_scope=["v2"],
                    interval=90,
                )
                await make_answer(
                    conn, EDITOR,
                    question="Can we pay by invoice?",
                    answer_md=(
                        "Annual Enterprise contracts can be paid by invoice. Team and Business are card only."
                    ),
                    quotes=[
                        "Annual Enterprise contracts can be paid by invoice. Team and Business are card only.",
                    ],
                    topic_slug="invoices",
                    visibility="internal",
                    interval=90,
                )
                draft = await make_answer(
                    conn, EDITOR,
                    question="Can I get a refund after 30 days?",
                    answer_md=(
                        "Annual plans can be refunded within 30 days of purchase. The refund is returned to "
                        "the original card."
                    ),
                    quotes=[
                        "Annual plans can be refunded within 30 days of purchase.",
                        "The refund is returned to the original card.",
                    ],
                    topic_slug="refunds",
                    interval=30,
                    approve=False,
                )
                pricing_answer = await fetch_one(
                    conn,
                    "select id from canonical_answers where question = 'What does the Business plan cost?'",
                )
                await conn.execute(
                    """insert into review_items (tenant_id, kind, payload, priority, dedupe_key)
                       values (%s, 'change_impact', %s, 80, 'seed:pricing-change')
                       on conflict (tenant_id, dedupe_key)
                         where dedupe_key is not null and status = 'pending' do nothing""",
                    (
                        TENANT_ID,
                        Jsonb({
                            "answer_id": str(pricing_answer["id"]),
                            "summary": "Pricing source changed: Business moved from $19 to $24 per user.",
                            "source_id": str(pricing_2025),
                            "detail": "The 2025 pricing sheet supersedes the 2024 sheet. The approved answer already uses $24; confirm its review date.",
                        }),
                    ),
                )

        # Demo questions populate qa_logs, gaps and the feedback loop.
        refund_ask = await ask_once(EDITOR, settings, question="What is the refund window for annual plans?")
        await ask_once(EDITOR, settings, question="How do I change the billing email?")
        await ask_once(EDITOR, settings, question="What does the Business plan cost per user?")
        await ask_once(EDITOR, settings, question="Do I need approval for team expenses?", version="v1")
        await ask_once(EDITOR, settings, question="Do I need approval for team expenses?", version="v2")
        await ask_once(EDITOR, settings, question="Does Harbour support SAML with Okta?", plan="enterprise")
        await ask_once(EDITOR, settings, question="Does Harbour support SAML with Okta?", plan="business")
        await ask_once(EDITOR, settings, question="Can we pay by invoice?")
        await ask_once(AGENT, settings, question="How do I change the billing email?", channel="public")
        await ask_once(AGENT, settings, question="Can we pay by invoice?", channel="public")
        await ask_once(AGENT, settings, question="What is the refund policy for annual plans?", channel="public")

        for _ in range(3):
            await ask_once(AGENT, settings, question="Do you offer a nonprofit discount?", channel="public")
        for _ in range(2):
            await ask_once(AGENT, settings, question="Is there a Zapier integration?", channel="public")
        await ask_once(AGENT, settings, question="Can I export receipts to Snowflake?", channel="public")

        leak_ask = await ask_once(
            AGENT, settings,
            question="What is the legacy 60-day refund grace period?",
            channel="public",
        )

        async with tenant_conn(TENANT_ID) as conn:
            if refund_ask.get("qa_log_id"):
                await record_feedback(
                    conn, EDITOR, UUID(refund_ask["qa_log_id"]), 1,
                    "Annual subscriptions can be refunded within 14 days of purchase. Contact support with the invoice number.",
                    "Tightened the wording for the support macro.",
                )
            if leak_ask.get("qa_log_id"):
                leaked = await fetch_one(
                    conn,
                    """select count(*) as count
                       from qa_logs q
                       join sources s on s.id = any(
                         select d.source_id from passages p join documents d on d.id = p.document_id
                         where p.id = any(q.retrieved_passage_ids)
                       )
                       where q.id = %s and s.visibility = 'internal'""",
                    (UUID(leak_ask["qa_log_id"]),),
                )
                if leaked and leaked["count"]:
                    raise RuntimeError("Public channel retrieved internal material. Seed aborted.")
                if leak_ask.get("answer_md") and "60-day" in leak_ask["answer_md"]:
                    raise RuntimeError("Public channel leaked the internal 60-day grace period.")

            counts = await fetch_one(
                conn,
                """select
                     (select count(*) from sources where status = 'active' and deleted_at is null) as sources,
                     (select count(*) from passages where status = 'current') as passages,
                     (select count(*) from canonical_answers where status = 'approved') as approved,
                     (select count(*) from canonical_answers where status = 'in_review') as in_review,
                     (select count(*) from review_items where status = 'pending') as review,
                     (select count(*) from review_items where status = 'pending' and kind = 'conflict') as conflicts,
                     (select count(*) from gaps where status = 'open') as gaps,
                     (select count(*) from qa_logs) as questions""",
            )
        print("\nWorkspace ready — https://harbour.example (local UI at http://127.0.0.1:3000)")
        print(f"  sources:          {counts['sources']}")
        print(f"  current passages: {counts['passages']}")
        print(f"  approved answers: {counts['approved']}")
        print(f"  in review:        {counts['in_review']}")
        print(f"  pending review:   {counts['review']} ({counts['conflicts']} conflicts)")
        print(f"  open gaps:        {counts['gaps']}")
        print(f"  questions logged: {counts['questions']}")
        print(f"  draft answer:     {draft['id']}")
        print("  leak test:        passed (no internal passage retrieved on the public channel)")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
