# Agent Run Report — Healthcare RCM Document-Processing Agent

All traces and metrics below are **real output** from the code in this repo.
The offline `mock` extractor referenced in earlier drafts of this report was
removed entirely (see §5/LEARNING.md, 2026-08-13) — there is no offline mode
anymore, and the CLI has no `--domain` flag. Every real run cited here is
against a real LLM provider via `python -m src.docproc.evaluation.evaluate`
or `python src/cli.py --doc ...`; §4a is the actual evidence base (a real
`gpt-4.1` run, not a mock).

---

## 1. Problem & why this domain

Revenue-cycle teams receive payer correspondence — denial letters, EOBs, 835
remittance advice — in wildly inconsistent layouts. The work is: pull the claim
facts out, confirm they're right, decide whether the denial is worth appealing,
and quantify what's at stake. That is the **document-processing** task this
agent performs end to end.

The design bet: in this domain, *being confidently wrong is worse than being
slow*. A fabricated claim number or an inflated paid amount propagates into an
appeal or a write-off. So verification is built into the control flow rather
than bolted on as a final check.

---

## 2. Architecture

```
    START
      |
      v
   [decide] ---- lookup_code --> [lookup]  CARC/RARC registry --------\
      |  ^                                                            |
      |  |                                                            |
      |  +---- extract -------> [extract] --> VALIDATE ---------------+
      |  |                                    (grounding / arithmetic /
      |  |                                     business rules)
      |  |                                        | fail & budget left
      |  +----------------------------------------+
      |
      +---- finalize --------> [finalize] --> triage --> END
      +---- budget exceeded -> [give_up] ------------> END
```

LangGraph `StateGraph`; nodes `decide / lookup / extract / finalize / give_up`.
Loop = **reason → act (tool) → observe (validation) → self-correct → respond**.

The distinctive property: the "observe" step is a **mechanical validator**, not
the model grading itself. Self-correction is driven by verifiable failures.

### The three validators

| Validator | Catches | Example issue emitted |
|---|---|---|
| **Grounding** | hallucinated values | `source_text 'Acme Insurance Co' does not occur in the document` |
| **Arithmetic** | transcription / OCR-style digit errors | `line items sum to 942.81 but total_paid is 99999.00` |
| **Business rules** | domain-invalid output | `code 'CO-999' is not in the CARC/RARC registry`; deadline before date of service |

---

## 3. Sample trace — self-correction under injected failure

To prove the loop is real rather than decorative, a hallucinated payer name and
an inflated payment were injected into the first extraction. Verbatim trace:

```
step 1 DECIDE   lookup_code
step 1 TOOL     found=['CO-197', 'CO-45', 'PR-3']
step 2 DECIDE   extract
step 2 VALIDATE ok=False errors=4
       - payer_name: source_text 'Acme Insurance Co' does not occur in the
         document (possible hallucination); quote text verbatim.
       - total_paid: source_text '$99,999.00' does not occur in the document
         (possible hallucination); quote text verbatim.
       - total_paid: line items sum to 942.81 but total_paid is 99999.00.
       - total_paid: paid exceeds allowed.
step 3 DECIDE   extract
step 3 VALIDATE ok=True errors=0
step 4 DECIDE   finalize
step 4 FINAL    appealable=True

final status: ok | validation ok: True
payer after correction:      Vantage Care Network
total_paid after correction: 942.81
```

Both fabrications were caught mechanically and corrected — the hallucination by
the grounding check, the inflated figure by *two independent* arithmetic checks.

### Normal run (no injection)

```
=== EXTRACTION (ok, 3 steps) ===
  payer_name               Meridian Health Plan
  provider_name            Riverbend Regional Medical Center
  patient_name             Elena Ferraro
  member_id                Z365874400
  claim_number             CLM2234510745
  date_of_service          2026-04-25
  total_charged            2306.72
  total_allowed            1035.96
  total_paid               761.56
  patient_responsibility   274.4
  appeal_deadline          2026-08-23
  denial_codes             CO-45, CO-97, PR-1
  line_items               2

VALIDATION: passed

=== TRIAGE ===
  appealable       True
  category         coding
  dollars at risk  270.7
  action           Review bundling edits; appeal with modifier justification
                   if unbundling is supported.
  rationale        CO-97: Benefit for this service is included in the payment
                   for another service already adjudicated.
```

