# Harbour Knowledge improvement plan

## Goal and constraints

Make ingestion, storage, retrieval, and answering **reliable and measurably accurate** while keeping the system practical on an i5-9400, 16 GB RAM, and a GTX 1660 Super with 6 GB VRAM. Favor safe abstention over a confident but unsupported answer. Optimize only after measuring on the target machine and representative data.

This is an implementation plan, not a claim that the improvements are already complete. It is based on a review of the current working tree, including uncommitted changes. The existing development authentication is not suitable for exposure to untrusted users; production currently fails closed.

### Guiding principles

- Preserve source content and provenance; do not silently convert uncertain extraction into authoritative knowledge.
- Match an approved answer to the **question and scope**, not merely to a related passage.
- A verbatim citation establishes provenance, not that a claim is entailed by that citation.
- Keep approved answers and their evidence versioned. A replacement source must not silently rewrite an approved citation.
- Use a representative, held-out evaluation set before selecting models, thresholds, chunk sizes, or ranking strategies.
- Maintain tenant and public/internal isolation at every retrieval, answer, and approval boundary.
- Keep local defaults conservative in memory usage; do not assume the README's throughput estimates apply to the target hardware.

## Current assessment and priorities

| Priority | Area | Main risk | Outcome sought |
|---|---|---|---|
| P0 | Answer selection and scope | A relevant passage can cause an unrelated canonical answer to be returned; missing scope can mix incompatible policies. | Correct answer matching, explicit scope handling, safe abstention. |
| P0 | Evidence and ingestion transactions | Quote existence is treated as faithfulness; failed ingestion may leave partial work. | Honest evidence verification and atomic ingestion. |
| P0 | Evaluation | The existing small unit suite does not establish end-to-end accuracy. | A reproducible baseline and regression gates. |
| P1 | Parsing and chunking | Content, ordering, page references, and embedding input can be lost or distorted. | Faithful extraction, provenance, and bounded chunks. |
| P1 | Embedding lifecycle and freshness | Mixed embedding models, aliases, or changed evidence can lead to missing or stale knowledge. | Explicit index identity, safe reindex, and rechecks. |
| P1 | URL and data security | URL fetching and development authentication are unsafe for an untrusted deployment. | Controlled fetching, private data handling, real authentication before exposure. |
| P2 | Ranking and performance | Lexical reranking and repeated model calls may reduce quality or add latency. | Measured, resource-conscious improvements. |
| P2 | Conflict detection and operations | Heuristic conflict detection and review dates do not guarantee freshness or completeness. | Explicit governance, observability, and tested recovery. |

## Phase 0 — establish a trustworthy baseline

**Do this first, before tuning or selecting a new model.**

1. Create a versioned evaluation corpus representative of actual documents: policies, pricing, procedures, error messages, tables, PDFs, and any important customer-specific formats. Include public/internal sources, multiple plans and product versions, updated sources, and intentionally conflicting material.
2. Build a labeled question set with expected answer or acceptable answers, relevant evidence spans, audience, plan, product version, date where applicable, and whether the correct behavior is to abstain or ask a clarifying question. Include:
   - Exact and paraphrased questions, plus different questions supported by the same passage.
   - Near-neighbor questions about different conditions, plans, prices, dates, or entities.
   - Negation, exceptions, numbers, units, error codes, and multi-part questions.
   - Missing scope, ambiguous scope, conflicting sources, stale answers, and unanswerable questions.
   - Simple answerable questions that should not abstain, including the Mikey example in Section 1.4, alongside superficially similar questions that genuinely lack evidence.
   - Public questions where useful evidence is internal, and tenant-isolation cases.
3. Separate development and held-out evaluation questions. Record corpus and model versions, settings, and the date of each run. Do not tune on the held-out set.
4. Measure the stages separately:
   - Retrieval: evidence recall@k and MRR, including exact identifiers and filtered queries.
   - Answer selection: correct canonical-answer selection and false-match rate.
   - Grounding: unsupported or contradicted claim rate, citation validity and claim-to-citation support, checked by a human-labeled sample.
   - Safety: correct abstention/clarification, false-answer rate on unanswerable and ambiguous cases, public/internal and tenant isolation.
   - Performance: cold and warm p50/p95 end-to-end and per-stage latency, ingestion throughput, peak RAM and VRAM, and database/index size.
