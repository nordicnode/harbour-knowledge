# Support knowledge base — design specification

A NotebookLM-like product built specifically for customer support and knowledge bases. Users dump unstructured, unorganised notes and documents in. The system organises them into a governed, searchable knowledge base, and an AI answers support questions from approved knowledge only — with citations, a confidence signal, and an honest "I don't know" that becomes a tracked gap when no good source exists.

Working description: NotebookLM (source-grounded chat) × Obsidian (linked topics and browsing) × Notion (editing and team workspace), rebuilt around the one job none of them do well: keeping a messy support knowledge base clean, current, and safe to answer from.

| Item | Decision in this spec |
|---|---|
| Primary users | Support teams of 2–10 people with no dedicated knowledge manager |
| Core unit of truth | One approved, versioned canonical answer per topic or question |
| Raw documents | Candidates and evidence only — never the direct source of truth |
| AI providers | External, via OpenAI-compatible endpoints; provider-agnostic by design |
| MVP database | Postgres with pgvector — relational data, vectors, and full-text in one store |
| MVP run cost target | Roughly $30–80 per month before paying customers, excluding the builder's time |
| Pricing and model names | Point-in-time research from November 2026 — re-check vendor pages before budgeting |

## Contents

- 1. Purpose and product thesis
- 2. Users, jobs, and scope
- 3. Product principles
- 4. Feature specification
- 5. System architecture
- 6. AI provider abstraction — OpenAI-compatible by design
- 7. Data model
- 8. Ingestion pipeline
- 9. Organisation intelligence — sorting, storing, and linking
- 10. Indexing design
- 11. Retrieval pipeline
- 12. Answer generation, citations, confidence, and abstention
- 13. Governance — canonical answers, conflicts, freshness, change impact
- 14. Review queue, gap dashboard, and feedback loop
- 15. User experience and surfaces
- 16. Roles, permissions, and visibility
- 17. Public help centre and integrations
- 18. API contracts
- 19. Background jobs
- 20. Caching and invalidation
- 21. Evaluation and quality gates
- 22. Observability, analytics, and audit
- 23. Security and privacy
- 24. Costs and capacity
- 25. Failure modes and mitigations
- 26. Build phases
- 27. Deferred work and explicit non-goals
- 28. Risks and open decisions
- 29. Acceptance criteria for v1
- Appendix A. Glossary
- Appendix B. Sources

---

## 1. Purpose and product thesis

### 1.1 The problem

Support knowledge does not start clean. It lives in PDFs, help-centre drafts, ticket replies, chat threads, screenshots, release notes, pricing pages, internal wiki pages, and one-off notes written under time pressure. The same question is answered five different ways across three years. Pricing changes and the old price stays in a PDF. A workaround for version 1 is still searchable after version 2 ships. Internal caveats sit in the same document as customer-safe wording.

A person maintaining this by hand — collecting scattered answers, deduplicating them, resolving contradictions, and keeping one guide current — is doing the exact job this product automates.

### 1.2 Why naive retrieval-augmented generation is not the product

The obvious prototype — split documents into chunks, embed them, retrieve the top few by similarity, and ask a model to answer — fails in predictable ways on support corpora:

- The right passage exists but never reaches the top results.
- The right passage is retrieved but stale, superseded, or for the wrong plan.
- Two retrieved passages contradict each other and the model blends them into one fluent, wrong sentence.
- A condition ("only for accounts opened before 2024") is split into a different chunk from the rule it limits.
- The model answers confidently from insufficient evidence instead of abstaining.
- An internal note is retrieved for a customer-facing question because visibility was a prompt instruction, not a data filter.

Retrieval quality matters, but governance matters more. A stale answer can be perfectly grounded in an old passage and still be wrong for the customer today. No faithfulness metric catches that. Currency, authority, and approval have to be modelled in the data, not hoped for in the prompt.

### 1.3 The thesis

**Sort at write time, not at query time.**

Pay the organisation cost once, at ingestion, where it is amortised over every future question. Keep query time cheap, fast, and deterministic.

Concretely:

1. **Dump.** Anything unstructured goes in, with no formatting requirement.
2. **Organise.** The system parses, deduplicates, classifies, links, and drafts structured knowledge. A human approves what becomes truth.
3. **Answer.** Questions are answered from approved canonical answers and their stored evidence first. Raw passages are a labelled fallback, never an equal authority.

The differentiators are not the chat box. They are the conflict detector, freshness tracking, gap finder, change-impact analysis, and citations with a calibrated confidence signal.

### 1.4 Positioning against existing tools

NotebookLM is a personal research tool: strong source-grounded chat, no public API, a per-notebook source cap, and no team governance, version scoping, or gap workflow. Notion is a flexible wiki whose AI answers across the workspace, with no verification or expiry workflow and no canonical-answer model. Guru has the strongest verification model — a card with an owner and a review interval, with AI answering only from verified cards — but cards suit short answers, pricing is quote-only with a seat floor small teams struggle to clear, and there is no public help centre. Zendesk and Intercom's Fin are helpdesk platforms: capable, but priced per agent and per resolution, and neither auto-organises messy source documents. Fini is the closest in thesis — approved articles as the only source of truth, with conflicts and gaps surfaced in a review queue — and this design adopts that principle in spirit while keeping fuzzy retrieval for discovery.

The gap this product fills, for a 2–10 person team:

- Dump-and-organise ingestion instead of manual authoring.
- One approved answer per question, with owner and expiry.
- Conflicts and gaps surfaced before customers see them.
- Answers that respect plan, product version, and internal-versus-public visibility.
- Citations, abstention, and a gap dashboard as first-class behaviour.
- A price payable without a sales call or a per-resolution meter.


---

## 2. Users, jobs, and scope

### 2.1 Personas

**Support agent (primary daily user).** Answers customer questions all day. Needs a draft reply in seconds, in the right tone, with sources they can check. Needs to know immediately when the knowledge base has no good answer, rather than receiving a plausible invention. Edits drafts; those edits should improve the knowledge base.

**Knowledge owner / editor.** Usually a senior agent or team lead doing this alongside support work. Approves canonical answers, resolves conflicts, clears the review queue, owns topics, and watches freshness and gaps. There is no full-time knowledge manager — every governance screen must be usable in minutes, in a queue, not as a library-science project.

**Admin.** Configures the workspace: providers and models, channels, visibility defaults, members and roles, billing, data retention, and provider allow-lists.

**End customer (indirect user).** Never logs in to the core app. Encounters the knowledge through a public help centre, an embeddable widget, or a bot. Only ever sees approved, public, current answers for their plan and product version.

### 2.2 Jobs to be done

- "I have a folder of messy docs. Turn it into something I can trust."
- "A customer asked this. Give me a correct draft with proof."
- "Tell me what our knowledge base cannot answer yet, in priority order."
- "Our refund policy just changed. Tell me everything that is now wrong."
- "Two docs disagree. Show me, and let me pick the winner once."
- "Publish the same approved knowledge as a help centre without maintaining it twice."

### 2.3 Scope for v1

In scope: single-workspace multi-tenancy from day one (every row carries a tenant identifier), document and URL ingestion, OCR for screenshots and scans, canonical answers with approval, hybrid retrieval with reranking, citations, confidence and abstention, conflict and gap queues, freshness and expiry, plan and version scoping, internal and public channels, a public help centre, analytics, and an embeddable widget or Discord bot as a thin channel over the same answering path.

Out of scope for v1: replacing a helpdesk or ticketing system, voice support, full graph-database traversal, autonomous multi-hop agents, self-hosted models, automatic publishing of AI-proposed edits without human approval, and a whole-corpus knowledge-graph build. Section 27 lists deferred work in full.

### 2.4 Assumptions to validate

English-language corpora, fewer than one million passages per tenant at MVP scale, and both internal agent-assist and customer-facing channels. A multilingual corpus changes the embedding-model choice and the glossary design, and must be treated as a separate design pass if it becomes a requirement.

---

## 3. Product principles

1. **Approved answers are truth; raw sources are evidence.** Drafts never answer customers.
2. **Never mutate history.** Edits create new versions. Superseded material is retained for audit and explicitly marked, never silently overwritten or deleted from the record.
3. **Visibility is enforced in the database query, not in the prompt.** A prompt instruction is not a security boundary.
4. **Abstention is a successful outcome.** A logged gap is worth more than a confident invention.
5. **Every claim needs stored evidence.** Citations are selected from evidence linked at approval time, not invented during generation.
6. **Humans approve; AI proposes.** Ingestion, conflict detection, and edit suggestions are drafts until a person approves them.
7. **Currency is measured separately from correctness.** An answer can be faithful to its source and still be out of date.
8. **Provider choice is configuration.** No model name, dimension, or vendor behaviour is hard-coded into business logic.
9. **One database until measurement says otherwise.** Simplicity is a feature for a solo builder.
10. **The review queue is the product.** If governance is slower than the inbox, teams will bypass it, and the knowledge base will rot anyway.

---

## 4. Feature specification

### 4.1 Ingest — getting messy material in

