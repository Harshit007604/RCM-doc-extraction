# Learning Log

A changelog kept the *learning* way. Every entry records not just **what
changed**, but **why I believed it would work**, **what the evidence said**, and
**what I actually learned** — including the times the belief was wrong.

Rule for this file: **an entry is only worth writing if it contains something I
did not know before making the change.** Pure housekeeping goes in git history,
not here.

## Entry template

```markdown
## YYYY-MM-DD — <short title>

**Goal** — what I was trying to achieve or find out.
**Action** — what I actually changed or ran.
**Evidence** — real output, numbers, error text. No paraphrase.
**Interpretation** — what the evidence means. Say it plainly if it disproves
the hypothesis.
**Learned** — the transferable lesson. One or two lines.
**Next** — the decision this unlocks, or the open question it leaves.
```

---

## 2026-08-16 — WAL-mode SQLite corruption, for real, over a Docker Desktop bind mount

**Goal** — Continue exercising the containerized pipeline from the previous
entry's e2e run. No new action taken deliberately -- this was caught by the
Streamlit UI itself throwing an unhandled exception mid-session.

**Action** — While the 4-worker fleet from the prior entry was still
draining in the background, opening the Review queue (HITL) mode threw:

```
sqlite3.DatabaseError: file is not a database
  File "/app/ui/streamlit_app.py", line 284, in <module>
    store = JobStore(db_path)
  File "/app/src/docproc/queue/store.py", line 203, in _conn
    conn.execute("PRAGMA journal_mode=WAL")
```

Confirmed on the host, not just in the container:
```
$ file data/queue/jobs.db
data/queue/jobs.db: data          # NOT "SQLite 3.x database"
$ sqlite3 data/queue/jobs.db "PRAGMA integrity_check;"
Error: in prepare, file is not a database (26)
$ xxd data/queue/jobs.db | head -1
00000000: 0500 0000 340e fc00 0000 003b 0ffb 0ff6  ....4......;...
```
The first 16 bytes should be the literal string `SQLite format 3\0`; instead
byte 0 holds what looks like raw B-tree page content. That's the exact
signature of a page write landing at the wrong file offset -- a checkpoint
or commit from one connection clobbering another's view of the file header.
`sqlite3 data/queue/jobs.db ".recover"` failed immediately (can't even read
the schema), confirming this isn't fixable after the fact.

**Interpretation** — This is not app-level corruption (no bug in `store.py`'s
SQL) -- it's the topology. WAL journal mode requires a shared-memory
(`-wal`/`-shm`) segment that every accessor `mmap()`s and keeps coherent so
readers and writers agree on which frames are valid. The actual topology
under test was 4 separate worker **containers** + a `ui` container + several
one-off `status` containers, all bind-mounting `./data` on **Docker Desktop
for macOS**. Docker Desktop's bind-mount implementation does not reliably
keep that shared-memory segment coherent across genuinely separate
containers (as opposed to threads/processes inside one container sharing a
native filesystem view) -- this is a known class of issue with SQLite WAL
over virtualized/network filesystems, and this project's own multi-container
`--scale worker=4` demo is exactly the shape that triggers it.

**Fix** — Switched `_conn()`'s `PRAGMA journal_mode` from `WAL` to `DELETE`
(the classic rollback journal). Rollback-journal mode only needs plain
POSIX byte-range locks (`fcntl`), which the same bind mount honors
correctly -- the cost is a writer briefly holding an exclusive lock during
commit (readers wait, via the existing `busy_timeout=30000`, instead of
never blocking at all under WAL). Given the alternative is silent, total
queue corruption, correctness wins the trade.

**Evidence the fix actually works** (not just "should"):
1. `rm -rf data/queue`, rebuilt the 4 images that touch `store.py`,
   re-enqueued the same 110-document corpus.
2. Recreated the EXACT failing topology: `docker compose up -d --scale
   worker=4 worker ui`, then hammered it with 6 rapid sequential
   `docker compose run --rm status` calls, then 10 backgrounded parallel
   ones. After each round: `file data/queue/jobs.db` → `SQLite 3.x
   database...`, `PRAGMA integrity_check` → `ok`.
3. Reloaded the actual browser tab that had thrown the exception --
   Review queue (HITL) now loads cleanly and shows the live, correct
   pipeline counters (4/110 processed at the time, 0 needs_review).
4. `pytest tests/test_store.py` -- 22/22 still passing (the schema/queue
   logic itself never changed, only the journal-mode PRAGMA).

**Learned** — "WAL mode makes this multi-process-safe" is a claim that has
to be tested against the ACTUAL deployment topology, not just against
multiple threads/processes on one filesystem view. Docker Compose's
`--scale worker=N` looks like "just more processes" from the code's
perspective, but it's actually N separate containers, each with their own
container-to-host filesystem boundary -- a meaningfully different
concurrency environment than the multi-threaded-single-process case WAL is
usually validated against. A placeholder assumption ("SQLite WAL + bind
mount = safe") that's never been stress-tested against the specific
multi-container topology it's deployed into is exactly the kind of gap that
only a real, sustained e2e run surfaces -- a code review of `store.py` alone
would never have caught this, since every individual SQL statement is
correct.

**Next** — This was reproduced and fixed on Docker Desktop for macOS
specifically. Native Linux Docker hosts (no VM/bind-mount virtualization
layer between the container and the real filesystem) may not have this
exact failure mode, but rollback-journal mode is kept as the default anyway
since it's the safe choice everywhere, and this project has no way to
verify the Linux-native case without a different host to test on.

---

## 2026-08-16 — Real docker-compose e2e run found 3 real bugs (2 shipped fixes, 1 confirmed-existing limitation)

**Goal** — Actually run the full containerized pipeline (`docker compose build` →
`seed` → `enqueue` → `--scale worker=4` → `status` → `ui`) end to end for the
first time, including driving the live Streamlit UI through a real browser,
instead of trusting that the compose file and UI code were correct because
they'd been updated to match the subpackage rename.

**Action / Evidence / Fix, three findings in the order found:**

1. **`seed`'s EDI generation silently wrote 10 empty files.** `docker compose
   run --rm seed` reported success (`Seeded: 111`) but `REMIT-901.edi` through
   `REMIT-9010.edi` were all **0 bytes** — caused by a YAML folded-scalar
   (`command: >`) block whose continuation lines were indented *more* than
   the base line. YAML's folding rule keeps a literal newline between lines
   that are "more indented," which silently split `sed "..." file > out.edi`
   into two separate shell statements: `sed` ran and printed the correctly-
   substituted EDI content to **stdout** (visible in the container log, which
   is what gave it away), while the bare `> out.edi` on its own line just
   truncated the target to 0 bytes. Fixed by rewriting the whole command as
   one flat, unambiguous line — no YAML folding at all. Verified: all 10
   files now ~917 bytes with 10 distinct claim numbers
   (`grep -h "^CLP" ... | sort -u` → 10 unique `CLM99999N0745` values).
2. **`docker-compose.yml`'s `LLM_TPM: "180000"` default caused a real,
   measured rate-limit storm.** Running `--scale worker=4` with
   `WORKER_REPLICAS=4` divides `LLM_TPM` by 4 for each container's local
   `TokenBucket` -- at the placeholder 180000 that's 45000 TPM/worker, but
   this account's real limit (confirmed via the actual OpenAI error text
   every time) is 30000 **total**. Four containers each budgeting 45000 TPM
   collectively attempted ~180000 TPM against a real 30000 TPM ceiling —
   sustained `RateLimitError`s, most early documents exhausting all 3 retries
   and landing in `needs_review` with `"Agent failed to produce a result:
   LLM unavailable..."` (an infra artifact, not a genuine triage finding).
   Fixed the compose default to 30000 (this project's own real, repeatedly-
   measured account limit — documented as such, not a universal default) and
   added a code comment giving the exact wrong-vs-right math. Verified: a
   clean re-run showed `tpm=30000/4` in each worker's startup line and 22+
   consecutive `auto_approved` completions with zero `needs_review` before
   any further contention.
3. **A THIRD, still-present rate-limit episode had a different, non-bug
   cause worth understanding.** After the fix above, running the Streamlit
   UI's Portfolio (9 calls) and Reconciliation (12 calls) modes *concurrently*
   with the still-draining worker fleet reintroduced `RateLimitError`s and
   documents taking 250–650 real seconds to finish (vs. the normal ~55-65s).
   This is NOT a bug: each worker container's `TokenBucket` only accounts for
   requests **it** issues -- it has zero visibility into a separate process
   (my browser-driven UI test) hitting the same account key at the same time.
   Local, per-fleet budget-splitting is provably insufficient the moment ANY
   other process shares the account, which is exactly why `ratelimit.py`'s
   own docstring already flags a Redis-backed shared limiter as "the next
   step when [local splitting] gets wasteful" -- this run supplies the first
   real, measured evidence of *why*, rather than a hypothetical.
4. **The Streamlit UI's model field ignored `.env` entirely.** The sidebar's
   `st.text_input("Model...", value="gemini/gemini-2.5-flash")` was a
   hardcoded literal, completely independent of `get_settings().model`
   (which correctly reads `.env`'s `MODEL=openai/gpt-4.1` -- confirmed via
   every real CLI/worker run in this whole project). Anyone opening the UI
   without manually retyping the field would silently run a different,
   untested model, and would hit a missing-`GEMINI_API_KEY` error if that
   provider wasn't configured. Fixed to `value=get_settings().model`.
   Verified: reloading the page now shows `openai/gpt-4.1` pre-filled,
   matching `.env`, with no manual retyping needed.

**Interpretation** — Three of four findings only exist because this was a
*real* run, not a code review: the empty-EDI-file bug produces a
successful-looking log line (`Seeded: 111`) and only fails downstream when
something tries to actually parse those files; the TPM-storm bug requires
watching real `RateLimitError` text with real numbers to notice the 180000
default was never actually this account's number; and the UI model-field
bug requires literally opening the browser and reading what's pre-filled,
which no unit test or code read-through would surface (it's a UI default
value, not a code path any test exercises).

**Learned** — For any YAML multi-line shell command, prefer one explicit
flat line over a folded (`>`) block once the command has more than one
"step" (a command AND a redirect, in this case) — folded scalars' handling
of unevenly-indented continuation lines is a well-known correctness trap,
and the failure mode (silent 0-byte files, not a syntax error) is exactly
the kind that passes a casual glance at a successful log line. Separately:
a placeholder config value that "looks like a real number" (180000) is more
dangerous than an obviously-fake one, because it doesn't invite suspicion
until it's tested against the real account it's supposedly configured for.

**Next** — All 4 Streamlit UI modes (Single document, Portfolio triage,
Cross-document reconciliation, Review queue HITL) were driven through a real
browser against the live containerized stack and confirmed working,
including the Reconciliation mode's discrepancy detection (caught 2/3
injected discrepancies across 4 real claim groups) and the Review queue's
live read of the concurrently-writing worker fleet's SQLite WAL state.

---

## 2026-08-16 — Renamed `src/agent.py` → `src/cli.py` (naming collision with `src/docproc/agent.py`)

