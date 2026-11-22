# Harbour Knowledge

A governed support knowledge base: messy documents go in, approved answers come out, and every answer cites stored evidence. Raw passages are a labelled fallback — never the source of truth.

This is the first implementation slice of [support-knowledge-base-design-spec.md](support-knowledge-base-design-spec.md).

## What works

| Area | Status |
|---|---|
| Postgres schema with tenant RLS, pgvector, full-text search, answer versions, conflicts, gaps, QA logs | Done |
| Upload / URL ingestion with background jobs, duplicate aliases, heading-aware parent + child chunks, contextual blurbs | Done |
| Hybrid dense + lexical retrieval with Reciprocal Rank Fusion, SQL metadata filters, reranking, exact error-code lookup | Done |
| Canonical answers: automatic drafts from sources and fallback questions, one-click publish, edit as a new immutable version, archive, freshness dates | Done |
| Answering from approved answers first; labelled fallback from raw passages; citations verified against stored quotes; abstention | Done |
| Visibility isolation (public vs internal) and plan/version scope enforced in SQL before ranking | Done |
| Review queue: new answers, edits, conflicts, change impact, stale-answer feedback | Done |
| Conflict detector with permanent registry and review resolutions | Done |
| Gap clustering, gap status, coverage dashboard, recent-question metrics | Done |
| Workspace UI: Knowledge home with one-step add and live source status, Ask, and Review only when a person is genuinely needed | Done |
| Deterministic offline intelligence (`AI_MODE=dev`) so every path runs without an API key | Done |
| OCR for screenshots and scanned PDFs | Not yet — image uploads are accepted but text extraction is next |
| Real authentication / sessions | Not yet — development uses a header stub, and production fails closed |
| Public help centre, widget, Discord bot, billing | Not yet |

## Stack

- **Web:** Next.js 16 App Router, React 19, Tailwind CSS 4, lucide icons.
- **API and worker:** FastAPI, psycopg 3, and one elected polling worker per deployment (Postgres advisory lock). It requeues jobs left running by a crashed process.
- **Database:** Postgres 16 + pgvector. One store for relational data, vectors, and full-text search.
- **Intelligence:** OpenAI-compatible chat (including a fully local Ollama model), a local Sentence-Transformers embedding provider, and a deterministic offline provider for development.

## Quick start

Prerequisites: Docker, Python 3.12 with `uv`, Bun (or Node 20+ with npm), and `psql`.

```bash
cp .env.example .env
make db               # Postgres + pgvector on 127.0.0.1:55432
make migrate          # applies new migrations; applied files are tracked
make local-embeddings # optional: install the local GPU embedding stack
make seed             # realistic messy corpus, conflicts, gaps, approved answers
```

Start the two services in separate terminals:

```bash
make api         # http://127.0.0.1:8000  (also starts the ingestion worker)
make web         # http://127.0.0.1:3000
```

Open http://127.0.0.1:3000. It lands on **Knowledge**: add a file or URL at the top, watch it process, and only open **Review** when Harbour flags a decision it cannot make alone. The seeded workspace is `harbour`; the development member is `Maya Chen` (`maya@harbour.example`, admin).

`make seed` deletes and recreates the `harbour` tenant. It is the clean reset path for demos.

## Fully local chat

The app can run without Vyce or any hosted model. The recommended fit for a 6 GB GPU is **Qwen3-4B-Instruct-2507 Q4_K_M**: it uses about 2.5 GB of weights, runs fully on the GPU at roughly 35–55 tokens/s, and is reliable at strict JSON, which this pipeline needs for classification, rewriting, and citation drafting.

```bash
make local-chat-install   # download Ollama into storage/ (once)
make local-chat           # terminal 1: 127.0.0.1:11434
make local-chat-pull      # terminal 2: pull the model (~2.5 GB)
make local-chat-warm      # load it before the first question
```

Then point `.env` at it:

