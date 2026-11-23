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
| Answering from approved answers first; labelled fallback from raw passages; verbatim extraction of a stated attribute; citations verified against stored quotes; abstention | Done |
| Visibility isolation (public vs internal) and plan/version scope enforced in SQL before ranking | Done |
| Review queue: new answers, edits, conflicts, change impact, stale-answer feedback | Done |
| Conflict detector with permanent registry and review resolutions | Done |
| Gap clustering, gap status, coverage dashboard, recent-question metrics | Done |
| Workspace UI: Knowledge home with one-step add and live source status, Ask, and Review only when a person is genuinely needed | Done |
| Deterministic offline intelligence (`AI_MODE=dev`) so every path runs without an API key | Done |
| OCR for screenshots and scanned PDFs | Not yet — image uploads are not supported, and scanned PDFs need OCR before ingestion |
| Real authentication / sessions | Not yet — development uses a header stub, and production fails closed |
| Public help centre, widget, Discord bot, billing | Not yet |

## Stack

- **Web:** Next.js 16 App Router, React 19, Tailwind CSS 4, lucide icons.
- **API and worker:** FastAPI, psycopg 3, and one elected polling worker per deployment (Postgres advisory lock). It requeues jobs left running by a crashed process up to a bounded retry limit, then fails them.
- **Database:** Postgres 16 + pgvector. One store for relational data, vectors, and full-text search.
- **Intelligence:** OpenAI-compatible chat (including a fully local Ollama model), local or remote OpenAI-compatible embeddings, Cohere-style or NVIDIA NIM reranking, and a deterministic offline provider for development.

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

Supported uploads have these extensions: `.pdf`, `.docx`, `.pptx`, `.html`, `.htm`, `.txt`, `.md`, `.markdown`, `.csv`, and `.log`. XLSX, EPUB, images, and files without a supported extension are rejected; export them to a supported format first. PDFs require an extractable text layer. Supported extensions do not guarantee that a malformed file or unsupported document structure can be parsed.

`make seed` deletes and recreates the `harbour` tenant. It is the clean reset path for demos.

Ingestion claims are persisted before processing; abandoned running jobs are requeued after 30 minutes, up to three recovery attempts. A failed replacement retains the last complete indexed version and reports the failed job. Manually re-queueing resets the recovery budget. URL snapshots are immutable; a crash can leave an unreferenced snapshot that is not automatically cleaned up.

Separators are not knowledge: a horizontal rule or any other block without a letter or digit never becomes a passage, because a near-neutral vector for `---` outranks real evidence in dense ranking while offering nothing to cite. Contextual blurbs are written once per section and shared by that section's passages rather than once per passage; on a single GPU that removes most of ingestion's model calls while keeping the document, section, plan and version context in the embedded text.

The local embedding provider checks its configured token limit before encoding. Oversized tables and passages may be split; an indivisible oversized table row, sentence, or code block fails with a message to split the source. Existing sources are not automatically re-ingested, and existing embeddings may already have been truncated. Re-ingestion creates a new document version and can require review of answers citing the older one. Remote embedding providers do not yet have equivalent truncation checks.

Dense retrieval is gated by an embedding identity: provider, model, dimensions, query instruction, sequence limit, remote `input_type`, embedding version, and ingestion pipeline revision. A passage is only considered for dense ranking when its recorded identity matches the active configuration. If they differ, Search and Ask use keyword matching and say the index needs rebuilding instead of comparing incompatible vectors; `/v1/meta` reports compatible, stale, and total passage counts. After changing any of those settings, run `make reindex` for the default workspace. For another workspace, run `cd services/api && uv run --extra local-embeddings python -m app.reindex <slug>`. The rebuild embeds in committed batches, switches one workspace atomically, and resumes staged batches after an interruption. An older passage that exceeds the new limit stays out of the rebuilt index and must be re-ingested so the chunker can split it.

**Upgrading from pipeline revision 2:** the embedded text of a passage changed (separators are no longer indexed and blurbs are per section), so the identity is now `p3-…`. Existing passages stay out of dense ranking until you re-ingest each source and then run `make reindex`; `make seed` is the clean reset for a demo workspace.

To run the PostgreSQL ingestion failure tests against the migrated database, use `cd services/api && HARBOUR_TEST_POSTGRES=1 ./.venv/bin/python -m pytest -q tests/test_ingest_atomicity.py`. They create and remove an isolated temporary tenant and do not reset your workspace. Stop the API first, or hold the worker's lock (`psql "$MIGRATION_DATABASE_URL" -c "select pg_advisory_lock(1212240450)" -c 'select pg_sleep(120)' &`), because the ingestion worker is global by design and will otherwise claim a test tenant's queued job while the suite is running.

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

