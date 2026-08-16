# Runbook — module-by-module guide to this codebase

This is the "read this to actually understand the code" document. [README.md](README.md)
sells the project and tells you how to run it; [LEARNING.md](LEARNING.md) is a
chronological log of what broke and what was learned. This file is neither —
it's a static walkthrough of **every active module**, organized bottom-up
(shared infrastructure first, then the extraction agent, then the two
multi-agent extensions, then the UI/demo layer), explaining **why each module
exists** and **what each function in it does**.

Scope: this covers `src/`, `scripts/`, and `ui/` — the active, primary
codebase (document extraction). `legacy/` (the CSV data-analysis agent) is
intentionally out of scope here; it has its own docstrings and
[legacy/reports/agent_run_report.md](legacy/reports/agent_run_report.md), and
is deliberately not part of "what we're building" going forward (see
[LEARNING.md](LEARNING.md), 2026-08-12 entries).

---

## 1. Shared infrastructure (`src/`)

Everything in this section is used by the extraction agent and has no
knowledge of documents, claims, or payers — it's generic plumbing.

### `src/config.py` — externalized settings

**Why it exists:** every tunable (model, temperature, budgets, timeouts, keys)
must come from environment/`.env`/CLI, never be hard-coded, so the same code
runs against any LiteLLM-supported provider without a code change.

- **`Settings`** (pydantic-settings `BaseSettings`) — every field the agent
  reads: `model`, `temperature`, `max_tokens`, `api_key`, `api_base`,
  retry/backoff knobs, `max_steps`/`max_code_retries` (loop budgets),
  `sandbox_timeout` (legacy CSV agent only), `log_level`, `log_dir`,
  `memory_dir` (legacy CSV agent only). There is no provider-selection field —
  LiteLLM is the only backend, and the actual AI provider is chosen entirely
  by the `model` string (`openai/gpt-4.1`, `groq/llama-3.3-70b-versatile`, ...).
- **`get_settings(**overrides)`** — builds a `Settings()` from the
  environment, then applies any non-`None` CLI-flag overrides via
  `model_copy`. This is the one function every entrypoint (`src/cli.py`,
  `ui/streamlit_app.py`, `src/docproc/evaluation/evaluate.py`) calls to get its config.

### `src/llm.py` — the LLM client abstraction

**Why it exists:** one interface (`LLMClient.complete`) in front of LiteLLM
means the agent loop is never coupled to a specific provider's SDK — swapping
models is a config change (`MODEL=...` in `.env`), not a code change. There is
no offline mode: every call goes to a real provider and requires a valid key.

- **`LLMError`** / **`LLMTransientError`** — the two-tier error taxonomy.
  Transient (rate limit, 5xx, timeout) is retryable; everything else is fatal
  and surfaces immediately so the loop can fail gracefully instead of hanging.
- **`LLMClient.__init__`** — builds the single `_LiteLLMBackend` from settings.
- **`LLMClient.complete`** — the retry loop: catches `LLMTransientError`,
  backs off exponentially (`retry_base_delay * 2**attempt`), gives up after
  `max_retries` and raises `LLMError`. Non-transient errors re-raise
  immediately without burning retries.
- **`_LiteLLMBackend`** — the real-provider path, and the only backend.
  `__init__` lazy-imports `litellm` and disables its own retries
  (`num_retries=0`) so this project owns retry policy in exactly one place.
  `complete` builds the LiteLLM `kwargs`, streams token-by-token if `on_token`
  is given, and maps every provider exception to `LLMTransientError` or
  `LLMError`.

### `src/common.py` — the one truly shared helper

**Why it exists:** `_extract_json` is needed by both agents (docproc and the
legacy CSV one) to tolerate a model wrapping its JSON in prose or code fences.
Everything else that used to live here (`AgentOutcome`, `ClarifyHandler`) was
CSV-specific and moved to `legacy/common_csv.py` once a second, unrelated
consumer (docproc) proved they weren't actually generic — see the
2026-08-12 "module boundary drawn wrong" entry in [LEARNING.md](LEARNING.md).

- **`_extract_json(text)`** — strips a leading/trailing code fence if present,
  then scans for the first *complete, balanced* JSON object: tracks brace
  depth from the first `{`, skipping characters inside string literals, and
  returns as soon as depth returns to zero. Deliberately not a greedy regex
  (`\{.*\}`) — that matches from the first `{` to the LAST `}` in the whole
  response, so any trailing content the model appends after the real object
  closes gets swallowed into the "extracted" JSON and fails to parse. This
  was 69 of 198 real parse failures across this project's trace logs before
  the fix (see the 2026-08-14 entry in [LEARNING.md](LEARNING.md)). Raises
  `ValueError` if nothing looks like JSON at all, or the braces never
  balance (the caller re-prompts the model when this happens).