- Accept PDF, DOCX, PPTX, XLSX, HTML, EPUB, plain text, Markdown, images and screenshots, chat-log exports, ticket exports, and URLs.
- Upload returns immediately. Parsing runs asynchronously as a tracked job with stage, status, and error reporting; a failed parse is retryable and never silently drops a source.
- Every source records its kind, title, origin URL or path, SHA-256 hash, ingestion time, source tier, and default visibility.
- Source tiers, in descending authority: system of record, official document, wiki or help centre, chat or ticket export. Tier is metadata used in ranking and conflict resolution, never a prompt hint alone.
- Screenshots and scans go through an OCR path. The original image is retained, OCR confidence is stored, and low-confidence passages are flagged for human review rather than treated as truth.
- URLs are fetched and snapshotted, so a citation points at the version that was ingested, not at whatever the page says today.
- Re-ingesting an unchanged file is detected by hash and becomes an alias, not a duplicate parse.
- Voice notes are a later ingestion type; the job model must not assume text input, so transcription can be added as a stage without a schema change.

### 4.2 Auto-classification on ingest

Each document and section is proposed — not finally assigned — a type from a closed set: policy, pricing, procedure or how-to, FAQ, known issue, bug or workaround, troubleshooting, product description, and internal note. Classification drives chunking defaults, review intervals, and freshness sensitivity. Pricing, policy, and legal material get the shortest review intervals by default.

### 4.3 Organise

- Canonical answers: one clean answer per question or topic, drafted by the model from all relevant sources, with the original passages linked underneath as evidence. See Section 13.
- Deduplication at document, passage, and semantic level. See Section 9.3.
- Conflict detection with a permanent registry. See Section 13.3.
- Freshness: every fact-bearing answer carries an owner, a review interval, a next review date, and effective dates where applicable.
- Topic graph: topics, canonical answers, and entities linked for browsing, derived from relational data. See Sections 9.1 and 15.4.
- Plan and version awareness: an answer can be scoped to Free versus Pro, or to product version 1 versus version 2, with separate approved variants coexisting.
- Internal and public layers: visibility is a property of sources, passages, and answers, enforced at retrieval. See Section 16.

### 4.4 Answer

- Plain-English questions, with chat history folded into a standalone query before retrieval.
- Draft replies with exact citations, a confidence band, and a visible list of sources used — plus sources deliberately ignored as outdated or conflicting, where that affected the answer.
- Tone variants for the same grounded answer: short, friendly, formal. Tone changes wording only; it must never change facts, scope, or citations.
- Macros: an approved answer can be saved as a reusable reply template with placeholders.
- Error-code and identifier lookup: exact-match search is a first-class path, not an accident of semantic search.

### 4.5 Maintain

- Gap dashboard ordered by frequency and recency. See Section 14.2.
- Change-impact report on every new or updated source. See Section 13.4.
- Human approval for every AI-proposed edit before it goes live.
- Feedback loop: when an agent edits a draft materially, the edit is proposed back as a canonical-answer revision.
- Full audit log: who changed what, when, and why.

### 4.6 Team and publishing workflow

- Roles: admin, editor, agent. See Section 16.
- A public help centre generated from the same approved, public answers — no second copy to maintain.
- An embeddable widget and a Discord bot as channels over the identical answering pipeline and visibility rules.
- Analytics: top questions, deflection, abstention rate, and which answers agents keep editing.

---

## 5. System architecture

### 5.1 Shape

Three deployables, one database. No microservices, no Kubernetes, no second data store in v1.

| Layer | Choice | Reason |
|---|---|---|
| Web app | Next.js (App Router), Tailwind, shadcn/ui, hosted on Vercel | Server rendering for the public help centre, streaming chat, fast deploys |
| API and worker | Python FastAPI service, hosted on Railway, Fly.io, or Render | The ingestion, parsing, clustering, and evaluation ecosystem is Python-native |
| Database | Postgres with pgvector, on Supabase or Neon | Relational data, vectors, and full-text search in one store — no synchronisation drift |
| File storage | Supabase Storage or Cloudflare R2 (S3-compatible) | Originals retained for citations and audit; extracted text alone is never the only copy |
| Auth | Clerk, or Supabase Auth if already on Supabase | Never build authentication directly; organisations and multi-factor support included |
| Billing | Stripe Billing with Checkout and Customer Portal | Subscriptions, tax, and dunning are not a differentiator worth building |
| Email, analytics, errors | Resend, PostHog, Sentry | Transactional mail, product analytics, and error tracking on free or cheap tiers |
| Graph view | React Flow over Postgres-derived nodes and edges | Browsing and navigation without a graph database |

### 5.2 Request flow — answering

Web app → FastAPI answering endpoint → standalone rewrite → hybrid retrieval in Postgres → rerank via provider → confidence gate → grounded generation → citation verification → response with answer, citations, confidence band, and any conflict notice → question-and-answer log row written.

### 5.3 Request flow — ingestion

Upload or URL → source row and storage object created → ingestion job queued → parse → normalise → deduplicate → chunk and contextualise → embed → classify and extract entities → canonical-answer candidates and conflict candidates proposed → review queue. Nothing from a new source answers a customer until the approval path in Section 13 has run, except through the clearly labelled raw-passage fallback described in Section 12.5.

### 5.4 Tenancy

Multi-tenancy is structural, not a later retrofit. Every tenant-owned table carries `tenant_id`. Every query is tenant-scoped. Row-level security or an equivalent application-level enforcement layer must be tested with cross-tenant adversarial queries in the evaluation set (Section 21). A cross-tenant retrieval is a security incident, not a quality defect.

### 5.5 What is deliberately not in the architecture

No LangChain or LlamaIndex as the application framework: their patterns (recursive retrieval, multi-representation indexing, summary indexes) are adopted, but the answering contract — stored citations, visibility filtering, abstention — needs direct control that a framework's hidden prompts and version churn work against. No Neo4j, no Qdrant, and no Pinecone in v1; migration triggers are defined in Section 24.3. No self-hosted models in the hosted product; a local OpenAI-compatible endpoint remains possible through the provider abstraction for development.

---

## 6. AI provider abstraction — OpenAI-compatible by design

### 6.1 Standardise on Chat Completions

The portable standard across providers is the Chat Completions shape, not the Responses API. Responses is OpenAI-specific, with different structures for structured outputs and tool calling, and state that lives on the vendor. The Assistants API was shut down on 26 August 2026. Building on Chat Completions keeps DeepSeek, Gemini's compatible endpoint, OpenRouter, Groq, Mistral, Together, Fireworks, and local servers such as Ollama, vLLM, and LocalAI reachable through one implementation.

Rules:

- Build every chat call on Chat Completions semantics.
- Keep conversation state in this product's database, never in a vendor's stateful API.
- Treat any provider-specific extension as optional capability, detected from configuration, never assumed.

### 6.2 Internal provider interface

Business logic calls only this internal interface. It is implemented once against the OpenAI SDK with `base_url`, API key, and model taken from configuration.

```text
complete(messages, options) -> completion
  options: model_role, json_schema?, tools?, temperature, max_tokens

embed(texts, options) -> vectors
  options: model_role, task (document | query)

rerank(query, documents, options) -> scored documents
  options: model_role, top_n
```

An acceptable alternative is the LiteLLM Python library used in-process, which translates across provider shapes behind one call. Do not run a separate LiteLLM gateway service at MVP scale: it adds an operable service, a failure mode, and a bill, for no benefit a library call does not already give. Whichever route is chosen, the rest of the codebase must not import a vendor SDK directly.

### 6.3 Per-role model configuration

Models are selected by role in configuration or in the database, never named in code.

| Role | Job | Selection guidance |
|---|---|---|
| `ingest_small` | Contextual blurbs, classification, entity extraction, hypothetical questions | Cheapest capable model; volume is high, difficulty is low |
| `rewrite` | Standalone query rewrite, expansion, decomposition | Cheap and fast; runs on every question |
| `answer` | Final grounded answer | Best quality the budget allows; this is the customer-visible call |
| `judge` | Faithfulness and evaluation checks | Prefer a different provider from `answer` to reduce judge bias |
| `embed` | Document and query embeddings | Chosen for retrieval quality per dollar, then pinned — see 6.5 |
| `rerank` | Cross-encoder reranking | Bought before a dearer embedder or a bigger answer model — see 6.6 |

Each configured model records its capabilities: context limit, embedding dimensions where applicable, strict JSON-schema support, tool-calling support, and prompt-caching support. Startup validation fails loudly if a configured model lacks a capability its role requires.

### 6.4 Compatibility pitfalls to test per provider

"OpenAI-compatible" is a spectrum. Before a provider is enabled for a tenant, run a conformance check against the exact payloads this product sends:

- Structured outputs: parameter names, strictness, and schema support vary; some providers silently ignore a response-format instruction. Fallback is parse, repair, and retry, escalating to a stronger model.
- Tool calling: the wire shape is standard, but smaller models emit malformed calls, wrong argument types, or ignore tool choice. Tools stay read-only and narrow, and are gated to tested models.
- Embeddings: some providers require an input-type or task parameter (document versus query); omitting it degrades results silently.
- Streaming, authentication headers, rate limits, and error shapes differ. The adapter normalises errors into the product's own error types.
- Prompt caching differs and changes the economics of contextual retrieval and repeated system prompts. Treat caching as an optimisation, never a correctness dependency.

### 6.5 Embeddings are the real lock-in

Vectors from different embedding models are not comparable, and dimensions differ widely — for example 1,536, 3,072, 1,024, and 2,048 across common models. Swapping the embedding model therefore means re-embedding the entire corpus. There is no migration shortcut.

Design consequences:

- Every passage row stores `embedding_model` and `embedding_version`.
- Dimensions are never mixed in one column or index.
- A swap builds a new index alongside the live one, validates it against the golden set (Section 21), then flips a configuration flag.
- The vector index is a derived artefact. Originals and passages live in Postgres and storage, so the index can always be rebuilt.

