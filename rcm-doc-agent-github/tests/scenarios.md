# Representative test scenarios and expected outcomes

Five named scenarios, each with real evidence already produced elsewhere in
this repo (`tests/`, `LEARNING.md`, `README.md`) — this file exists to make
that evidence checkable in one place without reading the eval code, not to
introduce new claims. Every number below is a real, reproduced result; none
are illustrative/hypothetical.

---

### 1. Clean multi-format extraction (denial letter / EOB / 835)

**Input:** the 9-document corpus in `data/docs/` — 3 denial letters, 3 EOB
tables, 3 remittance-advice summaries, each rendering the *same* underlying
schema in a different layout/wording/date-money format.

**Expected outcome:** every field extracted correctly, every document
validates (`validation.ok = True`), every triage decision correct.

**Real result** (`openai/gpt-4.1`, reproduce with
`python -m src.docproc.evaluation.evaluate --docs data/docs`):
```
precision 1.000   recall 1.000   F1 1.000
grounding rate        99/99 (100.0%)
validation passed     9/9
triage decision correct  9/9
```

---

### 2. Injected hallucination (fabricated value + broken arithmetic)

**Input:** a real corpus document (`DOC-1000_denial_letter.txt`) with a
deliberately fabricated `total_charged` value (`9999999.99`, which never
appears anywhere in the source text) substituted for the real one.

**Expected outcome:** the grounding check rejects the fabricated value
(`severity="error"`, `check="grounding"`) rather than accepting whatever the
model claims — this is the anti-hallucination guarantee the whole
architecture is built around, not a best-effort heuristic.

**Real result** — reproduced as a permanent regression test, not just a
one-off manual check:
`tests/test_validation.py::TestGrounding::test_fabricated_value_is_rejected`
— asserts `report.ok is False` and the specific `total_charged` / `grounding`
error is present. A parallel test
(`TestArithmetic::test_line_items_not_summing_to_total_is_rejected`) does
the same for line items that don't sum to the stated claim total.

---

### 3. X12 835 EDI input (deterministic path, zero LLM calls)

**Input:** `data/real_world/sample_835.edi` — a real X12 835 (Electronic
Remittance Advice) transaction, hand-built from the public HIPAA 5010
segment grammar, not synthetic prose.

**Expected outcome:** the ingestion router recognizes the `.edi` extension
and routes to `x12_parser.parse_835` — fully deterministic extraction *and*
triage (`derive_triage` against the CARC registry), **zero LLM calls**, so
no hallucination surface exists on this path at all.

**Real result** — reproduce with
`python src/cli.py --doc data/real_world/sample_835.edi`, or through the
queue: `[worker] job N sample_835.edi -> auto_approved (edi, 0 llm calls,
0.0s)`. `tests/test_x12_parser.py` covers `parse_835` itself against this
exact bundled file.

---

### 4. Weak model omits `triage` on finalize (mechanically reconstructed)

**Input:** the same `DOC-1000_denial_letter.txt`, run through a genuinely
weaker/different model (`gpt-4.1-nano`) rather than the frontier model the
architecture was originally validated against.

**Expected outcome:** even when the model's `finalize` `DocStep` omits the
`triage` object entirely (a real behavior gpt-4.1-nano exhibits that
gpt-4.1/gpt-4.1-mini never did), the agent must not leave
`DocOutcome.triage = None` — it should mechanically construct a `Triage`
from the CARC registry instead, the same way `derive_triage` already
overrides a *present-but-wrong* triage.

**Real result** — found via real testing, not anticipated in advance: before
the fix, `gpt-4.1-nano` scored 9/10 triage-correct on a 10-document sample
(one silent `None`); after the fix
(`src/docproc/agent.py::_finalize_node` — see the `if triage is None:`
branch), a repeat run scored **10/10**, with the previously-`None` document
now showing `appealable=True, category=coverage, dollars_at_risk=2553.78`.
Full before/after numbers in `LEARNING.md`, 2026-08-15 entry.

---

### 5. Cross-document reconciliation with an injected discrepancy

**Input:** matched claim triads (`src.docproc.generator --mode triads`) —
the same claim rendered as a denial letter, an EOB, and a remittance advice,
where the generator has injected a real, internally-consistent discrepancy
into a random subset (e.g. a later remittance pays less than the EOB
already told the provider, simulating a post-adjudication take-back).

**Expected outcome:** `reconcile()` flags the specific field(s) that
disagree across documents (not just "something's wrong"), and does NOT
false-positive on the claim groups with no injected discrepancy.

**Real result** — a live run through the Streamlit UI's Cross-document
reconciliation mode against 4 real claim groups: 2 groups reconciled clean
(`"All cross-checked fields agree."`), 2 groups correctly flagged with the
exact field-level disagreement (e.g. `total_paid disagrees across
documents: denial_letter=2070.82, eob=2070.82, remittance_advice=1979.93`),
for a final scoreboard of **"Caught 2/3 injected discrepancies across 4
claim groups."**

---

## Why these five

Each targets a *different* failure surface this project's guardrails are
designed against: (1) is the happy path actually clean, (2) can a fabricated
value slip past verification, (3) does routing correctly bypass the LLM
where a deterministic parser is strictly better, (4) does a weaker model
break an assumption a stronger model never exercised, (5) can a discrepancy
that only exists *across* documents (invisible to any single-document
validator) still be caught. None of these are hypothetical — every one has
already happened for real in this project's history and is cited above with
its evidence.