5. Save a baseline report before fixes. Define release thresholds based on the cost of wrong answers and the baseline; do not invent accuracy targets or interpret heuristic confidence as a calibrated probability.

**Acceptance:** the same corpus and questions can be rerun locally; results distinguish retrieval, selection, grounding, and latency; a held-out set is untouched by tuning.

## Phase 1 — answer correctness and safe scope

### 1.1 Match canonical answers to the question

**Progress:** canonical selection now matches the question separately from evidence relevance. Exact text is checked first, then a deterministic comparison of content words after removing function words and generic hardware acronyms such as CPU, GPU and RAM; numbers, negation, qualifiers and named entities must still agree, and the model must confirm equivalence in both directions when the deterministic path does not apply. Duplicate approved answers are served only when their text agrees, choosing the oldest deterministically; conflicting duplicates abstain. This fixes the reported `i5 9400` / `i5 9400 CPU` split and the Mikey age paraphrase without weakening entity, date or negation guards. Unit tests cover these cases plus ambiguous and conflicting duplicates.

**Still needed:** equivalence without shared content words remains model-dependent, and the offline provider only recognises age variants. A model that rejects a valid paraphrase still causes a fallback and a possible new draft. Existing duplicate approvals from before the fix are not automatically merged; one may also cite superseded evidence until change-impact handling is implemented.

**Plan**

- Rank eligible approved canonical questions against the rewritten user question independently of passage ranking. Evaluate lexical and semantic question matching rather than assuming either is sufficient.
- Verify that the chosen answer addresses the user's intent and all requested parts. Treat the evidence match as a separate grounding check, not as a substitute for question matching.
- Resolve exact matches deterministically by audience and scope; if multiple answers remain valid and disagree, ask for clarification or abstain instead of selecting arbitrarily.
- Consider an exact-question fast path only after it enforces the same visibility, scope, freshness, and evidence checks as the normal path.
- Preserve SQL-side visibility filters and revalidate all evidence before serving a public answer.

**Acceptance:** tests include related questions that share a passage, paraphrases, multiple scoped answers, and unrelated questions. The held-out false-match rate improves without an unacceptable drop in correct-answer coverage.

### 1.2 Make plan, version, and time semantics explicit