On a 6 GB card, the chat model takes the GPU and the 0.6B embedder runs on the CPU. Putting both on the GPU leaves no headroom and can fail with CUDA out-of-memory. On the reference i5-9400, a warm CPU process embeds about eight variable-length passages in 10 seconds and sixteen in 24 seconds; the earlier “few seconds” estimate was optimistic. If you have more VRAM, set `LOCAL_EMBEDDING_DEVICE=cuda` and restart the API. Changes to embedding model or dimension need `make reindex`.

### Embedding speed on CPU

CPU inference does not benefit from large batches the way a GPU does. Sentence-Transformers pads every item in a batch to the longest item, so a batch of sixteen variable-length chunks can do much more work than sixteen independent encodes. On the reference i5-9400, batch `1` measured **10.15 s** for eight passages versus **14.67 s** at batch `8`; sixteen passages measured **23.84 s** at batch `1` versus **30.12 s** at batch `8`. The provider uses `LOCAL_EMBEDDING_CPU_BATCH_SIZE=1` by default while leaving `LOCAL_EMBEDDING_BATCH_SIZE=16` for GPU inference. Through the full provider path, including token-limit checks, the same eight passages took 12.6 s at batch 1 versus 19.6 s at batch 16.

Repeated query strings are cached in-process (`LOCAL_EMBEDDING_QUERY_CACHE_SIZE=256` by default), so asking the same question again skips the CPU model entirely. Document embeddings are not cached because the index already stores them. `LOCAL_EMBEDDING_THREADS=0` uses PyTorch's default; on a 6-core machine that also runs the API and chat, try `4` and compare. A quantized ONNX build of Qwen3 was tested and rejected here: it preserved vectors (cosine ≥ 0.9996) but was slower than PyTorch on this CPU.

### NVIDIA NIM embeddings and reranking

Remote embeddings use the same `EMBEDDING_PROVIDER=openai` path as any OpenAI-compatible endpoint. A local 9router instance can route NVIDIA NIM embeddings. The tested 9router 0.5.123 setup needs the exact routed ID from its own request example, which is double-prefixed because 9router adds the provider prefix to the registry ID:

```dotenv
AI_MODE=openai
# Keep local Ollama chat; send only embeddings through 9router.
OPENAI_BASE_URL=http://127.0.0.1:11434/v1
OPENAI_API_KEY=ollama
OPENAI_EMBEDDING_BASE_URL=http://127.0.0.1:20128/v1
OPENAI_EMBEDDING_API_KEY=<9router API key>
EMBEDDING_PROVIDER=openai
MODEL_EMBED=nvidia/nvidia/nemotron-3.5-embed-8b
OPENAI_EMBEDDING_INPUT_TYPE=true
OPENAI_EMBEDDING_DIMENSIONS=true
```

`OPENAI_EMBEDDING_BASE_URL` is optional. Leave it empty when chat and embeddings use the same endpoint; set it when a gateway such as 9router serves only the embedding model.

To move chat off the local GPU as well, call NVIDIA's chat endpoint directly. 9router's current chat route returns 404 for these NVIDIA models even though its embedding route works. Nemotron 3.5 also reasons by default and can spend the entire output budget before producing a visible answer, so disable thinking for strict JSON responses:

```dotenv
OPENAI_BASE_URL=https://integrate.api.nvidia.com/v1
OPENAI_API_KEY=<NVIDIA API key>
MODEL_ANSWER=nvidia/nemotron-3.5-lightning-30b-a3b
MODEL_ANSWER_COMPLEX=nvidia/nemotron-3-ultra-550b-a55b
MODEL_REWRITE=nvidia/nemotron-3-ultra-550b-a55b
MODEL_INGEST_SMALL=nvidia/nemotron-3.5-lightning-30b-a3b
MODEL_JUDGE=nvidia/nemotron-3.5-lightning-30b-a3b
OPENAI_DISABLE_THINKING=true
```

A stronger rewrite role makes follow-up questions substantially more reliable: `MODEL_REWRITE` sees the previous question and answer, replaces pronouns and back-references with the exact subject, and preserves qualifiers such as a plan or condition. It also handles canonical question-equivalence checks. `MODEL_ANSWER_COMPLEX` is used only for a request with several independent parts (for example, "When was it released? How many cores does it have?"); each part is retrieved separately, answered in one structured call, and retried individually if the combined reply omits a part. Leave it empty to reuse `MODEL_ANSWER`.

