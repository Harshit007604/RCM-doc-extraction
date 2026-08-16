"""Portfolio-level triage orchestration (multi-agent, design A).

An RCM analyst doesn't work one denial at a time — they work a queue. This is
the direct extraction-domain analog of the CSV agent's planner/synthesizer
pattern (`legacy/orchestrator.py`): a batch of documents is delegated to a
fresh `DocumentAgent` specialist per document (unchanged, stateless, exactly
the single-document agent), and a synthesizer ranks the results into a
worklist by dollars-at-risk — the thing a limited-staff team actually needs
to decide what to work first.

    [doc_1, doc_2, ... doc_N] --> fresh DocumentAgent each --> N outcomes
                                                                   |
                                                                   v
                                          synthesizer: rank by $ at risk,
                                          group appealable $ by category

`max_workers` controls concurrency: each `DocumentAgent.run()` call is
dominated by network-bound LLM API latency, not local CPU, so a thread pool
(not multiprocessing) is the right tool -- documents are fully independent
(fresh agent, fresh trace file per document), so there's no shared state to
race on. This is the first, smallest step in the enterprise-scalability path
described in README.md's "Scaling this to enterprise volume" section: a
single-process worker pool today, a distributed queue of the same
fresh-agent-per-document worker function tomorrow.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from src.config import Settings
from src.llm import LLMClient
from ..agent import DocumentAgent
from ..schemas import DocOutcome, PortfolioOutcome, WorklistItem


class PortfolioOrchestrator:
    """Planner + synthesizer over a batch of documents. No LLM call of its own
    -- it delegates each document to a fresh `DocumentAgent` and does the
    ranking/aggregation itself, deterministically."""

    def __init__(self, settings: Settings, llm: LLMClient):
        self.s = settings
        self.llm = llm

    def run(self, doc_paths: list[str], on_event=None, max_workers: int = 1) -> PortfolioOutcome:
        """Extract + triage every document in `doc_paths`, then rank the
        batch by dollars-at-risk and roll up totals by category.

        `max_workers=1` (default) preserves the original sequential order and
        per-document `on_event` streaming exactly. `max_workers>1` processes
        documents concurrently via a thread pool -- real wall-clock speedup
        for I/O-bound (LLM API) work, verified in LEARNING.md. Streaming is
        disabled in concurrent mode (events from different documents would
        interleave with no way to tell which document a "decision" belongs
        to); each document still writes its own independent JSONL trace.
        """
        if max_workers <= 1:
            items = [self._process_one(path, on_event) for path in doc_paths]
        else:
            if on_event:
                on_event({"kind": "info",
                          "message": f"Concurrent batch mode ({max_workers} workers): "
                                     "live streaming disabled, per-document traces still written."})
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                items = list(pool.map(lambda p: self._process_one(p, on_event=None), doc_paths))

        items.sort(key=lambda it: it.dollars_at_risk, reverse=True)

        by_category: dict[str, float] = {}
        for it in items:
            if it.is_appealable and it.denial_category:
                by_category[it.denial_category] = (
                    by_category.get(it.denial_category, 0.0) + it.dollars_at_risk)

        return PortfolioOutcome(
            items=items,
            total_dollars_at_risk=round(sum(v for v in by_category.values()), 2),
            appealable_count=sum(1 for it in items if it.is_appealable),
            by_category={k: round(v, 2) for k, v in by_category.items()},
        )

    def _process_one(self, path: str, on_event) -> WorklistItem:
        """Extract + triage a single document with a fresh `DocumentAgent` --
        the unit of work each thread-pool worker runs independently."""
        filename = os.path.basename(path)
        if on_event:
            on_event({"kind": "delegate", "file": filename})
        document = open(path, encoding="utf-8").read()
        agent = DocumentAgent(self.s, self.llm)
        outcome = agent.run(document, filename, on_event=on_event)
        return self._to_item(filename, outcome)

    @staticmethod
    def _to_item(filename: str, outcome: DocOutcome) -> WorklistItem:
        """Reduce one document's full `DocOutcome` down to the worklist row
        fields (drops the extraction/validation detail the CLI/UI show separately)."""
        ext, tri = outcome.extraction, outcome.triage
        return WorklistItem(
            filename=filename,
            status=outcome.status,
            claim_number=ext.claim_number.value if ext else None,
            payer_name=ext.payer_name.value if ext else None,
            is_appealable=getattr(tri, "is_appealable", None),
            denial_category=getattr(tri, "denial_category", None),
            dollars_at_risk=getattr(tri, "dollars_at_risk", 0.0) or 0.0,
            recommended_action=getattr(tri, "recommended_action", None),
        )
