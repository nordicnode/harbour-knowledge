# Harbour Knowledge improvement plan

## Goal and constraints

Make ingestion, storage, retrieval, and answering **reliable and measurably accurate** while keeping the system practical on an i5-9400, 16 GB RAM, and a GTX 1660 Super with 6 GB VRAM. Favor safe abstention over a confident but unsupported answer. Optimize only after measuring on the target machine and representative data.

This is an implementation plan, not a claim that the improvements are already complete. It is based on a review of the current working tree, including uncommitted changes. The existing development authentication is not suitable for exposure to untrusted users; production currently fails closed.

**Status snapshot (2026-11-23):** the API suite reports **89 passed, 16 skipped**; the skips are opt-in PostgreSQL integration tests. Answer selection, claim-level lexical grounding, atomic replacement, parser preservation, separator removal, bounded chunks, embedding identity, and staged reindex are implemented and covered by focused tests. The saved `evaluation/baseline-dev.json` is a **pre-fix synthetic baseline**; do not use it as the current score. A representative held-out corpus, a current end-to-end baseline, duplicate-answer cleanup, exact-identifier retrieval, provider preflight, and the security work remain open. The local model server is a manual process; a stopped server must produce an actionable 503, never a fabricated answer.

### Guiding principles

- Preserve source content and provenance; do not silently convert uncertain extraction into authoritative knowledge.
- Match an approved answer to the **question and scope**, not merely to a related passage.
- A verbatim citation establishes provenance, not that a claim is entailed by that citation.
- Keep approved answers and their evidence versioned. A replacement source must not silently rewrite an approved citation.
- Use a representative, held-out evaluation set before selecting models, thresholds, chunk sizes, or ranking strategies.
- Maintain tenant and public/internal isolation at every retrieval, answer, and approval boundary.
- Keep local defaults conservative in memory usage; do not assume the README's throughput estimates apply to the target hardware.

## Current assessment and priorities

| Priority | Area | Status | Main remaining risk or next action |
|---|---|---|---|
| P0 | Answer selection and scope | Implemented for covered cases | Held-out false-match rate, duplicate-answer cleanup, and scope normalization remain. |
| P0 | Evidence and grounding | Implemented lexical safeguards | Semantic paraphrase, dropped conditions, and comparatives still need evaluation; this is not entailment. |
| P0 | Ingestion transactions | Implemented; opt-in integration coverage | Stage visibility, crash injection, and snapshot cleanup remain. |
| P0 | Evaluation | Harness exists; saved baseline is stale | Build a representative held-out set and regenerate the baseline before tuning. |
| P1 | Parsing and chunking | Mostly implemented | OCR, XLSX/EPUB/images, source offsets, and complex tables remain. |
| P1 | Embedding lifecycle | Implemented staged reindex | Concurrent-reindex lock, dashboard status, and remote truncation remain. |
| P1 | Provider availability | **Partly implemented** | Actionable errors exist; preflight/status and an in-app retry path remain. |
| P1 | URL and data security | Open | SSRF-safe fetching and real authentication before any untrusted exposure. |
| P2 | Ranking and performance | Partially measured | Fusion sweep, cross-encoder benchmark, exact-identifier retrieval, and a representative exact-scan/child-cap comparison remain. |
| P2 | Conflict detection and operations | Open | Freshness/review operations and honest limits on conflict detection. |

## Next milestone — make local operation and evaluation trustworthy

These are the next actions in order. Detailed rationale and acceptance criteria remain in the phases below.