```dotenv
AI_MODE=openai
OPENAI_BASE_URL=http://127.0.0.1:11434/v1
OPENAI_API_KEY=ollama
MODEL_INGEST_SMALL=qwen3:4b-instruct-2507-q4_K_M
MODEL_REWRITE=qwen3:4b-instruct-2507-q4_K_M
MODEL_ANSWER=qwen3:4b-instruct-2507-q4_K_M
MODEL_JUDGE=qwen3:4b-instruct-2507-q4_K_M
EMBEDDING_PROVIDER=local
LOCAL_EMBEDDING_DEVICE=cpu
LOCAL_EMBEDDING_DTYPE=float32
```

On a 6 GB card, the chat model takes the GPU and the 0.6B embedder runs on the CPU. Putting both on the GPU leaves no headroom and can fail with CUDA out-of-memory; the CPU embedder still indexes the demo corpus in about 3.4 seconds. If you have more VRAM, set `LOCAL_EMBEDDING_DEVICE=cuda` and restart the API. Changes to embedding model or dimension need `make reindex`.

`make local-chat` uses the project-local runtime in `storage/`; set `OLLAMA_BIN` to use a system-wide Ollama instead. The first request after a cold start loads the model in a few seconds. Keep the Ollama server running while using Ask or ingestion.

## The demo loop

1. **Add knowledge** — drop a document onto the home page or paste a URL. Harbour reads, classifies, links, and indexes it automatically; the source list updates on its own.
2. **Ask** — ask “What is the refund window for annual plans?”. The answer comes from an approved canonical answer with two verified quotes.
3. **Review** — only genuine decisions appear. Harbour drafts publish-ready answers for questions it can ground in sources, so most items are one click. Conflicts show a suggested source based on authority and recency. Press `J`/`K` to move, `A`/`R`/`D` to decide, and `N` to add a note. After a decision, press `U` (or **Undo** in the toast) within 15 seconds to revert it, including any answer, conflict, or version side effects.
4. **Open a source** — the extracted text is the default view. Passages and processing details are tucked behind progressive disclosure, so the page stays readable. To remove sources, use the trash icon on a knowledge row, or choose **Select** to pick several and remove them at once; a source that is still being read is withdrawn when that read finishes. Decisions and conflicts tied to a removed source leave the review queue with it, and approved answers that cited it move to **Needs recheck** so the ready count stays honest.
5. **Ask a gap** — ask “Do you offer a nonprofit discount?”. The system abstains and logs the question. If it finds enough evidence to draft an answer, that draft appears in Review ready to publish.
6. **Public check** — open **Change** in the Ask composer and switch the channel to **Public help centre**. Internal answers and internal citations are not available.

## Governance invariants

- **Every tenant-owned row has `tenant_id`.** The app connects as `kb_app`; RLS is keyed to the transaction-local `app.tenant_id`. The only privileged read is `tenant_id_for_slug`.
- **Approved answers are truth.** A customer-facing channel only serves approved, public answers. Raw passages can produce an internal fallback, clearly labelled as unapproved.
- **Evidence is frozen to a version.** `answer_evidence` rows point at a passage and store the exact quote used at approval time. Re-ingesting a source never silently repoints a citation.
- **Edits create versions.** A workspace edit inserts a new `answer_versions` row, returns the answer to review, and stops it answering until re-approved.
- **Visibility and scope are SQL filters.** They are applied to candidate passages before ranking, not passed to a model as an instruction.
- **Abstention is a result.** A failed answer creates or increments a gap and writes a `qa_logs` row with retrieved evidence and scores.
- **Conflicts do not disappear.** They live in `conflicts` with a resolution/audit trail, independent of the review item that surfaced them.

## Project layout

```text
apps/web/                 Next.js workspace
  app/                    Knowledge home, Ask, Review, plus answer/source detail, gaps, topics, and search
  components/             Answer cards, review cards, source upload, shared UI
  lib/api.ts              Server-side API client and dev tenant headers
services/api/             FastAPI service
  app/ingest/             Parsers, heading-aware chunking, pipeline, storage
  app/retrieval/          Hybrid search, confidence, answering path
  app/routers/            HTTP contracts
  app/worker.py           Committed ingest-job poller (runs inside the API process)
  app/seed.py             Deterministic demo corpus and answer proposals
  app/reindex.py          Re-embeds current passages after a model change
db/migrations/            RLS schema, tenant lookup, source scope, worker queue, vector size
scripts/local-chat.sh     Project-local Ollama install/serve/pull/warm helper
seed/corpus/              Messy demo documents
storage/                  Uploaded originals (gitignored)
```