Embedding cost is not the optimisation target at small scale: indexing ten million tokens costs on the order of cents to a dollar or so on cheaper models, and one major provider offered a large free token allowance in the research period. Choose for retrieval quality per dollar.

### 6.6 Reranking is the cheapest quality win

In a November 2026 OpenRouter test cited in the research (274 questions, 30 candidates), adding a reranker lifted precision at rank 1 from 81% with no reranking to about 97–98%, at roughly $0.10–$0.26 per 1,000 queries for the cheaper Voyage rerankers, versus about $2 per 1,000 searches for Cohere. Provider and price must be re-checked before committing, but the ordering of investment is stable: buy reranking before buying a more expensive embedder or a larger answer model.

### 6.7 Privacy gates on provider choice

Providers differ more in data terms than in API shape. The research found major US and EU paid tiers that do not train on API data by default, and other providers reported to train or improve on customer data by default, with abuse-log retention commonly in the 30–55 day range. Therefore:

- No tenant document is sent to a provider without a data-processing agreement on file for that provider.
- Admins maintain a per-tenant provider allow-list. Adding a routed provider adds a sub-processor and requires a terms re-check.
- Prompts contain retrieved, minimised evidence only — never whole documents, and never billing details, secrets, or unrelated personal data. See Section 23.

---

## 7. Data model

Postgres is the single store. Vectors use pgvector with an HNSW index. Full-text uses Postgres `tsvector`. All tables carry `id`, `tenant_id` where tenant-owned, `created_at`, and `updated_at` unless stated otherwise. History is append-only where noted.

### 7.1 Sources and documents

**`sources`** — provenance for everything ingested.

- `kind`: pdf, docx, pptx, xlsx, html, epub, text, markdown, image, chat_export, ticket_export, url
- `title`, `origin_url`, `storage_path`
- `hash_sha256`: change detection and exact deduplication
- `source_tier`: system_of_record, official_doc, wiki, chat_export
- `visibility`: internal, public
- `status`: active, withdrawn
- `ingested_at`

**`documents`** — one row per version of a source's content. The version chain is explicit and never inferred from filenames.

- `source_id`, `version` (integer, monotonically increasing per source)
- `effective_from`, `effective_to` (nullable end): the window this version governs
- `supersedes_id`: previous document version, if any
- `status`: draft, current, superseded, withdrawn
- `plan_scope` (array), `product_version_scope` (array), `language`
- Only `status = current` is retrievable by default.

### 7.2 Passages

**`passages`** — immutable chunks, per document version. Re-ingesting a new version creates new rows; old rows remain for audit and for citations attached to old answer versions, but are filtered out of retrieval.

- `document_id`, `document_version`
- `parent_id`: the section-level passage this child belongs to (Section 10)
- `text`, `text_hash`, `chunk_index`
- `section_path`: heading trail, for display and citation context
- `page_ref`, `line_ref` where the parser provides them
- `contextual_blurb`: the situating text described in Section 10.3
- `embedding` (vector), `embedding_model`, `embedding_version`
- `bm25_text` (`tsvector`), built from text plus contextual blurb
- `ocr_confidence` where applicable
- `dup_cluster_id`, `dup_similarity`, `canonical_passage_id` for deduplication metadata
- `status`: current, superseded, tombstoned

Deletion is a tombstone on the retrieval path: a deleted source's passages must stop answering immediately, while the audit record of what was deleted and when is retained.

### 7.3 Canonical answers — the source of truth

**`canonical_answers`** — one approved answer per topic or question.

- `topic_id`
- `question`: the canonical phrasing
- `answer_md`: current approved text (Markdown)
- `status`: draft, in_review, approved, needs_recheck, archived
- `owner_id`, `review_interval_days`, `next_review_at`
- `plan_scope` (array), `version_scope` (array), `visibility`
- `human_confidence`: an editor's assessed confidence, distinct from any model score

**`answer_versions`** — append-only history. An approved edit creates a new version; the old version is retained.

- `answer_id`, `version`, `answer_md`
- `changed_by`, `change_reason`, `supersedes_version`

**`answer_evidence`** — the citation store. Every claim-bearing sentence in a canonical answer links to exact passages.

- `answer_id`, `answer_version`, `passage_id`
- `quote`: the exact supporting span
- `claim_ref`: which claim in the answer this evidence supports

Citations at answer time are drawn from this table. The generator may only cite evidence identifiers supplied to it (Section 12.3).

### 7.4 Organisation tables

**`topics`** — the frozen taxonomy tree (Section 9.1).

- `parent_id`, `slug`, `name`, `description`
- `owner_id`, `review_interval_days`, `taxonomy_version`

**`terms`** — the controlled glossary and synonym ring.

- `preferred_term`, `aliases` (array), `definition`, `scope`

**`entities`** and **`entity_links`** — the light ontology (Section 9.4).

- Entity: `type` (closed set: Product, Plan, Feature, ErrorCode, Policy, Version, Integration, Team), `canonical_name`, `aliases` (array)
- Link: `from_entity_id`, `to_entity_id`, `relation` (closed set: applies_to, supersedes, requires, caused_by, part_of), `passage_id`, `confidence`, `status`

**`tags`** — folksonomy tags attached to passages and answers, with a usage count. Tags support discovery and promotion into the glossary; they are never a retrieval filter.

### 7.5 Governance and operations tables

**`conflicts`** — the permanent conflict registry. Rows are never deleted; they move through a lifecycle.

- `passage_id_a`, `passage_id_b` and/or `answer_id_a`, `answer_id_b`
- `kind`: contradiction, overlap, version_disagreement
- `detector`: nli, llm, rule
- `score`, `status`: pending, resolved, dismissed
- `resolution`, `resolved_by`, `resolved_at`

**`gaps`** — unanswered or abstained questions, clustered.

- `normalised_question`, `representative_question`
- `count`, `first_seen_at`, `last_seen_at`
- `status`: open, answered, wont_answer
- `linked_answer_id` once resolved

**`qa_logs`** — one row per answered or abstained question. This is the feedback, evaluation, and audit substrate.

- `question`, `standalone_query`, `channel`
- `retrieved_passage_ids`, `rerank_scores`
- `answer_id`, `answer_version`, `citation_ids`
- `confidence_composite`, `confidence_band`, `abstained`
- `feedback`: thumbs, agent edit distance, escalation outcome
- `latency_ms`, `provider`, `model`, `token_cost`

**`ingest_jobs`** — `source_id`, `status`, `stage`, `error`, `started_at`, `finished_at`.

**`review_items`** — the unified queue behind Section 14: `kind` (new_answer, edit_proposal, conflict, dedup_merge, topic_change, stale_answer, low_ocr, change_impact), `payload`, `status`, `assignee_id`, `priority`, `created_at`, `decided_by`, `decided_at`, `decision_note`.

### 7.6 Invariants

- No approved answer exists without at least one evidence row pointing at a current passage.
- No two approved canonical answers share the same topic, canonical question, plan scope, version scope, and visibility — variants differ in at least one scope dimension.
- A passage's embedding model and version always match the index it sits in.
- Every state change to an approved answer, conflict, or review item is attributable to a user or a named background job.

---

## 8. Ingestion pipeline

Ingestion is asynchronous, staged, idempotent, and resumable. Re-running a stage must not duplicate passages, answers, or review items; idempotency keys are the source hash plus stage name plus pipeline version.

### 8.1 Stages

1. **Intake.** Store the original, compute SHA-256, create the source row and job. Exact-duplicate hash → alias to the existing canonical source and stop.
2. **Parse.** Convert to structured Markdown or JSON with heading structure, tables, and reading order preserved. Default parser: Docling. Fast path for clean text-layer PDFs: PyMuPDF. Broad-format fallback: MarkItDown. Hardest scans and tables, where quality justifies a paid call: a hosted OCR API such as Mistral OCR or LlamaParse. Local OCR for screenshots and scans: PaddleOCR first, Surya or Tesseract as alternatives. Licence-check any parser with copyleft or revenue-capped terms before commercial use.
3. **Normalise.** Strip repeated headers, footers, page numbers, and quoted-reply boilerplate from ticket threads. Normalised text (lowercased, whitespace-collapsed, boilerplate-stripped) is hashed for near-exact deduplication.
4. **Redact.** Detect and mask obvious secrets and personal data before any text reaches an external provider (Section 23.3). Redactions are recorded by type and location, not by storing the secret.
5. **Segment.** Split into parent sections and child chunks (Section 10).
6. **Contextualise.** Generate a contextual blurb per child chunk (Section 10.3) using the `ingest_small` model, with the parent document cached so per-chunk cost stays low.
7. **Embed and index.** Embed child text plus blurb; build the `tsvector` from the same text; store hypothetical questions and section summaries as additional representations (Section 10.4).
8. **Classify and extract.** Propose document type, topics, tags, glossary terms, and entities (closed schema only).
9. **Deduplicate.** Document-level and chunk-level near-duplicate detection (Section 9.3).
10. **Propose.** Draft or update canonical-answer candidates for affected topics; run conflict detection over affected clusters; create review items. Nothing goes live in this stage.

### 8.2 Failure handling

A stage failure marks the job failed with the stage and a human-readable error, retains all completed stages, and offers retry from the failed stage. A source whose parse quality is unusable (for example, OCR confidence below threshold across most passages) is quarantined: visible in the admin UI, excluded from retrieval, with a review item explaining why.

### 8.3 Incremental updates

