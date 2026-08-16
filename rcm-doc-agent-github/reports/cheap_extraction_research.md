# Research: cost-effective document extraction — where this project can differentiate

Real research, real numbers, done 2026-08-13. Sources: OpenAI's live pricing
page and API docs (fetched directly, not recalled from training data — see
citations inline). Token/character counts are measured from this repo's
actual prompt and corpus, not estimated in the abstract.

**Status of the "run against a real LLM" gap:** closed the same day this was
written. See `agent_run_report_docproc.md` §4a for the full trace: `gpt-4.1`
reached F1 1.000 / 100% grounding / 8-of-9 correct triage on the real corpus,
after two real prompt bugs were found and fixed (a missing few-shot example
causing schema confusion, then a worked example that itself under-represented
patient-responsibility codes). The rest of this report — the cost/caching/
batch research — still stands as the plan for scaling that verification up.

**Update, same day: cross-tier comparison run.** Once `gpt-4.1` hit F1 1.000,
the natural next test was whether a cheaper tier holds up under the same,
now-fixed prompt. It doesn't, and *how* it fails is the real finding — see
§5 at the end of this report.

---

## 1. What this actually costs, with real numbers

Measured directly from this repo:

| Quantity | Value |
|---|---|
| `DOC_SYSTEM_PROMPT` length | 2,393 chars ≈ 600 tokens |
| Average document (`data/docs/*.txt`) | ≈1,047 chars ≈ 260 tokens |
| Largest document | 1,830 chars ≈ 460 tokens |
| Typical run length | 3 turns (`lookup_code` → `extract` → `finalize`) |

Because `DocumentAgent` resends the full growing message list every turn
(standard chat-completion pattern, no server-side conversation state), total
tokens compound across turns. Rough per-document totals for a clean 3-turn
run: **~4,250 input tokens, ~800 output tokens** (the `ClaimExtraction` JSON
is the largest single output).

Pricing pulled live from `developers.openai.com/api/docs/pricing` (2026-08-13):

| Model | Input $/1M | Output $/1M | Cost per doc (synchronous) |
|---|---|---|---|
| `gpt-4.1-nano` | $0.10 | $0.40 | ≈ $0.0007 |
| `gpt-4.1-mini` | $0.40 | $1.60 | ≈ $0.0030 |
| `gpt-4.1` | $2.00 | $8.00 | ≈ $0.015 |
| `gpt-4o-mini` | $0.15 | $0.60 | ≈ $0.0011 |

At **any** of these tiers, $5 in credits covers 300–7,000+ documents. Cost is
not the constraint for a corpus this size — it's a non-issue until volume is
orders of magnitude larger than anything in this repo.

---

## 2. Two real cost levers this project already qualifies for — zero code change

### Lever A: automatic prompt caching (source: OpenAI's Prompt Caching guide)

> "By default, caching is enabled automatically for prompts that are 1,024
> tokens or longer... Cached input tokens are billed at 0.1× the uncached
> input token rate" — no cache-write fee on `gpt-4.1`-generation models.

This project's own numbers show why this matters *without changing anything*:
turn 1's prompt (system + task message) is ~1,000 tokens — right at the
threshold — but **turn 2's prompt (~1,350 tokens) and turn 3's (~1,900
tokens) are both comfortably over it**, and each repeats the previous turn's
content verbatim as a prefix (append-only message history is exactly the
"structure prompts for reuse" pattern the docs recommend). That means **any
document needing 2+ turns already gets a partial cache hit today**, for free,
with the existing code — nothing to build. It doesn't need to be verified by
code changes, only by checking `usage.prompt_tokens_details.cached_tokens` in
a real response (worth doing once real credits are available, so this claim
is evidenced, not just plausible).

Caveat, also confirmed from the docs: caching does **not** carry across
different documents in a batch — each document's task message diverges
immediately after the shared ~650-token system-prompt-plus-wrapper prefix,
which is below the 1,024 minimum. The benefit here is within one document's
multi-turn conversation, not across a portfolio run.

### Lever B: Batch API (source: OpenAI's Batch API guide)

> "50% cost discount compared to synchronous APIs... each batch completes
> within 24 hours... a separate pool of significantly higher rate limits."

This is a genuine, currently-unbuilt differentiation opportunity. Three of
this project's four run modes are **already non-interactive** — `--batch`
(portfolio), `--reconcile`, and `python -m src.docproc.evaluate` — none need
a synchronous response. Submitting a whole corpus as one Batch API job
instead of N sequential synchronous calls is a 50% cost cut *and* removes
synchronous per-model rate-limit pressure from a large `--batch` run, at the
cost of results arriving asynchronously (up to 24h, typically much faster).
Only `--doc` (single, interactive) and the Streamlit UI genuinely need
synchronous calls.

---

## 3. The differentiation angle

Most LLM-extraction demos pick one frontier model and call it synchronously,
every time, at full price. This project's existing architecture already has
the one piece that's usually missing to do better — **it retries against a
mechanical validator, not against itself** (`src/docproc/validation.py`).
That retry point is a natural, low-effort place to add **cost-aware model
escalation** instead of just re-prompting the same model:

```
gpt-4.1-nano (~$0.0007/doc)  →  gpt-4.1-mini (~$0.003/doc)  →  gpt-4.1 (~$0.015/doc)
        |                              |
        +--- validation fails, escalate one tier, same document, same validator ---+
```

(Note: this project's `mock` regex extractor — the free tier that would have
sat before `gpt-4.1-nano` here — was removed from the codebase on 2026-08-13
once the real-LLM run in §4a of `agent_run_report_docproc.md` replaced it as
the evidence base. Every tier now shown is a real, paid model call.)

Concretely: only escalate to a stronger (pricier) model on the *same*
document if the cheaper model's extraction fails `validate()` after its
correction-round budget — most documents most of the time should validate
clean on the cheapest tier, and only the genuinely hard cases pay for a
stronger model. This turns the existing self-correction loop into a
**cost-aware** self-correction loop, and is a defensible, evidence-based
differentiation story: "cheap by default, escalates only when the mechanical
validator proves it needs to" — rather than "cheap because we picked a small
model and hoped."

Combined with Batch API for the non-interactive modes, the two together are
the actual "cheap way to extract docs" differentiation: **tiered synchronous
escalation for interactive single-document use, batched submission for bulk
use** — not a single blanket choice of "use a cheap model."

---

## 4. Recommended next steps (not yet built — pending your go-ahead)

1. **Get one real trace committed.** Needs a working key with credits;
   nothing else in this report unblocks that.
2. **Verify the caching claim** with a real 3-turn run's `cached_tokens`
   field — currently a documented mechanism, not yet an observed number from
   this project's traffic.
3. **Build model-tier escalation** into `DocumentAgent`'s retry path (small
   change: pass a list of models instead of one, step up on validation
   failure past the correction budget).
4. **Build a Batch API path** for `--batch`/`--reconcile`/`evaluate` — a new
   `src/docproc/batch_submit.py` that renders the JSONL, submits, polls, and
   parses results back into the same `DocOutcome` shape everything else uses.

None of this is implemented yet — this document is the research and the
plan. Say which of #3/#4 to build first, or whether to wait on #1/#2 first
so the escalation and batch paths have a real baseline to compare against.

---

## 5. Real finding: `gpt-4.1-mini` extracts perfectly but reasons worse

Ran the identical fixed prompt/schema against `gpt-4.1-mini` on the same
9-document corpus. Real numbers:

| Metric | `gpt-4.1` | `gpt-4.1-mini` |
|---|---|---|
| Per-field accuracy (docs that completed) | 100% | 100% |
| Grounding rate | 100% | 100% |
| Validation passed | 9/9 | 8/9 (one doc failed to parse; not reproducible on retry — same real-API nondeterminism noted in §4a of the run report) |
| **Triage decision correct** | **8/9** | **2/9** |

Extraction — pulling literal spans out of the document — is unaffected by the
cheaper tier. **Triage collapsed.** Two concrete, diagnosed cases:

- **DOC-1002** (codes `CO-27, CO-45, PR-3`): `CO-27`'s registry category is
  `eligibility`. Mini's own rationale correctly explains what CO-27 means, but
  writes `category: coverage` — a plausible-sounding paraphrase instead of the
  literal string the `lookup_code` tool actually returned in that turn's
  observation.
- **DOC-1004** (codes `CO-45, CO-97, PR-1`): the documented rule picks the one
  actionable code (`CO-97`, category `coding`, appealable). Mini instead
  **blended both codes into `category: "fee schedule/bundling"`** — a label
  that doesn't exist anywhere in the registry's 8 real categories — and
  **inverted the appealability verdict** (said non-appealable; correct answer
  is appealable via CO-97).

**Diagnosis**: this is not a grounding failure (both cases had 100% grounding)
and not an extraction failure (every field was correct in both documents).
It's specifically the *"pick exactly one driving code among several, and copy
its registry category verbatim rather than re-derive/paraphrase it"* step —
a different skill than quoting a span off a page, and the mini tier is
measurably worse at it on this exact task, on this exact prompt.

**Implication for the tiered-escalation idea in §3**: it's directionally
right but the escalation trigger needs to be more specific than "validation
failed." `validate()` never catches a wrong-but-plausible category string or
an inverted appealability call — those aren't grounding/arithmetic/business-
rule violations, they're *reasoning* errors the current validators are
structurally blind to. A real tiered pipeline would need a fourth check —
something like re-deriving `expected_triage`-style logic mechanically from
the extracted `denial_codes` and registry, and escalating to a stronger model
specifically when the model's stated triage disagrees with that mechanical
derivation — rather than reusing the existing three validators as the
escalation trigger.

**Recommendation**: use `gpt-4.1` (not `gpt-4.1-mini`) wherever the triage
decision matters, despite the ~5x cost difference — at real corpus sizes the
absolute cost gap is still cents, and a wrong appealability verdict is exactly
the "confidently wrong is worse than slow" failure this project is designed
to avoid. `gpt-4.1-mini` is plausible for extraction-only use (no triage), or
as the cheap first pass in a tiered pipeline *if* the fourth mechanical
triage-check above gets built to catch its specific failure mode.