1. **Provider preflight and recovery.** Expose chat and embedding reachability separately, make a stopped local server obvious before Ask, and keep the failure fail-closed with a retry path. Starting `make local-chat` must recover without restarting the API.
2. **Regenerate the evaluation baseline and add a held-out split.** Run the current stack, save the report with corpus hash, model/config, and per-stage metrics, and stop tuning on the development cases. The existing synthetic baseline is pre-fix and must not be cited as current.
3. **Add the Mikey end-to-end regression.** Exercise upload, ingestion, retrieval, generation, citation validation, and the final internal Ask response. Add the public, different-person, absent-age, and invalid-citation negatives.
4. **Clean up canonical duplicates.** Find existing near-duplicate approved questions, merge them through review, and prevent a new wording variant from creating a second canonical answer.
5. **Add exact-identifier retrieval.** Use a dedicated exact query for error codes and named product identifiers, then verify it with `E4021` and a false identifier.
6. **Run the security gate before exposure.** Do not expose URL ingestion or the development session stub until SSRF protections and real tenant authentication are tested.

### Release gates for this milestone

- Stopping the local model server produces an actionable 503 naming the endpoint and start command, with no fallback answer; restarting it recovers without an API restart.
- The API suite is green, the opt-in PostgreSQL suite has been run against an isolated database, and the current integration count is recorded in the report.
- A new baseline exists, is reproducible from the recorded corpus hash and configuration, and the held-out split has not been used for tuning.
- The eligible Mikey internal case returns a supported, correctly cited, time-qualified answer; the public and negative cases still abstain.
- The CPU wording variants do not create a second canonical answer; existing duplicates are either merged or explicitly queued for review.
- `E4021` retrieves its exact source and a false identifier does not; no public answer cites internal evidence.

## Phase 0 — establish a trustworthy baseline

**Status:** a synthetic development harness and a saved pre-fix baseline exist. The corpus is too small and synthetic to serve as a quality benchmark, and the saved report predates the fixes. Completing a current, representative baseline is the gate for any ranking, model, or threshold change.

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
5. Save a baseline report before further tuning. Define release thresholds based on the cost of wrong answers and the baseline; do not invent accuracy targets or interpret heuristic confidence as a calibrated probability.

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

**Progress:** claim support is now judged per claim against the exact cited child passage, accepting three standards: verbatim, near-extractive (every content word kept, identical numbers, units and negation polarity), and markup-only difference (table pipes and emphasis removed). Short value answers are supported with their unit. A claim that drops a condition ("only Enterprise", "within 30 days") or adds a clause is refused, and a single content word is never accepted on its own because the same digit appears in many rows of one table. Detection remains a safeguard, not entailment.

