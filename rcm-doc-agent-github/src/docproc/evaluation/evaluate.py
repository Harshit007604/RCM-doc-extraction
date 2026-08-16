"""Field-level evaluation for the document-processing agent.

Because documents are RENDERED FROM known records, ground truth is exact and no
hand labelling is involved. That lets us report real extraction metrics rather
than a qualitative judgement:

  per field   : correct / incorrect / missed  (exact match after normalization)
  aggregate   : precision, recall, F1 over all populated fields
  grounding   : share of emitted values whose source_text occurs in the document
  triage      : accuracy of the appealable / category decision vs. the registry
  validation  : share of documents passing all mechanical checks
  per doc-type: the same breakdown split by denial_letter / eob / remittance_advice
  cost/latency: real $ (src/docproc/evaluation/pricing.py) and real wall-clock time per doc

`evaluate()` returns a structured results dict (not just an exit code) so
`compare_models.py` can call it programmatically across several models
without shelling out to a subprocess per model.

Run:
    python -m src.docproc.evaluation.evaluate --docs data/docs
    python -m src.docproc.evaluation.evaluate --docs data/docs_100 --limit 10 --model openai/gpt-4.1-nano
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.config import get_settings                      # noqa: E402
from src.docproc.agent import DocumentAgent              # noqa: E402
from src.docproc.registry.codes import derive_triage      # noqa: E402
from src.docproc.evaluation.pricing import estimate_cost  # noqa: E402
from src.llm import LLMClient                            # noqa: E402
from src.logging_utils import setup_logging              # noqa: E402

SCALAR_FIELDS = ["payer_name", "claim_number", "member_id", "patient_name",
                 "provider_name", "date_of_service", "total_charged",
                 "total_allowed", "total_paid", "patient_responsibility",
                 "appeal_deadline"]
MONEY_FIELDS = {"total_charged", "total_allowed", "total_paid", "patient_responsibility"}


def _norm_scalar(field: str, value) -> str | None:
    """Normalize a value for exact-match comparison: money to 2-decimal
    strings, everything else to whitespace-collapsed lowercase. None/empty
    both mean "absent" so ground truth and extraction compare fairly."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if field in MONEY_FIELDS:
        try:
            return f"{float(text.replace('$', '').replace(',', '')):.2f}"
        except ValueError:
            return text.lower()
    return re.sub(r"\s+", " ", text).strip().lower()


def expected_triage(truth: dict) -> tuple[bool, str]:
    """Derive the correct triage from the registry -- the same rule an analyst
    applies. Now that `derive_triage` (`src/docproc/registry/codes.py`) is also wired
    into the agent's own `_finalize_node`, this and the agent's output should
    always agree by construction; kept as a thin wrapper so the eval's intent
    ("what SHOULD this claim's triage be") stays readable on its own.
    """
    return derive_triage(truth["denial_codes"])