**Current behavior:** [retrieval filters](services/api/app/retrieval/search.py#L63-L75) admit scoped documents when a request omits scope. Source scope input is lowercased, while the development rewriter can return title-cased plan names. `as_of` is applied to retrieval but not to canonical matching, and retrieval requires current passages even for historical requests.

**Plan**

- Define a canonical representation for plan and version identifiers at ingestion, request parsing, rewriting, answer creation, and filtering; migrate and verify existing values carefully.
- Define the missing-scope policy: prefer a truly general answer when justified; otherwise ask for the needed scope, return clearly separated alternatives, or abstain. Never combine incompatible scoped claims as if they applied universally.
- Apply the same scope rules to passage candidates, canonical answers, and their supporting evidence. Validate scope compatibility during approval as well as serving.
- Decide whether historical answering is supported. If not, explicitly reject or disable `as_of` rather than returning a current answer as historical. If it is, model effective intervals and answer versions consistently, retrieve eligible superseded evidence, and test historical visibility and source withdrawal rules.

**Acceptance:** casing differences do not change eligibility; missing, explicit, conflicting, and historical scopes have deterministic documented behavior across search and Ask.

### 1.3 Verify grounding without overstating confidence

**Current behavior:** canonical faithfulness is based on the fraction of quotes that still exist; [fallback faithfulness](services/api/app/retrieval/search.py#L267) is lexical overlap. In a local reproduction, an answer and its negation received a score of 1.0. The [retry path](services/api/app/retrieval/answer.py#L675-L684) can accept an improved score without checking that it reaches the original minimum.

**Plan**

- Treat quote validity, evidence freshness, claim support, and question relevance as separate signals. Show what was actually checked; do not label a heuristic score as a probability of correctness.
- Map each answer claim to the evidence actually supplied to the generator. Reject invented citation IDs, missing citations, and claims with no mapped support.
- Add deterministic checks for mismatched numbers, units, dates, and negation as useful safeguards, not as a complete entailment solution. Evaluate any model-based verifier against human-labeled examples before trusting it.
- Require a retry to meet the same grounding and abstention criteria as an initial answer. Respect a retry's abstain flag.
- Retain human review of canonical answers. Make reviewers see claims and their supporting quotes, relevant scope, source status, and any known conflict.
- Public answers must remain approved and supported by eligible public evidence; internal fallback remains clearly labeled and must abstain when evidence is inadequate.

**Acceptance:** adversarial tests cover reversed meaning, altered numbers, unsupported extra claims, invalid IDs, weak retries, and internal evidence on public answers. Quotation validity is reported separately from claim support.

### 1.4 Investigate and fix false abstention on simple evidence

**Reported case:** an uploaded text file contains:

```text
Mikey is 40 years old.
Mikey was born on August 8th, 1986.
```

Asking **“How old is Mikey?”** produced “I could not answer that safely” with the reason “The answer could not be grounded in the retrieved evidence.” For an internal Ask request, the system should be able to answer from an eligible current passage rather than abstaining merely because a short, clearly relevant claim or citation is mishandled. Public channels must continue to require an eligible approved public answer; an uploaded passage alone is not sufficient for a public answer.

**What is known:** in the current answer path, that specific reason is returned when generation reports abstention, returns no answer, or supplies no valid cited passage IDs. It does not by itself establish which condition occurred. A local isolated check using the development generator preserved both sentences, produced an answer and a valid cited ID, and gave a faithfulness score of 1.0. This did **not** reproduce the reported end-to-end failure or establish its cause; the actual provider, indexed passage, request scope, and generation response still need investigation. The same isolated check selected the birth-date sentence as the supporting excerpt for the combined generated answer, which also warrants testing for claim-level citation relevance.

**Plan**

- Add an end-to-end regression fixture with this exact document and question, exercising ingestion, retrieval, generation, citation validation, and the final internal Ask response. Run it with the development provider and the configured local chat/embedding providers where available. Inspect redacted stage diagnostics and reproduce the reported outcome before attributing a cause.
- Identify whether the problem is missing or stale indexing, retrieval or scope eligibility, invalid/missing generated citation IDs, a provider abstention, answer parsing, or a grounding gate. Repair the actual failure without bypassing evidence validation or lowering thresholds indiscriminately.
- Ensure short supported statements and short answers can be grounded; the current lexical faithfulness check excludes sentences of 25 characters or fewer. Verify that each returned claim is linked to the passage and exact excerpt that supports it, rather than merely choosing a broadly related sentence.
- Define time-dependent age semantics. A stated age can become stale; a date of birth supports a calculated age only with an explicit reference date and correct birthday handling. Prefer a qualified statement of what the source says, or calculate and label age **as of** a known date when policy permits; do not silently present a stale age as current. Mark conflicting or insufficiently dated age information for clarification or review.
- Include nearby negative cases: a different person, an absent age or birth date, mismatched or invalid citation IDs, an internal-only source queried through a public channel, and dates before and after a birthday. Confirm these do not become unsupported answers when the positive case is fixed.

**Acceptance:** the eligible internal case returns a supported, correctly cited, appropriately time-qualified answer instead of the reported false abstention; public and negative cases remain safe. Tests show which actual failure was repaired and report any provider-dependent checks that could not run.

## Phase 2 — reliable ingestion and faithful source representation

### 2.1 Make ingestion atomic and observable

**Progress:** replacement writes now roll back to a savepoint on ordinary failures, with the job failure recorded while holding the source lock. The claim commits separately for restart recovery; recovery skips active locked jobs. Prior complete documents remain searchable during re-ingestion and after a failed replacement. URL snapshots no longer overwrite the prior file, and ingestion validates embedding counts, dimensions, and finite values.

**Still needed:** parsing and inference still run inside a long transaction, and only the initial running stage is visible until it finishes. A hard crash or commit failure can leave an unreferenced URL snapshot; snapshot retention and cleanup remain undefined. Failure testing does not yet inject faults at every stage or simulate a real killed process or network loss.

**Plan**

- Roll back the complete replacement attempt on failure; record job failure in a separate healthy transaction or appropriate savepoint boundary. Keep the previous complete document and its retrieval visibility until the replacement is ready.
- Separate long parsing and inference work from short database transactions where practical, while maintaining source-withdrawal locking and preventing stale work from being published. Define the atomic commit/switch point explicitly.
- Persist useful stage transitions and failure diagnostics without exposing secrets or raw sensitive content. Ensure status updates are visible during long work.
- Test crashes, retries, timeouts, provider failures, SQL errors, withdrawal during ingestion, and restart recovery. Ensure retry limits and idempotency remain correct.
- Validate provider response counts, dimensions, and values before inserting embeddings; do not let `zip` silently drop missing results.

**Acceptance:** fault injection at each stage leaves either the prior complete version or a complete new version, never partial current knowledge. Failures are visible and retryable according to the documented policy.

### 2.2 Preserve content during normalization and redaction

**Progress:** normalization preserves blockquotes, standalone numbers, quoted-reply headers, whitespace, tables, and code instead of treating them as noise. Fenced code stays intact during section and passage construction. Redaction still uses regex and can treat long identifiers as card numbers; it is not a complete sensitive-data detector.

**Plan**

- Preserve blockquotes, standalone numbers, code blocks, and table structure by default. Apply cleanup rules only where source format and context justify them.
- Separate raw originals, normalized text, redacted text, and indexed passages with clear provenance and access controls. Keep redaction before any external model call.
- Use context-aware sensitive-data detection and review for uncertain cases; avoid claiming regex redaction detects every secret. Record redaction type and location without leaking the original value in logs.
- Provide a way for an authorized reviewer to inspect extraction or redaction problems without making originals public.

**Acceptance:** fixture tests preserve meaningful numeric values, quotes, lists, and code; actual secrets and personal data are handled according to a documented policy; logs do not expose them.

### 2.3 Improve parsers, supported formats, and provenance

**Progress:** PDF empty-text detection now examines extracted text, including short text; pages without extractable text are recorded in document metadata. PDF sections and passages are split at page boundaries, and new documents record unknown parse quality instead of an unmeasured perfect score. DOCX paragraphs and tables retain their order; HTML traversal avoids duplication of nested blocks and handles common table spans. Explicit re-ingestion rebuilds older indexes, even when the normalized text is unchanged.

**Still needed:** absent PDF text may indicate a scan or an intentionally blank page; OCR and a reviewer-facing warning are not implemented. Complex nested tables and irregular DOCX rows are rejected rather than silently misrepresented; multi-level table headers and other complex structures need further evaluation. XLSX, EPUB, and images are rejected at upload because their parsers are not implemented. Source offsets, comprehensive parser fixtures, and safe re-review of answers affected by replacement remain open.

**Plan**

- Assess actual extracted page text, not page markers, and flag wholly or partly scanned PDFs for OCR or review. Add OCR only if the corpus warrants its CPU, disk, and maintenance costs; record its confidence and provenance if added.
- Preserve DOCX paragraph/table order and HTML block structure without duplicating nested elements. Preserve table headers and relationships; handle malformed or empty documents explicitly.
- Carry page and source offsets through sections and child chunks so citations point to the correct page or location. Avoid assigning a single final page number to a multi-page section.
- Align upload acceptance and UI messaging with implemented parser support. Either reject XLSX, EPUB, and images early with a clear message, or implement and test those formats deliberately.
- Replace the unconditional `parse_quality = 1.0` with meaningful measured quality, an explicit unknown value, or review flags.

**Acceptance:** fixtures cover blank and mixed scanned/text PDFs, multi-page references, DOCX interleaved tables, nested HTML, malformed files, and each supported file type. Extracted facts and citation locations match the originals.

### 2.4 Bound and evaluate chunks

**Progress:** the local embedding provider checks untruncated model inputs, including special tokens, default prompts, contextual blurbs and query instructions, against its configured limit before encoding. Ingestion splits oversized multi-row tables at row boundaries with repeated headers and prose or lists at existing boundaries. It removes an overlong generated blurb rather than truncating source content. Unsplittable rows, code blocks, long individual sentences and unpunctuated blocks fail ingestion explicitly; a failed replacement keeps the prior version. Explicit re-ingestion rebuilds older indexing versions.

**Still needed:** splitting at sentence or list boundaries can separate related conditions and needs evaluation, overlap and source offsets. Very large code blocks and individual sentences require manual splitting. These checks apply only to the local embedding provider; remote provider limits and silent truncation are not verified. Existing indexed passages remain unchanged until explicitly re-ingested, and old vectors may already be truncated. Child deduplication by parent can hide other matching chunks. Large split operations still need throughput/resource benchmarks.

**Plan**

- Use the active embedding tokenizer to enforce a maximum input length, accounting for contextual text and special tokens. Monitor and report truncation.
- Split long sentences, tables, lists, and code predictably while preserving useful headers, conditions, and adjacent context. Store precise offsets and page references.
- Evaluate child size, overlap, parent expansion, and context budget on the labeled corpus. Avoid sending entire large parents when a compact relevant window gives equal or better results.
- Re-ingest or rebuild affected passages after changing chunking; preserve historical evidence and put affected approved answers through recheck.

**Acceptance:** long unpunctuated text and large tables/lists cannot silently exceed the selected embedding input budget; evaluation checks whether relevant conditions remain retrievable and citable.

## Phase 3 — storage, indexing, and knowledge lifecycle

### 3.1 Enforce embedding identity and safe reindexing

**Progress:** each new passage records an embedding identity covering provider, model, dimensions, query instruction, sequence limit, embedding version, and ingestion pipeline revision. Dense retrieval only ranks passages whose identity matches the active configuration, and otherwise reports the keyword-only fallback in Search and Ask; `/v1/meta` reports compatible, stale, and total passage counts. Migration 014 deliberately leaves existing rows without an identity so old vectors stay out of dense search until they are rebuilt. Reindex now writes embeddings to a staging table in short committed batches, validates vector counts, dimensions, and finiteness, and switches a workspace atomically only after every current passage is staged. An interrupted run leaves the previous index usable and resumes from its staged rows. The integration suite covers the lexical fallback, an interrupted rebuild, and a successful resumed switch. The local workspace was re-ingested to split one oversized passage and then rebuilt successfully.

**Still needed:** reindex re-embeds every current passage even when the active identity is already recorded, and has no cross-process lock preventing two simultaneous rebuilds of one workspace. There is no all-workspaces command; each workspace must be passed as a slug. Remote-provider truncation and identity compatibility remain unverified, and an old passage over the model limit still requires source re-ingestion rather than an automatic replacement. The knowledge dashboard does not yet surface index status beyond ask/search warnings and the meta endpoint.

**Plan**

- Define index identity using provider/model, model revision where available, dimensions, preprocessing, query instruction, and pipeline version. Check it before dense retrieval.
- On mismatch or incomplete index, fail closed or use an explicitly reported lexical-only mode; never silently compare incompatible vectors.
- Design a resumable, staged reindex with an atomic per-tenant switch after all vectors validate. Consider additional temporary disk and memory requirements; maintain a clear rollback path.
- Use bounded database batches and validate each returned vector. Make tenant scope and progress explicit. Verify how reindex interacts with ongoing ingestion.
- Check backup and restore of both PostgreSQL and original files before migrations or large reindex jobs.

**Acceptance:** changing embedding configuration cannot silently mix vector spaces; interrupted reindex leaves the prior usable index or an explicit safe fallback; multi-tenant runs are documented and tested. *Partly met:* identity enforcement, the reported keyword-only fallback, atomic per-workspace switching, and interruption/resume are implemented and tested; multi-tenant operation is documented per slug but not exercised as one automated run, and concurrent reindex is not locked.

### 3.2 Handle aliases, supersession, and answer freshness

**Progress:** source withdrawal already marked affected approved answers for recheck. Re-ingestion now does the same inside the replacement transaction: when a document version is superseded, approved answers whose current evidence cites that version move to `needs_recheck`, while their quotes and passage links stay frozen for audit. A failed replacement rolls the status change back. The local stale duplicate from the i5 re-ingest was repaired with the same rule.

**Still needed:** [duplicate uploads](services/api/app/routers/sources.py#L375-L408) can still alias by hash despite different visibility, scope, or tier, including while an original is processing. The passage deduplication stage is still only a comment. Re-ingestion marks answers but does not create a review-queue item or a guided evidence-refresh path, and review-date freshness still does not affect serving. The remaining plan below is otherwise unchanged.

**Plan**

- Define when files with identical bytes but different metadata may share storage or an index. Ensure visibility, scope, authority, failure, withdrawal, and deletion behavior are unambiguous. Do not advertise an alias as successfully indexed when its original is not ready.
- On replacement or withdrawal, find approved answers and pending reviews that cite affected versions; mark them for recheck as appropriate without rewriting historical evidence.
- Define how overdue review dates affect internal and public serving. Make freshness status visible and queue actionable reviews; choose fail-closed rules according to risk.
- Implement exact passage deduplication only after deciding how it interacts with citations, source authority, scope, and provenance. Treat near-duplicate merging as later evaluation-driven work.

**Acceptance:** duplicate metadata variations, failed originals, reingest changes, withdrawal, approval, and overdue answers have regression coverage. Historical citations remain auditable.

## Phase 4 — retrieval quality and resource efficiency

### 4.1 Evaluate retrieval and ranking changes

**Current behavior:** [hybrid search](services/api/app/retrieval/search.py#L115-L187) fuses dense and lexical candidates, then sorts solely by reranker scores. The default reranker is lexical when no rerank endpoint is configured. The Ask rewrite extracts error codes, but Ask does not use them for a dedicated exact search. Generation receives parent text while citations identify child passages.

**Plan**

- Compare dense-only, lexical-only, RRF, and current reranking on the evaluation set. Test whether retaining a calibrated fusion signal helps before changing the ranking formula.
- Add robust exact-identifier retrieval for error codes, product names, and other critical identifiers without discarding semantic recall. Confirm actual SQL plans and index use with representative data.
- Evaluate a small CPU-compatible local reranker only if it improves held-out answer quality enough to justify p95 latency and memory cost. Do not add it solely because it is more sophisticated.
- Keep evidence identity aligned with text provided to generation: either cite exact child spans or represent expanded context with accurate provenance. Deduplicate near-identical contexts without hiding independent authoritative evidence.
- Test approximate vector-index recall under tenant, visibility, scope, and date filters against exact search. Tune index/query settings only after measuring corpus size, memory, latency, and recall.

**Acceptance:** relevant-evidence recall and false matches are measured before and after. Error-code, filtered, paraphrase, and provenance cases have regression tests; no ranking change is accepted on latency alone.

### 4.2 Measure and reduce expensive work

**Current behavior:** ingestion performs a contextualization call for each chunk sequentially ([pipeline](services/api/app/ingest/pipeline.py#L270-L283)); Ask performs query rewriting and embedding before canonical matching. The local embedding model serializes its own calls with a lock.

**Plan**

- Measure per-stage time, model warm-up, CPU use, RAM, GPU memory, prompt length, queue wait, and database time. Separate ingestion from interactive Ask measurements.
- Compare generated contextual blurbs with deterministic title/section/scope context; keep generated blurbs only where they improve measured retrieval enough to justify cost and potential invented context.
- Evaluate batching embeddings across appropriately bounded chunks and reusing loaded models. Bound batch size and inference concurrency to avoid CPU contention and GPU out-of-memory failures.
- Test a scope-safe exact canonical fast path, compact evidence windows, and skipping unnecessary rewriting for simple standalone questions. Check quality as well as speed; do not bypass security or evidence checks.
- Tune Postgres and index settings only with actual query plans and workload measurements. Avoid broad parallelism or extra heavyweight models by default on 16 GB RAM / 6 GB VRAM.
- Keep the documented local-chat-on-GPU and local-embeddings-on-CPU configuration as a starting point, not a measured guarantee. Record actual model, quantization, context size, batch size, and warm/cold conditions for every benchmark.

**Acceptance:** publish before/after measurements on the target machine with the same workload, including accuracy and peak resources. Revert optimizations that materially compromise safety or exceed the memory budget.

## Phase 5 — security, conflicts, and operational readiness

### 5.1 Secure ingestion and deployment boundaries

**Current behavior:** [URL fetching](services/api/app/ingest/parsers.py#L197-L240) validates a DNS resolution before the HTTP connection and reads the response before applying the size cap; DNS rebinding and excessive buffering need attention. The [development session stub](services/api/app/deps.py#L31-L43) trusts headers and production fails closed.

**Plan**

- Before exposing ingestion to untrusted users, use a fetch mechanism that validates and pins the actual connection destination, checks every redirect, handles IPv4/IPv6 safely, and considers proxy behavior. Stream responses with a hard byte cap and timeouts.
- Restrict parser resource use for malformed or compressed documents and enforce upload limits at the HTTP boundary. Test oversized, slow, redirected, and private-address fetches.
- Introduce real authentication and tenant membership checks before any public deployment. Keep production fail-closed until that is verified; keep development endpoints local and trusted.
- Protect stored originals, snapshots, backups, logs, and model prompts according to their sensitivity. Document retention, deletion, encryption and access policies; verify that public answers never expose internal evidence.
- Treat document contents as untrusted input to models. Add prompt-injection examples to the evaluation set and prevent retrieved text from authorizing tools or overriding application rules.

**Acceptance:** security tests cover redirect/address bypasses, streaming limits, unauthorized access, tenant isolation, public/internal isolation, and malicious source instructions. Backup/restore and deletion policies are documented and exercised.

### 5.2 Improve conflict handling and review operations

**Current behavior:** [conflict detection](services/api/app/ingest/pipeline.py#L341-L449) is a bounded numeric heuristic over selected document types. Review dates exist, but they do not themselves guarantee that outdated answers are excluded from serving.

**Plan**

- Document the detector's limited coverage; do not present absence of a detected conflict as proof that sources agree.
- Record source authority, ownership, effective dates, and explicit human resolutions. Distinguish a resolved conflict from a merely ignored or superseded source.
- Evaluate conflict detection using labeled contradictions, including non-numeric and scope-specific disagreements. Choose conservative behavior for unresolved conflicts and stale answers based on audience and risk.
- Add operational dashboards or reports only as needed to track failed jobs, index completeness, overdue reviews, unsupported citations, abstentions, and evaluation regressions.

**Acceptance:** reviewers can identify affected claims, sources, scope and resolution history; unresolved conflicts and overdue evidence behave according to a tested policy.

## Suggested delivery order

1. **Baseline and regression fixtures:** assemble the evaluation set and capture current results.
2. **Answer safety:** canonical matching, normalized/missing scope, honest grounding checks, and safe retry behavior.
3. **Ingestion integrity:** rollback/failure handling, preservation of source content, parser/page fixes, and bounded chunks.
4. **Index and lifecycle:** embedding identity, staged reindex, alias semantics, and answer rechecks on changed evidence.
5. **Security readiness:** hardened URL ingestion and real authentication before untrusted exposure.
6. **Measured ranking and performance:** benchmark alternative ranking, exact identifiers, context size, and inference costs.
7. **Governance refinement:** calibrated freshness and conflict workflows informed by actual errors.

Some items can be developed in parallel, but benchmark comparisons require a stable baseline. Schema or index changes should include a migration, a backup/rollback plan, and a decision on whether existing sources must be reprocessed.

## Verification and completion criteria

- Existing tests continue to pass; add focused unit tests for each repaired defect and database-backed integration tests for ingestion, reindexing, scope, canonical matching, withdrawal, and RLS.
- Run the labeled evaluation against both development and the intended local-model configuration. Report results and failure examples, not just aggregate scores.
- Verify public answers use approved public evidence only; test tenant isolation with multiple tenants and users.
- Run failure/restart and backup/restore exercises without risking production data.
- Record cold/warm latency and peak RAM/VRAM on the target machine; do not claim a speedup without a comparable measurement.
- Treat the work as iterative: new failures feed the evaluation set, and thresholds are calibrated against the cost of incorrect answers versus abstention.

## Evidence from the review

The pre-implementation review found **9 passing** API unit tests. Small local reproductions confirmed that a negated claim can receive a faithfulness score of 1.0; a blockquote and standalone number can be removed; a 13-digit identifier can be redacted; a long unpunctuated chunk can exceed its target; nested HTML content can be duplicated; PDF page references can be wrong; and a blank three-page PDF can pass the extraction check. Those defects have since been repaired with focused unit tests; an opt-in PostgreSQL integration suite now covers ingestion atomicity, withdrawal locking, worker recovery, page provenance, bounded chunks, and staged reindex interruption/resume. The current `make test` run passes **75 tests** with 15 opt-in database tests skipped; run against an isolated database, that suite adds **14 passed, 1 skipped**.

The review itself did not perform a corpus-wide quality evaluation, live-model benchmark, or target-hardware benchmark. A synthetic evaluation harness now exists, but broad reviewed-corpus accuracy, peak RAM/VRAM, and cold/warm latency measurements remain open.