When a source changes: diff new passages against the old version by hash and embedding similarity; create a new document version; re-embed only changed passages; re-run contextual blurbs for changed parents; tombstone passages that no longer exist; then run the change-impact job (Section 13.4). Unchanged content is never re-processed.

---

## 9. Organisation intelligence — sorting, storing, and linking

This is the core of the product. The design rule: **every fact must be findable six ways independently — by meaning, by exact words, by topic, by entity, by time, version, and plan, and by authority.** Any single index alone is fragile, so the system keeps all six over the same content.

### 9.1 Three organisation layers, used together

Taxonomy, folksonomy, and ontology are not alternatives. Each does a job the others cannot.

**Controlled glossary (highest return on effort).** One preferred term per concept, with aliases pointing to it: internal codenames to product names, "repayment" to "refund", "single sign-on" to "SSO". Human-seeded, then model-proposed from the corpus and human-approved. Applied twice: at ingestion, for tagging, and at query time, for expansion. Terminology mismatch — the customer writes one word, the document uses another — is the most common silent retrieval failure, and the glossary is the direct fix.

**Taxonomy (the official filing system).** A topic tree two to three levels deep, roughly 20–60 topics at maturity: for example Billing → Refunds → Eligibility. One primary topic per canonical answer; secondary topics allowed. Topics carry an owner and a review interval.

**Folksonomy (the language people actually use).** Free tags extracted from documents, tickets, agent edits, and real customer questions. Noisy by design. Tags feed discovery and glossary promotion; they are never trusted as filters.

**Light ontology (typed entities, closed relations).** Entities and relations are constrained to the closed sets in Section 7.4. Free-form model-extracted relations are prohibited in v1 and v2: recent research on graph-based retrieval found that unstable relation extraction produces noisy graphs that degrade retrieval, with context relevance in some graph approaches measured below plain retrieval. Entities are reliable anchors; invented relations are the noise source. The practical alternative — link content by shared entities, with a small set of human-reviewable relation types — is what this design adopts.

### 9.2 Topic discovery, then freeze

Topics are discovered, then frozen. They are never continuously re-clustered.

1. Nightly or weekly, embed canonical answers and section summaries and cluster them (HDBSCAN where the number of topics is unknown; a topic-modelling pipeline such as BERTopic provides clustering plus term-based topic representations out of the box).
2. The model names each cluster from its most typical members, using verb-first goal names — "Change delivery address", not "Delivery".
3. A human reviews, merges, splits, and renames. The result becomes a new versioned taxonomy.
4. Between discovery runs, new content is classified into the frozen taxonomy. Unclassifiable content goes to a review item, not a new auto-created topic.

Re-clustering continuously would rename topics weekly, break trend lines, and orphan links. Clusters are for discovery; the frozen taxonomy is for reporting and retrieval.

Merge rule: two topics whose term vectors are near-identical and whose canonical answers cite the same passages. Split rule: a topic containing two low-similarity sub-clusters, a cluster the model labels mixed, or a topic accumulating multiple conflicting answers. Both are review items carrying a proposed migration mapping, so existing answers and links move rather than orphan.

### 9.3 Deduplication — three levels, conservative by design

False merges hurt more than false splits, so every threshold leans conservative and uncertain pairs go to review.

**Level 1 — exact.** SHA-256 of raw bytes, and of normalised text. A hit creates an alias record pointing at the canonical document. Nothing is silently deleted.

**Level 2 — near-duplicate.** Word 5-gram shingles, MinHash with 128 permutations, locality-sensitive hashing bucketing (for example 16 bands of 8 rows), flagging pairs around 0.80–0.85 Jaccard similarity, confirmed by exact Jaccard or embedding cosine. SimHash with a small Hamming distance is a cheaper alternative at larger scale. Applied at document level (saves parsing) and at chunk level (catches boilerplate and quoted replies inside ticket threads). Support corpora routinely contain 5–30% near-duplicates. The decision is stored as metadata — cluster identifier, similarity, chosen canonical — and the canonical is picked by newest governing version, then highest source tier, then best parse quality.

**Level 3 — semantic.** Nightly embedding clustering (HDBSCAN or DBSCAN) catches paraphrases that shingling misses. Same-cluster passages produce a merge proposal in the review queue. Semantic dedup never auto-merges.

### 9.4 Entity extraction and resolution

Extraction is constrained to the closed type set. Resolution then merges mentions of the same entity:

1. Normalise names (case, punctuation, known aliases from the glossary).
2. Block candidates on shared tokens so comparison stays tractable.
3. Score pairs; merge strongest first.
4. **Refuse any merge that would join records with different strong identifiers** — different error codes, SKUs, or account identifiers. This conflict-aware rule prevents chaining errors, where A merges with B and B with C and three distinct things become one.
5. Rejected and uncertain merges become review items, with the evidence shown.

Entities with a single mention are pruned or demoted in hygiene jobs (Section 9.5): they are usually extraction noise.

### 9.5 Hygiene jobs (nightly)

- Canonical answers whose evidence passages are no longer current.
- Passages never retrieved in 90 days — archive candidates, never auto-deletes.
- Topics with zero answers; answers past their review date (Section 13.2).
- Single-mention entities; tags frequent enough to propose for glossary promotion.
- Orphaned links after merges, splits, or withdrawals.

Hygiene produces a knowledge-base health report and review items. It never silently deletes knowledge.

---

## 10. Indexing design

### 10.1 Multi-level index

One flat chunk index cannot serve a factoid lookup, a multi-step procedure, and a synthesis question at once. Published chunk-size studies disagree on a single best size precisely because answer locality differs by question type. The resolution is to index at several granularities and let the query choose the level.

```text
Document (raw file, hash, source, tier, dates)
  Section / parent (about 800–1,500 tokens)
    Child chunk (about 256–512 tokens)
      + contextual blurb (50–100 tokens)
      + hypothetical questions (2–3)
      + extracted keywords and entities
    Section summary (2–3 sentences, separately indexed)
  Document summary
Topic summary (phase 2, per-topic only — see 10.5)
```

### 10.2 Chunking rules

- Markdown-aware recursive splitting: split on headings first, then paragraphs, then sentences. Separators in order: heading boundary, blank line, line break, sentence end, space.
- Child size: about 512 tokens with 50–100 tokens of overlap for procedures and policies; about 256 tokens for FAQ-style factoids.
- Parent: the containing section, up to about 1,500 tokens.
- Never split mid-sentence, mid-table, mid-list, or mid-procedure-step.
- A condition stays in the same parent as the rule it limits. Splitting a rule from its condition is a named defect class in testing.
- These are starting defaults. They must be validated on the golden set built from real support documents (Section 21); the literature does not settle chunk size, and any vendor claim that it does should be distrusted.

### 10.3 Contextual retrieval

Before embedding, each child chunk is prepended with a short situating blurb (50–100 tokens) written by the `ingest_small` model from the document and section context: what document this is, what section, what product, plan, and version it concerns. The blurb is prepended to **both** the embedding text and the BM25 text — indexing only one side loses much of the gain.

Anthropic's published measurement is the basis for making this a v1 feature rather than an optimisation: top-20 retrieval failure fell from 5.7% at baseline to 2.9% with contextual embeddings plus contextual BM25 (a 49% reduction), and to 1.9% with reranking added (67%). Cost was reported around $1.02 per million document tokens at 2024 pricing, falling with prompt caching because the document is cached once and each chunk is a small suffix.

### 10.4 Multi-representation indexing

Each child chunk is reachable through several representations, all pointing back to the same passage:

- The chunk text itself (dense vector and BM25).
- Its section and document summaries (for broad and conceptual questions).
- Two to three hypothetical questions the chunk answers, written in customer phrasing (for question-shaped queries).
- Extracted keywords, error codes, and entity names (for exact lookup).

### 10.5 Summary trees — phase 2, deliberately limited

A RAPTOR-style approach — recursively clustering chunks and summarising clusters into a tree — improves broad synthesis questions substantially in published results, but whole-corpus rebuilds are expensive and unstable. If adopted in phase 2: cluster **within a topic only**, at most two levels, summaries as an additional index alongside the others, and retrieval over a flattened pool of all levels rather than tree traversal. It never becomes the primary retrieval path.

### 10.6 Parent–child return

Retrieval matches on children for precision. Generation receives parents for context. When several children of one parent are retrieved, the parent is returned once, with the best child marked. This separation — small units for finding, large units for reading — is the core fix for both over-chunking (lost context) and under-chunking (diluted embeddings and drowned context windows).

---

## 11. Retrieval pipeline

Retrieval runs in eight stages. Every stage before generation is cheap relative to the answer call, and each stage exists because a named failure mode demands it.

### 11.1 Stage 1 — standalone rewrite

The raw question plus chat history goes to the `rewrite` model with structured output:

- A standalone question with pronouns and follow-up references resolved.
- Extracted filters: plan, product version, date or as-of intent, named entities, error codes.
- An intent classification: factoid, procedure, troubleshooting, comparison, synthesis, or out of scope.

A customer's question is usually a bad search query: short, vague, and in different vocabulary from the documents. Every later stage works on the standalone form, and reranking is always scored against it.

### 11.2 Stage 2 — expansion

Rules first, model second:

- Glossary and synonym expansion, acronym expansion, and spelling normalisation — grounded in corpus vocabulary before model vocabulary.
- Two to four paraphrases of the standalone question.
- Decomposition of compound questions into sub-questions, each retrieved independently. A compound question embedded as one vector lands between two topics and matches neither.
- Optionally, one hypothetical-answer (HyDE) embedding: a model-written plausible answer, embedded as an additional vector. HyDE is vector-only, never used for BM25 (invented terms would match invented evidence), never shown to the user, and never the only query representation.