`/v1/models/embedding` may list the single-prefixed name `nvidia/nemotron-3.5-embed-8b`; requests in this 9router version still use the provider-prefixed name above. NVIDIA NIM's asymmetric models reject requests without `input_type`, so the app sends `query` for questions and `passage` for indexed text. The output is requested at the index's 1024 dimensions; the model's native dimension is 4096 and supports Matryoshka truncation. Remote latency was highly variable in testing: an initial browser request embedded three passages in 0.78 s, but a later cold SDK run took 5.3 s for one query and 4.6 s for two passages. The local CPU provider measured 12.6 s for eight passages and about 0.34 s for a unique warm query, so remote can help bulk ingestion when the provider is warm but is not a guaranteed interactive speedup. Measure the 9router connections before switching; document text leaves the machine, so use this path only when that is acceptable.

Changing to a remote embedder changes the index identity. Run `make reindex`; until it completes, dense retrieval reports keyword-only mode rather than comparing incompatible vectors. Remote inputs do not have the local provider's exact token-limit check; keep chunks within the provider's limit and treat silent remote truncation as unverified.

9router does not currently expose a rerank route, so reranking must call a NIM endpoint directly. For NVIDIA's hosted reranker:

```dotenv
MODEL_RERANK=nvidia/nemotron-3.5-rerank-8b
RERANK_API_STYLE=nim
RERANK_BASE_URL=https://ai.api.nvidia.com/v1/retrieval/nvidia/nemotron-3.5-rerank-8b
RERANK_PATH=/reranking
RERANK_API_KEY=<NVIDIA API key>
```

For a local NIM container, use `RERANK_BASE_URL=http://127.0.0.1:8000/v1`, `RERANK_PATH=/ranking`, and the served model name (for example `nvidia/Nemotron-3.5-Rerank-8B-BF16`). NIM returns raw logits; the client maps them through a sigmoid to a monotonic 0–1 score. The abstention bands were calibrated on lexical/Cohere-style scores, so recalibrate them on labeled questions before trusting confidence bands with a new reranker. If the rerank endpoint is unavailable or returns an unusable response, search falls back to lexical scoring rather than failing the request.

### Answers that need no model call

Two paths keep asking cheap on this hardware, and both are deterministic:

- With no conversation history, a question that already stands alone skips the rewrite call entirely. Once history exists, the rewrite model sees the previous question and answer and returns a subject-complete standalone query, or the same question when it already stands alone.
- When the question names a product ("i5 9400") and asks for one stated attribute ("TDP", "cores", "cache"), Harbour extracts the exact two-column row from the top-ranked document and cites it verbatim. A named identifier in the question is a constraint: an `L4` question is never answered from an `L3` row, rows that disagree are refused, and a document that merely mentions the product in passing is not used. The returned text is copied from the source, so it verifies by construction and costs no tokens.

Measured on the reference machine (i5-9400, 16 GB, GTX 1660 Super 6 GB, Qwen3-4B on the GPU, embeddings on the CPU), against a 32-passage product document: attribute questions answered in **2–4 s** with one model call, a semantic question ("When was the i5 9400 released?", answered by "Launched on January 7, 2019") in about 12 s, and out-of-scope or false-premise questions abstained rather than answered. Re-ingesting that document dropped from 39 model calls to 16 and indexed 32 passages instead of 39, with no separator fragments.