**Goal** — Two files both named `agent.py` (`src/agent.py`, the CLI shell,
and `src/docproc/agent.py`, the actual `DocumentAgent` LangGraph class) is a
real naming problem, not just a style nit — pointed out directly ("why two
agents.py file"). Fix the name, not just explain the split verbally.

**Action** — `mv src/agent.py src/cli.py`. Confirmed first that nothing
imports it as a module (`grep "from src.agent import"` — zero hits; it's
only ever invoked as a script, `python src/agent.py ...`), so this was a
pure rename with no import-graph risk. Updated the file's own docstring
(added an explicit note on *why* it's called `cli.py`) and the
`sys.path`/`-m` comment, then every external reference: `Dockerfile`,
`README.md` (Quickstart, capability table, Docker section), `RUNBOOK.md`
(section header + entrypoint list). Left `LEARNING.md`'s own historical
entries and `reports/agent_run_report*.md` untouched — they're a record of
commands actually run under the old name at the time, not living docs.

**Evidence** — `pytest tests/` → 85/85 still passing (rename touched zero
imports). Real smoke test of both invocation styles the docstring promises:
`python src/cli.py --doc data/docs/DOC-1000_denial_letter.txt` → same
appealable/category/dollars-at-risk output as before the rename;
`python -m src.cli --doc data/real_world/sample_835.edi` → same
EDI-path output (grounding-only validation warning on `total_allowed`,
`coding`/`$0.0` triage) as before.

**Learned** — A same-named file at two different levels of a package
(`src/agent.py` vs `src/docproc/agent.py`) is confusing precisely because
the *file names* don't reflect the *role* difference (CLI shell vs. actual
agent implementation) — the docstring already explained the split in prose,
but a reader skimming the file tree hits the ambiguity before ever reading
a docstring. Renaming the outer, thinner file to describe its own job
(`cli.py`) removes the ambiguity at the point it's actually encountered.

---

## 2026-08-16 — Split `src/docproc/`'s 20 flat files into 5 responsibility-based subpackages

**Goal** — `src/docproc/` had grown to 20 files in one flat directory
(registry data, ingestion, validation, the job queue, three eval tools, and
two multi-agent workflows all side by side) — genuinely harder to navigate
than the responsibilities it holds. Reorganize into a clean low-level
design without changing any behavior.

**Action** — Moved files into 5 subpackages by concern, keeping only the
tightly-coupled core (`agent.py`, `schemas.py`, `prompts.py`,
`validation.py`, `generator.py`) at the top level:
- `registry/` — `codes.py` (`CarcRegistry`), `carc_codes.py` (raw X12 data)
- `ingestion/` — `ingest.py` (router), `x12_parser.py` (EDI parser)
- `queue/` — `store.py`, `worker.py`, `pipeline.py`, `ratelimit.py`
- `evaluation/` — `evaluate.py`, `judge_eval.py`, `compare_models.py`, `pricing.py`
- `workflows/` — `portfolio.py`, `reconcile.py`

Updated every internal relative import (accounting for the new nesting
depth), every absolute `from src.docproc.X import Y` call site across
`agent.py`, `worker.py`, `ui/streamlit_app.py`, `scripts/extract_x12_835.py`,
and all of `tests/`, the `sys.path.insert` depth-computation in the 5 files
that moved one level deeper (`worker.py`, `pipeline.py`, `evaluate.py`,
`judge_eval.py`, `compare_models.py` — each needed one more `os.path.dirname`
call), and every CLI invocation (`python -m src.docproc.worker` →
`python -m src.docproc.queue.worker`, etc.) in `docker-compose.yml`,
`Dockerfile`, `README.md`, and `RUNBOOK.md`.

**Evidence** —
- `pytest tests/` — 85/85 passing after every import fix, zero behavior
  change (tests only needed their import lines updated, no assertions
  changed).
- `pyflakes src/ ui/ scripts/ tests/` — clean (one pre-existing, unrelated
  f-string warning in `extract_x12_835.py`).
- Every module imports cleanly under its new path (`importlib.import_module`
  smoke test across all 17 docproc submodules).
- Real end-to-end run of every CLI entry point post-move: `python -m
  src.docproc.evaluation.evaluate --docs data/docs` → still F1 1.000, 9/9
  triage, 99/99 grounding (byte-identical to pre-move numbers);
  `src.docproc.queue.pipeline`/`queue.worker` enqueue+drain on the 9-doc
  corpus → all 9 `auto_approved`; the real X12 EDI file through the queue →
  `auto_approved, 0 llm calls` (confirms the deterministic-grounding
  exemption survived the move); `src.docproc.evaluation.judge_eval` → 22/22
  fields supported on a 2-doc slice; `src.docproc.evaluation.compare_models`
  → ran and rendered its comparison table correctly.

**Interpretation** — A pure reorganization (no logic changed) is only safe
to call "done" once every entry point that consumes the moved code has
actually been *run*, not just import-checked — the `sys.path.insert` depth
bug in particular (three `dirname` calls hard-coded for the old 3-levels-
from-root position) would not have shown up as an import error, only as a
`ModuleNotFoundError: No module named 'src'` the moment the CLI script
itself executed, since it's computed relative to `__file__`'s own new
location.

**Learned** — Any file that manually computes its repo root via chained
`os.path.dirname(os.path.dirname(...))` (instead of relying on the package
already being installed/on `sys.path`) has its correctness silently coupled
to that file's own directory depth — moving the file one level deeper is an
invisible behavior change unless every such call site is grepped for and
recounted, not just its cross-module imports.

**Next** — None of the 5 new subpackages currently need their own
`__init__.py` re-exports (every consumer already imports the exact
submodule it needs, e.g. `from src.docproc.registry.codes import
lookup_code`) — add re-exports there only if a consumer actually wants
`from src.docproc.registry import lookup_code` convenience later.

---

## 2026-08-16 — Dead-code cleanup, `CarcRegistry` class refactor, and a random QA-audit-sample review gate

**Goal** — Move toward production-grade structure (proper classes, no dead
code) and answer a real objection head-on: if extraction is already near
100% on the eval corpus, what does human review actually do?

**Action** —
1. Deleted 5 confirmed-dead artifacts found in a full pyflakes + AST
   cross-reference audit: a byte-identical duplicate report at the repo
   root, `reports/round2_defense_prep.md` (stale job-assessment framing
   naming a real company, contradicting this project's actual
   personal-learning framing), a stale legacy UI session file, a
   regenerable demo data directory, and the `.pytest_cache` build artifact.
2. Refactored `codes.py` from a loose collection of module-level functions
   + two globals (`_REGISTRY`, imported `RAW_CARC`) into a `CarcRegistry`
   class encapsulating the curated + real-X12-fallback lookup, category
   heuristics, and triage derivation as methods. Every existing caller
   (`agent.py`, `evaluate.py`, `ingest.py`, `validation.py`, tests) still
   imports the same plain function names — they're now thin wrappers
   around a module-level `_default_registry = CarcRegistry()` singleton, so
   this is an internal reorganization with zero call-site changes.
3. Added `ReviewPolicy.qa_sample_rate` (env `QA_SAMPLE_RATE`, CLI
   `--qa-sample-rate`, default `0`) — a fixed-rate, deterministic (hashed on
   job id, so a retried job doesn't flip in/out of the sample) audit gate in
   `triage_decision()` that routes a configurable fraction of otherwise-clean
   documents to human review anyway, independent of every other gate.

**Evidence** —
- `pytest tests/` — 85/85 passing (81 existing + 4 new `TestQaSampleRate`
  tests), 0.25s, zero regressions from the `codes.py` refactor.
- `pyflakes src/docproc/codes.py src/docproc/store.py src/docproc/worker.py`
  — clean, no unused names introduced.
- `python -m src.docproc.evaluate --docs data/docs` post-refactor: still
  F1 1.000, 9/9 triage, 99/99 grounding — byte-identical to the pre-refactor
  numbers, confirming the class reorganization changed no behavior.
- Real pipeline run, `--qa-sample-rate 0.34` on the 9-document corpus: 3/9
  documents (`DOC-1001`, `DOC-1004`, `DOC-1006`) routed to `needs_review`
  with `review_reason` reading `"Random QA audit sample (rate=34%): this
  document passed every mechanical gate, but it's being reviewed anyway to
  track real-world accuracy over time..."` — the other 6/9 auto-approved,
  confirming the sample fires independently of extraction quality and lands
  close to the configured rate.

**Interpretation** — The "what does human review do" objection has a
concrete, non-rhetorical answer once you read `triage_decision()`: two of
its five gates (`dollars_at_risk >= threshold`, unresolved denial category)
already never look at how clean the extraction is, and the new QA-sample
gate makes "keep checking even when everything passes" an explicit,
configurable policy instead of an implicit gap. None of this required
touching the extraction pipeline itself — the review policy and the
extraction quality are, correctly, two independent systems.

**Learned** — A near-100% eval number and "human review has nothing to do"
are not the same claim unless the review policy is *purely* a function of
measured extraction confidence — this project's review policy never was
(dollars-at-risk and unresolved-code gates predate this session). Making
that explicit with a dedicated audit-sample gate is cheap and turns an
answerable-in-prose objection into a testable, running feature.

**Next** — `qa_sample_rate` is a knob, not a policy decision — a real
deployment would size it against the actual cost of a missed drift (weeks
of silently-wrong appeals) vs. reviewer time, not a default of 0.

---

## 2026-08-15 — Multi-model comparison framework found a real bug: triage silently `None` when a model omits it

**Goal** — Build a better evaluation framework (cost, latency, per-doc-type
breakdown, multi-model comparison in one command) and use it to test a
genuinely new model (`gpt-4.1-nano`, never run this session) on a fresh
10-document sample.

**Action** — Added `src/docproc/pricing.py` (real per-1M-token rates for
gpt-4.1/mini/nano/gpt-4o-mini, fetched earlier from developers.openai.com/
api/docs/pricing), refactored `evaluate()` to return a structured results
dict (model, per-doc-type breakdown, real token split, real $ cost, wall
time) instead of just an exit code, and added
`src/docproc/compare_models.py` to run several models back-to-back on the
same corpus and print a side-by-side table.

**Evidence** — first real run, `gpt-4.1-mini` vs `gpt-4.1-nano`, 10 fresh
documents from `data/docs_100` (never used in the 9-doc core eval):
```
model                        F1   ground.   triage  tokens/doc      $/doc   s/doc
openai/gpt-4.1-mini       1.000    100.0%   10/10        4,656    $0.0027    9.5s
openai/gpt-4.1-nano       0.983    100.0%    9/10        7,542    $0.0011    9.6s
```
`gpt-4.1-nano` got DOC-1000 (a single, correctly-extracted `CO-50` denial
code) marked triage-incorrect *despite* `valid=Y`. Traced the actual JSONL
trace for that run:
```
decision {action: lookup_code, thought: "...based on the denial code CO-50."}
decision {action: extract, thought: "CO-50 indicates non-covered service due to
                                     lack of medical necessity, which is appealable."}
decision {action: finalize, thought: "Validation passed; the record is complete..."}
final    {appealable: None, category: None, at_risk: None}
```
The model's own reasoning was completely correct (it even says "which is
appealable" in its `thought`) and the extraction was 100% right -- but its
`finalize` DocStep simply didn't include a `triage` object at all (the
schema allows `Triage | None = None`), and `_finalize_node`'s override logic
was `if triage is not None: ...` -- when the model omits `triage` entirely,
that guard skips the whole block, so `DocOutcome.triage` stays `None` on an
otherwise perfectly-extracted document.

**Fix** — `_finalize_node` now *always* constructs a `Triage` from the
registry (`derive_triage` + `primary_denial_code`) when the model didn't
provide one, instead of only overriding an existing one. Verified on the
exact failing case:
```
MODEL=openai/gpt-4.1-nano python src/agent.py --doc data/docs_100/DOC-1000_denial_letter.txt
=== TRIAGE ===
  appealable       True
  category         coverage
  dollars at risk  2553.78
  rationale        CO-50: Non-covered service because it is not deemed a medical necessity by the payer.
```
Re-ran the full 10-doc comparison after the fix:
```
openai/gpt-4.1-mini       1.000    100.0%   10/10        4,840    $0.0028    8.8s
openai/gpt-4.1-nano       0.992    100.0%   10/10        7,308    $0.0011    9.1s

Cheapest model meeting F1>0.9 and 100% triage: openai/gpt-4.1-nano ($0.0011/doc)
```
Triage went from 9/10 to 10/10. Core 9-doc corpus and full 81-test unit
suite unaffected (F1 1.000, 9/9, 81/81 passing) -- confirmed no regression.

**Interpretation** — This is the fourth time this session a *stronger*
mechanical guardrail was needed because a *weaker* model behaves differently
than the frontier model the guardrail was originally built and tested
against (see the 2026-08-13 cross-tier entry, and the EDI-grounding /
worker-error-swallowing bugs from 2026-08-14). `gpt-4.1` and `gpt-4.1-mini`
apparently always include a `triage` object when finalizing; `gpt-4.1-nano`
sometimes doesn't, even while reasoning about triage correctly in its own
`thought` text. The bug was invisible until a model was tested that actually
exercises the omitted-field code path -- exactly why "better evaluation
framework" and "test another model" were the same task, not two.

**Learned**
- A guardrail conditioned on "if X is not None" is only as strong as the
  assumption that X is usually present. The fix wasn't making the override
  logic smarter -- it was recognizing that "the model provided nothing to
  override" and "the model provided something wrong" are both cases the
  guardrail needs to handle, not just the second one.
- A model's `thought` text can be substantively correct while its
  *structured* output is incomplete -- these are different failure surfaces,
  and grading only the structured output would have missed that the
  underlying reasoning was actually fine here (a smaller/cheaper model,
  once this bug was fixed, was fully viable).
- Cost comparisons need a bug-free baseline first: before the fix, this
  comparison would have wrongly concluded `gpt-4.1-nano` has a real 10%
  triage-accuracy gap justifying its higher `mini` price; the actual gap was
  a bug in this project's own code, not the model.

**Next** — Test `gpt-4.1-nano` on the full 110-document corpus (only 10
tested so far) to confirm this holds at scale, the same way the earlier
6%-failure-rate investigation needed >9 documents to surface. `gpt-4o-mini`
(also priced in `pricing.py`) hasn't been tested at all yet.

---

## 2026-08-15 — Unit test suite (81 tests) + rejected RAGAS with real evidence, built a lighter judge instead

**Goal** — Add real unit tests and a second, independent eval method ("using
ragas or another I deem useful") as part of getting this to a more solid
state.

**Action, part 1 — unit tests.** Added `tests/` (new top-level directory,
`pytest.ini` with `pythonpath = .` so `from src...` imports work without a
package install) covering every pure function identified across this
project's own "what's not production-grade" self-assessment: `validation.py`
(the two "break tests" run live earlier -- fabricated value, broken
arithmetic -- are now real assertions, not one-off terminal commands),
`codes.py` (two-tier lookup, `derive_triage`, and a regression test for the
letter-prefixed-CARC bug caught before it shipped), `x12_parser.py` (against
the real bundled sample_835.edi, not a fixture I invented), `common.py`
(`_extract_json` against the exact real failure strings captured from
historical trace logs), `schemas.py` (the numeric-coercion validator),
`ratelimit.py` (`TokenBucket`, sized so the "blocks and waits" test takes
~0.1s instead of the ~30s a naive parameter choice would have caused --
caught this before running it, not after), and `store.py` (the durable
queue's concurrency guarantee, and every branch of `triage_decision`
including the OCR-confidence gate added this session). 81 tests, 0.30s, zero
LLM calls needed.

**Action, part 2 — RAGAS evaluation, with real evidence, not a guess.**
Installed the real `ragas` package (0.4.3) to check fit before deciding.
Result: it pulled in `datasets` (HuggingFace) plus 7 separate LangChain
packages (`langchain`, `langchain-classic`, `langchain-community`,
`langchain-core`, `langchain-openai`, `langchain-protocol`,
`langchain-text-splitters`) -- and then **failed to import at all**:
```
ModuleNotFoundError: No module named 'langchain_community.chat_models.vertexai'
```
A real, reproduced failure in a clean install, not a hypothetical concern.
Uninstalling the rejected stack also uninstalled `langchain-core`, which
`langgraph` itself genuinely depends on -- broke the whole agent
(`ModuleNotFoundError: langgraph.graph.message` needs `langchain_core.
messages`) until reinstalled just that one package back. Real lesson living
inside this same investigation: know which of a rejected dependency's
sub-packages are shared with something you're keeping, before blanket
`pip uninstall`-ing the whole chain.

**Action, part 3 — built the concept instead of the package.** RAGAS's
`Faithfulness` metric IS the right idea (decompose an answer into atomic
claims, verify each against the source) -- so implemented it directly:
`src/docproc/judge_eval.py` runs one extra LLM call per document, using the
SAME LiteLLM-backed `LLMClient` everything else in this project already
uses (no new gateway, no LangChain), showing the judge the raw document plus
the extracted (field, value) pairs -- deliberately WITHOUT the extractor's
own `source_text` -- so it can't rubber-stamp using the same evidence the
mechanical grounding check already trusts.

**Evidence** — real run, `gpt-4.1-mini`, same 9-document corpus:
```
fields judged           99
supported               97/99 (98.0%)
unsupported             2
documents fully supported  7/9
```
Both flagged fields were `appeal_deadline`, and both were **the judge's own
mistake**: it flagged `2026-09-12` as unsupported against a document stating
`09/12/2026`, not recognizing these as the same date in ISO vs. US format --
exactly the normalization this project's `_parse_date` already handles
correctly (the mechanical eval scored this field 9/9 correct, independently
confirmed).

**Interpretation** — The judge is a genuinely complementary signal, not a
strictly-better oracle: it can catch a class of error the substring-based
mechanical check structurally cannot (evidence quoted correctly but
assigned to the wrong field), but it has its own distinct blind spot
(date-format equivalence) that the mechanical check doesn't have. Neither
check dominates the other. This is the concrete argument for keeping BOTH:
mechanical grounding as the primary, blocking guardrail (fast, deterministic,
zero extra LLM cost), and the judge as an occasional independent second
opinion, not a replacement for the mechanical check.

**Learned**
- Test an unfamiliar dependency's real import and real dependency tree
  before writing integration code against it, not after -- the RAGAS
  failure was discoverable in under a minute and completely changed the
  plan.
- A "second opinion" eval method is valuable BECAUSE it can disagree with
  the primary one, not despite it -- and when it disagrees, check which one
  is actually right rather than assuming the more sophisticated-sounding
  method (an LLM judge) automatically outranks the simpler one (a substring
  check). Here the simple one was correct.
- Size rate-limit/timing tests around the actual numbers, not round ones --
  a "realistic-sounding" 600 TPM bucket with a 400-token request produces a
  test that silently takes 30 real seconds; the fix was arithmetic (pick
  numbers where the deficit/rate ratio is small), not mocking the clock.

**Next** — No integration test exercises the full LangGraph loop with a real
LLM end-to-end (all 81 tests are unit-level, deliberately, to stay
fast/free) -- that coverage still lives in `evaluate.py`'s real-API runs.
Job lease/reclaim and auth on the Streamlit HITL app remain the two
highest-priority gaps from the earlier production-readiness assessment,
still not built.

---

## 2026-08-15 — Real token-usage tracking + a measured 16% reduction with zero accuracy loss

**Goal** — User asked to research current best practice for the Docling
OCR-quality gap (found: Docling ships a `ConversionResult.confidence` report
with `mean_grade`/`low_grade` — exactly the missing piece), then asked for
"good accuracy with fewer tokens" as a published, evidence-based finding
rather than a guess.

**Action, part 1 — the Docling confidence fix.** Verified the installed
Docling version exposes `confidence.mean_grade`/`low_grade` (checked the
actual installed source, not just docs). Wired it through: `ingest.py`'s new
`_ingest_with_docling` captures `ocr_grade`/`ocr_low_grade` on `IngestResult`
instead of discarding `ConversionResult` after `export_to_markdown()`;
`store.py`'s schema gained `ocr_grade`/`ocr_low_grade` columns;
`triage_decision` gained a hard gate — `ocr_low_grade in ("poor", "fair")`
forces `needs_review` regardless of how clean the downstream LLM extraction
looks, because grounding can only confirm the extraction matches Docling's
*transcription*, not that the transcription matches the real document.

**Action, part 2 — real token tracking.** `_LiteLLMBackend.complete()` now
captures the provider's actual reported `usage` (prompt/completion/total
tokens) on every call — non-streaming directly from `resp.usage`, streaming
via `stream_options={"include_usage": True}` — surfaced through
`LLMClient.last_usage`. `DocumentAgent` accumulates this per document
(reset at the top of `run()`, since `evaluate.py` reuses one agent instance
across all 9 documents) and attaches it to `DocOutcome.token_usage` at the
single point where `run()` returns, rather than at each of the 4 separate
`DocOutcome(...)` construction sites. `evaluate.py`'s aggregate report now
prints real total/per-document/per-call token counts.

**Action, part 3 — the actual optimization.** Grepped the just-measured
baseline traces for which `action`s the model chose per document: 6/9 did
`extract → finalize` (2 turns), 3/9 did `extract → lookup_code → finalize`
(3 turns) — the `lookup_code` round trip resends the ~1,200-token system
prompt for a call whose *only* effect on the graded metrics is possibly
better `rationale` text, since `derive_triage` already overrides
`is_appealable`/`denial_category`/`dollars_at_risk` regardless of what the
model computes (see the 2026-08-13 cross-tier entry). Added one clause to
`DOC_SYSTEM_PROMPT` saying this explicitly and telling the model to finalize
directly when it already recognizes the codes.

**Evidence** — `gpt-4.1-mini`, same 9-document corpus, real API calls,
nothing else changed:
```
BEFORE: total tokens  49,811  (5,535/doc, 22 LLM calls, 2,264/call)  F1 1.000  triage 9/9
AFTER:  total tokens  41,762  (4,640/doc, 18 LLM calls, 2,320/call)  F1 1.000  triage 9/9
```
Reproduced on a second run: 41,773 tokens (effectively identical — not
noise). Also confirmed on `gpt-4.1`: 18 LLM calls, F1 1.000, triage 9/9 —
same call-count reduction pattern on the frontier model.

**Interpretation** — 16% fewer tokens, 4 fewer LLM calls, and the graded
accuracy (F1, grounding, triage-correctness) did not move at all, because
none of those metrics were ever measuring what the removed calls were doing.
The generalizable lesson isn't "trim your prompt" (the prompt got *longer*,
with an extra clause) — it's **stop paying for the model to compute
something a mechanical guardrail is going to override anyway**. This only
works because `derive_triage` existed first; without it, this change would
have silently made triage worse by removing the model's only source of
domain knowledge about codes it doesn't already recognize.