The original standalone query is always kept as one variant. Expansion degrades on unfamiliar topics and ambiguous questions, so paraphrases are capped and the reranker (Stage 5) restores precision.

### 11.3 Stage 3 — hybrid retrieval

Per query variant, in parallel:

- Dense retrieval: pgvector cosine similarity over child chunks, summaries, and hypothetical questions.
- Lexical retrieval: BM25 over `tsvector`. Lexical search is not optional for support: product names, plan names, SKUs, version numbers, and error codes are exact strings, and dense embeddings smear that specificity.
- Fusion: Reciprocal Rank Fusion with k = 60. Rank-based fusion needs no score-scale tuning across the two retrievers.
- Candidate pool: top 20–50 passages before reranking.

### 11.4 Stage 4 — metadata filtering, before final ranking

Filtering happens in SQL, before ranking is final — ideally as part of the retrieval query itself:

```text
tenant_id = current tenant
AND visibility IN (visibilities allowed for this channel)
AND status = current
AND plan_scope contains the question's plan (or is unscoped)
AND version_scope contains the question's product version (or is unscoped)
AND effective_from <= as-of date < COALESCE(effective_to, infinity)
```

The model is never asked to respect scope described in a prompt. If it is not filtered in the query, it is not filtered.

### 11.5 Stage 5 — reranking

A cross-encoder reranker scores the top 20–50 candidates against the standalone query and keeps the top 5–8. Paraphrases and HyDE are recall tools; the reranker is the precision tool, which is why it sees the standalone query and nothing else.

### 11.6 Stage 6 — routing by query type

| Intent | Primary index | Notes |
|---|---|---|
| Factoid | Child chunks | Small result set, high precision |
| Procedure | Parents | Larger set, steps kept in order |
| Troubleshooting | BM25-heavy, entity-filtered | Error code as a hard filter where present |
| Comparison | Both sides explicitly | Retrieve each compared item separately, then merge |
| Synthesis | Summary index first | Broad questions need summaries, not fragments |
| Compound | Decomposed sub-questions | One retrieval per sub-question, merged at assembly |

### 11.7 Stage 7 — context assembly

- Deduplicate by parent: one copy of each parent, best child marked.
- Merge neighbouring passages from the same section.
- Token budget: roughly 60% of the context for evidence, the remainder for instructions and the answer.
- Order by rerank score, with the two strongest passages placed first and last — models under-use material in the middle of long contexts.
- Attach stored citation identifiers to every passage. Passages without identifiers do not enter the prompt.

### 11.8 Stage 8 — confidence gate

The assembled evidence is scored before generation (Section 12.4). Below the abstention threshold, generation does not run: the system abstains and logs a gap. In the middle band, it may retry once with relaxed filters or a stronger retrieval pass before deciding.

---

## 12. Answer generation, citations, confidence, and abstention

### 12.1 Canonical answers first

Answering reads approved canonical answers first. If an approved answer matches the standalone question within the question's plan, version, and visibility scope, it is the answer: returned with its stored citations, optionally re-toned (Section 4.4) without factual change. Retrieval over raw passages runs only when no approved answer covers the question.

### 12.2 Grounded generation

When generation is required, the prompt contains only: the standalone question, the assembled evidence with citation identifiers, any detected conflict notice (Section 12.6), and the instruction to answer solely from the evidence and to abstain if it is insufficient. No whole documents, no unfiltered search results, no internal material on a public channel — the SQL filter in Section 11.4 has already decided what may appear.

### 12.3 Citations

- The generator may cite only evidence identifiers present in its prompt.
- For canonical answers, citations come from `answer_evidence`: exact quotes stored at approval time.
- After generation, every cited identifier is verified: it must exist in the retrieved set, and its quote must appear verbatim or near-verbatim in the passage. A citation that fails verification fails the answer — it is regenerated once, then abstained.
- Citation hallucination — citing a real source for a claim it does not support — is tracked as its own metric (Section 21), distinct from factual fabrication.

### 12.4 Confidence is composed, never self-reported

A model's own confidence score is the weakest available signal and is weighted lowest. The composite is built from independent signals, in descending weight:

1. Reranker top score, mean of the top three, and the count of candidates above a mid threshold. Raw cosine similarity has no absolute meaning and drifts with corpus and query length, so it is never used directly as confidence.
2. Post-generation faithfulness: the fraction of the answer's atomic claims supported by the retrieved evidence, checked by an entailment pass or the `judge` model.
3. Judge-model assessment on sampled high-stakes answers.
4. Historical accuracy for the topic: how often answers in this topic survived agent review unedited.

Thresholds are calibrated, not guessed: build a labelled set of answerable questions plus an equal number of plausible unanswerable ones (out of scope, out of date, another plan, another tenant, internal-only), sweep the threshold, and plot correct abstentions against false refusals. Recalibrate on every embedder, reranker, or major corpus change.

### 12.5 Two thresholds and the fallback mode

- **Above the high threshold:** answer normally.
- **Middle band:** hedge visibly, ask a clarifying question, escalate to a human, or retry with a stronger pass — per channel configuration.
- **Below the low threshold:** abstain (Section 12.7) and log a gap.

Customer-facing channels tolerate false refusals in exchange for fewer wrong answers. Internal agent-assist tolerates more hedging. Thresholds are therefore per-channel settings.

Raw-passage fallback (no covering canonical answer, evidence above threshold): the answer is labelled as drafted from unapproved sources, its confidence is capped below the canonical-answer band, and a review item proposes a canonical answer from it. Fallback answers are never served on the public channel in v1.

### 12.6 Contradiction at answer time

If the top reranked passages contain a high-confidence contradiction on the same claim, the generator must not blend them. Either the current, higher-authority passage wins and the answer surfaces the disagreement ("the current policy says X; an older document says Y"), or authority cannot decide and the system abstains into the review queue. The conflict is passed to the generator as explicit context, and a conflict-registry entry is created or updated either way.

### 12.7 Abstention wording

Never a bare "I don't know." An abstention states what was searched and what to try next: that the knowledge base was searched, how many documents covering which scope, that no current approved answer was found, and the suggested next step (a human agent, a named team, or a linked adjacent topic). Every abstention creates or increments a gap record.

---

## 13. Governance — canonical answers, conflicts, freshness, change impact

Governance does more work in this product than any retrieval technique. Retrieval raises recall; governance is what makes an answer trustworthy enough to show a customer.

### 13.1 Canonical answer lifecycle

```text
draft → in_review → approved → (needs_recheck → in_review → approved) → archived
```

- **Draft.** Created by ingestion proposals, by the fallback path, or by an editor. Never answers any channel.
- **In review.** An editor sees the draft side by side with its evidence quotes, detected conflicts, scope (plan, version, visibility), and the passages it was built from.
- **Approved.** Goes live for its scope. Approval records who, when, and which evidence version.
- **Needs recheck.** Set automatically by change impact (13.4), conflict detection (13.3), or expiry (13.2). The answer keeps serving on internal channels with a visible stale warning; on the public channel it serves until its hard expiry, then is withdrawn to abstention. Both behaviours are per-workspace settings with these defaults.
- **Archived.** Withdrawn from all retrieval. History retained.

An edit to an approved answer creates a new row in `answer_versions` and returns the answer to review. There is no direct edit of live text.

### 13.2 Freshness and verification

Modelled on the verified-card pattern: every canonical answer has an owner and a review interval. Defaults by type: pricing, policy, and legal — 30 days; procedures — 90 days; general product knowledge — 180 days. Owners are prompted before `next_review_at`; at the date the answer is flagged stale; after two failed verification cycles it is archived unless an owner re-approves it. Verification means re-checking the answer against its current evidence, not clicking a button: the review screen shows evidence diffs since approval.

Three distinct time problems are handled distinctly, never collapsed into one "freshness score":

- **Expiration** — the fact is now false: hard supersede plus tombstone on the retrieval path.
- **Temporality** — the fact is true only in a window: boost while `effective_from`–`effective_to` is active, expire after.
- **Versioning** — the fact was replaced: recency plus authority ranking, so the newer governing version outranks the older when both are retrievable.

A temporal adjustment sits between retrieval and generation: expired material is removed, active time-bounded material is boosted, the rest decays gently. Freshness never overrides relevance outright — a freshly edited wiki page can be wrong and an old signed policy can be current — which is why source tier participates alongside dates.

### 13.3 Conflict detection and resolution

Detection runs at index time, because that is when it pays off — before a customer meets the contradiction.

1. Cluster near-duplicate and same-topic passages by topic and entity. Whole-corpus pairwise comparison is infeasible, so clustering always comes first.
2. Within a cluster, run a lightweight entailment check (a natural-language-inference cross-encoder, or a cheap model pass over clustered candidates only) for pairs asserting different values for the same claim: different prices, dates, limits, eligibility rules, or steps.
3. Flagged pairs enter the `conflicts` registry with kind, detector, and score, and create a review item proposing a unified canonical answer.
4. Resolution is human: pick the winner (guided by effective dates and source tier), correct one source, scope the two answers to different plans or versions if both are right in context, or dismiss as a false positive. The registry entry records the resolution and is never deleted.

Recency and authority weighting narrow the blast radius at retrieval; they resolve nothing on their own, and the system must never pretend otherwise.

### 13.4 Change-impact analysis

On every new or updated source, a background job:

1. Diffs new passages against the superseded version.
2. Finds canonical answers that cite a changed passage, share an entity with changed text, or sit in the same topic.
3. Marks those answers `needs_recheck` and creates one grouped review item per source change, listing each affected answer with the specific passage diff.
4. Re-runs conflict detection over affected clusters.
5. Invalidates cached answers keyed to affected answers (Section 20).

This is the feature support teams feel most — "the returns policy changed twice and the assistant knows none of it" — and the one most competing tools lack.

---

## 14. Review queue, gap dashboard, and feedback loop

### 14.1 Review queue

One queue for every governance decision, so an owner can clear it in one sitting. Item kinds: new canonical answer, edit proposal, conflict, deduplication merge, topic merge or split, stale answer, low-confidence OCR, change-impact group, entity merge, glossary promotion.

Each item shows: what is proposed, the evidence for it, the diff against what is live, the scope affected, and one-click approve, edit-then-approve, or reject with a note. Priority ordering: public-channel change impact first, then conflicts on high-traffic topics, then stale answers by traffic, then everything else by age. Queue metrics — items pending over seven days, median time to decision — are health indicators, reported in Section 22.

### 14.2 Gap dashboard

Every abstention, every low-confidence answer, and every agent escalation feeds `gaps`. Raw questions are clustered the same way topics are (embedding clustering plus model naming), so the dashboard shows intents, not thousands of near-identical strings.

Columns: normalised question, count, first and last seen, channels seen on, status. Sorted by count weighted by recency. Actions: draft a canonical answer from selected source material, assign to an owner, mark as won't-answer (for example, out-of-scope questions the product deliberately does not cover), or link to an existing answer the retrieval missed — which files a retrieval defect, not a content gap, and feeds the evaluation set.

The distinction matters: a gap with no source material is a writing task; a gap where the source exists is a retrieval bug. The dashboard keeps them visibly separate.

### 14.3 Feedback loop

- Agent edits to a generated draft are measured (edit distance and whether the edit changed facts, tone, or scope). Material factual edits propose a canonical-answer revision into the review queue, pre-filled with the agent's wording.
- Thumbs feedback on answers attaches to the `qa_logs` row with its retrieval trace, so a bad answer is diagnosable as a retrieval failure, a governance failure, or a generation failure.
- Recurring corrections to the same answer escalate its review priority automatically.
- Feedback never trains or mutates anything automatically. It proposes; humans dispose.

---

## 15. User experience and surfaces

### 15.1 Ask (agent-assist chat)

The primary screen. A question box with channel, plan, and version context shown explicitly (defaults from the workspace, overridable per question). The answer panel shows: the draft reply, tone switcher, copy button, confidence band in plain language, citations expandable to exact quotes with source title and date, a "sources ignored" disclosure where staleness or conflict affected the result, and feedback controls. An abstention renders as a first-class result with next steps and a visible "logged as a gap" confirmation — not as an error.

### 15.2 Browse and edit (knowledge workspace)

Notion-like editing over canonical answers: Markdown editor, scope and visibility controls, owner and review-date panel, evidence panel with quotes linked to source passages, version history with diffs, and status controls matching the lifecycle in Section 13.1. Raw sources are viewable read-only alongside, with their parse, passages, and ingestion history.

### 15.3 Search

A dedicated search surface distinct from chat: hybrid search with filters for topic, entity, type, plan, version, visibility, status, and date. Exact-match mode for error codes and identifiers. Results show passages in their parent section, not isolated fragments.

### 15.4 Topic graph

An Obsidian-like browsing graph derived from Postgres: nodes are topics, canonical answers, and entities; edges are parent–child, related, supersedes, and cites. A small number of hub nodes per area (the map-of-content pattern) keep the graph connected rather than a field of islands. The graph is for navigation, discovery, and spotting orphans and over-connected topics. It is never a retrieval path in v1 or v2.

### 15.5 Dashboards

Gap dashboard (14.2), review queue (14.1), knowledge health (stale percentage, conflicts pending, orphan counts, evidence currency), and analytics (Section 22). One combined "needs attention" home for owners, ranked across all four.

---

## 16. Roles, permissions, and visibility

### 16.1 Roles

| Capability | Admin | Editor | Agent |
|---|---|---|---|
| Ask and draft replies | Yes | Yes | Yes |
| Edit drafts for own use | Yes | Yes | Yes |
| Create draft canonical answers | Yes | Yes | Propose only |
| Approve, archive, resolve conflicts | Yes | Yes | No |
| Manage topics, glossary, taxonomy | Yes | Yes | No |
| Manage members, providers, billing | Yes | No | No |
| Publish to public help centre | Yes | Yes | No |

### 16.2 Visibility model

Visibility values: `internal` and `public`. Settable on sources (as a default), passages (by override), and canonical answers. The public channel's retrieval query includes only `public`; the internal channel includes both, with internal material visually marked in agent-assist answers. There is no configuration in which the public channel's SQL filter can include internal rows — the allowed-visibility list is derived from the channel type in code, not from request parameters.

Leak testing is mandatory: the adversarial evaluation set includes questions designed to elicit internal-only content (internal codenames, unreleased pricing, internal caveats) run against the public channel. Any leak fails the release (Section 21.4).

---

## 17. Public help centre and integrations

### 17.1 Public help centre

Generated from approved, public canonical answers, organised by the frozen taxonomy. Each article is the canonical answer rendered for customers: no internal evidence panel, no confidence band, a "last verified" date from its review metadata. Search on the help centre uses the same hybrid retrieval restricted to public, current, approved content. An article is withdrawn automatically when its answer is archived or passes hard expiry — there is no separate unpublish step to forget.

### 17.2 Widget and Discord bot

Both are thin clients of the answering API with a fixed channel type, so visibility, thresholds, and abstention behaviour come from channel configuration, not from client code. The widget carries the customer's plan and product version where the host site knows them, and passes them as retrieval filters. The Discord bot answers only in configured channels, cites its sources, and abstains publicly rather than speculating — with the gap logged identically to any other channel.

### 17.3 Imports and exports

- Bulk import: folder upload and URL list, processed through the standard ingestion pipeline.
- Export: the full knowledge base as Markdown plus JSON — sources metadata, canonical answers with versions, evidence links, topics, glossary, and entities. A tenant can leave with their knowledge. Export is also the backup format for the knowledge layer.
- Ticket and chat imports in v1 are file-based (exports from the source system). Live helpdesk connectors are phase 3 (Section 26).

---

## 18. API contracts

FastAPI service, JSON over HTTPS, tenant resolved from the authenticated session or API key — never from a request-body field. All list endpoints paginate. All mutating endpoints are idempotent where a retry is plausible, using client-supplied idempotency keys.

### 18.1 Answering

```text
POST /v1/ask
  body: { question, conversation_id?, channel, plan?, product_version?,
          as_of?, tone? }
  200:  { answer_md, citations: [{ evidence_id, quote, source_title,
          source_date, passage_id }], confidence_band,
          sources_ignored: [{ title, reason }], conflict_notice?,
          answer_id?, answer_version?, abstained: false }
  200 (abstained): { abstained: true, searched_scope, next_steps,
          gap_id }

POST /v1/ask/{qa_log_id}/feedback
  body: { thumbs?, edited_answer_md?, note? }
```

### 18.2 Knowledge management

```text
POST   /v1/sources            (multipart upload or { url })
GET    /v1/sources/{id}       (parse status, passages, job history)
POST   /v1/sources/{id}/reingest
DELETE /v1/sources/{id}       (tombstones retrieval immediately)

GET    /v1/answers            (filters: topic, status, scope, stale)
POST   /v1/answers            (draft)
PATCH  /v1/answers/{id}       (creates a new version, returns to review)
POST   /v1/answers/{id}/approve | /archive

GET    /v1/review-items       (filters: kind, priority, age)
POST   /v1/review-items/{id}/decide { decision, note?, edited_payload? }

GET    /v1/gaps               (filters: status, channel; sort: weighted count)
GET    /v1/conflicts          (filters: status, topic)
GET    /v1/topics, /v1/terms, /v1/entities
GET    /v1/search             (hybrid search surface, Section 15.3)
GET    /v1/export             (Markdown + JSON archive)
```

### 18.3 Errors

Errors are normalised across providers and internal failures into: `validation`, `not_found`, `permission`, `provider_unavailable`, `provider_rejected`, `rate_limited`, `ingest_failed`, each with a human-readable message and a correlation identifier that also appears in `qa_logs` or `ingest_jobs`. A provider failure on the answering path returns `provider_unavailable` and never a fabricated answer.

---

## 19. Background jobs

All jobs run in the FastAPI worker with retries, exponential backoff, and a dead-letter state visible in the admin UI.

| Job | Trigger | Work |
|---|---|---|
| Ingest | Upload, URL, re-ingest | Section 8 pipeline stages |
| Contextualise | Ingest stage | Blurbs for new or changed chunks |
| Conflict scan | Ingest, answer edit | Cluster-then-entailment pass over affected clusters |
| Change impact | Source version change | Section 13.4 |
| Semantic dedup | Nightly | Section 9.3, level 3 |
| Topic discovery | Weekly | Section 9.2, proposal only |
| Freshness sweep | Daily | Expiry flags, owner prompts, archive after two failed cycles |
| Hygiene | Nightly | Section 9.5 health report |
| Gap clustering | Daily | Cluster new gap questions into intents |
| Eval run | On pipeline change, weekly | Section 21 suites against the current index |
| Export | On demand, nightly backup | Section 17.3 |

---

## 20. Caching and invalidation