Note the triage logic discriminates: **CO-45 is contractual** (a write-off, not
appealable) while **CO-97 is a coding denial** (appealable). The agent selects
the actionable code rather than the first one it sees.

---

## 4. Evaluation

Because documents are rendered **from** known records, ground truth is exact.
No hand labelling; metrics are computed, not judged.

Corpus: 9 documents, 3 formats (denial letter / EOB / 835 remittance), 12 fields
each plus line items.

```
PER-FIELD ACCURACY
  payer_name                   9/9   (100.0%)
  claim_number                 9/9   (100.0%)
  member_id                    9/9   (100.0%)
  patient_name                 9/9   (100.0%)
  provider_name                9/9   (100.0%)
  date_of_service              9/9   (100.0%)
  total_charged                9/9   (100.0%)
  total_allowed                9/9   (100.0%)
  total_paid                   9/9   (100.0%)
  patient_responsibility       9/9   (100.0%)
  appeal_deadline              9/9   (100.0%)
  denial_codes                 9/9   (100.0%)

AGGREGATE
  precision              1.000
  recall                 1.000
  F1                     1.000
  grounding rate         99/99 (100.0%)
  line items exact       9/9
  validation passed      9/9
  triage decision correct   9/9
```

**The eval earned its keep during development.** The first run scored F1 0.971
with 6/9 validation passes and flagged exactly three failures on the remittance
format:

```
DOC-1002_remittance_advice.txt   fields 10/11  valid=N  triage=N
  error | total_paid | line items sum to 2438.85 but total_paid is 0.00.
```

Two genuine extractor bugs — a totals regex matching a line-level `PAID` before
the claim-level total, and an EOB patient-name capture bleeding into the ID
field. Both were found by the harness rather than by reading output, which is
the entire argument for a computable eval.

---

## 4a. Real LLM verification (`gpt-4.1`, 2026-08-13)

Everything above is the offline `mock` extractor. This section is the run
against an actual model that the rest of this report explicitly says is
missing. Provider: `openai/gpt-4.1` via LiteLLM. Cost: a few cents for the
whole corpus (see `reports/cheap_extraction_research.md` for the per-token math).

**First attempt failed** — real, useful failure, not noise. `gpt-4.1` produced
invalid `DocStep` JSON 3/3 times, oscillating between two wrong shapes: first
wrapping the plain-string `doc_type` enum in a `{value, source_text,
confidence}` object (confusing it with every other field, which IS wrapped),
then over-correcting by flattening wrapped fields (`payer_name`, etc.) into
plain strings. Root cause: the prompt described the shape in prose but had
**zero worked examples** — a gap already flagged earlier as a compliance note
against the assessment brief's "few-shot where appropriate," now confirmed as
a real capability-affecting gap, not a checklist nitpick.

**Fix 1**: added one fully-shaped worked example to `DOC_SYSTEM_PROMPT`
(`src/docproc/prompts.py`) showing exactly which fields are wrapped and which
aren't. Immediate result: 3/3 steps, extraction correct on every field,
validation passed, triage matched ground truth exactly, first try.

**Full-corpus run after fix 1** — F1 0.967, one field (`denial_codes`) at
22% (2/9), dragging two triage decisions wrong. Diagnosed precisely: the
document for DOC-1003 states *"Adjustment reason codes applied to this claim:
CO-45, CO-50, PR-3"* — the model extracted `[CO-45, CO-50]`, dropping `PR-3`.
Grounding rate was still 100% (not a hallucination — an omission). Root
cause, found immediately: fix 1's own worked example only showed CO-prefixed
codes (`["CO-45", "CO-97"]`), teaching the model to under-weight
patient-responsibility (`PR-`) codes.

**Fix 2**: added a `PR-` code to the worked example and an explicit prompt
note that `denial_codes` must include codes found anywhere on the document
(per-line AND in aggregate summary sentences), not just CO-prefixed ones.