def evaluate(docs_dir: str, limit: int | None = None, model: str | None = None,
            quiet: bool = False) -> dict:
    """Run every document in `docs_dir` through a fresh `DocumentAgent`, score
    each field against `ground_truth.json`, and return a structured results
    dict (also printed unless `quiet=True`).

    `model` overrides the configured model (e.g. for `compare_models.py`
    running several models back to back without touching `.env`).
    """
    with open(os.path.join(docs_dir, "ground_truth.json"), encoding="utf-8") as fh:
        truth_records = json.load(fh)
    if limit:
        truth_records = truth_records[:limit]

    settings = get_settings(model=model)
    setup_logging("WARNING")
    llm = LLMClient(settings)
    agent = DocumentAgent(settings, llm)

    per_field = defaultdict(lambda: {"correct": 0, "wrong": 0, "missed": 0, "spurious": 0})
    by_doc_type = defaultdict(lambda: {"docs": 0, "field_correct": 0, "field_total": 0,
                                       "triage_correct": 0})
    grounded_ok = grounded_total = 0
    triage_correct = validation_passed = line_exact = 0
    total_prompt_tokens = total_completion_tokens = total_tokens = 0
    total_llm_calls = 0
    total_wall_s = 0.0
    rows = []

    if not quiet:
        print(f"\nDocument extraction eval — {len(truth_records)} docs, "
              f"model='{settings.model}'\n" + "=" * 68)

    for truth in truth_records:
        path = os.path.join(docs_dir, truth["file"])
        document = open(path, encoding="utf-8").read()

        started = time.perf_counter()
        outcome = agent.run(document, truth["file"])
        wall_s = time.perf_counter() - started
        total_wall_s += wall_s

        ext = outcome.extraction
        dt_stats = by_doc_type[truth["doc_type"]]
        dt_stats["docs"] += 1

        doc_correct = doc_total = 0
        if ext is not None:
            for field in SCALAR_FIELDS:
                got = _norm_scalar(field, getattr(ext, field).value)
                want = _norm_scalar(field, truth.get(field))
                if want is not None:
                    doc_total += 1
                    if got is None:
                        per_field[field]["missed"] += 1
                    elif got == want:
                        per_field[field]["correct"] += 1
                        doc_correct += 1
                    else:
                        per_field[field]["wrong"] += 1
                elif got is not None:
                    per_field[field]["spurious"] += 1

                fv = getattr(ext, field)
                if fv.value is not None:
                    grounded_total += 1
                    if fv.source_text and re.sub(r"\s+", " ", fv.source_text).strip().lower() \
                            in re.sub(r"\s+", " ", document).lower():
                        grounded_ok += 1

            got_codes = sorted({c.upper() for c in ext.denial_codes})
            want_codes = sorted({c.upper() for c in truth["denial_codes"]})
            key = "denial_codes"
            (per_field[key].__setitem__("correct", per_field[key]["correct"] + 1)
             if got_codes == want_codes
             else per_field[key].__setitem__("wrong", per_field[key]["wrong"] + 1))

            want_lines = [(li["cpt_code"], round(li["charge_amount"], 2))
                          for li in truth["line_items"]]
            got_lines = [(li.cpt_code, round(li.charge_amount or 0, 2))
                         for li in ext.line_items]
            if want_lines == got_lines:
                line_exact += 1

        dt_stats["field_correct"] += doc_correct
        dt_stats["field_total"] += doc_total

        if outcome.validation and outcome.validation.ok:
            validation_passed += 1

        want_appeal, want_cat = expected_triage(truth)
        got_appeal = outcome.triage.is_appealable if outcome.triage else None
        got_cat = outcome.triage.denial_category if outcome.triage else None
        this_triage_ok = got_appeal == want_appeal and got_cat == want_cat
        if this_triage_ok:
            triage_correct += 1
            dt_stats["triage_correct"] += 1

        usage = outcome.token_usage or {}
        total_prompt_tokens += usage.get("prompt_tokens", 0)
        total_completion_tokens += usage.get("completion_tokens", 0)
        total_tokens += usage.get("total_tokens", 0)
        total_llm_calls += outcome.steps_used

        rows.append((truth["file"], truth["doc_type"], doc_correct, doc_total,
                     outcome.validation.ok if outcome.validation else False, this_triage_ok))
        if not quiet:
            print(f"  {truth['file']:<34} fields {doc_correct}/{doc_total}  "
                  f"valid={'Y' if outcome.validation and outcome.validation.ok else 'N'}  "
                  f"triage={'Y' if this_triage_ok else 'N'}  "
                  f"tokens={usage.get('total_tokens', 0)}  {wall_s:.1f}s")

    # ---- aggregate ----
    tp = sum(v["correct"] for v in per_field.values())
    fp = sum(v["wrong"] + v["spurious"] for v in per_field.values())
    fn = sum(v["missed"] for v in per_field.values())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    n = len(truth_records)
    cost = estimate_cost(settings.model, total_prompt_tokens, total_completion_tokens)

    if not quiet:
        print("\n" + "=" * 68)
        print("PER-FIELD ACCURACY")
        for field in SCALAR_FIELDS + ["denial_codes"]:
            s = per_field[field]
            tot = s["correct"] + s["wrong"] + s["missed"]
            pct = 100 * s["correct"] / tot if tot else 0.0
            print(f"  {field:<26} {s['correct']:>3}/{tot:<3} ({pct:5.1f}%)"
                  f"  wrong={s['wrong']} missed={s['missed']}")

        if len(by_doc_type) > 1:
            print("\nPER DOC-TYPE")
            for doc_type, s in sorted(by_doc_type.items()):
                pct = 100 * s["field_correct"] / s["field_total"] if s["field_total"] else 0.0
                print(f"  {doc_type:<20} {s['docs']:>3} docs  fields {pct:5.1f}%  "
                      f"triage {s['triage_correct']}/{s['docs']}")

        print("\nAGGREGATE")
        print(f"  precision              {precision:.3f}")
        print(f"  recall                 {recall:.3f}")
        print(f"  F1                     {f1:.3f}")
        print(f"  grounding rate         {grounded_ok}/{grounded_total} "
              f"({100 * grounded_ok / grounded_total if grounded_total else 0:.1f}%)")
        print(f"  line items exact       {line_exact}/{n}")
        print(f"  validation passed      {validation_passed}/{n}")
        print(f"  triage decision correct{triage_correct:>4}/{n}")
        print(f"  total tokens           {total_tokens:,}  ({total_tokens / n:,.0f}/doc, "
              f"{total_llm_calls} LLM calls, {total_tokens / total_llm_calls if total_llm_calls else 0:,.0f}/call)")
        print(f"  estimated cost         "
              f"{f'${cost:.4f} (${cost / n:.4f}/doc)' if cost is not None else 'unknown model, not in pricing.py'}")
        print(f"  wall time              {total_wall_s:.1f}s ({total_wall_s / n:.1f}s/doc)")
        print("=" * 68)

    return {
        "model": settings.model, "n_docs": n,
        "precision": precision, "recall": recall, "f1": f1,
        "grounding_ok": grounded_ok, "grounding_total": grounded_total,
        "line_items_exact": line_exact, "validation_passed": validation_passed,
        "triage_correct": triage_correct,
        "total_tokens": total_tokens, "prompt_tokens": total_prompt_tokens,
        "completion_tokens": total_completion_tokens, "total_llm_calls": total_llm_calls,
        "cost_usd": cost, "wall_s": total_wall_s,
        "by_doc_type": dict(by_doc_type),
        "rows": rows,
        "exit_code": 0 if (f1 > 0.9 and triage_correct == n) else 1,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: `python -m src.docproc.evaluation.evaluate --docs data/docs`."""
    ap = argparse.ArgumentParser(description="Evaluate the document-processing agent.")
    ap.add_argument("--docs", default="data/docs")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--model", default=None, help="Override the configured model.")
    args = ap.parse_args(argv)
    results = evaluate(args.docs, args.limit, model=args.model)
    return results["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