### `src/logging_utils.py` — observability

**Why it exists:** the assessment (and good practice) requires every agent
step be inspectable after the fact, not just visible in a live terminal.

- **`setup_logging(level)`** — configures the root logger's console format
  once, at CLI startup.
- **`RunTrace`** — one instance per agent run. `__init__` opens
  `logs/run_<8-hex-id>.jsonl`. `record(step, kind, **fields)` appends one JSON
  line to that file *and* echoes a short human-readable line to the console
  for `decision`/`observation`/`final`/`error` events. `as_dict()` returns the
  whole trace in memory (used where a caller wants it without re-reading the
  file). `_truncate` clips a long string for the console echo only — the
  JSONL file always has the full text.

---

## 2. The document-extraction agent (`src/docproc/`)

This package is the primary deliverable. Every module here is specific to
turning one payer document into a validated claim record plus a triage
decision.

### `src/docproc/schemas.py` — the data contracts

**Why it exists:** every value that crosses a boundary in this project
(model output, validator input/output, agent outcome) is a Pydantic model, not
a dict — that's what makes "structured output parsing" and "grounding" checks
possible mechanically instead of by convention.

- **`DocType`** — which of the 3 surface formats a document is.
- **`FieldValue`** — the grounding unit: `value` (normalized) +
  `source_text` (verbatim span) + `confidence`. This split exists so
  normalization (money/date formatting) and grounding (exact-quote checking)
  are two independent, non-conflicting concerns. A `field_validator` on
  `value` coerces a bare JSON number to `str` (excluding `bool`) — models
  routinely emit `"value": 2306.72` instead of `"value": "2306.72"`; this
  was 129 of 198 real parse failures across this project's history before
  the fix (see the 2026-08-14 entry in [LEARNING.md](LEARNING.md)).
- **`LineItem`** — one billed service line.
- **`ClaimExtraction`** — the full structured record: every scalar field is a
  `FieldValue`, plus `denial_codes` (plain string list) and `line_items`.
- **`ValidationIssue`** / **`ValidationReport`** — one concrete failure, and
  the aggregate pass/fail + issue list `validate()` returns.
  `ValidationReport.render()` turns it into the next turn's prompt text.
  Each issue carries `check` (`grounding`/`arithmetic`/`business_rules`) so
  callers can weigh a *grounding* failure differently by provenance — see
  `store.triage_decision` in the enterprise pipeline section below.
- **`DocAction`** / **`Triage`** / **`DocStep`** — the agent's control
  protocol. `DocStep` is what the model must emit every turn: a `thought`, one
  `DocAction`, and the payload for that action (`codes_to_look_up` /
  `extraction` / `triage`).
- **`DocOutcome`** — what `DocumentAgent.run()` returns.
- **`WorklistItem`** / **`PortfolioOutcome`** — multi-agent A's output shape.
- **`ReconciliationIssue`** / **`ReconciliationReport`** — multi-agent B's
  output shape.

### `src/docproc/registry/codes.py` — the CARC/RARC registry (the agent's tool)

**Why it exists:** this is what turns raw extraction into an *actionable*
triage decision. Knowing a code's *meaning* — and whether it's appealable — is
domain knowledge that must come from a maintained table, not a guess, so it's
modeled as a tool call rather than baked into the prompt.

**`CarcRegistry`** — the registry and every lookup/derivation method live on
this one class (curated codes, real-X12 fallback, category heuristics,
triage derivation, rendering). Every other module still imports plain
functions (`lookup_code`, `derive_triage`, ...) — they're thin wrappers
around a module-level `_default_registry = CarcRegistry()` singleton, kept
for backward compatibility with every existing call site. Internals:

- **`DenialCode`** — one registry entry: code, CARC/RARC kind, category
  (`coverage`/`coding`/`authorization`/.../`contractual`), `appealable`, and
  the `typical_action` an RCM analyst would take.
- **`CarcRegistry._curated`** — ~16 hand-curated codes with analyst-authored
  `typical_action` text, tried first by `.lookup()`.
