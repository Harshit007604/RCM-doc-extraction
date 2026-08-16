"""Cross-document reconciliation — the multi-agent case a single document
cannot cover.

Every other validator in this project (`validation.py`) checks ONE document
against itself: does a value's span occur in the text, do the numbers add up,
are the codes real. None of those can catch a claim where the denial letter,
the EOB, and the remittance advice individually validate perfectly but
disagree with EACH OTHER — e.g. a later remittance shows a smaller total_paid
than the EOB already told the provider, because of an intervening take-back.
Catching that requires more than one agent's output to exist at the same
time; it is a genuinely different kind of check, not a bigger single-document
validator.

Usage: extract each document in a claim group independently (fresh
`DocumentAgent` per document — see `src/docproc/workflows/portfolio.py` for
the same "fresh specialist per input" pattern applied to a batch instead of
a claim group), then call `reconcile()` on the resulting extractions.
"""

from __future__ import annotations

import os
import re

from src.config import Settings
from src.llm import LLMClient
from ..agent import DocumentAgent
from ..schemas import ClaimExtraction, ReconciliationIssue, ReconciliationReport

# Fields worth cross-checking. Line items are deliberately excluded: layouts
# vary enough (letter vs. table vs. EDI-summary) that per-line comparison
# would need CPT-level matching, a bigger job than this first pass.
RECONCILE_FIELDS = ["claim_number", "total_charged", "total_allowed",
                    "total_paid", "patient_responsibility", "date_of_service"]
MONEY_FIELDS = {"total_charged", "total_allowed", "total_paid", "patient_responsibility"}
CENTS_TOLERANCE = 0.02
_WS = re.compile(r"\s+")


def _num(v) -> float | None:
    """Parse a currency-ish value; None if not numeric (mirrors validation.py's
    helper -- kept separate since this module has no import on that one)."""
    if v is None:
        return None
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def _norm(v) -> str:
    """Collapse whitespace and lowercase, for tolerant string-field comparisons."""
    return _WS.sub(" ", str(v or "")).strip().lower()


def reconcile(extractions: dict[str, ClaimExtraction]) -> ReconciliationReport:
    """`extractions`: doc_type -> ClaimExtraction, each already individually
    validated. Compares the fields in RECONCILE_FIELDS pairwise across all
    documents in the group and flags any that disagree.
    """
    issues: list[ReconciliationIssue] = []

    for field in RECONCILE_FIELDS:
        raw = {doc: getattr(ext, field).value for doc, ext in extractions.items()}
        present = {doc: v for doc, v in raw.items() if v not in (None, "")}
        if len(present) < 2:
            continue  # nothing to cross-check if only one document has it

        if field in MONEY_FIELDS:
            nums = {doc: _num(v) for doc, v in present.items()}
            distinct = {round(n, 2) for n in nums.values() if n is not None}
            if len(distinct) > 1:
                issues.append(ReconciliationIssue(
                    field=field, values={doc: f"{n:.2f}" for doc, n in nums.items()},
                    message=f"{field} disagrees across documents: " +
                            ", ".join(f"{doc}={n:.2f}" for doc, n in nums.items())))
        else:
            normed = {doc: _norm(v) for doc, v in present.items()}
            if len(set(normed.values())) > 1:
                issues.append(ReconciliationIssue(
                    field=field, values={doc: str(v) for doc, v in present.items()},
                    message=f"{field} disagrees across documents: " +
                            ", ".join(f"{doc}={v}" for doc, v in present.items())))

    claim_number = next(
        (ext.claim_number.value for ext in extractions.values() if ext.claim_number.value), None)
    return ReconciliationReport(claim_number=claim_number, ok=not issues,
                                issues=issues, per_doc=extractions)


class ClaimReconciler:
    """Orchestrates one claim group: a fresh `DocumentAgent` specialist per
    document (denial letter / EOB / remittance advice for the SAME claim),
    then `reconcile()` across the results. Same "fresh specialist per input"
    shape as `PortfolioOrchestrator`, applied to one claim's documents instead
    of a batch of unrelated ones.
    """

    def __init__(self, settings: Settings, llm: LLMClient):
        """Store the shared config/LLM client; each `run()` call creates its
        own fresh `DocumentAgent` instances."""
        self.s = settings
        self.llm = llm

    def run(self, doc_paths: dict[str, str], on_event=None) -> ReconciliationReport:
        """`doc_paths`: doc_type -> file path (e.g. {"denial_letter": "...",
        "eob": "...", "remittance_advice": "..."})."""
        extractions: dict[str, ClaimExtraction] = {}
        for doc_type, path in doc_paths.items():
            if on_event:
                on_event({"kind": "delegate", "doc_type": doc_type, "file": path})
            document = open(path, encoding="utf-8").read()
            agent = DocumentAgent(self.s, self.llm)
            outcome = agent.run(document, os.path.basename(path), on_event=on_event)
            if outcome.extraction is not None:
                extractions[doc_type] = outcome.extraction
        return reconcile(extractions)

