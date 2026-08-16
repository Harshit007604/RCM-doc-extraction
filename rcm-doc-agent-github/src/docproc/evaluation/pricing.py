"""Real per-model token pricing, for computing actual dollar cost from
measured token usage (src/docproc/evaluation/evaluate.py, compare_models.py).

Prices fetched live from developers.openai.com/api/docs/pricing during this
project's cost research (see reports/cheap_extraction_research.md). Prices
change over time -- these are a snapshot, not guaranteed current; re-verify
before using this for real budgeting decisions. `estimate_cost` returns
`None` (never a guessed number) for any model not in this table, so an
unrecognized model silently costing "$0.00" in a report is impossible.
"""

from __future__ import annotations

# (input $ / 1M tokens, output $ / 1M tokens)
PRICING_PER_1M: dict[str, tuple[float, float]] = {
    "openai/gpt-4.1": (2.00, 8.00),
    "openai/gpt-4.1-mini": (0.40, 1.60),
    "openai/gpt-4.1-nano": (0.10, 0.40),
    "openai/gpt-4o-mini": (0.15, 0.60),
}


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """Real $ cost from real token counts. Returns None for an unrecognized
    model -- never fabricates a number for a model not in `PRICING_PER_1M`."""
    rates = PRICING_PER_1M.get(model)
    if rates is None:
        return None
    input_rate, output_rate = rates
    return (prompt_tokens * input_rate + completion_tokens * output_rate) / 1_000_000