- **Real X12 fallback** — anything not in `_curated` falls back to
  `carc_codes.RAW_CARC`: the actual, official Claim Adjustment Reason Code
  list (297 current codes, fetched from x12.org — "X12 External Code Source
  139", the real HIPAA-mandated code set), generated by
  [scripts/fetch_carc_codes.py](scripts/fetch_carc_codes.py). `CarcRegistry._categorize`
  is a keyword heuristic (our own domain judgment, not X12's — the official
  list has no category field) that maps the real description to one of the
  existing categories; `_ACTION_BY_CATEGORY` templates a generic action per
  category. Net effect: any of the ~280 real CARC codes outside the curated
  set now resolves to a genuine, sourced description instead of `None`.
- **`NON_APPEALABLE_CATEGORIES`** — `{"contractual", "duplicate"}`; used both
  by triage logic and by the eval's `expected_triage`.
- **`lookup_code(code)`** (→ `CarcRegistry.lookup`) — normalizes formatting
  (`CO45` ≡ `CO-45`), tries the curated registry, then the real X12 fallback
  (stripping a group-code prefix like `CO-`/`PR-`/`OA-`/`PI-`/`CR-` via
  `_bare_carc_number` first — careful not to confuse that with CARCs that
  have their own letter prefix, e.g. `A1`, `B4`, `P12`). Returns `None` only
  for truly unrecognized codes (never invents a meaning).
- **`lookup_codes(codes)`** (→ `CarcRegistry.lookup_many`) — batch form,
  keyed by the original string — this is literally what the `lookup_code`
  tool call returns to the agent.
- **`known_codes()`** — sorted list of every *curated* code, used to seed a
  sample in the system prompt.
- **`render_lookup(results)`** — formats a batch lookup as the `CODE_LOOKUP:`
  tool-observation message the agent reads on its next turn.
- **`primary_denial_code(denial_codes)`** — resolves the single code that
  should drive the triage decision (first appealable/actionable code, else
  the first resolvable code, else `None`).
- **`derive_triage(denial_codes)`** — mechanically derives
  `(is_appealable, denial_category)` straight from the registry. Added after
  a real cross-tier comparison (`gpt-4.1` vs `gpt-4.1-mini`, see
  [LEARNING.md](LEARNING.md)) showed a model can extract every field
  correctly and still get triage wrong — paraphrasing a registry category
  instead of copying it verbatim, or inverting appealability. `_finalize_node`
  in `agent.py` now overrides the model's triage with this function's output
  instead of trusting the model's own reasoning about codes it already looked
  up. This closed a real bug: `gpt-4.1-mini`'s triage-correctness went from
  2/9 to 9/9 on the full corpus once this was wired in — with zero change to
  the model or prompt.

### `src/docproc/prompts.py` — the system prompt

**Why it exists:** the entire control contract (what JSON shape to emit, the
grounding rule, the normalization rule) lives in one prompt string so it's
auditable and versionable, not scattered across code.

- **`DOC_SYSTEM_PROMPT`** — the full instruction set: emit one `DocStep` JSON
  per turn, the `FieldValue` shape, the hard rules (never invent a value,
  `source_text` must be verbatim, normalize dates/money, look up codes before
  reasoning about them, finalize only after validation passes).
- **`build_doc_task_message(document, filename)`** — wraps the raw document
  in `<<<BEGIN>>>...<<<END>>>` delimiters (what the prompt tells the model to
  expect) plus the initial instruction.
- **`build_validation_message(report)`** — renders a `ValidationReport` as the
  next turn's message.

### `src/docproc/validation.py` — the mechanical feedback signal

**Why it exists:** this is the project's central design bet — self-correction
must be driven by *verifiable* failures, not "reflect on your answer." Three
independent, composable checks:

- **`check_grounding(ext, document)`** — every populated field's
  `source_text` must literally occur in the document (whitespace/case
  normalized). Missing `source_text` entirely is also an error. This is the
  anti-hallucination check.
- **`check_arithmetic(ext)`** — line items must sum to the stated totals;
  paid ≤ allowed ≤ charged, both at claim level and per line. Catches
  transcription/OCR-style digit errors that grounding alone would miss (a
  wrong-but-quoted number still "occurs" in the text).
- **`check_business_rules(ext)`** — dates parse and are ordered (deadline
  after date of service), denial codes exist in the registry, member id looks
  plausible.
- **`validate(ext, document)`** — runs all three and combines them; `ok` is
  `False` if any issue is `severity="error"` (warnings don't block finalize).
- Helpers: **`_norm`** (whitespace/case fold for substring checks), **`_num`**
  (parse a currency-ish string), **`_parse_date`** (try a few known date
  formats).

### `src/docproc/agent.py` — the LangGraph loop itself

**Why it exists:** this is the actual agent. Everything above is either a
contract it uses or a tool it calls; this module is the `reason → act →
observe → self-correct → respond` control flow.

- **`DocumentAgent`** — one instance per document (multi-agent orchestrators
  create a fresh one per document, so there's never shared state between
  claims). `__init__` builds and compiles the graph once. `_build` wires the
  five nodes (`decide`/`lookup`/`extract`/`finalize`/`give_up`) and their
  conditional routing.
- **`run(document, filename, on_event, on_token)`** — invokes the compiled
  graph to completion and returns a `DocOutcome`. `on_event`/`on_token` are
  optional callbacks so a live UI (CLI `--stream`, the Streamlit app) can
  render progress without the agent knowing anything about UIs.
- **`_emit`** — the single choke point every node uses to both write to the
  JSONL trace and forward to `on_event`.
- **Nodes:**
  - **`_decide_node`** — asks the LLM for the next `DocStep`; an `LLMError`
    here ends the run with `status="error"` instead of raising, so the graph
    always terminates cleanly.
  - **`_lookup_node`** — the agent's one tool call: resolves requested codes
    against the registry, feeds the result back as an observation.
  - **`_extract_node`** — the "observe" step: runs `validate()` against the
    just-emitted extraction and feeds the report back. This is what makes
    self-correction meaningful.
  - **`_finalize_node`** — terminal success: packages the validated extraction
    plus triage into the returned `DocOutcome`. Does **not** trust the
    model's own `is_appealable`/`denial_category`/`dollars_at_risk` — these
    are mechanically re-derived via `codes.derive_triage` /
    `primary_denial_code`, and if the model's triage disagreed,
    `recommended_action`/`rationale` are regenerated from the registry too
    (so the printed explanation never contradicts the overridden verdict).
    See `codes.py` above for why.
  - **`_give_up_node`** — terminal failure: step budget exhausted before
    reaching `finalize`.
- **Routers:** **`_route_decide`** enforces the step budget and routes on the
  model's chosen action; **`_loop`** (used after lookup/extract/finalize)
  either ends (if a terminal outcome was set) or loops back to `decide`.
- **`_decide`** — gets one validated `DocStep`, self-correcting up to 3
  attempts if the model emits invalid JSON.

### `src/docproc/generator.py` — the synthetic corpus

**Why it exists:** ground truth must be exact, so evaluation is computed
metrics, not a qualitative judgement. Documents are rendered **from** known
records, not the other way around.

- **`build_record(rng, doc_id)`** — creates one ground-truth record: random
  payer/provider/patient, 1–3 line items, one line always carries the primary
  denial (allowed=paid=0), the rest adjudicate normally with a contractual
  (`CO-45`) adjustment.
- **`render_denial_letter`** / **`render_eob`** / **`render_remittance`** —
  the same record through three deliberately different surfaces (prose
  letter, fixed-width EOB table, plain-text 835-style summary) so an
  extractor that only handles one template gets caught by the mixed corpus.
- **`generate(out_dir, n, seed)`** — the standard single-document corpus:
  writes `n` documents round-robin across the 3 formats plus
  `ground_truth.json`.
- **`build_corrected_record(rec, rng)`** — multi-agent B's data: a deep copy
  with a realistic post-adjudication correction (a later remittance takes
  back part of a payment, keeping its own arithmetic internally consistent).
  Returns `(rec, [])` unchanged if there's nothing to take back (a
  fully-denied claim already at `total_paid=0`) — a real payer can't recoup a
  payment that was never made (see the 2026-08-12 bug entry in
  [LEARNING.md](LEARNING.md) about this exact edge case).
- **`generate_triads(out_dir, n_claims, seed, discrepancy_rate)`** — writes
  matched claim triads (same claim, all 3 formats) plus `manifest.json`
  recording which claims got a *real* injected discrepancy.
- Formatting helpers: **`_money`** (round to cents), **`_fmt_date`** /
  **`_fmt_money`** (render in whichever of the corpus's mixed styles a given
  renderer needs).

### `src/docproc/evaluation/evaluate.py` — the field-level harness

**Why it exists:** because ground truth is exact by construction, this can
report real precision/recall/F1/grounding-rate instead of eyeballing output.

- **`_norm_scalar(field, value)`** — normalizes a value for exact-match
  comparison (money to 2-decimal strings, else whitespace-collapsed
  lowercase); `None`/`""` both mean "absent."
- **`expected_triage(truth)`** — thin wrapper around `codes.derive_triage`;
  since the agent's own `_finalize_node` now uses the same function, the
  eval's "expected" and the agent's "actual" triage agree by construction
  whenever the underlying `denial_codes` extraction is correct.
- **`evaluate(docs_dir, limit)`** — runs every document through a
  fresh `DocumentAgent`, scores each field, prints the per-field and
  aggregate report (precision/recall/F1/grounding rate/line-items-exact/
  validation-passed/triage-correct). Returns a CI-friendly exit code
  (0 if F1 > 0.9 and every triage decision is correct).
- **`main`** — `python -m src.docproc.evaluation.evaluate --docs data/docs` CLI.

---

## 3. Multi-agent extensions

Both of these reuse `DocumentAgent` completely unchanged — they only add an
orchestration layer around it.

### `src/docproc/workflows/portfolio.py` — A: portfolio triage

**Why it exists:** an RCM analyst works a queue, not one denial at a time.
This mirrors the CSV agent's old planner/synthesizer shape
(`legacy/orchestrator.py`) applied to a batch of documents instead of
sub-questions.

- **`PortfolioOrchestrator`** — no LLM call of its own; it delegates and
  aggregates deterministically. `run(doc_paths, on_event, max_workers)`
  extracts+triages every document (fresh `DocumentAgent` each), then ranks
  the batch by `dollars_at_risk` and rolls up totals by category.
  `max_workers=1` (default) is strictly sequential, unchanged order and
  streaming; `>1` uses a `ThreadPoolExecutor` (I/O-bound LLM calls
  parallelize well) and disables live streaming. Benchmarked against the
  real API: concurrency reduces wall-clock time but real per-account rate
  limits cause failures at even modest concurrency — see
  [README's "Scaling this to enterprise volume"](README.md#scaling-this-to-enterprise-volume)
  and [LEARNING.md](LEARNING.md) for the real numbers. `_to_item` reduces one
  document's full `DocOutcome` down to a `WorklistItem` row.

### `src/docproc/workflows/reconcile.py` — B: cross-document reconciliation

**Why it exists:** no single-document validator can catch a claim where the
denial letter, EOB, and remittance advice each individually validate
perfectly but disagree *with each other* (e.g. a later remittance paid less
than the EOB already told the provider). This needs more than one agent's
output to exist at once — a genuinely different multi-agent case than A's
"parallelize for speed."

- **`RECONCILE_FIELDS`** — the claim-level fields worth cross-checking
  (line-item comparison is out of scope for this first pass — noted in the
  module docstring as needing CPT-level matching across differently-laid-out
  documents, a bigger job).
- **`reconcile(extractions)`** — pure function: for each field, collects the
  value from every document that has it, and flags disagreement (numeric
  tolerance for money, normalized-string equality for the rest).
- **`ClaimReconciler`** — the orchestration layer: `run(doc_paths, on_event)`
  extracts every document in a claim group (fresh `DocumentAgent` each, same
  "fresh specialist per input" shape as `PortfolioOrchestrator`), then calls
  `reconcile()` on the results.
- Helpers: **`_num`**, **`_norm`** — same normalization as `validation.py`,
  kept local since this module doesn't import that one.

---

## 4. Enterprise ingestion pipeline

Turns the single-process agent into something that scales horizontally and
has a human review step. See
[README's "Scaling this to enterprise volume"](README.md#scaling-this-to-enterprise-volume)
for the architecture diagram and the `docker compose` commands.

### `src/docproc/queue/store.py` — durable job queue + review state

**Why it exists:** a real pipeline needs work to survive a crash, be claimed
by exactly one worker, and hold review state between the worker that produced
a result and the human who signs off on it. SQLite (WAL mode) gives all three
with zero extra infrastructure, so the whole thing runs on a laptop.

- **`JobStore`** — the queue. `_conn()` opens a short-lived connection per
  operation with WAL (readers never block writers), `busy_timeout` (wait
  rather than raise under contention), and `row_factory=Row`.
- **`enqueue(paths, batch)`** — idempotent per `(doc_path, batch)`, so
  re-running after a partial failure doesn't duplicate work.
- **`claim_next(worker_id, batch)`** — the concurrency-critical one: a
  `BEGIN IMMEDIATE` transaction wrapping a subselect + UPDATE, so two workers
  can never claim the same row. Returns `None` when drained.
- **`complete(...)`** — records extraction/validation/triage JSON plus
  denormalized columns (claim number, `$` at risk, category) for cheap
  worklist sorting. Takes `error` so an agent that fails *by returning*
  `status="error"` (rather than raising) doesn't lose its message.
- **`fail(job_id, error, max_attempts)`** — re-queues while attempts remain
  (transient LLM errors), then routes to `needs_review` so nothing vanishes.
- **`record_review(...)`** — applies a human `approved`/`rejected` decision,
  optionally overriding the triage.
- **`requeue(status, batch, only_errors)`** — send failed jobs back to
  `pending` after a fix. `only_errors=True` by default so a blanket requeue
  can't discard genuine human-review items.
- **`list_jobs` / `get` / `stats` / `reset`** — worklist query (ranked by `$`
  at risk), single fetch, ops counters, and teardown.
- **`ReviewPolicy`** — the mechanical gates (`high_value_threshold`,
  `qa_sample_rate`).
- **`triage_decision(...)`** — decides `needs_review` vs `auto_approved`.
  Never uses model self-confidence: agent error, failed validators,
  unresolved denial code, dollars over threshold, or a random QA-audit
  sample (`qa_sample_rate`, hashed on job id so a retry doesn't flip a
  document in/out of the sample). **Provenance-aware** —
  `deterministic=True` (X12 parse) suppresses *grounding-only* failures,
  because grounding is an anti-hallucination check and a parser can't
  hallucinate; without this, 10/10 EDI documents were flagged for a derived
  value with no quotable span (see [LEARNING.md](LEARNING.md)).

### `src/docproc/queue/ratelimit.py` — token-bucket pacing

**Why it exists:** the direct fix for the measured failure where naive
concurrency turned into `RateLimitError`s rather than throughput.

- **`TokenBucket(tokens_per_minute, safety_factor, num_processes)`** —
  thread-safe bucket shared by all threads in a worker process.
  `safety_factor` (0.85) reserves headroom because estimated token cost is
  never exact; `num_processes` splits the account budget across worker
  containers. `acquire(n)` blocks until budget exists and returns the wait,
  so pacing overhead is reported rather than guessed.
- **`NullLimiter`** — same interface, no-op, so callers never branch.
- **`estimate_tokens(document, max_steps)`** — order-of-magnitude budget
  (~4 chars/token × turns + prompt/response allowance).

### `src/docproc/queue/worker.py` — the horizontally-scalable unit

**Why it exists:** the thing `--scale worker=N` multiplies. Stateless and
worker-agnostic by construction: jobs are claimed atomically, agents are
per-document, each document writes its own trace.

- **`Worker.run_once` / `run_until_drained(batch, threads, follow)`** —
  claim-and-process; `threads>1` puts several documents in flight inside one
  process, `follow=True` keeps polling (service mode under compose).
- **`Worker._process(job)`** — the core: `ingest()` → EDI goes to
  `finalize_structured` with **zero LLM calls and zero rate-limit budget**,
  everything else acquires budget then runs `DocumentAgent`; then
  `triage_decision` routes the result. Catches every exception so one poison
  document can't kill a fleet.
- **`build_worker` / `parse_args` / `main`** — CLI wiring
  (`--threads`, `--tpm`, `--processes`, `--review-threshold`,
  `--qa-sample-rate`, `--follow`), all also settable by env var for compose.

### `src/docproc/queue/pipeline.py` — operator CLI

**Why it exists:** submitting work and processing work are different services
in any real deployment (an SFTP poller vs. a worker fleet); keeping them in
separate entrypoints makes that boundary explicit.

- **`discover(dir)`** — every *ingestible* format, not just `.txt`.
- **`cmd_enqueue` / `cmd_status` / `cmd_requeue` / `cmd_reset`** — push a
  directory onto the queue, print ops counters (including
  `processed WITHOUT an LLM`, the EDI cost story), retry failures after a
  fix, and tear down.

---

## 5. Demo, CLI, and UI layer

### `src/cli.py` — the CLI entrypoint

**Why it exists:** the single command surface for all three capabilities
above, so `--doc` / `--batch` / `--reconcile` are just three branches over the
same underlying agent/orchestrators. Named `cli.py`, not `agent.py`, to keep
it visually distinct from `src/docproc/agent.py` (the actual `DocumentAgent`
this module is a thin shell around) — the two were easy to confuse by name
alone before this rename.

- **`parse_args`** — defines the flags for all three modes plus the shared
  model/temperature/stream flags.
- **`make_stream_callback`** — builds the `on_event` used for `--stream`;
  renders `decision`/`tool_call`/`validation`/`final`/`error` events to
  stderr.
- **`render_worklist`** / **`render_reconciliation`** — console formatting for
  the portfolio and reconciliation outcomes.
- **`_run_single`** / **`_run_batch`** / **`_run_reconcile`** — one function
  per mode; each builds its own `LLMClient` and orchestrator/agent and prints
  the result. `_run_single` routes the document through
  `docproc.ingestion.ingest.ingest()` first (see below) instead of always
  reading it as plain text.
- **`main`** — dispatches to whichever of `--batch` / `--reconcile` / `--doc`
  was given.

### `scripts/fetch_carc_codes.py` — real CARC registry fetcher

**Why it exists:** generates `src/docproc/registry/carc_codes.py` from the live,
authoritative X12.org CARC page instead of hand-transcribing ~300 codes
(error-prone and instantly stale). A reproducible fetch, not a one-off paste.

- **`fetch_current_codes()`** — downloads the page, parses `<table
  id="codelist">` with BeautifulSoup, keeps only `class="...current"` rows
  (skips deactivated codes), strips the `<span class="dates">` before taking
  the description text.
- **`render_module(codes)`** — renders the fetched `{code: description}` as a
  plain generated Python module — verbatim X12 data only, no invented fields.
- **`main`** — fetch + write `src/docproc/registry/carc_codes.py`. Re-run to refresh
  on X12's own update cadence (the page publishes a "Last updated" date).

### `src/docproc/ingestion/x12_parser.py` — reusable X12 835 parser

**Why it exists:** promoted out of `scripts/extract_x12_835.py` so
`ingestion/ingest.py` can call it directly for `.edi` files without going through the
LLM loop — an 835 is already-structured EDI, not prose, so there's nothing
for an LLM to read a "payer name" *sentence* out of.

- **`parse_835(raw)`** — a minimal segment-grammar parser (splits on `~` then
  `*`, dispatches on segment tag: `N1`/`CLP`/`NM1`/`DTM`/`SVC`/`CAS`) that
  builds a real `ClaimExtraction`, reusing the project's schema unchanged.
  Deliberately minimal — real 835s have loops/repeats/optional segments this
  doesn't handle.

### `src/docproc/ingestion/ingest.py` — document ingestion router

**Why it exists:** real payer documents arrive in different formats, and
feeding all of them through the LLM wastes a paid call (and adds a
hallucination surface) on data that's already deterministic. This picks the
cheapest trustworthy extractor per source type instead of treating "call an
LLM" as the only tool.

- **`ingest(path)`** — routes by file extension: `.edi`/`.835`/`.x12` →
  `x12_parser.parse_835` (fully structured, zero LLM calls); `.pdf`/`.docx`/
  image extensions → [Docling](https://docling-project.github.io) converts
  to Markdown, which then feeds the normal LLM path exactly like a `.txt`
  document; anything else → read as plain text (the original behavior,
  unchanged). Docling is an optional dependency (`pip install docling`,
  commented in `requirements.txt`) — `ingest()` raises a clear `RuntimeError`
  with the install command if it's missing and a PDF/image/DOCX is given.
- **`finalize_structured(ext, raw)`** — for the `.edi` path: builds a
  complete `DocOutcome` with **no LLM call at all** — `validate()` runs the
  same mechanical checks as the LLM path, and triage uses the exact same
  `codes.derive_triage` / `primary_denial_code` functions `agent.py`'s
  `_finalize_node` uses. Verified against `scripts/extract_x12_835.py`'s
  sample and against a real generated PDF round-tripped through Docling +
  the LLM (100% field match against ground truth, see [LEARNING.md](LEARNING.md)).

### `scripts/extract_x12_835.py` — real-format extraction demo

**Why it exists:** proves extraction against a genuinely public wire format
(X12 835 EDI), not just the synthetic prose corpus — see the
[Data sources](README.md#data-sources) section of the README for why this
matters. The parser itself now lives in `src/docproc/ingestion/x12_parser.py`; this
script is the narrated demo that imports it.

- **`main`** — parses the bundled sample via `x12_parser.parse_835` and runs
  it through the project's real `validate()`, printing both — the one
  command that demonstrates the whole claim end to end.

### `ui/streamlit_app.py` — the primary Streamlit UI

**Why it exists:** every capability above previously only had a CLI, which
made the project's own tooling asymmetric with its stated priority (the
*legacy* CSV agent had a polished UI; the primary agent didn't). See the
2026-08-13 entry in [LEARNING.md](LEARNING.md).

- **`make_on_event(log_box)`** — returns an `on_event` closure that renders an
  accumulating markdown trace into a Streamlit placeholder; the exact same
  event-kind vocabulary as the CLI's `make_stream_callback`.
- **`render_extraction(outcome)`** — renders one `DocOutcome` (extraction
  table, validation result, triage card) — shared by the single-document
  mode so the CLI and UI never drift in what they show.
- Three mode blocks (not extracted into functions, since each is a distinct
  Streamlit page-flow driven by `st.radio`): **Single document** (pick/upload
  → `DocumentAgent.run`), **Portfolio triage** (directory →
  `PortfolioOrchestrator.run`), **Cross-document reconciliation** (directory +
  optional `generate_triads` call → `ClaimReconciler.run` per claim group,
  plus a "caught N/M injected discrepancies" scoreboard).
- **Review queue (HITL)** — the fourth mode, and the human half of the
  enterprise pipeline. Reads `JobStore` directly (WAL means it can read live
  while workers write): an ops dashboard (queued / pending / auto-approved /
  needs-review / `$` at risk / LLM calls / documents processed with zero
  inference), the flagged worklist ranked by dollars at risk with a "why
  flagged" column, and a per-document review pane — source text, the
  extracted fields *with the span each was grounded in*, validation errors,
  the agent's triage — plus approve / edit-and-approve / reject, written
  back via `record_review`.

---

## 6. Tests and independent evaluation

### `tests/` — unit tests (pytest)

**Why it exists:** every pure function identified in the earlier
production-readiness self-assessment now has real assertions, not just
one-off terminal verification. `pytest.ini` (repo root) sets
`pythonpath = .` so `from src...` imports work without installing the
project as a package. 85 tests, ~0.3s, no LLM/API key/network needed.

- **`test_validation.py`** — the three mechanical validators, including the
  fabricated-value and broken-arithmetic cases run live earlier in the
  project, now as real assertions.
- **`test_codes.py`** — the two-tier CARC lookup, `derive_triage`, and a
  regression test for the letter-prefixed-CARC bug (`A1`/`B4`/`P12`) caught
  before it shipped.
- **`test_x12_parser.py`** — `parse_835` against the real bundled
  `data/real_world/sample_835.edi`.
- **`test_common.py`** — `_extract_json` against the exact real failure
  strings captured from historical trace logs (the greedy-regex
  trailing-characters bug).
- **`test_schemas.py`** — the `FieldValue` numeric-coercion validator (the
  dominant real parse-failure fix).
- **`test_ratelimit.py`** — `TokenBucket`/`NullLimiter`/`estimate_tokens`.
- **`test_store.py`** — the durable queue's atomic-claim guarantee and every
  branch of `triage_decision` (error, high-value, unresolved-code,
  grounding-provenance, OCR-confidence gate).
- **`test_ingest.py`** — extension-based routing and the Docling
  import-error hint (mocked; doesn't require Docling installed).

### `src/docproc/evaluation/judge_eval.py` — independent LLM-judge faithfulness eval

**Why it exists:** a second, independent accuracy signal, deliberately NOT
built on the `ragas` package -- installing it (checked: v0.4.3) pulls in ~8
LangChain-family packages plus HuggingFace `datasets` and then fails to
import out of the box (`ModuleNotFoundError:
langchain_community.chat_models.vertexai`); this project also already made
an architectural choice to use one provider-agnostic gateway (LiteLLM), not
LangChain. Implements the same underlying idea RAGAS's `Faithfulness` metric
does (verify each claim against the source) using the project's own
`LLMClient`.

- **`judge_extraction(llm, document, ext)`** — one extra LLM call per
  document; shown the raw document plus the extracted (field, value) pairs
  WITHOUT `source_text`, so it can't rubber-stamp using the same evidence
  the mechanical grounding check already trusts.
- **`run(docs_dir, limit)`** / **`main`** — CLI: `python -m
  src.docproc.evaluation.judge_eval --docs data/docs`. Real result on the 9-document
  corpus: 97/99 fields judged supported; both flagged fields were the
  judge's OWN mistake (date-format confusion), not a real extraction error
  -- see [LEARNING.md](LEARNING.md) for the full analysis. A genuinely
  complementary signal, not a strictly-better oracle.

---

## Reading order, if you're new to this repo

1. [README.md](README.md) — what this is and why.
2. `src/docproc/schemas.py` — the data shapes everything else passes around.
3. `src/docproc/agent.py` — the actual loop.
4. `src/docproc/validation.py` — what makes self-correction real.
5. `src/docproc/workflows/portfolio.py` and `reconcile.py` — the two multi-agent
   extensions, once the single-document loop makes sense.
6. `src/docproc/queue/store.py` and `worker.py` — how one agent run becomes a
   scalable, reviewable pipeline.
6. [LEARNING.md](LEARNING.md) — the mistakes made and caught along the way;
   several are more instructive than the code that was eventually correct.