## Configuration

Copy `.env.example` and keep the defaults for a local demo. Important variables:

- `DATABASE_URL` — RLS-enforced application role.
- `MIGRATION_DATABASE_URL` — migration/seed owner role. Never expose it to the API.
- `AI_MODE=dev` — deterministic offline chat. Set `AI_MODE=openai` and fill the chat model variables to use an OpenAI-compatible endpoint, including local Ollama.
- `EMBEDDING_PROVIDER=local` — run embeddings on this machine with Sentence-Transformers. `dev` is the deterministic fallback; `openai` uses the endpoint's `/embeddings` route.
- `LOCAL_EMBEDDING_MODEL` — defaults to `Qwen/Qwen3-Embedding-0.6B` (1024 dimensions, about 1.2 GB of VRAM at fp16). Set `LOCAL_EMBEDDING_DEVICE=cpu` when a local chat model is using the GPU.
- `make local-chat*` — install, start, pull, and warm the local Ollama model documented above.
- `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_PORT` — Docker bootstrap values, read by `docker compose`. Keep `MIGRATION_DATABASE_URL` in sync if you change them.
- `OPENAI_EMBEDDING_DIMENSIONS` — defaults to `true` and asks OpenAI-compatible embedding endpoints for 1024 dimensions. Disable it only for a provider that rejects the optional parameter; the model must still return exactly `EMBEDDING_DIM` values.
- `INTERNAL_LOW/HIGH_THRESHOLD` and `PUBLIC_LOW/HIGH_THRESHOLD` — abstention bands.
- `API_URL`, `DEV_TENANT_SLUG`, `DEV_USER_EMAIL` — read by the Next server.

The index uses 1024 dimensions (migration 006). After switching embedding models or providers, run `make reindex`; the migration clears stale vectors so the old and new models are never compared. `make api`, `make seed`, `make reindex`, and `make test` automatically include the local embedding extra only when `.env` sets `EMBEDDING_PROVIDER=local`; override with `UV_EXTRA=local-embeddings` or `UV_EXTRA=`.

## Verification

```bash
make test      # API tests + web typecheck
make check     # test, then the production web build
curl -s http://127.0.0.1:8000/healthz
```

Manual checks that matter more than a build:

- Ask the same question on **Agent assist** and **Public help centre** and confirm the public path cannot see an internal answer.
- Edit an approved answer, confirm it returns to review, then approve it and confirm the version number increments.
- Ask a question that only raw sources can answer, then confirm Review contains a publish-ready draft rather than an empty task.
- Re-upload an identical file and confirm it becomes an alias instead of a second document.
- Open the seeded conflict and resolve it with a note; the conflict registry row moves to `resolved`.
- Abstain from a question, then confirm the gap count increments and the question appears under **Coverage gaps**.

## Known limitations and next work

1. **OCR.** Add a `tesseract` parser stage for images and scanned PDFs, store OCR confidence as `parse_quality`, and create a `low_ocr` review item below threshold.
2. **Auth.** Replace the development header stub with Clerk/Supabase/Auth.js and derive the tenant from the verified session. The current production guard intentionally returns 503.
3. **Public help centre and channels.** Add a public read surface and one thin channel (widget or Discord) over the same answering contract.
4. **Evaluation harness.** Build the golden and adversarial sets before tuning thresholds; the current bands are explicit defaults, not calibrated values.
5. **Semantic dedup and change impact.** Document-hash aliases exist; near-duplicate passage detection, topic discovery, and a richer change-impact pass remain.
6. **Provider testing.** The OpenAI-compatible client is tested against local Ollama; a hosted-model matrix and more retrieval evaluation remain.