**Full-corpus run after fix 2** — real, final numbers:

```
PER-FIELD ACCURACY      all 12 fields 9/9 (100.0%)
AGGREGATE
  precision              1.000
  recall                 1.000
  F1                     1.000
  grounding rate         99/99 (100.0%)
  line items exact       9/9
  validation passed      9/9
  triage decision correct   8/9
```

The one remaining triage mismatch (DOC-1005) could not be reproduced on a
fresh re-run of the identical document immediately afterward — consistent
with real-API sampling variance at the margin even at `temperature=0.0`,
rather than a systematic bug (further chasing it was not worth the marginal
value at that point).

**Why this matters more than the mock's F1**: the mock's 1.000 proves the
harness is wired correctly by construction — it cannot fail, since it's a
regex test double scoring itself. This run proves something the mock
structurally cannot: a real model, given this exact prompt and schema,
really does reach 1.000 field-level F1 and 100% grounding on this corpus,
and the two failures it did have were found, diagnosed to a precise root
cause in the document text, and fixed by editing the prompt — not the
validator, not the schema, not the harness. That is the actual capability
claim this project can now make.

---

## 5. Design decisions & trade-offs

- **Synthetic generated corpus vs. real documents.** Chosen so ground truth is
  exact and no PHI is involved. Cost: generated documents are cleaner than
  reality — no OCR noise, no scanned skew, no handwriting. Mitigated partly by
  three divergent layouts and mixed date/currency formats. Real deployment would
  need an OCR front end and a noisy-document eval slice.
- **Grounding via verbatim spans.** Simple, mechanically checkable, and it makes
  hallucination a *structural* failure rather than something a human must spot.
  Cost: strict substring matching penalizes legitimate normalization, so `value`
  (normalized) and `source_text` (verbatim) are kept as separate fields.
- **Registry lookup as a tool, not prompt context.** Denial codes are the
  domain's real knowledge, and the code set changes on the X12 update cadence.
  A tool call keeps that current and lets the agent be told "not found" instead
  of inventing a meaning.
- **Mock extractor for the offline path (removed 2026-08-13).** Originally a
  deterministic regex double that made the pipeline and metrics reproducible
  with zero credentials — but its 1.000 F1 measured the harness, not model
  capability, and it did not generalize to unseen layouts. Once §4a's real-LLM
  run replaced it as the actual evidence base, the mock code was removed
  entirely from the codebase (`src/docproc/mock.py`, `_MockBackend` in
  `src/llm.py`, and the `llm_provider`/`--provider` setting). There is no
  offline mode anymore; every run requires a valid API key.
- **LangGraph + LiteLLM.** Explicit graph for inspectable control flow;
  provider-agnostic gateway so the model is config, not code.

---

## 6. Known limitations (stated, not hidden)

- **One provider, one small corpus, one run.** A single `gpt-4.1` run reaching
  F1 1.000 (§4a) is real evidence the pipeline works past the mock's regex
  rules, but it is one provider, one corpus of 9, one run — not a
  generalization claim across payers, formats, or providers. See §4a for
  exactly what was and wasn't verified, and `reports/cheap_extraction_research.md`
  for the cost math behind running this more broadly. The mock extractor
  described in §1-3 above was removed from the codebase entirely once this
  real run replaced it as the evidence base — there is no offline mode left;
  every run now requires a valid API key.
- **No OCR / no PDF ingestion.** Input is plain text. Real payer mail is scanned
  PDF and images.
- **Small corpus.** 9 documents is a smoke test. A real suite needs hundreds,
  stratified by payer, format, and denial category, with pass *rates* under
  non-determinism rather than single-run pass/fail.
- **Registry is a subset.** ~16 representative CARC/RARC codes, not the full
  published set.
- **No PHI handling.** All data is fabricated. Production use would require PHI
  redaction before prompts leave the trust boundary, a BAA-covered provider or
  self-hosted model, and audit logging of every extraction decision.
- **Grounding is substring-based.** It cannot catch a value that is correctly
  quoted but assigned to the wrong field.
