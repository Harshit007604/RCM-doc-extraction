"""CLI entrypoint — document-extraction agent.

Reads one payer document (denial letter / EOB / remittance advice), extracts
a validated structured record, mechanically verifies it, self-corrects, and
returns a triage decision.

Named `cli.py` (not `agent.py`) deliberately: the actual agent -- the
LangGraph `DocumentAgent` state machine -- lives in `src/docproc/agent.py`.
This module is just the command-line shell around it (arg parsing, mode
dispatch, console rendering); it holds no agent logic of its own.

Examples
--------
    python src/cli.py --doc data/docs/DOC-1000_denial_letter.txt
    python src/cli.py --doc data/docs/DOC-1000_denial_letter.txt --stream
    python src/cli.py --doc data/real_world/sample_835.edi --model openai/gpt-4.1

    # Multi-agent (A): portfolio triage across a batch of documents
    python src/cli.py --batch data/docs

    # Multi-agent (B): cross-document reconciliation for matched claim triads
    python -m src.docproc.generator --mode triads --out data/matched_claims --n 4
    python src/cli.py --reconcile data/matched_claims

Requires a valid API key for the configured provider (see `.env.example`) --
there is no offline mode.

The legacy CSV data-analysis agent (planner/orchestrator/sandbox tool) has
moved to `legacy/agent_csv.py` — see `LEARNING.md` for why.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

# Allow both `python src/cli.py` and `python -m src.cli`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import get_settings
from src.llm import LLMClient
from src.logging_utils import setup_logging
from src.docproc.agent import DocumentAgent
from src.docproc.ingestion.ingest import finalize_structured, ingest
from src.docproc.workflows.portfolio import PortfolioOrchestrator
from src.docproc.workflows.reconcile import ClaimReconciler
from src.docproc.schemas import PortfolioOutcome, ReconciliationReport


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Define and parse the CLI surface for all three modes (single doc,
    portfolio batch, reconciliation) plus the shared LLM/provider flags."""
    p = argparse.ArgumentParser(
        description="Extract + validate + triage payer documents.")
    p.add_argument("--doc", default=None,
                   help="Path to a single document (denial letter / EOB / remittance advice).")
    p.add_argument("--batch", default=None,
                   help="Directory of documents to triage as a portfolio (multi-agent: A).")
    p.add_argument("--reconcile", default=None,
                   help="Directory of matched claim triads to cross-check (multi-agent: B). "
                        "Expects a manifest.json from "
                        "`python -m src.docproc.generator --mode triads`.")
    p.add_argument("--model", default=None,
                   help="LiteLLM model string, e.g. groq/llama-3.3-70b-versatile.")
    p.add_argument("--temperature", type=float, default=None, help="Override temperature.")
    p.add_argument("--stream", action="store_true",
                   help="Stream reasoning steps live to stderr.")
    p.add_argument("--workers", type=int, default=1,
                   help="Concurrent documents for --batch (thread pool; I/O-bound LLM "
                        "calls parallelize well). Default 1 = sequential, unchanged order.")
    return p.parse_args(argv)


def make_stream_callback(enabled: bool):
    """Return on_event that renders live progress to stderr, or None."""
    if not enabled:
        return None

    def on_event(ev: dict):
        kind = ev.get("kind")
        if kind == "decision":
            print(f"\n[step {ev['step']}] {ev['action']}: {ev.get('thought', '')}",
                  file=sys.stderr)
        elif kind == "tool_call":
            print(f"[tool] {ev.get('tool')} found={ev.get('found')} "
                  f"not_found={ev.get('not_found')}", file=sys.stderr)
        elif kind == "validation":
            print(f"[validate] ok={ev.get('ok')} errors={ev.get('errors')}", file=sys.stderr)
        elif kind in ("final", "error", "parse_error"):
            print(f"[{kind}] {ev.get('message') or ev.get('summary', '')}", file=sys.stderr)
        elif kind == "delegate":
            label = ev.get("file") or f"{ev.get('doc_type')}: {ev.get('file')}"
            print(f"\n[delegate] {label}", file=sys.stderr)
        elif kind == "info":
            print(f"\n[info] {ev.get('message', '')}", file=sys.stderr)

    return on_event