**Residual behavior:** canonical-answer faithfulness still counts whether its stored quotes are present and current; fallback answers use the per-claim lexical check above. The old reproduction where an answer and its negation both scored 1.0 is now rejected by the contradiction guard, and retries must reach the same grounding threshold and respect `abstain` ([retry path](services/api/app/retrieval/answer.py#L1151-L1156)). The remaining weakness is semantic: a genuine paraphrase, a dropped condition, or a comparative can still be rejected or, in narrow cases, accepted on lexical overlap. The score is reported as faithfulness, not as a probability that the answer is correct.

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

**Progress:** the reported failure was reproduced and traced on the reference machine with the documented local stack, and it had four independent causes, all repaired:

1. **The local model's citations were discarded.** It returns `cited_ids: ["P:E2"]` and writes `[P:E2]` inline, while the parser only accepted bare `E2`. A correct answer was therefore treated as uncited. Labels are now normalised (`E2`, `P:E2`, `[P:E2]`) and recovered from the answer text when the array is empty; a label the request did not supply is still rejected.
2. **The lexical reranker overrode correct retrieval.** Ordering used the reranker score alone. With no cross-encoder configured that score is lexical overlap, which is nearly identical for every passage of a document that names the subject. In a measured case the correct passage ranked first by dense retrieval and first by RRF while sharing no vocabulary with the question ("released" vs "Launched"), and the reranker pushed it out of the top four. Final ordering now retains a calibrated fusion signal (weight 0.3) alongside the reranker.
3. **Averaging a fixed top three diluted a filtered query.** With `plan=v2` only one passage is eligible, so two filler passages' zero scores divided a good match by three and pushed a supported answer below the abstention band. Zero-scored passages no longer count toward retrieval strength.
4. **The offline provider required every question word verbatim.** "Mean" never matched "means" and "need" never matched "require", so the deterministic evaluation path abstained on correct evidence. Gating terms now use inflection matching (shared four-character prefix, never for digits or identifiers) and words that describe the kind of answer (`need`, `mean`, `require`, `support`) no longer gate. Each removed cause has a regression test.

**Still needed:** a genuine paraphrase on a customer-facing channel still abstains unless the model writes an approved answer (by design) or the semantic equivalence check succeeds; the fusion weight is a measured default on one corpus, not a calibrated optimum; and the deepest retrieval gap remains: a question whose vocabulary is absent from the source ("released" vs "Launched") depends entirely on the embedding model until a cross-encoder reranker is evaluated.

**Historic investigation note (pre-fix):** that specific reason is returned when generation reports abstention, returns no answer, or supplies no valid cited passage IDs. A local isolated check using the development generator preserved both sentences, produced an answer and a valid cited ID, and gave a faithfulness score of 1.0, but it did **not** reproduce the reported end-to-end failure. The four causes above are now guarded by focused tests, yet a full end-to-end Mikey fixture with the configured local provider is still the acceptance test. The same isolated check selected the birth-date sentence as the supporting excerpt for the combined generated answer, so claim-level citation relevance also remains a test case.

**Plan**

- Add an end-to-end regression fixture with this exact document and question, exercising ingestion, retrieval, generation, citation validation, and the final internal Ask response. Add a regression that would have failed before these fixes and passes now; the four focused tests above prove the individual causes, but only this fixture proves the final response.
- Keep provider-dependent checks separate from deterministic unit tests. If the configured local chat or embedding provider is unavailable, report the end-to-end test as skipped, not passed, and never substitute the development provider for a claimed local-model result.
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

**Progress:** PDF empty-text detection now examines extracted text, including short text; pages without extractable text are recorded in document metadata. PDF sections and passages are split at page boundaries, and new documents record unknown parse quality instead of an unmeasured perfect score. DOCX paragraphs and tables retain their order; HTML traversal avoids duplication of nested blocks and handles common table spans. Explicit re-ingestion rebuilds older indexes, even when the normalized text is unchanged. Separator-only blocks are no longer indexed: a horizontal rule produced a passage whose entire text was `---`, whose near-neutral embedding outranked real evidence (measured: 0.7425 for `---` against 0.7241 for the paragraph stating the answer) while providing nothing to cite. Separator lines are now treated as separators when a section is assembled and when child blocks are split, while fenced content such as a YAML `---` is preserved. On the reference document this removed 7 of 39 passages.

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

**Still needed:** splitting at sentence or list boundaries can separate related conditions and needs evaluation, overlap and source offsets. Very large code blocks and individual sentences require manual splitting. These checks apply only to the local embedding provider; remote provider limits and silent truncation are not verified. Existing indexed passages remain unchanged until explicitly re-ingested, and old vectors may already be truncated. The two-child-per-section cap can still hide a third matching chunk; one child remains available for controlled comparisons. Large split operations still need throughput/resource benchmarks.

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

**Progress:** final ordering now blends the reranker score with a normalised reciprocal-rank-fusion signal (`FUSION_WEIGHT = 0.3`), because the default lexical reranker cannot separate passages of one document and was discarding a correct dense+RRF ranking. Comparison terms are lightly inflected so "expense"/"expenses" agree without touching stored vectors. A deterministic extractor answers a stated two-column attribute of a named product from the top-ranked document, refuses identifiers the row does not satisfy (`L4` from an `L3` row), refuses disagreeing rows, and requires the document to be about that product. In an instrumented development run (not yet saved as a fresh baseline), the false-abstention rate went from 0.40 to 0.0 with no false answers and unchanged hit@1/MRR; on the real local stack 5 of 5 answerable product questions answered and 6 of 6 must-abstain cases abstained.

**Progress (2026-11-22):** dense retrieval now chooses an exact cosine scan when the filtered eligible chunk count is at most `DENSE_EXACT_MAX_CHUNKS` (default 5,000), otherwise it uses HNSW; the response reports which path ran. Selection keeps up to two children per section instead of one. On the local 32-chunk corpus, twelve labeled i5-9400 questions gave identical hit@1 0.75, hit@5 1.00, and MRR 0.854 for HNSW and exact scan, with warm search medians of 21.2–21.8 ms; the exact path added no measurable end-to-end cost at this size. On three multi-part questions, two children recovered both expected facts in the package/junction-temperature and L3-cache/TLB cases where one child returned only one; the socket/chipset case still missed the chipset table. A separate synthetic-vector benchmark measured an exact scan at about 5.4 ms for 1k, 26.3 ms for 5k, and 57.6 ms for 10k chunks, against under 0.4 ms for HNSW; those warm timings measure the vector scan, not the full search.

**Residual behavior:** [hybrid search](services/api/app/retrieval/search.py#L115-L187) keeps a fusion signal alongside the reranker (`FUSION_WEIGHT = 0.3`). The default reranker is still lexical when no rerank endpoint is configured. The Ask rewrite extracts error codes, but Ask still does not use them for a dedicated exact search. Generation receives parent text while citations identify child passages. The exact-scan threshold is a latency/recall trade-off measured on synthetic vectors; it did not improve accuracy on the small local corpus, and filtered HNSW recall above the threshold has not been measured on representative data. The two-child cap is a fixed default, not an evaluated optimum. The fusion weight and a real cross-encoder have not been benchmarked on this hardware.

**Plan**

- Compare dense-only, lexical-only, RRF, and current reranking on the evaluation set. Test whether retaining a calibrated fusion signal helps before changing the ranking formula.
- Add robust exact-identifier retrieval for error codes, product names, and other critical identifiers without discarding semantic recall. Confirm actual SQL plans and index use with representative data.
- Evaluate a small CPU-compatible local reranker only if it improves held-out answer quality enough to justify p95 latency and memory cost. Do not add it solely because it is more sophisticated.
- Keep evidence identity aligned with text provided to generation: either cite exact child spans or represent expanded context with accurate provenance. Deduplicate near-identical contexts without hiding independent authoritative evidence.
- Test approximate vector-index recall under tenant, visibility, scope, and date filters against exact search. Sweep the exact-scan threshold and the per-section child cap on a representative corpus, measuring filtered recall as well as latency; tune index/query settings only after measuring corpus size, memory, latency, and recall.

**Acceptance:** relevant-evidence recall and false matches are measured before and after. Error-code, filtered, paraphrase, and provenance cases have regression tests; no ranking change is accepted on latency alone.

### 4.2 Measure and reduce expensive work

**Progress:** ingestion writes one contextual blurb per section and shares it across that section's passages, instead of one model call per passage; the same document dropped from 39 calls to 16 (102 s wall clock, warm model). A question that is already standalone skips the rewrite model call entirely; only history or a pronoun-led follow-up uses it. A verified attribute extraction now runs before any retry generation call, so a factoid answer costs one model call (2–4 s measured) rather than two.

**Progress (2026-11-22):** CPU embeddings now use a CPU-only batch of 1 by default. On the reference i5-9400, eight real passages took 12.6 s at batch 1 versus 19.6 s at batch 16 through the full provider path, a 36% reduction. Repeated query strings hit a bounded in-process cache (about 0.02 ms versus roughly 300 ms for the first encode); a live repeated search fell from about 417 ms to 34 ms, while a unique warm query still costs about 338 ms. Model load remains roughly 6–10 s and is paid lazily. Limiting PyTorch to four threads measured 10.59 s versus 10.68 s at six threads for eight passages, so it remains opt-in rather than a default. A quantized ONNX Qwen build preserved vectors (cosine ≥ 0.9996) but was slower here (12.5 s for eight passages, 733 ms per query), so it was rejected. GPU embedding measured 4.24 s for eight passages and about 134 ms per query, but cannot safely share a 6 GB card with the 4.4 GB chat model.

**Progress (2026-11-22, remote providers):** the OpenAI-compatible embedding path can now send NIM's required `input_type` (`query`/`passage`) and validates the response count and every vector dimension. A local 9router instance routes NVIDIA Nemotron 3.5 Embed 8B at the index's 1024 dimensions. Latency was highly variable: an initial browser request embedded three passages in 0.78 s, while a later cold SDK run took 5.3 s for one query and 4.6 s for two passages, against about 4.7 s for three passages and 0.34 s for a unique warm query on the local CPU. The tested 9router 0.5.123 model ID is `nvidia/nvidia/nemotron-3.5-embed-8b`; the single-prefixed listing does not match the request route. The live SDK run also caught that the OpenAI Python client requires NIM's `input_type` in `extra_body`, not as a top-level keyword; the regression test now checks the actual client contract. 9router exposes no rerank service, so a separate NIM adapter was added for `/ranking` (`query.text`, `passages[].text`, `rankings[].logit`), with a configurable path for NVIDIA's hosted `/reranking` endpoint. NIM logits are sigmoid-normalised to a monotonic 0–1 score; the existing lexical fallback remains if the endpoint is unavailable. Unit tests cover payload shape, score normalisation, embedding input types, dimension/count validation, and identity changes.

**Follow-up (2026-11-22, remote embedding, planned):** add a NIM `truncate` policy and a preflight input-length check for remote providers, because the local tokenizer check does not apply. Recalibrate the abstention bands on labeled data with the NIM reranker before treating its confidence bands as comparable. 9router routing is local but the embedding request leaves the machine for NVIDIA; document the privacy boundary and per-request cost. No end-to-end retrieval-quality comparison between Qwen 0.6B and Nemotron 3.5 Embed 8B has been run yet, and the embedding identity change requires a full reindex.

**Residual behavior:** ingestion now generates one contextual blurb per section, but sections are still processed sequentially ([pipeline](services/api/app/ingest/pipeline.py#L270-L283)), ingestion remains serial per document, and no per-stage timing is persisted. Ask still performs any needed rewrite and embedding before canonical matching. The local embedding model serializes its own calls with a lock and loads per process. The 0.6B model is still the dominant CPU cost; batch sizing and caching reduce repeated work but do not change per-vector compute. A smaller 1024-dimension English model (BAAI/bge-large-en-v1.5) measured about 16.7 s for sixteen passages versus 23.8 s for Qwen, but it is English-only and requires a reindex, so it remains an evaluated option rather than a default.

**Plan**

- Measure per-stage time, model warm-up, CPU use, RAM, GPU memory, prompt length, queue wait, and database time. Separate ingestion from interactive Ask measurements.
- Compare generated contextual blurbs with deterministic title/section/scope context; keep generated blurbs only where they improve measured retrieval enough to justify cost and potential invented context.
- Evaluate batching embeddings across appropriately bounded chunks and reusing loaded models. CPU and GPU need separate batch policies: CPU batches should avoid variable-length padding, while GPU batches should use it. Bound batch size and inference concurrency to avoid CPU contention and GPU out-of-memory failures.
- Evaluate a smaller 1024-dimension model on the held-out corpus before changing the embedding identity. Measure retrieval quality as well as CPU speed, and require a reindex and rollback plan.
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

### 5.3 Keep the local model server available and diagnosable

**Current behavior:** chat uses an OpenAI-compatible endpoint; on the reference machine this is a manually started project-local Ollama server. When it is not running, Ask returns `provider_unavailable`. The error now names the endpoint and `make local-chat`, but there is no preflight status and no in-app recovery path. A stopped server must never cause a fallback answer.

**Plan**

- Add a cheap readiness check for the chat endpoint, and separately for the embedding provider, that does not load a generation model or make a billable call. Keep database health separate.
- Surface provider status in Ask and Search before submission: endpoint, start command, and a retry action. Do not silently switch to the deterministic development provider, which would change answer semantics.
- Log provider failures with the endpoint and a correlation ID, never the API key. Keep the response a `provider_unavailable` error with no answer or gap draft.
- Document the startup order and provide one command or script for the local model server, API, and web app. Keep `make local-chat` usable independently.
- Test the stopped-server path, recovery without an API restart, and an ingestion attempt that fails cleanly without leaving a partial document.

**Acceptance:** a stopped local model server is visible before a question is sent and produces an actionable error; recovery needs no API restart; no fallback answer is generated.

## Delivery order and current position

1. **Completed:** answer matching and lexical grounding safeguards; atomic ingestion/replacement; parser preservation, separator removal, and bounded chunks; embedding identity and staged reindex; actionable provider errors.
2. **Next (M1):** provider preflight; regenerate the current baseline and add a held-out set; the Mikey end-to-end fixture; canonical duplicate cleanup; exact-identifier retrieval.
3. **Before untrusted exposure:** SSRF-safe URL fetching and real authentication/tenant checks.
4. **Then:** measured ranking and performance (fusion sweep, cross-encoder, compact evidence, batching) and governance/freshness work.
5. **Ongoing:** every new production failure becomes an evaluation case; keep a current baseline and run the integration suite against an isolated database.

Some items can be developed in parallel, but benchmark comparisons require a stable baseline. Schema or index changes should include a migration, a backup/rollback plan, and a decision on whether existing sources must be reprocessed.

## Verification and completion criteria

- Existing tests continue to pass; add focused unit tests for each repaired defect and database-backed integration tests for ingestion, reindexing, scope, canonical matching, withdrawal, and RLS.
- Run the labeled evaluation against both development and the intended local-model configuration. Report results and failure examples, not just aggregate scores.
- Verify public answers use approved public evidence only; test tenant isolation with multiple tenants and users.
- Run failure/restart and backup/restore exercises without risking production data.
- Record cold/warm latency and peak RAM/VRAM on the target machine; do not claim a speedup without a comparable measurement.
- Treat the work as iterative: new failures feed the evaluation set, and thresholds are calibrated against the cost of incorrect answers versus abstention.

## Evidence from the review

The pre-implementation review found **9 passing** API unit tests. Small local reproductions confirmed that a negated claim can receive a faithfulness score of 1.0; a blockquote and standalone number can be removed; a 13-digit identifier can be redacted; a long unpunctuated chunk can exceed its target; nested HTML content can be duplicated; PDF page references can be wrong; and a blank three-page PDF can pass the extraction check. Those defects have since been repaired with focused unit tests; an opt-in PostgreSQL integration suite covers ingestion atomicity, withdrawal locking, worker recovery, page provenance, bounded chunks, and staged reindex interruption/resume.

The current API unit suite reports **89 passed, 16 skipped** (`services/api`, 2026-11-23). The skipped tests are the opt-in PostgreSQL suite; run it against an isolated database and record the exact integration count before citing it. The saved `evaluation/baseline-dev.json` predates the fixes and is not a current accuracy estimate.

A local provider outage on 2026-11-23 returned `503` until the project-local Ollama server was started; the error now names the endpoint and `make local-chat`. A preflight/status check is still open. The review did not perform a corpus-wide quality evaluation, live-model benchmark, or target-hardware benchmark. A synthetic harness exists, but broad reviewed-corpus accuracy, peak RAM/VRAM, and cold/warm latency measurements remain open.