`make local-chat` uses the project-local runtime in `storage/`; set `OLLAMA_BIN` to use a system-wide Ollama instead. The first embedding request after a cold start loads the model in roughly 6–10 seconds on the reference CPU. Keep the Ollama server running while using Ask or ingestion.

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
- **Abstention is a result.** A failed answer creates or increments a gap and writes a `qa_logs` row with retrieved evidence and scores. Internal generated fallback answers must match complete sentences from cited child passages; unsupported paraphrases and detected contradictions are rejected. This conservative check can miss semantic contradictions and can abstain on valid paraphrases, so it does not replace human review.
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
- `LOCAL_EMBEDDING_CPU_BATCH_SIZE` — CPU batch size, default `1`; avoids variable-length padding. GPU inference uses `LOCAL_EMBEDDING_BATCH_SIZE` instead.
- `LOCAL_EMBEDDING_THREADS` — `0` leaves PyTorch's default; on a 6-core CPU also running the API and chat, `4` can be faster overall.
- `LOCAL_EMBEDDING_QUERY_CACHE_SIZE` — in-process cache for repeated query strings, default `256`; set `0` to disable.
- `make local-chat*` — install, start, pull, and warm the local Ollama model documented above.
- `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_PORT` — Docker bootstrap values, read by `docker compose`. Keep `MIGRATION_DATABASE_URL` in sync if you change them.
- `OPENAI_EMBEDDING_DIMENSIONS` — defaults to `true` and asks OpenAI-compatible embedding endpoints for 1024 dimensions. Disable it only for a provider that rejects the optional parameter; the model must still return exactly `EMBEDDING_DIM` values.
- `OPENAI_EMBEDDING_INPUT_TYPE` — enable for NVIDIA NIM asymmetric embedders; sends `query` or `passage` with each request. Leave it off for providers that reject the field.
- `RERANK_API_STYLE` — `cohere` (default, `/rerank`) or `nim` (`/ranking`). `RERANK_PATH` overrides the path for hosted NIM endpoints such as `/reranking`; `RERANK_TIMEOUT_SECONDS` defaults to 60.
- `INTERNAL_LOW/HIGH_THRESHOLD` and `PUBLIC_LOW/HIGH_THRESHOLD` — abstention bands.
- `DENSE_EXACT_MAX_CHUNKS` — use exact cosine search when the filtered eligible chunk count is at most this value (default 5,000); above it, use HNSW. Set to `0` to force HNSW.
- `MAX_CHILDREN_PER_PARENT` — maximum child passages kept from one section (default 2), so a rule and its exception can both be returned.
- `API_URL`, `DEV_TENANT_SLUG`, `DEV_USER_EMAIL` — read by the Next server.

The index uses 1024 dimensions (migration 006). Changing the embedding provider, model, dimensions, query instruction, sequence limit, `OPENAI_EMBEDDING_INPUT_TYPE`, or `EMBEDDING_VERSION` changes the index identity. Run `make reindex` after any of those changes; until then dense retrieval is disabled for the workspace and the API reports keyword-only mode. `make reindex` rebuilds the default workspace only; pass another slug to `python -m app.reindex`. `make api`, `make seed`, `make reindex`, and `make test` automatically include the local embedding extra only when `.env` sets `EMBEDDING_PROVIDER=local`; override with `UV_EXTRA=local-embeddings` or `UV_EXTRA=`.

## Verification

```bash
make test      # API tests + web typecheck
make check     # test, then the production web build
curl -s http://127.0.0.1:8000/healthz
```

### Evaluation baseline

The synthetic evaluation exercises ingestion, retrieval, and Ask without resetting your workspace. It creates a temporary tenant and synthetic originals, runs each question independently, and rolls back all database writes. It requires the migrated database and the configured AI providers; it does **not** call `make seed` or alter existing knowledge. The corpus and labels are in [evaluation/corpus.json](services/api/evaluation/corpus.json).

```bash
cd services/api
# Deterministic development baseline, using a new output filename each time:
AI_MODE=dev EMBEDDING_PROVIDER=dev uv run python -m app.evaluate --split development --output evaluation/my-dev-baseline.json
# Configured providers (include --extra local-embeddings when needed):
uv run --extra local-embeddings python -m app.evaluate --split development --output evaluation/my-local-baseline.json
# Reserve this split for comparisons after development/tuning:
uv run --extra local-embeddings python -m app.evaluate --split heldout --output evaluation/my-heldout-report.json
```

Use the same configuration, corpus and split for comparisons. The starter set is tiny and synthetic, not a representative accuracy measurement; expand it with reviewed examples before setting thresholds. Reports include final retrieval ranks, abstentions, expected canonical selection, citation quote presence, ingestion and Ask wall time, and the generated answers for manual review. Quote presence does **not** prove support or correctness. Timings combine cold and warm requests and are not a hardware benchmark. The runner does not test HTTP uploads, worker polling, authentication, cross-tenant isolation, peak RAM/VRAM, or pre-rerank recall. Reports can contain document excerpts and answers; keep any private evaluation data and reports out of version control.

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
4. **Evaluation coverage.** The starter synthetic harness needs reviewed real-world golden and adversarial cases, human claim-support assessment, and held-out expansion before tuning thresholds; the current bands are explicit defaults, not calibrated values.
5. **Semantic dedup and change impact.** Document-hash aliases exist; near-duplicate passage detection, topic discovery, and a richer change-impact pass remain.
6. **Provider testing.** The OpenAI-compatible client is tested against local Ollama and a local 9router/NVIDIA NIM embedding path, with mocked NIM rerank HTTP coverage; a broader hosted-model matrix, live NIM rerank runs, and more retrieval evaluation remain.