def render_worklist(outcome: PortfolioOutcome) -> str:
    """Format a `PortfolioOutcome` as the console worklist table plus totals
    and a by-category breakdown."""
    lines = [f"\n=== PORTFOLIO WORKLIST ({len(outcome.items)} documents, ranked by $ at risk) ==="]
    for i, it in enumerate(outcome.items, 1):
        flag = "APPEAL" if it.is_appealable else ("no-appeal" if it.is_appealable is False
                                                   else it.status)
        lines.append(f"  {i}. {it.filename:<34} {flag:<10} ${it.dollars_at_risk:>10,.2f}  "
                     f"{(it.denial_category or ''):<14} {it.claim_number or ''}")
    lines.append(f"\nTotal appealable dollars at risk: ${outcome.total_dollars_at_risk:,.2f}")
    lines.append(f"Appealable claims: {outcome.appealable_count}/{len(outcome.items)}")
    if outcome.by_category:
        lines.append("\nBy category:")
        for cat, amt in sorted(outcome.by_category.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {cat:<16} ${amt:,.2f}")
    return "\n".join(lines)


def render_reconciliation(report: ReconciliationReport) -> str:
    """Format one `ReconciliationReport` as a console block: which documents
    were compared and, if not `ok`, exactly which fields disagreed and how."""
    lines = [f"\n=== RECONCILIATION: claim {report.claim_number or '(unknown)'} ==="]
    lines.append(f"documents compared: {', '.join(report.per_doc.keys())}")
    if report.ok:
        lines.append("ok: True \u2014 all cross-checked fields agree.")
    else:
        lines.append("ok: False")
        for issue in report.issues:
            lines.append(f"  [{issue.field}] {issue.message}")
    return "\n".join(lines)


def _run_single(args, settings) -> int:
    """`--doc` mode: extract + validate + triage one document and print the
    full report (fields, validation, triage). Returns 0 on `status == 'ok'`.

    Routes through `src.docproc.ingestion.ingest` first: X12 835 (`.edi`) is
    already structured EDI and skips the LLM loop entirely (deterministic
    extraction + deterministic triage); PDF/image/DOCX go through Docling to
    Markdown first; plain text is read as-is, same as always.
    """
    try:
        ingested = ingest(args.doc)
    except (OSError, RuntimeError) as exc:
        print(f"Could not read document '{args.doc}': {exc}", file=sys.stderr)
        return 2

    if ingested.kind == "structured":
        raw = open(args.doc, encoding="utf-8").read()
        outcome = finalize_structured(ingested.extraction, raw)
        print(f"[{ingested.source_note}]", file=sys.stderr)
    else:
        if ingested.source_note != "Read as plain text.":
            print(f"[{ingested.source_note}]", file=sys.stderr)
        llm = LLMClient(settings)
        on_event = make_stream_callback(args.stream)
        outcome = DocumentAgent(settings, llm).run(
            ingested.text, os.path.basename(args.doc), on_event=on_event)

    ext, tri, val = outcome.extraction, outcome.triage, outcome.validation
    print(f"\n=== EXTRACTION ({outcome.status}, {outcome.steps_used} steps) ===")
    if ext:
        for f in ["payer_name", "provider_name", "patient_name", "member_id",
                  "claim_number", "date_of_service", "total_charged", "total_allowed",
                  "total_paid", "patient_responsibility", "appeal_deadline"]:
            print(f"  {f:<24} {getattr(ext, f).value}")
        print(f"  {'denial_codes':<24} {', '.join(ext.denial_codes)}")
        print(f"  {'line_items':<24} {len(ext.line_items)}")
    print(f"\nVALIDATION: {'passed' if val and val.ok else 'failed'}")
    if val and not val.ok:
        for i in val.issues:
            print(f"  [{i.severity}] {i.field}: {i.message}")
    if tri:
        print("\n=== TRIAGE ===")
        print(f"  appealable       {tri.is_appealable}")
        print(f"  category         {tri.denial_category}")
        print(f"  dollars at risk  {tri.dollars_at_risk}")
        print(f"  action           {tri.recommended_action}")
        print(f"  rationale        {tri.rationale}")
    if outcome.trace_path:
        print(f"\nTrace: {outcome.trace_path}")
    return 0 if outcome.status == "ok" else 1


def _run_batch(args, settings) -> int:
    """Multi-agent (A): portfolio triage across every document in a directory."""
    paths = sorted(p for p in glob.glob(os.path.join(args.batch, "*.txt")))
    if not paths:
        print(f"No .txt documents found in '{args.batch}'.", file=sys.stderr)
        return 2
    llm = LLMClient(settings)
    on_event = make_stream_callback(args.stream)
    outcome = PortfolioOrchestrator(settings, llm).run(paths, on_event=on_event, max_workers=args.workers)
    print(render_worklist(outcome))
    return 0


def _run_reconcile(args, settings) -> int:
    """Multi-agent (B): cross-document reconciliation for matched claim triads."""
    manifest_path = os.path.join(args.reconcile, "manifest.json")
    if not os.path.exists(manifest_path):
        print(f"No manifest.json in '{args.reconcile}'. Generate one with:\n"
              f"  python -m src.docproc.generator --mode triads --out {args.reconcile}",
              file=sys.stderr)
        return 2
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)

    llm = LLMClient(settings)
    on_event = make_stream_callback(args.stream)
    reconciler = ClaimReconciler(settings, llm)
    n_groups = n_flagged = n_correct = 0
    for group in manifest:
        doc_paths = {doc_type: os.path.join(args.reconcile, fname)
                    for doc_type, fname in group["files"].items()}
        report = reconciler.run(doc_paths, on_event=on_event)
        print(render_reconciliation(report))

        n_groups += 1
        expected_bad = set(group.get("discrepancy_fields", []))
        found_bad = {issue.field for issue in report.issues}
        if expected_bad:
            n_flagged += 1
            if found_bad >= expected_bad:  # caught at least the injected fields
                n_correct += 1
        print(f"  (expected injected fields: {sorted(expected_bad) or 'none'}; "
              f"caught: {sorted(found_bad) or 'none'})")

    if n_flagged:
        print(f"\n=== RECONCILIATION SUMMARY: caught {n_correct}/{n_flagged} "
              f"injected discrepancies across {n_groups} claim groups ===")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Dispatch to whichever of --batch / --reconcile / --doc was given."""
    args = parse_args(argv)
    settings = get_settings(model=args.model, temperature=args.temperature)
    setup_logging(settings.log_level if not args.stream else "WARNING")

    if args.batch:
        return _run_batch(args, settings)
    if args.reconcile:
        return _run_reconcile(args, settings)
    if args.doc:
        return _run_single(args, settings)

    print("One of --doc, --batch, or --reconcile is required.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