- **Prompt caching** at the provider, where supported, for repeated system prompts and for the cached document during contextualisation. An optimisation only.
- **Semantic answer cache:** identical or near-identical standalone questions within the same tenant, channel, plan, version, and visibility scope may reuse a prior answer — but validity is keyed to **knowledge version, not elapsed time**. Every canonical answer and passage carries a version stamp; the cache entry records the versions it was built from. Any approval, archive, supersede, or change-impact event touching those versions invalidates the entry immediately. A time-to-live is a backstop, never the mechanism.
- **Retrieval cache:** none in v1. Retrieval is cheap relative to generation, and caching it invites stale-scope bugs.

---

## 21. Evaluation and quality gates

The evaluation harness is built before any tuning. Tuning without it is guessing, and regressions in this product are silent — answers stay fluent while getting wronger.

### 21.1 Test sets

**Golden set (about 50–100 items to start).** Hand-curated from real support material: factoid, procedure, troubleshooting, comparison, synthesis, and multi-hop questions, each with a ground-truth answer, its required evidence passages, and its scope (plan, version, channel).

**Adversarial set.** Plausible but unanswerable questions in equal weight to the golden set: out of scope, out of date, another plan's feature, another tenant's domain, and internal-only material asked on the public channel.

**Version and conflict set.** A corpus deliberately containing superseded and conflicting versions, with questions whose correct answer depends on selecting the current authority. Scored separately for retrieval of the current version and for answer correctness — because a stale answer can score perfectly on faithfulness while being wrong.

All three sets live in the repository, are versioned alongside the pipeline, and grow from real failures: every production failure that reaches a human becomes a test case.

### 21.2 Metrics

- **Retrieval:** recall at k, precision at k, normalised discounted cumulative gain, mean reciprocal rank — reported per topic, not only globally. A global average hides a broken topic.
- **Generation (RAGAS-style):** faithfulness (fraction of atomic claims supported by context), answer relevancy, context precision, context recall. Starting production bars from the research: faithfulness at least 0.80, answer relevancy at least 0.85, context precision at least 0.75, context recall at least 0.80. These scores come from a model judge, inherit judge bias, and drift if the judge changes — pin the judge model per evaluation run and prefer a different provider for judging than for generating.
- **End to end:** correctness against ground truth, refusal calibration (correct abstentions versus false refusals), citation precision and recall, version correctness (did it cite the current version?), conflict handling (did it surface the contradiction rather than blend it?), and internal-leak count on the public channel, which must be zero.

### 21.3 Cadence

The full suite runs on every change to chunking, embedding model, reranker, prompts, thresholds, or taxonomy, and weekly against production-like data. A human spot-check pass accompanies automated runs; automated metrics disagree with each other often enough that no single score gates a release alone.

### 21.4 Release gates

A release fails if: any internal-only material is retrieved on the public channel in the adversarial set; faithfulness falls below the bar on the golden set; version correctness regresses on the conflict set; or correct-abstention rate falls while false-refusal rate does not improve. Gate failures block deployment, not just generate a warning.

---

## 22. Observability, analytics, and audit

- **Per-question trace.** Every `qa_logs` row carries the standalone query, retrieved passages, rerank scores, citations, confidence components, model and provider, latency, and token cost. Any answer a customer received can be reconstructed exactly.
- **Product analytics.** Top questions by channel, deflection rate (questions answered without escalation), abstention rate, fallback-mode rate, and per-answer agent edit rate — the last being the best proxy for answer quality in production.
- **Knowledge health.** Percentage of answers past review date, percentage of retrieved passages superseded (target zero), median age of cited evidence, conflicts pending over seven days, gap rate, review-queue age.
- **Audit log.** Append-only record of approvals, edits, conflict resolutions, visibility changes, provider-configuration changes, exports, and deletions, each with actor, timestamp, and reason where given.
- **Errors and cost.** Sentry for exceptions; per-tenant, per-role token spend dashboards, because the answer model dominates variable cost (Section 24).

---

## 23. Security and privacy

This section is a design specification, not legal advice. Data-protection terms must be confirmed with a qualified adviser before launch, especially for EU customers.

### 23.1 Roles under data-protection law

The product is the controller of its tenants' knowledge-base data; each AI provider is a processor, engaged under a written data-processing agreement that limits it to the product's instructions. Routing through a gateway makes the gateway the processor and the underlying model providers its sub-processors — each one needs the same check. Transfers outside the EEA need a valid transfer mechanism. The product publishes its own data-processing addendum and sub-processor list for tenants.

### 23.2 Provider data terms

Before a provider is enabled: confirm it does not train on API data by default (or that an opt-out or enterprise term applies), record its retention period for prompts and outputs, and record whether an EU-residency or zero-retention option exists and under what conditions. Research-period findings, to be re-verified at enablement time: major US and EU paid tiers generally do not train on API data; some cheaper providers were reported to train by default; abuse-monitoring retention commonly runs 30–55 days; EU inference is available through specific cloud routes for some model families and not directly for others.

### 23.3 Data minimisation in prompts

- Prompts carry retrieved evidence only — the minimum passages needed for the question.
- Ingestion redacts detected secrets, credentials, and unnecessary personal data before storage of provider-bound artefacts, and redacted spans are excluded from prompts entirely.
- Customer-identifying data from tickets (names, emails, account identifiers) is masked in passages used for answering unless the answer genuinely requires it, which support knowledge almost never does.

### 23.4 Deletion

A tenant deletion request deletes: source files in storage, passages and their vectors, cached answers derived from them, and any provider-side files or indexes created from them. The retrieval path honours deletion immediately via tombstones; physical purge follows the retention schedule. The audit log records that a deletion occurred, without retaining the deleted content.

### 23.5 Application security basics

Tenant isolation tested adversarially (Sections 5.4, 21.1); visibility enforced in SQL (Section 16.2); provider keys held in server-side configuration only, never exposed to the web client; file uploads validated by content type, size-capped, and parsed in the worker, never in the web process; allowlists for URL ingestion to prevent server-side request forgery against internal addresses.

---

## 24. Costs and capacity

All figures are research-period estimates (November 2026) and must be re-checked against live vendor pricing before budgeting. The shape of the costs is more durable than the numbers.

### 24.1 Where the money goes

- **Embeddings: negligible.** Cents to a dollar or so to index ten million tokens on cheaper models; query embeddings are a rounding error.
- **Reranking: small.** On the order of $0.10 per 1,000 queries for a lite reranker — about $3 per month at 1,000 questions a day — versus roughly $60 per month at the same volume for a premium reranker at about $2 per 1,000 searches.
- **Chat: the variable cost that matters.** Ingestion-side calls (blurbs, rewrites, classification) on a cheap model run to roughly a dollar per million document tokens for contextualisation. Answering at 1,000 questions a day, at roughly 2,000 input and 500 output tokens per question, is on the order of $10–40 per month on cheap model tiers and $100–300 per month on frontier tiers. Hence per-role models (Section 6.3): cheap everywhere except the final answer.
- **Infrastructure: the fixed floor.** Roughly $25–50 per month: database, web hosting, worker hosting, and storage.

**Realistic MVP run-rate: about $30–80 per month before paying customers**, dominated by infrastructure and the answer model — not by embeddings or the vector store.

### 24.2 Cost controls

Per-tenant token budgets with alerting; per-role model ceilings an admin cannot exceed without a billing warning; semantic caching (Section 20); cheap-model-first ingestion with escalation only on low parse or extraction confidence; and `qa_logs` cost attribution so a single noisy channel is visible immediately.

### 24.3 Capacity and migration triggers

pgvector is sufficient well beyond MVP scale (the research cites usable latency at one million vectors, with HNSW indexing). Migrate the vector layer to a dedicated store such as Qdrant only when one of these is measured, not anticipated: sustained filtered-search latency breaching the product's target at production volume, a corpus beyond roughly 5–10 million passages per deployment, or a hard latency requirement pgvector demonstrably misses on the golden workload. Add a graph database only if multi-hop traversal queries become a measured product need. Both migrations are feasible because the vector index is derived data (Section 6.5) and the graph is a derived view (Section 15.4).

---

## 25. Failure modes and mitigations

| Failure | Symptom | Mitigation in this design |
|---|---|---|
| Right chunk never retrieved | Answer exists, never reaches context | Contextual retrieval, hybrid search, multi-query, reranking; recall measured per topic |
| Stale or conflicting chunk retrieved | Confident wrong answer, grounded in old text | Metadata-first filtering, source tier and recency in ranking, conflict registry, canonical-answer-first answering |
| Rule split from its condition | Answer omits the eligibility limit | Parent–child return, contextual blurbs, heading-aware splits, procedure steps never split |
| Chunk too large | Diluted embedding, noisy context, middle material ignored | Child chunks of 256–512 tokens, token budget and dedup at assembly, rerank to 5–8 |
| Graph noise | Distractor facts, worse relevance than plain retrieval | Closed entity and relation schema, entities as anchors, graph for navigation only, single-mention pruning |
| Taxonomy churn | Topics renamed weekly, trends and links break | Discovery-then-freeze, versioned taxonomy, merges and splits via review with migration mappings |
| Deduplication false merge | Two distinct products or customers merged | Conflict-aware merging refusing joins across different strong identifiers, conservative thresholds, review for uncertain pairs |
| Query-expansion drift | Invented terms retrieve invented evidence | Original query always retained, HyDE vector-only, glossary-grounded expansion, rerank on the standalone query |
| Embedding-model swap | Dimension mismatch, mixed vector spaces | Versioned embeddings, new index built alongside, golden-set validation, configuration flip |
| Visibility leak | Internal note in a customer answer | SQL-level visibility filter per channel, adversarial leak tests as a release gate |
| Ghost content | Deleted source still answers | Tombstones on the retrieval path at deletion time |
| Silent conflict resolution | Model blends two contradictory passages | Explicit conflict context to the generator, surface-or-abstain rule, registry entry either way |
| Cache staleness | Old answer served after a policy change | Knowledge-version-keyed invalidation, never time-based validity |
| Provider degradation | Malformed structured output, tool errors | Conformance checks per provider, parse-repair-retry, role fallback models, normalised errors |

