"""Demo: extract a ClaimExtraction from a REAL public data source — a raw
X12 835 (Electronic Remittance Advice) transaction — instead of the synthetic
prose documents in data/docs/.

Why this is a genuinely different extraction problem, not just a new input
file: the 835 is segment/element-delimited EDI (HIPAA 5010, X12.org), not
prose. There is nothing here for an LLM prompt or a prose regex to find a
"payer name" *sentence* in — the extractor has to know the segment grammar
(N1*PR = payer, CLP = claim summary, SVC = service line, CAS = adjustment)
the same way a prose extractor has to know where a denial letter puts its
claim number.

The point of this script is to reuse the project's EXISTING schema and
validators (src/docproc/schemas.py, src/docproc/validation.py) unchanged —
only the parser at the front is new. That is the actual architectural claim
the report makes ("verification is mechanical, independent of how the value
was produced") put under a real test.

The parser itself now lives in `src/docproc/ingestion/x12_parser.py` (promoted
there so the document-ingestion router can call it directly for `.edi`
files); this script is just the narrated demo.

Run:
    python scripts/extract_x12_835.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.docproc.validation import validate  # noqa: E402
from src.docproc.ingestion.x12_parser import parse_835  # noqa: E402

DOC_PATH = Path(__file__).resolve().parent.parent / "data" / "real_world" / "sample_835.edi"


def main() -> None:
    """Parse the bundled sample 835, run it through the project's real
    validators, and print both -- the end-to-end demo this script exists for."""
    raw = DOC_PATH.read_text()
    ext = parse_835(raw)

    print(f"=== EXTRACTION from {DOC_PATH.name} (X12 835, real EDI grammar) ===")
    for field in ["payer_name", "provider_name", "patient_name", "member_id",
                  "claim_number", "date_of_service", "total_charged",
                  "total_allowed", "total_paid", "patient_responsibility"]:
        fv = getattr(ext, field)
        print(f"  {field:<24}{fv.value}")
    print(f"  {'denial_codes':<24}{', '.join(ext.denial_codes)}")
    print(f"  {'line_items':<24}{len(ext.line_items)}")
    for li in ext.line_items:
        print(f"    - {li.cpt_code}: charge={li.charge_amount} allowed={li.allowed_amount:.2f} paid={li.paid_amount}")

    report = validate(ext, raw)
    print(f"\n=== VALIDATION (same validators as the prose pipeline) ===")
    print(f"ok: {report.ok}")
    for issue in report.issues:
        print(f"  {issue.severity:<8}{issue.field:<24}{issue.message}")


if __name__ == "__main__":
    main()
