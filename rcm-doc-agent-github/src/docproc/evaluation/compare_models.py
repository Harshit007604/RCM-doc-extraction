"""Compare multiple LLM models on the same document corpus -- the same
metrics evaluate.py already computes (F1, grounding, triage, tokens), run
back-to-back across models with a real $ cost comparison, in one command.

Run:
    python -m src.docproc.evaluation.compare_models --docs data/docs_100 --limit 10 \\
        --models openai/gpt-4.1-mini,openai/gpt-4.1-nano
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.docproc.evaluation.evaluate import evaluate  # noqa: E402


def run(models: list[str], docs_dir: str, limit: int | None) -> list[dict]:
    """Run the full eval for each model, back to back. Each model gets a
    completely fresh `DocumentAgent`/`LLMClient` (evaluate() builds its own),
    so there is no shared state between models -- this is N independent
    runs, not one run with a model-switching agent."""
    results = []
    for model in models:
        print(f"\n>>> Running {model} on {limit or 'all'} documents from {docs_dir} ...")
        results.append(evaluate(docs_dir, limit=limit, model=model, quiet=False))
    return results


def render_comparison(results: list[dict]) -> str:
    """One table, every model side by side -- the point of this script."""
    lines = ["\n" + "=" * 100, "MODEL COMPARISON", "=" * 100]
    header = (f"{'model':<24} {'F1':>6} {'ground.':>9} {'triage':>8} "
             f"{'tokens/doc':>11} {'$/doc':>10} {'s/doc':>7}")
    lines.append(header)
    lines.append("-" * len(header))
    for r in results:
        n = r["n_docs"]
        ground_pct = 100 * r["grounding_ok"] / r["grounding_total"] if r["grounding_total"] else 0
        cost_per_doc = f"${r['cost_usd'] / n:.4f}" if r["cost_usd"] is not None else "unknown"
        lines.append(
            f"{r['model']:<24} {r['f1']:>6.3f} {ground_pct:>8.1f}% "
            f"{r['triage_correct']:>4}/{n:<3} {r['total_tokens'] / n:>11,.0f} "
            f"{cost_per_doc:>10} {r['wall_s'] / n:>6.1f}s"
        )
    lines.append("=" * 100)

    # Cheapest model that still hit F1 > 0.9 and 100% triage -- the actual
    # decision this comparison exists to inform.
    viable = [r for r in results if r["f1"] > 0.9 and r["triage_correct"] == r["n_docs"]
             and r["cost_usd"] is not None]
    if viable:
        cheapest = min(viable, key=lambda r: r["cost_usd"] / r["n_docs"])
        lines.append(f"\nCheapest model meeting F1>0.9 and 100% triage: {cheapest['model']} "
                    f"(${cheapest['cost_usd'] / cheapest['n_docs']:.4f}/doc)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compare multiple models on the same corpus.")
    ap.add_argument("--models", required=True,
                    help="Comma-separated LiteLLM model strings, e.g. "
                         "openai/gpt-4.1-mini,openai/gpt-4.1-nano")
    ap.add_argument("--docs", default="data/docs")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    results = run(models, args.docs, args.limit)
    print(render_comparison(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