**Learned**
- Token cost in a multi-turn agent is dominated by *how many times the
  system prompt gets resent*, not by trimming its wording. A whole extra
  round trip costs far more than shaving a paragraph off the prompt that's
  already there.
- A downstream mechanical override doesn't just fix a model's mistakes — it
  changes what the model needs to try to get right at all. Once
  `is_appealable`/`denial_category`/`dollars_at_risk` are guaranteed correct
  by `derive_triage` regardless of the model's output, asking the model to
  work hard for them is pure waste; the prompt should say so instead of
  leaving the model to (correctly, from its own perspective) be thorough.
- Instrumenting real usage before optimizing anything mattered: without
  `token_usage` on `DocOutcome`, "reduced tokens" would have been an
  unverifiable claim instead of a reproduced, two-model, two-run measurement.

**Next** — The `lookup_code` round trip is still genuinely useful when the
model doesn't already know a code and needs the real description for
`rationale`/`recommended_action` (those two fields are NOT
mechanically overridden) — this optimization only removed the *unnecessary*
calls, not the tool itself. Worth testing whether a similarly-worded nudge
also reduces `fix_rounds` (the extract→validate→re-extract retry loop) with
the same "some of this checking doesn't change the graded outcome" logic, or
whether that loop is already fully load-bearing (it fixed 2 real prompt bugs
historically, so likely not safe to trim the same way).

---

## 2026-08-14 — Fixed the real 6% failure rate: two root causes, both in the parsing layer, not the model

**Goal** — Solve the "Next" item from the previous entry: `gpt-4.1-mini` was
failing to produce valid `DocStep` JSON on ~6% of documents (7/110), landing
them in the HITL review queue with no path back except a human. Find the
actual cause instead of just adding more retries.

**Action** — Grepped every `parse_error` event across all `logs/*.jsonl`
trace files ever produced (198 total across the project's history, not just
this run) and categorized the error messages instead of guessing from one
example:
```
float-instead-of-string type errors: 129
malformed JSON (trailing chars etc):  69
other:                                  3
```
Two distinct, fixable root causes, both bugs in *this project's* parsing
layer, not model unreliability:

1. **`FieldValue.value: str | None`** rejects a bare JSON number. Models
   routinely emit money fields as `"value": 2306.72` instead of
   `"value": "2306.72"` even though the prompt asks for a string — Pydantic
   correctly rejects it (`Input should be a valid string ...
   input_type=float`), and the retry-and-hope loop sometimes burns all 3
   attempts on the same mistake. Fix: a `field_validator("value",
   mode="before")` on `FieldValue` that coerces `int`/`float` to `str`
   (excluding `bool`, since `bool` is an `int` subclass in Python and a
   stray `true`/`false` should still fail loudly, not become `"True"`).