---

## 26. Build phases

### Phase 1 — governed answering core (weeks 1–2)

Postgres schema (Section 7, excluding entities and graph tables); Docling ingestion to heading-aware recursive chunks; pgvector plus `tsvector` hybrid retrieval with Reciprocal Rank Fusion; metadata filtering; canonical-answer create, review, approve flow; chat answering only from approved answers with stored citations; basic `qa_logs`.

Exit criteria: a messy folder of real support documents goes in, approved answers come out, and every answer cites stored evidence.

### Phase 2 — retrieval quality and abstention (week 3)

Contextual blurbs; reranker; composed confidence with two thresholds; abstention path with gap logging; tone variants.

Exit criteria: golden and adversarial sets exist, the harness runs, and thresholds are calibrated rather than guessed.

### Phase 3 — organisation basics (week 4)

Glossary and taxonomy freeze workflow; exact and near-duplicate deduplication at ingest; multi-query and HyDE expansion; parent–child return in generation context.

Exit criteria: re-ingesting a near-duplicate corpus produces aliases and merge proposals, not duplicate answers.

### Phase 4 — governance depth (month 2)

Entity extraction on the closed schema; topic graph view derived from Postgres; topic discovery proposals; conflict detection (cluster, then entailment); change-impact job; effective-date and version enforcement throughout; gap dashboard with clustering; public help centre; widget or Discord bot (one channel first).

Exit criteria: changing a policy document produces a change-impact review item naming every affected answer, and a planted contradiction produces a conflict item before it can be answered from.

### Phase 5 — hardening and launch readiness

Adversarial and version-conflict suites as release gates; provider conformance suite; per-tenant budgets; export and deletion flows verified end to end; privacy documentation and sub-processor list published.

### Phase 6 — only on measured need

Live helpdesk connectors, per-topic summary trees (Section 10.5), multilingual support, voice-note ingestion, dedicated vector store or graph database per the triggers in Section 24.3.

---

## 27. Deferred work and explicit non-goals

Deferred, with the trigger that would revive each:

- Full graph-based retrieval (community summaries over an extracted knowledge graph) — only if synthesis questions become a proven, measured need, and then as a separate summary index.
- Whole-corpus summary trees — superseded by the limited per-topic version in Section 10.5.
- A dedicated vector database or graph database — only on the measured triggers in Section 24.3.
- Agentic multi-hop retrieval — only if the multi-hop failure rate on the golden set justifies its cost and unpredictability.
- Self-hosted models — out of scope for the hosted product; the provider abstraction keeps a local endpoint possible for development.
- Audio overviews and other NotebookLM-style media features — a trap for v1; they do not help a support team answer correctly.
- Replacing a helpdesk — never a goal. This product makes the knowledge a helpdesk runs on trustworthy; integrations come before substitution.

Non-goals at any phase:

- Auto-publishing AI-proposed content without human approval.
- Answering from unapproved material on a public channel.
- Training any model on tenant data.
- Resolving conflicts silently, by recency alone, without a human decision on record.

---

## 28. Risks and open decisions

### 28.1 Risks

- **Governance bypass.** If approving and resolving is slower than working around the system, agents will paste raw model output instead. Mitigation: the review queue is designed for minutes-long sessions, and fallback-mode answers always generate their own review items.
- **Cold start.** A new tenant's knowledge base is only as good as their messiest documents. Mitigation: ingestion reports parse and coverage quality honestly; the gap dashboard gives an immediate, concrete worklist.
- **Provider volatility.** Model names, prices, and terms move fast; several figures in this spec are November 2026 snapshots. Mitigation: per-role configuration, conformance tests, and a standing rule to re-check vendor pages before any budgeting or default-model decision.
- **Evaluation theatre.** Scores that look precise while the judge model drifts. Mitigation: pinned judge per run, human spot checks, per-topic reporting, and production edit-rate as a ground-truth proxy.
- **Scope creep toward a helpdesk.** Ticketing is a gravitational pull and a different product. Mitigation: Section 27 non-goals, and integrations framed as channels, not features.

### 28.2 Open decisions for the builder

- First channel after the web app: embeddable widget or Discord bot. (A Discord bot is the faster proof if the first users are community support teams.)
- Default embedding and rerank providers at launch, after a golden-set bake-off on real documents — not before.
- Whether internal-channel `needs_recheck` answers keep serving with a warning, or abstain, as the default for paying tenants.
- Pricing shape: per-seat, per-workspace, or usage-banded — explicitly not per-resolution, which punishes the customer for the product working.
- Target corpus languages at launch; anything beyond English reopens Sections 6.5 and 9.1.

---

## 29. Acceptance criteria for v1

V1 is done when all of the following are demonstrably true on a real, messy support corpus:

1. A user can upload a mixed folder (PDFs, documents, text files, screenshots, a ticket export, and URLs) and receive a structured knowledge base without formatting anything first.
2. Duplicate and near-duplicate documents are aliased or proposed for merge; no question ends up with two competing approved answers for the same scope.
3. A planted contradiction between two sources produces a conflict review item, and neither version is served as settled truth until a human resolves it.
4. A pricing or policy change, ingested as a new document version, produces a change-impact item naming every affected canonical answer, and superseded passages stop being retrieved.
5. An answerable question returns a draft with citations that verify against stored evidence, in each tone variant, without factual drift between tones.
6. An unanswerable question abstains with searched scope and next steps, and appears on the gap dashboard, clustered with similar questions.
7. An internal-only fact cannot be elicited on the public channel, the widget, or the bot, under the adversarial test set.
8. The same question scoped to two different plans or product versions returns the correct different approved answers.
9. Swapping the configured chat provider requires configuration changes only, and passes the conformance suite; swapping the embedding provider triggers a full re-index that validates against the golden set before going live.
10. A tenant can export their entire knowledge base as Markdown plus JSON, and can delete a source and verify it no longer answers.
11. The evaluation suite runs in CI-equivalent form, and the release gates in Section 21.4 pass.

---

## Appendix A. Glossary

- **Canonical answer** — the single approved, versioned answer to a question or topic, with owner, expiry, scope, and stored evidence. The only source of truth for answering.
- **Passage** — an immutable chunk of a source document version, the unit of evidence and retrieval.
- **Parent / child chunk** — a section-sized unit returned for reading, and the smaller unit indexed for matching, respectively.
- **Contextual blurb** — a short model-written description situating a chunk in its document, prepended before embedding and keyword indexing.
- **Source tier** — an authority ranking of a source's origin, from system of record down to chat export.
- **Standalone query** — a question rewritten to be self-contained, with conversation references resolved.
- **HyDE** — hypothetical document embedding: embedding a model-written plausible answer as an extra query vector.
- **Reciprocal Rank Fusion (RRF)** — combining ranked lists by rank position rather than raw score, with a smoothing constant (here, k = 60).
- **Reranker** — a cross-encoder model that rescores retrieved candidates against the query for precision.
- **Abstention** — a deliberate non-answer with searched scope and next steps, logged as a gap.
- **Gap** — a clustered record of questions the knowledge base could not answer.
- **Conflict** — two sources or answers asserting incompatible claims about the same thing, held in a permanent registry until a human resolves it.
- **Change impact** — the set of canonical answers affected by a source change, computed and queued for recheck.
- **Tombstone** — a deletion marker that removes content from retrieval immediately while preserving the audit record.
- **Review queue** — the single queue through which every AI proposal becomes, or fails to become, approved knowledge.

## Appendix B. Sources

This specification synthesises the product brainstorm and a technical research report dated 22 November 2026. Prices, model names, and provider terms cited in it are point-in-time findings from that research (mostly September–November 2026 sources) and must be re-checked on vendor pages before budgeting or committing to a default provider. Principal sources behind the research:

- Anthropic, contextual retrieval measurements, via the research report's cited summaries of the September 2024 announcement.
- Microsoft GraphRAG paper and LinearRAG (ICLR 2026) on graph-based retrieval quality and noise: https://arxiv.org/pdf/2404.16130 and https://arxiv.org/html/2510.10114v4
- RAPTOR (ICLR 2024): https://arxiv.org/html/2401.18059v1
- Fini documentation on messy and conflicting documents: https://docs.usefini.com/en/knowledge/messy-or-conflicting-docs
- LiteLLM supported endpoints and providers: https://docs.litellm.ai/docs/supported_endpoints
- OpenAI, migrating to the Responses API: https://developers.openai.com/api/docs/guides/migrate-to-responses?api-mode=responses
- RAGAS evaluation guidance and abstention and confidence research, as cited in the research report.
- Competitor and pricing roundups for NotebookLM, Notion, Guru, Slab, Document360, Zendesk, and Intercom / Fin, as cited in the research report — several of these prices are quote-only or third-party reported and need direct verification.

Where the research could not verify a claim — live vendor pricing, competitor pricing from roundups, and any benchmark on this product's own future corpus — this specification says so in place rather than presenting the figure as settled.