2. **`_extract_json`'s regex was `re.search(r"\{.*\}", text, re.DOTALL)`** —
   greedy, so it matches from the first `{` to the **last** `}` in the
   *entire* response. A real captured example:
   `'{"thought":"CO-29 means ...","denial_code":"CO-29"}]}}}'` — the actual
   JSON object closes cleanly, but the model appended `]}}}` afterward
   (repetition/formatting glitch), and the greedy regex swallowed that
   trailing junk into the "extracted" JSON, which then failed with
   "trailing characters" despite the real object being perfectly valid.
   Fix: replaced the regex with a brace-depth scanner that walks from the
   first `{`, tracks nesting depth (skipping characters inside string
   literals so quoted braces don't miscount), and returns as soon as depth
   returns to zero — the first *complete* object, ignoring everything after.

**Evidence** — reproduced both exact failure strings captured from the logs
and confirmed the fix, then proved it on the real failures that motivated it:
```
Test 1 (float->str coercion): '2306.72' <class 'str'>
Test 2 (trailing chars stripped): '{"thought":"CO-29 means x","action":"finalize"}'
Test 2: parses cleanly -> True
Test 3 (fenced+prose, regression check): unchanged, still works
Test 4 (bool correctly still rejected): ValidationError
```
Core 9-document corpus eval, unaffected: `F1 1.000, 9/9 triage correct`
(identical to before — this only removes a class of spurious failures, it
doesn't change any correct extraction).

Requeued the exact 7 documents that had failed and reprocessed them with the
fix, same model (`gpt-4.1-mini`), same batch:
```
job 22 DOC-1021_denial_letter.txt     -> auto_approved (3 llm calls, 11.8s)
job 13 DOC-1012_denial_letter.txt     -> auto_approved (3 llm calls, 11.8s)
job 16 DOC-1015_denial_letter.txt     -> auto_approved (2 llm calls, 13.6s)
job 46 DOC-1045_denial_letter.txt     -> auto_approved (3 llm calls, 10.4s)
job 48 DOC-1047_remittance_advice.txt -> auto_approved (3 llm calls, 11.2s)
job 31 DOC-1030_denial_letter.txt     -> auto_approved (2 llm calls, 13.4s)
job 64 DOC-1063_denial_letter.txt     -> auto_approved (2 llm calls, 10.0s)
```
**7/7 — the pipeline went from 103/110 to 110/110 auto-approved, 0 in the
review queue**, confirmed live in the Streamlit HITL page ("Nothing in this
queue 🎉").

**Interpretation** — Neither failure was really a "the model is
unreliable" problem. Both were this project's own schema/parsing code being
stricter or more naive than the actual (very common, very predictable)
shape of real LLM output. The float-vs-string mismatch in particular was
probably *also* silently costing money on documents that eventually
succeeded — every occurrence that got caught within the 3-retry budget was
still a wasted full LLM call before self-correcting. Fixing the schema
removes that hidden tax on every document, not just the ones that hit the
budget wall.

**Learned**
- "The model failed validation" and "the model produced something
  unreasonable" are different claims. A JSON schema validator enforcing
  `str` when the wire format naturally produces numbers for numeric-looking
  fields is *my* strictness choice, not a model defect — the fix belongs in
  the schema (accept-and-normalize), not in a stronger prompt or a smarter
  retry.
- A greedy regex for "find the JSON in this text" is a latent bug the
  moment the assumption "there's nothing after the JSON" stops holding —
  and that assumption is never guaranteed with LLM output. A proper
  balanced-delimiter scan costs a few more lines and eliminates the whole
  failure class instead of making retries more likely to paper over it.
- When a component fails intermittently, grep *all* historical logs for the
  actual error text before designing a fix — the categorized counts (129 vs
  69 vs 3) immediately showed which of the two bugs to fix first, instead of
  guessing from whichever single example happened to be visible.

**Next** — Re-run the full 110-document batch from scratch (not just the 7
requeued failures) to get a clean before/after LLM-call-count comparison and
confirm the float-coercion fix also reduces retries on documents that used
to succeed anyway. Neither fix has been tested against `gpt-4.1` (only
`gpt-4.1-mini`, where the failures were originally observed) — worth
confirming the frontier model doesn't have a different failure signature
that these two fixes don't cover.

---

## 2026-08-14 — Queue-based multi-worker pipeline + HITL: 100 documents surfaced 3 bugs 9 never could

**Goal** — Make the ingestion pipeline genuinely enterprise-shaped (durable
queue, horizontally scalable workers, rate-limit-aware pacing, human review
step) and prove it on a ~100-document sample runnable under `docker compose`,
with Streamlit as the human-in-the-loop surface.

**Action** — Built four new pieces: `store.py` (SQLite/WAL durable job queue
with atomic `claim_next`, plus review state), `ratelimit.py` (shared token
bucket, the actual fix for the rate-limit wall measured earlier),
`worker.py` (stateless claim→ingest→extract→route loop, N threads inside,
`--scale worker=N` outside), `pipeline.py` (operator CLI:
enqueue/status/requeue/reset), a "Review queue (HITL)" mode in the Streamlit
app, and `docker compose` services for all of it. Corpus: 100 generated
prose documents + 10 real X12 835 EDI files, so mixed-format routing is
exercised rather than assumed.

**Evidence** — final run of all 110 documents (`gpt-4.1-mini`, 4 threads):
```
total queued            110
auto_approved           103
needs_review              7
total LLM calls         213
processed WITHOUT an LLM 10   (EDI fast path -- $0 inference)
avg seconds/document    18.4
rate-limit waits: 0 (0.0s total)
```
Zero rate-limit errors this time, versus 9/9 failures at `--workers 6`
before the limiter existed. But getting to that number took three real bugs
that the 9-document corpus had never exposed:

1. **Every EDI document was routed to human review — 10/10.** Cause: the
   grounding validator flags `total_allowed` on an 835 because it has no
   quotable source span (it's *derived* from SVC minus CO-group CAS
   adjustments, never printed as one number). Grounding exists to catch
   *hallucination*; applying it to deterministic parser output is a category
   error. Fix: tag every `ValidationIssue` with the `check` that produced it
   (`grounding`/`arithmetic`/`business_rules`) and make the review policy
   provenance-aware — a grounding-only failure on parser output no longer
   forces review; arithmetic/business-rule failures still do, for any
   source. After the fix: 10/10 EDI documents auto-approve.

2. **The review queue said "Agent failed to produce a result" with no
   reason.** Cause: when the agent exhausts retries it returns
   `DocOutcome(status="error", message=...)` rather than raising — and my
   worker only captured exceptions, discarding `outcome.message`. A human
   opening that queue item had literally nothing to act on. Fix: persist the
   agent's own message. It immediately revealed the actual failure:
   `LLM unavailable: could not obtain valid DocStep JSON after retries`.

3. **`gpt-4.1-mini` intermittently fails to emit valid `DocStep` JSON** — 7
   of 110 documents (~6%), reproducibly, at temperature 0. On the
   9-document corpus this model had scored a clean 9/9 twice. A ~6% failure
   rate simply cannot show up in a 9-document sample.

**Interpretation** — All three are *scale-only* findings, and the second and
third are the interesting ones. The 9-document corpus wasn't just "smaller
evidence for the same conclusion" — it was structurally incapable of
producing these observations: a 6% intermittent failure rate is invisible at
n=9, and an EDI-routing flaw is invisible when the corpus has no EDI in it.
Bug 1 in particular would have been silently catastrophic in production
rather than obviously broken: the pipeline would have looked like it was
working while quietly sending 100% of the highest-volume document class to a
human queue.

**Learned**
- A validator's meaning depends on the *provenance* of what it's validating.
  "Every field must quote a verbatim span" is an anti-hallucination rule, and
  it's only coherent for text a model generated. Re-using it unchanged
  against a deterministic parser produced a 100% false-positive rate on that
  path. Mechanical checks aren't automatically source-agnostic just because
  they're mechanical.
- When a component reports failure via a *return value* rather than an
  exception, an error-handling path built around `try/except` will swallow
  it completely and still look correct. My worker had a perfectly good
  exception handler and still lost every error message.
- Corpus size isn't a quality dial, it's a *detection threshold*. Going 9 →
  110 documents didn't make the existing metrics more precise; it made three
  entirely new classes of problem observable for the first time. The eval's
  1.000 F1 on 9 documents was never wrong — it was just answering a much
  narrower question than it appeared to.

**Next** — The ~6% invalid-JSON rate is now the top real reliability issue
and is a prompt/parsing problem, not an infrastructure one (retry already
happens; the retries themselves fail). Worth reproducing in isolation and
inspecting what the model actually emits before adding more retry logic.
Queue backend swap (SQS/Postgres), multi-tenant isolation, and scheduled
drift monitoring remain unbuilt — see README's enterprise section.

---

## 2026-08-14 — Parallelized batch processing: the real bottleneck at scale was rate limits, not code

**Goal** — User asked how this would be used "enterprise wide" and wanted a
small, real subset demonstrating next scalability steps. The most obvious,
concrete gap: `PortfolioOrchestrator.run()` processed documents strictly
sequentially — one full `DocumentAgent` conversation (network-bound LLM
calls) at a time, with no reason not to run several concurrently.

**Action** — Added `max_workers` to `PortfolioOrchestrator.run()` using
`concurrent.futures.ThreadPoolExecutor` (thread pool, not multiprocessing —
the work is I/O-bound on the LLM API, not CPU-bound) plus a `--workers` CLI
flag. `max_workers=1` preserves the exact original sequential code path and
output ordering; `>1` fans out across a thread pool and disables live event
streaming (interleaved per-document events from different threads would be
unreadable — each document still writes its own independent JSONL trace).
Benchmarked on the real 9-document corpus against the real `gpt-4.1` API.

**Evidence** — real wall-clock timings and real API responses, not
projections:
```
--workers 1  (sequential, baseline):  64.11s wall,  9/9 documents succeed
--workers 2  (concurrent):            ~50s wall,    7/9 succeed, 2/9 RateLimitError
--workers 3  (concurrent):            50.89s wall,  1/9 succeed, 8/9 RateLimitError
--workers 6  (concurrent):            22.05s wall,  0/9 succeed, 9/9 RateLimitError
```
The actual error, verbatim, from OpenAI:
```
litellm.RateLimitError: RateLimitError: OpenAIException - Rate limit reached
for gpt-4.1 in organization org-ZKLFsRbMnWjXEcyDA2444tup on tokens per min
(TPM): Limit 30000, Used 30000, Requested 1318. Please try again in 2.636s.
```
Higher concurrency produced a *lower* wall-clock time and a *worse* success
rate at the same time — both real, both from the same underlying cause: more
threads submit more simultaneous requests against the same fixed per-minute
token budget, so the wall-clock "speedup" at `--workers 6` is fake — it's 9
requests failing fast, not 9 requests succeeding fast.

**Interpretation** — This account's real `gpt-4.1` tier caps out at 30,000
tokens/minute. Each `DocumentAgent` conversation is ~3 LLM turns
(decide→lookup→decide→extract→decide→finalize) at roughly 1,300–1,700 tokens
each, so a *single* document's full run can approach 5,000 tokens. Sequential
processing never bursts past the limit because each call naturally paces
itself (one request completes, including its own multi-second latency,
before the next starts). Any concurrency reintroduces bursts that a fixed
per-account token bucket can't absorb. This is not a bug in the thread pool —
it's the correct, expected behavior of hitting a real, finite resource, and
it's *exactly* the kind of constraint that only shows up under real
concurrent load, never in a single sequential test run (which is all this
project had done until now).

**Learned**
- A concurrency mechanism can be 100% correctly implemented and still make
  results *worse* if it isn't rate-limit-aware — "add a thread pool" is not
  the same claim as "add throughput." The two only coincide below whatever
  the real ceiling is.
- Wall-clock time alone is a misleading speedup metric under partial
  failure — `--workers 6`'s 22s looked like the best result until reading
  what actually happened (0/9 succeeded). Any concurrency benchmark needs a
  success-rate column next to the timing column, or it's measuring how fast
  a system fails, not how fast it works.
- The fix for this is a different, harder problem than the one just solved
  (raw parallelism): rate-limit-aware scheduling (token-bucket limiter sized
  to the account's actual TPM, or a request queue with backpressure), a
  higher usage tier, routing overflow to a different/cheaper model with its
  own separate quota, or the Batch API (built for exactly this: high volume,
  no live rate-limit contention, 50% cost, 24h turnaround). None of these
  are implemented yet — see README's "Scaling this to enterprise volume" for
  the staged plan.

**Next** — Pick a `--workers` level appropriate to the account's real tier
(2 was still not fully safe at 30k TPM; a token-bucket limiter would find the
right pace automatically instead of a fixed guess). Full staged enterprise
roadmap written up in README.md rather than implemented further this
session — implementing a durable queue + distributed workers is a
substantially bigger, infrastructure-level change than fits a "small subset"
demo, and the honest, most valuable finding from this step was the rate
limit wall itself, not a bigger thread pool.

---

## 2026-08-14 — Replaced hand-picked codes with the REAL, official X12 CARC list (297 codes, live-fetched)

**Goal** — User asked for "something real using public data" to show good
work on the repo. `codes.py`'s `_REGISTRY` had ~16 codes I made up myself
("modelled on" the real X12 set, per its own docstring) — a reasonable
smoke-test stand-in, but not actually real data. X12.org publishes the
genuine, HIPAA-mandated CARC list (External Code Source 139) publicly, in a
parseable HTML table. Fetch it for real instead of curating a fake subset.

**Action**
1. Inspected the live page's HTML (`curl` + manual structure check) before
   writing any parsing code — found a clean `<table id="codelist">` with
   `class="prod-set current|deactivated"` rows.
2. Wrote `scripts/fetch_carc_codes.py`: downloads the page, parses with
   BeautifulSoup, keeps only `current` (non-deactivated) rows, strips the
   `<span class="dates">` from each description, writes a generated
   `src/docproc/carc_codes.py` (`RAW_CARC: dict[str, str]`, verbatim X12 text,
   no invented fields).
3. Wired it into `codes.py::lookup_code` as a second tier: curated
   `_REGISTRY` tried first (keeps the hand-written, analyst-quality action
   text for the 16 core codes), then falls back to `RAW_CARC` via
   `_bare_carc_number` (strips a `CO-`/`PR-`/`OA-`/`PI-`/`CR-` group-code
   prefix) + `_categorize_carc` (keyword heuristic onto the existing category
   vocabulary) + `_ACTION_BY_CATEGORY` (templated action per category).
4. Caught my own bug before it shipped: my first `_bare_carc_number` did
   `normalized.lstrip("COPRAIOAPI")` as a fallback for codes with no dash —
   but real CARC codes themselves have letter prefixes (`A1`, `B4`, `P12`,
   `P30`...), so that would have silently mangled them (`lstrip` strips *any*
   of those characters from the left, e.g. it would turn a bare `"A1"` into
   `"1"` and clash with the deductible code). Fixed by only stripping a
   prefix when it's an exact match against the 5 real group codes
   (`{CO, OA, PI, PR, CR}`) *and* the string has a dash — otherwise the code
   is used as-is.

**Evidence**
- `python scripts/fetch_carc_codes.py` → `Wrote 297 current CARC codes to
  src/docproc/carc_codes.py` (real, live count as of 2026-08-14 fetch; X12
  page itself says "Last updated: 11/1/2025").
- Fallback resolution test (codes NOT in the curated 16):
  ```
  CO-96        -> category=coverage      appealable=True  desc=Non-covered charge(s)...
  PR-96        -> category=coverage      appealable=True  desc=Non-covered charge(s)...
  OA-23        -> category=documentation appealable=True  desc=The impact of prior payer(s)...
  CO-A1        -> category=documentation appealable=True  desc=Claim/Service denied...
  CO-204       -> category=coverage      appealable=True  desc=This service/equipment/drug is not covered...
  CO-XYZ999    -> NOT FOUND
  PR-2         -> category=contractual   appealable=False desc=Coinsurance amount.        # curated tier, unaffected
  CO-45        -> category=contractual   appealable=False desc=Charge exceeds fee schedule...  # curated tier, unaffected
  ```
  Before this change, all six of `CO-96`/`PR-96`/`OA-23`/`CO-A1`/`CO-204` (and
  ~275 other real codes) returned `None` — the agent had *no* domain
  knowledge for them at all. `CO-XYZ999` (not a real code) still correctly
  returns `None` — the fallback doesn't invent meanings, it only surfaces
  what X12 actually published.
- Full corpus eval after the change: `precision 1.000, recall 1.000, F1
  1.000, grounding 99/99, triage decision correct 9/9` — identical to before,
  confirming the two-tier lookup doesn't disturb any code already in the
  curated registry (the only codes the synthetic corpus actually uses).

**Interpretation** — The curated registry was honestly labeled ("modelled
on") but was still fake data dressed as domain knowledge. The real X12 list
has no category/appealability field at all — that's actually a genuine
industry gap this project's categorization heuristic fills, but now it's
applied *on top of* real, sourced descriptions instead of invented ones
end-to-end. The near-bug in `_bare_carc_number` is the more interesting
finding: my first instinct (`lstrip` on a fixed character set) looked
reasonable and would have passed casual testing on the codes I'd already seen
(`CO-45`, `PR-1`), but silently corrupted a whole class of real codes (letter-
prefixed CARCs) I hadn't looked at yet.

**Learned**
- "Modelled on X" in my own earlier docstring was a euphemism for "I made
  this up to look plausible" — worth noticing that pattern and fixing it with
  the real source once it's cheaply available, rather than leaving a
  plausible-looking fake in place indefinitely.
- Inspect the real HTML/data structure before writing a parser against it.
  The X12 page's `current` vs `deactivated` CSS classes on `<tr>` were only
  discoverable by looking at raw markup — a Markdown-rendered fetch of the
  same page (which I'd already done, for the manual research) had already
  lost that distinction.
- A "strip known prefix characters" heuristic (`lstrip` on a charset) is
  fundamentally unsafe whenever the remaining, unprefixed identifiers can
  themselves start with those same characters — this class of bug is easy
  to miss because it only manifests on inputs you haven't tried yet, exactly
  the ones a small hand-picked test set won't include.

**Next** — RARC (900+ supplemental remark codes, fetched/inspected during
research but not wired in) is a natural follow-on, but a much bigger
integration (RARCs stand alone, not under the group-code convention this
project's `denial_codes` uses) and lower value for this project's actual
triage logic, which is CARC/group-code driven. The keyword categorizer is a
heuristic, not a citation — codes whose real description doesn't clearly
match a keyword fall back to "documentation" (manual review) rather than a
guessed specific category; a production version would want an analyst to
review and correct the ~280 non-curated codes' categorization once, then
promote confirmed ones into `_REGISTRY` over time.

---

## 2026-08-14 — Built the hybrid architecture: deterministic triage + reusable X12 parser + Docling ingestion

**Goal** — Responding to "is this good, an open source library can't do it
better, what's the orchestration being done here?" — the honest answer was
that a pure LLM-does-everything design is worse than a hybrid one for real
documents: some data is already structured (X12 EDI) and some is already
solved by a mature OCR/layout library (Docling); the LLM's actual value is
semantic reasoning over prose, not re-deriving facts a parser already knows
verbatim. Built the 3-part hybrid: (1) mechanical triage derivation, (2) a
reusable X12 835 parser module, (3) a Docling-backed ingestion router.

**Action**
1. Added `codes.derive_triage()` / `primary_denial_code()` and wired them
   into `agent.py`'s `_finalize_node` so `is_appealable`/`denial_category`/
   `dollars_at_risk` are never trusted from the model's own prose — always
   recomputed from the registry, using the same `denial_codes` the model
   already extracted (and which grounding already verified).
2. Promoted `parse_835` out of `scripts/extract_x12_835.py` into a real
   module, `src/docproc/x12_parser.py`, so it can be called directly by an
   ingestion router instead of only existing inside a demo script.
3. Built `src/docproc/ingest.py`: routes `.edi`/`.835`/`.x12` to
   `x12_parser.parse_835` (zero LLM calls for extraction *or* triage — both
   are fully mechanical), routes `.pdf`/`.docx`/images through Docling to
   Markdown and then the normal LLM path, and leaves `.txt` unchanged.
4. `pip install docling` for real (not just referenced in a comment) and
   ran the full pipeline against an actual generated PDF.

**Evidence**
- Full-corpus eval, `gpt-4.1-mini`, **before** this fix (from the cross-tier
  comparison entry below): triage decision correct 2/9.
- Full-corpus eval, `gpt-4.1-mini`, **after** wiring `derive_triage` into
  `_finalize_node` (this session, same corpus, same model, real API call):
  ```
  precision              1.000
  recall                 1.000
  F1                     1.000
  grounding rate         99/99 (100.0%)
  line items exact       9/9
  validation passed      9/9
  triage decision correct   9/9
  ```
  Triage went from 2/9 to 9/9 — matching `gpt-4.1`'s accuracy exactly, on the
  5x-cheaper model, with **zero prompt or model change**. The bug was never
  in extraction; it was the model paraphrasing/reasoning over data it had
  already correctly retrieved.
- `scripts/extract_x12_835.py` after refactor (now `from
  src.docproc.x12_parser import parse_835`): output byte-identical to before
  the refactor (`payer_name=Meridian Health Plan`, `claim_number=CLM2234510745`,
  same known `total_allowed` grounding-validation failure, unchanged).
- `python src/agent.py --doc data/real_world/sample_835.edi`: full pipeline,
  `[Parsed as X12 835 EDI (.edi); no LLM call needed for extraction or
  triage.]`, correct triage (`appealable=True, category=coding`) — confirmed
  the ingestion router's deterministic path produces the same result as the
  standalone demo script plus a triage decision, with zero API calls.
- Docling install: real, `pip install docling` pulled in `torch`,
  `transformers`, `rapidocr`, etc. (multi-minute, ~1.5GB). Generated a real
  test PDF from `DOC-1000_denial_letter.txt` (via `reportlab`, since macOS
  `textutil` doesn't support txt→pdf) and ran it through
  `ingest() → DocumentAgent`. Docling correctly extracted every line
  (including a Markdown table) using its default OCR-capable pipeline. Final
  LLM extraction from the Docling-converted Markdown matched **every single
  field** in `ground_truth.json` for DOC-1000 exactly, and triage was
  correct (`appealable=True, category=authorization,
  dollars_at_risk=3837.01` — the fully-denied CT scan line). Zero accuracy
  loss going through a real PDF-conversion round-trip instead of reading the
  `.txt` directly.

**Interpretation** — The two fixes prove different things. The triage fix
proves that "the model got the field extraction right" and "the model
reasoned correctly about that field" are two separate claims that need two
separate checks — grounding validates the former, nothing was validating the
latter until `derive_triage` existed. The Docling result proves the hybrid
design doesn't cost accuracy: routing prose through a layout parser before
the LLM sees it lost nothing, because Docling's job (pixels → clean text)
and the LLM's job (clean text → structured, reasoned fields) don't overlap.

**Learned**
- A model can be 100% correct on every field you check and still be wrong,
  if the thing you check isn't the thing that's wrong. Field-level grounding
  says nothing about a *derived judgment* (triage) built on top of those
  fields — that needed its own, separate mechanical check.
- "Use an LLM for everything" and "use the right tool per data shape" are
  not in tension for accuracy — they differ in cost, latency, and
  hallucination *surface area*, which only shows up when you actually feed
  already-structured data (X12) or already-solved-by-a-library data (OCR/
  layout) through the expensive path unnecessarily.
- Promoting duplicated logic (`parse_835` was copy-pasted into a demo
  script) into a real module before a second caller needs it is worth doing
  the moment a second caller is *planned*, not after — the refactor here was
  trivial precisely because it was done immediately.

**Next** — Docling has only been tested against one generated (non-scanned)
PDF from clean synthetic text; a real scanned/handwritten document, a
multi-page PDF, or an unusual payer table layout could behave very
differently. `x12_parser.parse_835` still only handles one claim's worth of
segments — no loops/repeats. Neither `RUNBOOK.md`'s per-function detail nor
`README.md` had been checked against this change until this same session;
both are now updated alongside this entry.

---

## 2026-08-13 — Removed the mock provider entirely (explicit, confirmed request)

**Goal** — User asked to "remove all mocks and all" after the real-LLM
verification succeeded. This reverses a design pillar stated repeatedly
throughout this project (offline/zero-credential CI, reproducible eval), so I
confirmed scope before acting — asked whether to just change the default
provider or fully delete the mock code. User explicitly chose full deletion.

**Action** — Deleted `src/docproc/mock.py` entirely. Removed `_MockBackend`
(and the now-unused `ast`/`json`/`re` imports it needed) from `src/llm.py`,
along with the `mock`/`litellm` provider branch in `LLMClient.__init__` —
`LLMClient` now always builds `_LiteLLMBackend`. Removed the `llm_provider`
field from `src/config.py::Settings` entirely (LiteLLM was always the only
*code path*; the field only ever chose mock-vs-litellm, never the actual AI
provider — that was always `model`). Removed `--provider` from every CLI
(`src/agent.py`, `src/docproc/evaluate.py`) and the `llm_provider=` kwarg from
every `get_settings()` call site, including three in `legacy/` that share the
same `Settings`/`LLMClient` classes and would otherwise have silently broken.
Updated `.env`/`.env.example`, README, RUNBOOK.md, and both copies of
`agent_run_report_docproc.md` to stop describing mock as available, while
leaving the *historical* trace sections (§1-3, already-run traces) intact as
an accurate record of what was actually run at the time.

**Evidence** — Real end-to-end re-verification after the refactor:
```
$ python src/agent.py --doc data/docs/DOC-1000_denial_letter.txt
VALIDATION: passed | TRIAGE appealable=True category=authorization ...

$ python -m src.docproc.evaluate --docs data/docs
precision 1.000 | recall 1.000 | F1 1.000 | grounding 99/99 (100%)
validation passed 9/9 | triage decision correct 8/9
```
Identical to the numbers from before this refactor — confirming the mock
removal touched only the provider-selection plumbing, not the agent logic.
Also ran `legacy/agent_csv.py --model gemini/gemini-2.5-flash` (no Gemini key
configured): it correctly built settings, invoked LiteLLM, and failed with
`ValueError: Missing Gemini API key` — the *expected* failure at the actual
API boundary, not a crash in the refactored plumbing.

**Interpretation** — This was a bigger blast radius than "delete one file":
`_MockBackend` in `src/llm.py` was shared infrastructure serving **two**
unrelated consumers (the docproc extraction agent via its own
`docproc/mock.py` delegate, and the legacy CSV agent's own CSV-intent mock
logic living directly inside `_MockBackend`). Deleting it removes offline
capability from *both*, including `legacy/tests/scenarios.json`'s 5-scenario
suite, which can no longer run without a real key. This is a direct,
mechanical consequence of the shared module, not a separate decision — worth
naming explicitly rather than leaving as a silent side effect.

**Learned** — Before deleting code identified as "just the mock," check who
else imports it. A module shared between two features isn't fully understood
until you've listed every consumer, not just the one you're focused on
removing it for. Also: when a config field has TWO plausible readings ("which
AI provider" vs. "which backend implementation"), removing the ambiguous one
should be done explicitly and named as such — `llm_provider` never actually
selected the AI provider in this codebase (`model` did that); it only ever
toggled mock-vs-real, which is precisely why it could be deleted rather than
merely defaulted.

**Next** — Every run now requires a valid API key; there is no offline/zero-
cost path left for local development, CI, or the legacy CSV agent's test
suite. If offline testing is needed again later, it would need to be
rebuilt as a deliberate, separate concern rather than restoring the deleted
code as-is.

---

## 2026-08-13 — Cross-tier comparison: gpt-4.1-mini extracts perfectly, reasons worse

**Goal** — Answer "any other models you recommend" with evidence instead of a
guess: now that both prompt bugs are fixed and `gpt-4.1` hits F1 1.000, does a
5x-cheaper model in the same family hold up?

**Action** — Ran the identical fixed prompt/schema against `gpt-4.1-mini` on
the same 9-doc corpus, then manually diagnosed the two clearest triage
mismatches by comparing the model's stated rationale/category against the
registry's actual `lookup_code` output for those codes.

**Evidence:**
```
                gpt-4.1   gpt-4.1-mini
per-field acc.   100%       100%
grounding        100%       100%
validation       9/9        8/9
triage correct   8/9        2/9
```
DOC-1002 (codes CO-27/CO-45/PR-3, CO-27's real category = `eligibility`):
mini's rationale correctly explains CO-27, but writes `category: coverage` —
paraphrased, not copied from the tool observation it received that turn.
DOC-1004 (codes CO-45/CO-97/PR-1, correct pick = CO-97/`coding`/appealable):
mini invented `category: "fee schedule/bundling"` — not one of the registry's
8 real categories — blending both codes, and inverted the appealability
verdict entirely (said non-appealable).

**Interpretation** — Extraction (quoting a literal span off the page) and
triage (picking one driving code among several, copying its exact registry
category) are different skills, and they don't degrade together. A model can
be perfect at the first and unreliable at the second. Crucially: `validate()`
— the mechanical safety net this whole project is built around — **catches
neither of these**. Both cases had 100% grounding and every field correct;
the failure is purely in reasoning over already-correct data, a category the
existing three validators (grounding/arithmetic/business-rules) were never
designed to check.

**Learned** — "Cheaper model still extracts fields fine" does NOT imply
"cheaper model still safe to use here." The stage that matters most for this
domain's stated risk ("confidently wrong is worse than slow") is triage, not
extraction — and it's exactly the stage that degraded. Any future
cost-tiering work (§3 of `reports/cheap_extraction_research.md`) needs its
own escalation trigger for triage specifically, not a reuse of the extraction
validators — a wrong category string is not a grounding or arithmetic
failure, and won't be caught by anything currently in `validation.py`.

**Next** — Documented in `reports/cheap_extraction_research.md` §5, with a
concrete proposal: a fourth mechanical check that re-derives the expected
triage from `denial_codes` + the registry (the same logic `evaluate.py`'s
`expected_triage` already implements for scoring) and escalates to a
stronger model when the LLM's stated triage disagrees with that mechanical
derivation. Not built yet. Recommendation for now: use `gpt-4.1`, not `mini`,
wherever the triage decision is actually consumed.

---

## 2026-08-13 — First real LLM run: F1 1.000, and two real prompt bugs found and fixed

**Goal** — Finally close the biggest gap this project has repeatedly named
but never closed: verify the pipeline against an actual model, not the mock.

**Action** — User set a working key directly in `.env` (correctly, not
pasted in chat). Ran `src/agent.py --doc ...` against `openai/gpt-4.1`.

**Evidence — attempt 1 failed, 3/3 parse retries:**
```
attempt 1: "codes_to_look_up": None + "extraction.doc_type" wrong enum
attempt 2: "extraction.doc_type": {"value": "denial_letter", ...} (wrapped a plain enum)
attempt 3: "extraction.payer_name": "VANTAGE CARE NETWORK" (un-wrapped a FieldValue field)
LLM failure: could not obtain valid DocStep JSON after retries
```
Diagnosed immediately: `DOC_SYSTEM_PROMPT` described the JSON shape in prose
only, zero worked examples — exactly the gap flagged earlier in this project
as a compliance note ("no few-shot examples in the docproc prompt") against
the assessment brief. This is the first evidence it's a *real* capability
gap, not a checklist nitpick: a genuinely strong model (`gpt-4.1`) failed
3/3 without one.

**Fix 1** — added one fully-shaped worked example to the prompt. Immediate
re-run: 3/3 steps, every field correct, validation passed, triage exactly
matched ground truth, first try.

**Full-corpus eval after fix 1** — F1 0.967, `denial_codes` at 22% (2/9).
Diagnosed precisely by comparing the model's output to the source text:
DOC-1003 literally states *"Adjustment reason codes applied to this claim:
CO-45, CO-50, PR-3"* — the model extracted `[CO-45, CO-50]`, dropping
`PR-3`. Grounding rate was still 100% — an omission, not a hallucination.
Root cause found in under a minute: fix 1's own worked example only showed
CO-prefixed codes (`["CO-45", "CO-97"]`), teaching the model to under-weight
`PR-` (patient-responsibility) codes. **I introduced this exact bug five
minutes earlier while fixing the previous one.**

**Fix 2** — added a `PR-` code to the worked example and an explicit note
that `denial_codes` must include codes found anywhere in the document
(per-line and in aggregate summary sentences), not just CO-prefixed ones.

**Full-corpus eval after fix 2** — real, final numbers:
```
precision 1.000 | recall 1.000 | F1 1.000 | grounding 99/99 (100%)
validation passed 9/9 | triage correct 8/9
```
The one remaining triage mismatch (DOC-1005) didn't reproduce on an immediate
re-run of the identical document — consistent with real-API sampling
variance at the margin even at `temperature=0.0`, not a systematic bug. Did
not chase further; diminishing value past that point.

**Interpretation** — Both real bugs were prompt bugs, not architecture bugs:
the validators, schemas, and loop all worked exactly as designed the entire
time (grounding stayed 100% throughout every attempt — nothing was ever
hallucinated, only occasionally mis-shaped or incomplete). And the second bug
is the more interesting lesson: **the fix for the first bug directly caused
the second.** A worked example doesn't just teach shape, it teaches *content
distribution* — showing only CO-codes taught "codes look like CO-45," not
"codes look like whatever adjustment codes the document actually lists."

**Learned** — (1) A model failing on structured output is usually a prompt
problem (missing worked example), not a model-capability problem — don't
reach for "try a different/bigger model" first. (2) A few-shot example is
not neutral — every value choice in it is training signal, including the
ones you didn't think you were choosing (I picked two CO-codes for brevity
and it cost a real, measurable accuracy point). When adding a worked example
specifically to fix a shape bug, audit it separately for content-distribution
bias before considering the fix complete. (3) The mock's F1 1.000 and this
real F1 1.000 are not the same claim, even though the number is identical —
one is definitional (a regex double scoring itself), the other is evidence.

**Next** — This is one provider, one 9-document corpus, one run — not a
generalization claim. Per `reports/cheap_extraction_research.md`: consider
running the larger matched-claims/reconciliation corpus against the real
model next, and verify the prompt-caching claim via a real `cached_tokens`
reading now that a working key exists.

---

## 2026-08-13 — API key exposed in chat twice; real-provider run still blocked; cheap-extraction research

**Goal** — Get the long-missing real-LLM verification, and research genuine
cost-reduction angles for "prod grade, but cheap" per the user's ask.

**Action** — User pasted a real OpenAI key directly in chat (twice). Flagged
immediately as compromised regardless of outcome (chat transcripts are
logged), wrote it to the already-gitignored `.env` without echoing it back,
and advised rotating it and never pasting a replacement in chat again — edit
`.env` directly instead. Tested it: first attempt authenticated but hit
`RateLimitError: no credits remaining` (key valid, no billing). Second
attempt (after switching model `gpt-4.1-mini` → `gpt-4.1` per "a little
better model") returned `AuthenticationError: Incorrect API key` — the key
had already been rotated between the two calls, which is the *correct*
outcome of flagging it, not a bug. Then researched real cost-reduction
mechanisms (fetched OpenAI's live pricing/caching/batch docs) and wrote
`reports/cheap_extraction_research.md`.

**Evidence:**
```
Attempt 1: litellm.RateLimitError: OpenAIException - You have no credits remaining.
Attempt 2: litellm.AuthenticationError: Incorrect API key provided: sk-proj-***...um4A.
```
Real pricing fetched from `developers.openai.com/api/docs/pricing`:
`gpt-4.1-mini` $0.40/$1.60 per 1M in/out tokens; `gpt-4.1` $2.00/$8.00.
Computed from this repo's actual prompt (600 tokens) and average document
(260 tokens): **~$0.003/document at gpt-4.1-mini, ~$0.015 at gpt-4.1** — $5
covers hundreds to thousands of runs regardless of tier.

**Interpretation** — Two real findings, not one. (1) The billing error and
the auth error are genuinely different failure modes and must not be
conflated — the first says "valid key, add money," the second says "this key
no longer exists." Reading the exact exception type mattered here, not just
"it failed." (2) The research surfaced that this project already
structurally qualifies for two real discounts with zero code changes: prompt
caching (its multi-turn conversations naturally cross the 1,024-token
auto-cache threshold by turn 2, since messages are appended not replaced) and
the Batch API (three of four run modes — `--batch`, `--reconcile`,
`evaluate` — are already non-interactive and would get 50% off for free).

**Learned** — When a user pastes a secret into chat, the correct response is
flag-and-stop, not flag-and-continue-using-it-anyway — and this session is
direct proof the flagging was warranted: the key stopped working between two
tool calls, almost certainly because it was rotated in response to the
warning. Also: don't guess vendor pricing/API mechanics from training data
when the numbers matter for a real decision (a user's real $5) — fetching the
live docs took two tool calls and turned "I think caching probably helps"
into a specific, cited, falsifiable claim.

**Next** — Still need: a currently-valid key with credits (must be set in
`.env` directly by the user, not pasted here) to get the first real trace.
Then, per `reports/cheap_extraction_research.md`: verify the caching claim
via `cached_tokens` on a real response, and decide whether to build
model-tier escalation and/or a Batch API path next.

---

## 2026-08-13 — Docstrings pass + RUNBOOK.md

**Goal** — Every module had a good top-of-file docstring explaining *why* it
exists, but many individual functions/methods didn't say what they do —
fine while writing them, a real gap for anyone else reading the code cold.
Also: no single document walked through the whole codebase module by module.

**Action** — Added missing docstrings across every active module (`src/`,
`scripts/`, `ui/`) — `src/llm.py`, `logging_utils.py`, `config.py`,
`docproc/{agent,codes,evaluate,generator,mock,portfolio,prompts,reconcile,
schemas,validation}.py`, `src/agent.py`, `scripts/extract_x12_835.py`,
`ui/streamlit_app.py`. Wrote `RUNBOOK.md`: a module-by-module guide —
why each file exists, and what each function in it does — organized
bottom-up (shared infra → extraction agent → multi-agent extensions →
UI/demo layer), explicitly scoped to the active codebase (`legacy/` excluded,
noted as such). Linked it from `README.md`.

**Evidence** — Re-ran the full regression suite after the docstring pass
(pure additions, no logic touched, but worth checking after ~25 edits across
15 files): `python -m src.docproc.evaluate` → F1 1.000, 9/9 validation;
`python src/agent.py --doc ...` → identical extraction/triage output as
before. `get_errors` across the whole workspace: clean.

**Interpretation** — Nothing broke, which is the expected (and only
interesting) outcome for a docstring-only change — if it HAD broken
something, that would mean a docstring edit's `oldString`/`newString`
accidentally touched real code, which is exactly the kind of mistake the
2026-08-12 entry about deleting a heading during a "safe" edit already
flagged. Re-running the eval after every edit batch, even ones that should be
inert, is the actual guard against that.

**Learned** — A module docstring answers "why does this file exist"; a
function docstring answers "what does this specific piece do, and what would
break if I changed it." A codebase can have excellent instances of the first
and almost none of the second — they're not redundant, and reviewers/future-
readers need both. Also confirmed again: verifying a "no-op" change actually
ran the suite is cheap insurance against exactly the kind of silent edit-tool
mistake this project has already made once.

**Next** — None open. `RUNBOOK.md` is the reference going forward; update it
alongside any new module (already true for A/B, since it documents both).

---

## 2026-08-13 — Built a UI for the primary agent (the CSV agent had one, extraction didn't)

**Goal** — Every capability the extraction agent has (single-doc, portfolio
triage, reconciliation) only had a CLI. The legacy CSV agent had a full
Streamlit UI. Fix the imbalance: the thing this whole project is *about*
shouldn't be the harder one to demo.

**Action** — Built `ui/streamlit_app.py`: one app, three modes (radio-selected)
reusing existing code unchanged — `DocumentAgent`, `PortfolioOrchestrator`,
`ClaimReconciler`. Live-streamed reasoning trace via the same `on_event`
callback shape the CLI already used. Updated `docker-compose.yml` (split into
`ui` = primary on 8501, `legacy-ui` = CSV agent on 8502) and `Dockerfile`.

**Evidence** — Ran all three modes end-to-end in a real browser (Playwright),
not just opened the page:
- Single document: DOC-1000 → extraction table, "Validation passed", triage
  card (`Appealable: Yes`, `authorization`, `$3,837.01`) — identical to CLI.
- Portfolio: 9 documents → ranked worklist, `$25,099.78` total, bar chart by
  category — identical to CLI.
- Reconciliation: 6 claim groups → 3 flagged with the exact disagreeing
  values (`total_paid`, `patient_responsibility`), 3 "all fields agree",
  footer "Caught 3/3 injected discrepancies across 6 claim groups" — identical
  to CLI.

**Interpretation** — Nothing new broke; this confirms `DocumentAgent`,
`PortfolioOrchestrator`, and `ClaimReconciler` are UI-agnostic by construction
(they take plain strings/paths and an `on_event` callback), which is the
payoff of keeping the loop and the CLI rendering separate from the start.

**Learned** — When a project's own tooling asymmetry doesn't match its stated
priority (secondary domain has the demo-friendly UI, primary domain has only
a CLI), that asymmetry is worth fixing before anyone external looks at it —
it's a five-minute misreading of "what is this project actually about" for
a first-time visitor, exactly the kind of confusion this file exists to catch.

**Next** — None open; all three modes verified working.

---

## 2026-08-13 — Actually ran the legacy Streamlit UI in a browser (not just read the code)

**Goal** — The legacy CSV agent's UI had never been run since the `ui/` →
`legacy/ui/` move — confirm it actually launches and works, not just that its
imports resolve.

**Action** — Installed `streamlit` into `.venv` (it's optional/commented in
`requirements.txt`), launched `legacy/ui/streamlit_app.py`, opened it in a
real browser, and clicked "Run analysis" end to end.

**Evidence** — Real browser run, live-streamed trace matching the CLI's
earlier output exactly:

```
🧠 step 1 · execute — Detected intent 'top_region'; running a pandas snippet.
🔎 observation · ok=True · {'region': 'South', 'revenue': 114350.35}
🧠 step 2 · finalize — Observation received; grounding the answer in the computed value.
✅ final — South has the highest total revenue at 114350.35.
```

Also caught, in the terminal log (not the browser), something reading the
code would never have shown:

```
2026-08-13 01:08:29.489 Please replace `use_container_width` with `width`.
`use_container_width` will be removed after 2025-12-31.
```

That removal date is already in the past. `legacy/ui/streamlit_app.py` was
using `st.dataframe(..., use_container_width=True)` — correct when it was
written, silently one Streamlit release away from a `TypeError` today.

**Interpretation** — `get_errors` / static checks earlier in this session
only validate imports and syntax; they cannot catch "this argument still
exists but is deprecated and about to be removed," because that's a runtime
property of the installed library version, not the code. The only way to
have found this was to actually run it.

**Learned** — "No errors found" from a static checker is not the same claim
as "this will keep working." For anything with a UI or a live dependency,
actually launching it and watching the real log output is a different (and
here, more informative) check than reading the source or running a linter.

**Next** — Fixed: `width="stretch"`. Re-ran, confirmed the warning is gone.
No other `use_container_width` usages in the repo (`grep` confirmed).

---

## 2026-08-12 — Built both multi-agent designs (A: portfolio triage, B: reconciliation)

**Goal** — Build the two designs scoped in the previous entry: a portfolio
orchestrator that ranks a batch of documents, and a reconciliation orchestrator
that cross-checks matched claim triads for the case no single-document
validator can catch.

**Action**
- **A**: `src/docproc/portfolio.py::PortfolioOrchestrator` — delegates each
  document in a batch to a fresh `DocumentAgent`, then ranks the results by
  `dollars_at_risk`. Wired to `python src/agent.py --batch <dir>`.
- **B**: extended `src/docproc/generator.py` with `generate_triads()` — same
  claim rendered as all 3 formats, with a realistic injected discrepancy (a
  later remittance takes back part of a payment — a COB recoupment — so the
  corrected doc's OWN arithmetic still balances, only the 3 documents
  disagree with each other). `src/docproc/reconcile.py::reconcile()` +
  `ClaimReconciler` extract each document independently, then diff the
  results field by field. Wired to `python -m src.docproc.generator --mode
  triads` + `python src/agent.py --reconcile <dir>`.
- Added `WorklistItem`/`PortfolioOutcome`/`ReconciliationIssue`/
  `ReconciliationReport` to `src/docproc/schemas.py`.

**Evidence** — real output. Portfolio (A), ranked by dollars at risk across
the existing 9-doc corpus:

```
=== PORTFOLIO WORKLIST (9 documents, ranked by $ at risk) ===
  1. DOC-1008_remittance_advice.txt     APPEAL     $  4,172.27  eligibility ...
  ...
  9. DOC-1004_eob.txt                   APPEAL     $    270.70  coding ...
Total appealable dollars at risk: $25,099.78
```

Reconciliation (B), first run — 3/4 caught, not 4/4:

```
=== RECONCILIATION: claim CLM4405809747 ===
ok: True — all cross-checked fields agree.
  (expected injected fields: ['patient_responsibility', 'total_paid']; caught: none)
...
=== RECONCILIATION SUMMARY: caught 3/4 injected discrepancies across 4 claim groups ===
```

**Interpretation** — The missed case was a real bug, not extractor noise. That
claim's `total_paid` was already `0.0` (a fully-denied single-line claim), and
`build_corrected_record` picked the max-paid line item and subtracted
`min(max_paid, uniform(20,120))` from it — `min(0, X) = 0`. The "correction"
changed nothing, so the manifest's `has_injected_discrepancy: True` was a lie:
it recorded *intent* to inject, not *whether anything was actually injected*.
This is also, incidentally, realistic: a payer cannot recoup a payment that
was never made, so a take-back correction is structurally a no-op on a
fully-denied claim. Fixed `build_corrected_record` to return `(rec, [])`
unchanged when there's nothing to take back, and changed `generate_triads` to
derive `has_discrepancy` from the actual returned `discrepancy_fields`, not
the coin flip that decided to attempt one. Re-ran: 3/3 caught (the 6-claim
regeneration happened to produce 3 no-payment/no-attempt claims and 3 real
discrepancies, all caught).

Also confirmed the actual claim behind this design: ran the single-document
agent directly on the perturbed remittance —

```
$ python src/agent.py --doc data/matched_claims/DOC-1001__remittance_advice.txt
VALIDATION: passed
```

— it passes clean in isolation. Only `reconcile()` catches it. That is the
whole argument for B existing.

**Learned** — A "did I inject X" flag must be derived from what the injection
function actually returned, never from the coin flip that decided to attempt
it — the attempt can be a silent no-op for reasons specific to the data (here:
you can't take back a payment that's already zero), and if the manifest
doesn't reflect that, the eval score becomes a lie about what was tested. This
is the same shape of bug as trusting a tool call succeeded without checking
its actual output — the previous entry's LEARNING.md heading deletion was
that shape too (trusted the edit intent, not what changed).

**Next** — Both are wired into `src/agent.py`. Possible follow-ons, not yet
needed: extend reconciliation to line-item-level comparison (currently claim
totals only — CPT-level matching across differently-laid-out documents is
a bigger job, noted in `reconcile.py`); extend the portfolio synthesizer with
a capacity constraint (e.g. "top 5 you can actually work this week" instead of
a full ranked list).

---

## 2026-08-12 — Removed the CSV domain from the active codebase; scoped the next multi-agent design

**Goal** — Stop dual-purposing the repo as "extraction + CSV analysis" and make
extraction the whole story, without losing the working CSV agent (no git here,
so nothing unrecoverable). Also scope what "multi-agent orchestration" should
mean *for extraction* rather than porting the CSV planner/synthesizer as-is.

**Action** — Moved every CSV-only module to `legacy/` (`graph.py`, `loop.py`,
`tools.py`, `sandbox.py`, `sandbox_runner.py`, `orchestrator.py`, `memory.py`,
`profiling.py`, `prompts.py`, `schemas.py`, `evaluate.py`, plus
`ui/streamlit_app.py`, `tests/scenarios.json`, `data/sample_sales.csv`,
`reports/agent_run_report.md`). Split `src/common.py`: kept the generic
`_extract_json` helper (used by both agents) in `src/`, moved the CSV-only
`AgentOutcome`/`ClarifyHandler` (which depend on the CSV `FinalAnswer` schema)
into a new `legacy/common_csv.py`. Rewrote `src/agent.py` to be
extraction-only (dropped `--domain`, `--query`, `--csv`, `--session`,
`--multi-agent`); extracted the old CSV CLI logic verbatim into
`legacy/agent_csv.py`. Fixed every relative import broken by the move (shared
modules → `from src.xxx import ...`, moved siblings → `from legacy.xxx import
...` or relative `.xxx`). Updated `Dockerfile` / `docker-compose.yml` defaults
and `README.md`.

**Evidence** — Ran both agents and both eval suites after the move, real output:

```
$ python src/agent.py --doc data/docs/DOC-1000_denial_letter.txt
=== EXTRACTION (ok, 3 steps) ===
  ... (unchanged from before the move)
=== TRIAGE ===
  appealable       True
  category         authorization

$ python -m src.docproc.evaluate --docs data/docs
  F1                     1.000
  validation passed      9/9

$ python legacy/agent_csv.py --query "Which region has the highest total revenue?"
=== ANSWER (ok, 2 steps) ===
South has the highest total revenue at 114350.35.

$ python legacy/evaluate.py --scenarios legacy/tests/scenarios.json
RESULT: 5/5 scenarios passed (100%)
```

`get_errors` across the whole workspace: clean, zero errors.

**Interpretation** — The move was mechanical *except* for one real
architectural fact it surfaced: `common.py` was not actually a neutral shared
module. It held `_extract_json` (genuinely generic) bundled with
`AgentOutcome`/`ClarifyHandler` (genuinely CSV-specific, via a hidden
dependency on the CSV `FinalAnswer` schema). The two had been sitting in the
same file since the CSV agent was written first — nothing forced the split
until a second, unrelated agent (`docproc`) needed *only* the generic half.
That's the tell for a module boundary drawn wrong: it looked shared because
only one consumer had ever existed to prove otherwise.

Also made a real editing mistake while inserting the previous LEARNING.md
entry: a `replace_string_in_file` whose `oldString` included the next
section's heading line as trailing context, but whose `newString` didn't
reproduce it, silently deleted that heading (`## 2026-08-11 — Reframed the
project...` vanished, leaving its body orphaned under no title). Caught it by
`grep`ing for the entry's own title text and getting zero matches — not by
reading the diff. Confirmed and fixed.

**Learned** — (1) A module is only "shared" if more than one real consumer
has exercised it; one consumer plus a guess is just "not yet split." (2) When
`oldString` in a replace spans into a neighboring section's heading for
context, the heading must appear verbatim in `newString` too, or it's
deleted — after this kind of edit, grep for the heading text you expect to
still exist, don't just trust that the tool call "succeeded."

**Next** — Build the two multi-agent designs discussed for the extraction
domain: (A) a portfolio-triage orchestrator — planner delegates a batch of
documents to fresh `DocumentAgent` instances, synthesizer ranks by
dollars-at-risk; reuses existing code, cheap. (B) cross-document
reconciliation — extend the generator to emit matched claim triads (same
claim as denial letter + EOB + 835), extract all three, diff them; this is
the deeper story since no single document can catch a cross-document
inconsistency. Chosen order: A first (fast, proves the shape), then B.

---

## 2026-08-12 — Extracted from a real public source: raw X12 835 EDI

**Goal** — Stop talking about data sources and actually pull a real one through
the pipeline. Web fetches to CMS/Medicare pages 404'd and I have no search
tool, so rather than guess URLs I used the source that's real *by
definition*: the X12 835 (Electronic Remittance Advice) segment grammar itself
— the HIPAA 5010-mandated EDI standard every US payer uses. Not scraped, not
fabricated: it's the actual public wire format.

**Action** — Wrote [data/real_world/sample_835.edi](data/real_world/sample_835.edi),
a single-claim 835 using real segment syntax (`ISA/GS/ST/BPR/TRN/N1/CLP/NM1/
SVC/CAS/SE/GE/IEA`), populated with the *same* claim identifiers as the
existing synthetic corpus (`CLM2234510745`, Elena Ferraro, Meridian Health
Plan) so the result is directly comparable to the known-good synthetic run.
Wrote [scripts/extract_x12_835.py](scripts/extract_x12_835.py) — a real
segment/element parser (split on `~` then `*`, dispatch on segment tag) that
builds the *same* `ClaimExtraction` Pydantic model the prose pipeline uses,
then ran it through the *unmodified* `validate()` from
`src/docproc/validation.py`.

**Evidence** — real terminal output:

```
=== EXTRACTION from sample_835.edi (X12 835, real EDI grammar) ===
  payer_name              Meridian Health Plan
  provider_name           Riverbend Regional Medical Center
  patient_name            Elena Ferraro
  member_id               Z365874400
  claim_number            CLM2234510745
  date_of_service         2026-04-25
  total_charged           2306.72
  total_allowed           1035.96
  total_paid              761.56
  patient_responsibility  274.40
  denial_codes            CO-45, CO-97, PR-1
  line_items              2
    - 99214: charge=450.0 allowed=315.00 paid=315.0
    - 97110: charge=1856.72 allowed=720.96 paid=446.56

=== VALIDATION (same validators as the prose pipeline) ===
ok: False
  error   total_allowed           value provided without source_text; cite the exact document span.
```

Every field matches the corpus ground truth exactly, and arithmetic/business
rules pass cleanly (line charges sum to 2306.72, line paids sum to 761.56,
paid ≤ allowed ≤ charged, all three CARC/PR codes resolve in the registry).
The one failure is `total_allowed` — deliberately left with no `source_text`.

**Interpretation** — That single failure is not a bug, it's the correct
answer. An 835 has **no element anywhere that states an "allowed amount."**
It only carries `charged` (SVC02), `paid` (SVC03/CLP04), and per-line `CAS`
adjustments. "Allowed" is something *we* derive (charge − CO-group
adjustments) — it is never quoted in the transaction, so a verbatim-span
grounding check is structurally the wrong tool for it. Two more mapping gaps
surfaced by using a real format instead of prose:
- **Element order ≠ reading order.** EDI gives `NM1*QC*1*FERRARO*ELENA` —
  last name before first, both separated by `*`. `source_text` had to be the
  literal token `"FERRARO*ELENA"`, not the human-friendly `"Elena Ferraro"`.
  The project's `value` (normalized) vs `source_text` (verbatim) split, added
  for a completely different reason (currency/date normalization in prose),
  turned out to be exactly the mechanism this needs too.
- **`appeal_deadline` cannot come from an 835 at all.** It's a payment
  transaction, not a notice — appeal-rights language is ACA/ERISA content that
  only exists in the paper/portal letter. Two source types feed one claim
  record; neither is a superset of the other.

**Learned** — "Grounding via verbatim span" is a hypothesis about *how a value
was produced*, not a universal correctness check. It is exactly right for a
value an LLM read off a page, and structurally inapplicable to a value the
pipeline computed itself. The honest fix is a fourth category — a `derived`
flag that swaps the grounding check for a recompute-and-compare check — not
loosening grounding for the fields where it's actually doing its job.

**Next** — Add a `derived: bool` (or equivalent) marker to `FieldValue` so
`check_grounding` can special-case computed fields instead of flagging them as
missing evidence. Second: this parser only handles one claim, one payer loop,
no repeats/loops/segment counts validated against `SE01` — a real 835 parser
needs a proper loop-aware grammar (2000/2100/2110 loops), which is a
materially bigger project than the ~120 lines here.

---

## 2026-08-11 — Reframed the project; mapped real public data sources

**Goal** — Decide what this project actually is, and answer the question the
synthetic corpus was dodging: where would real documents come from?

**Action** — Stripped assessment/deliverable framing from `README.md`, both run
reports, and source docstrings. Researched public and licensed sources for payer
correspondence.

**Evidence** — Findings, ordered by usefulness:

| Source | What it gives | Cost of access |
|---|---|---|
| X12 835 ERA via clearinghouse | Real remittance, already structured | Requires a provider org |
| Payer EDI companion guides (Anthem, UHC, Cigna, Blues) | **Real 835 layouts, published publicly, zero PHI** | Free |
| Synthea | Synthetic patients + FHIR `ExplanationOfBenefit`, coherent code distributions | Free |
| CMS Blue Button 2.0 sandbox | Synthetic Medicare EOBs over FHIR | Free, no DUA |
| CMS SynPUFs | De-identified Medicare claims | Free |
| State DOI / external-review (IRO) sites | Real denial-letter prose and structure, pre-redacted | Free |
| ResDAC | Genuine Medicare claims | DUA + IRB |
| RVL-CDIP, FUNSD, DocVQA, CORD, SROIE | OCR/layout noise | Free, not healthcare |

**Interpretation** — The framing I had was subtly wrong. An 835 is *already
structured*, so it is not an extraction target — it is a **ground-truth oracle**.
Parse the 835 for a claim, extract the paired paper letter, diff the two. That
manufactures real labels for free, which is precisely the property the synthetic
generator exists to fake.

Second correction: denial letters are far more predictable than "unstructured"
suggests. ERISA §503 and the ACA internal-appeal rules **mandate** their
content, so the variance is in layout and wording, not in which facts appear.

**Learned** — Look for a source that already contains the answer before building
a labelling process. And when a document type is legally regulated, read the
regulation before designing the schema — the required fields *are* the schema.

**Next** — Highest-value experiment is not acquiring real data, it is
**corrupting the data I have**: render to PDF, simulate print-scan (skew, blur,
JPEG artifacts, speckle), OCR with Tesseract, re-run the eval. Prediction: the
substring grounding check breaks first, because OCR mangles the exact span it
matches against. Worth writing down so I can be graded on it.

---

## Earlier — reconstructed from the run reports

These predate this file. Recorded because the lessons still apply.

### The eval caught bugs that reading the output did not

**Evidence** — First full run: F1 **0.971**, validation **6/9**.

```
DOC-1002_remittance_advice.txt   fields 10/11  valid=N  triage=N
  error | total_paid | line items sum to 2438.85 but total_paid is 0.00.
```

Two genuine extractor bugs: a totals regex matching a line-level `PAID` before
the claim-level total, and an EOB patient-name capture bleeding into the ID
field. After fixing: F1 **1.000**, validation **9/9**.

**Learned** — Both bugs were on the *remittance* format specifically. A
single-format corpus would have scored 1.000 and hidden them. Format diversity
is not polish; it is the part of the corpus that does the finding.

### 1.000 F1 measures the harness, not the model

The offline `mock` extractor is a deterministic regex double. Its perfect score
proves the pipeline and metrics are wired correctly and says **nothing** about
generalization to unseen layouts.

**Learned** — Always name what a metric is measuring. A number that can only go
one direction is a smoke test wearing a benchmark's clothes.

### Mechanical validation beats asking the model to reflect

Three validators — grounding (verbatim span must occur in the document),
arithmetic (line items sum to totals; paid ≤ allowed ≤ charged), business rules
(codes in the CARC/RARC registry, dates ordered). Under injected failure, a
fabricated payer name and an inflated `$99,999.00` were both caught, the latter
by two independent arithmetic checks:

```
step 2 VALIDATE ok=False errors=4
step 3 VALIDATE ok=True  errors=0
payer after correction:      Vantage Care Network
total_paid after correction: 942.81
```

**Learned** — Self-correction is only real when the critic is something other
than the model. "Reflect on your answer" produces motion; a failing arithmetic
check produces a fix. Redundant checks are cheap and caught the same error twice.

### Grounding by substring — what it cannot do

Verbatim-span matching makes hallucination a *structural* failure rather than
something a human must notice. But it cannot catch a value that is correctly
quoted and assigned to the **wrong field**, and it penalizes legitimate
normalization — which is why `value` (normalized) and `source_text` (verbatim)
are separate fields.

**Learned** — Know the exact shape of what a guard misses. Every guard has a
blind spot; the dangerous ones are those whose blind spot you have not named.

### Sandbox: the guard I did not have

AST guard blocks disallowed imports, dangerous builtins, and dunder access;
execution happens in a subprocess with restricted builtins and a kill-timeout.
Verified: blocked import, dunder escape, `KeyError`, infinite loop.

The gap: `open` is blocked, but `df.to_csv('/etc/passwd')` is **not**.

**Learned** — A blocklist of method names is an arms race. The fix belongs at
the container layer (read-only mount, no network, memory cap), not in the AST
guard. This is a trusted-single-user sandbox, not a hostile-multi-tenant one.

### Structured JSON control loop over provider tool-calling

Every turn is a Pydantic-validated `AgentStep`. Cost: parsing and validating it
myself, with a self-correction retry for malformed JSON. Benefit: one identical
loop across providers plus a deterministic offline mode.

**Learned** — Owning the contract between loop and model is what made the
offline mock possible at all. The mock is not a shortcut; it fell out of the
design decision.

### Hand-rolled loop → LangGraph

The custom loop is preserved (commented) in `src/loop.py`, which re-exports the
graph as `AgentLoop` so call sites are unchanged.

**Learned** — Keeping the predecessor readable turns a rewrite into a
comparison. Budgets still need enforcing in the routers; the graph's
`recursion_limit` is a backstop, not the policy.

---

## Open questions

- [ ] How much does F1 drop under OCR noise? (prediction above: grounding
      breaks first)
- [ ] Does a real LLM beat the regex mock on an **unseen** fourth layout? That
      is the only test that separates capability from harness.
- [ ] Can the 835-as-oracle idea produce labels without any hand annotation?
- [ ] Is field-level F1 even the right metric, or should it be
      *dollars-at-risk* error — since a wrong `total_paid` costs more than a
      wrong `provider_name`?
