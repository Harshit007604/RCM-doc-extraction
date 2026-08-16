"""Document ingestion router.

Why this exists: real payer correspondence arrives in wildly different
formats -- plain text, digital or scanned PDF, DOCX, images, or already-
structured X12 835 EDI. Feeding all of them through the LLM extraction loop
wastes a paid call (and adds a hallucination surface) on data that's already
deterministic. This router picks the cheapest trustworthy extractor for each
source type instead of treating "call an LLM" as the only tool:

  .edi/.835/.x12   -> `x12_parser.parse_835` -- already-structured EDI, zero
                       LLM calls needed for extraction OR triage (both are
                       fully mechanical: registry lookup + arithmetic).
  .pdf/.docx/image -> Docling converts to Markdown; the LLM agent then reads
                       that Markdown exactly like a .txt document. Docling
                       handles OCR/layout/tables; the LLM handles semantic
                       normalization the layout parser can't (which field is
                       the claim-level total, what a payer's odd wording
                       means, which code is the actionable one).
  .txt / anything else -> read as-is (the original, unchanged behavior).

Requires the optional `docling` dependency only for the PDF/DOCX/image path;
`.edi` and `.txt` never need it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..registry.codes import derive_triage, primary_denial_code
from ..schemas import ClaimExtraction, DocOutcome, Triage
from ..validation import validate
from .x12_parser import parse_835

EDI_EXTENSIONS = {".edi", ".835", ".x12"}
DOCLING_EXTENSIONS = {".pdf", ".docx", ".pptx", ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp"}


@dataclass
class IngestResult:
    """What the router produced, and what the caller should do next."""

    kind: str                                    # "text" | "structured"
    text: str | None = None                      # feed to DocumentAgent.run() as-is
    extraction: ClaimExtraction | None = None     # already complete; no LLM needed
    source_note: str = ""                         # what happened, for logging/UI
    ocr_grade: str | None = None                  # Docling's own quality grade, if applicable
    ocr_low_grade: str | None = None              # Docling's worst-5th-percentile grade


def ingest(path: str) -> IngestResult:
    """Route one document by file extension to its cheapest trustworthy
    extractor. Raises `RuntimeError` with an install hint if a PDF/image/DOCX
    is given but `docling` isn't installed."""
    ext = os.path.splitext(path)[1].lower()

    if ext in EDI_EXTENSIONS:
        raw = open(path, encoding="utf-8").read()
        extraction = parse_835(raw)
        return IngestResult(kind="structured", extraction=extraction,
                            source_note=f"Parsed as X12 835 EDI ({ext}); no LLM call needed "
                                        "for extraction or triage.")

    if ext in DOCLING_EXTENSIONS:
        return _ingest_with_docling(path, ext)

    text = open(path, encoding="utf-8").read()
    return IngestResult(kind="text", text=text, source_note="Read as plain text.")


def _ingest_with_docling(path: str, ext: str) -> IngestResult:
    """Convert a PDF/DOCX/image via Docling, and CARRY THROUGH its confidence
    report instead of discarding it.

    Docling computes four component scores (ocr/layout/parse/table) and two
    aggregate grades (`mean_grade`, `low_grade`) for exactly this purpose --
    its own docs list "identify documents requiring manual review" and "set
    confidence thresholds for unattended batch conversions" as the intended
    use cases (docling-project.github.io/docling/concepts/confidence_scores).
    Before this function existed, `ingest()` called `export_to_markdown()`
    and threw the rest of `ConversionResult` away -- meaning a badly-OCR'd
    scan and a clean digital PDF looked identical to everything downstream:
    the LLM, the validators, and the review policy had no way to know the
    transcription itself might be wrong. Grounding can only confirm the
    LLM's extraction matches Docling's OUTPUT; it says nothing about whether
    that output matches the real document.
    """
    try:
        from docling.document_converter import DocumentConverter  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            f"Reading '{ext}' documents requires the optional `docling` dependency. "
            "Install it with: pip install docling"
        ) from exc
    converter = DocumentConverter()
    result = converter.convert(path)
    text = result.document.export_to_markdown()

    grade = low_grade = None
    confidence = getattr(result, "confidence", None)
    if confidence is not None:
        grade = getattr(confidence.mean_grade, "value", None)
        low_grade = getattr(confidence.low_grade, "value", None)

    note = f"Converted via Docling ({ext} -> Markdown)."
    if grade:
        note += f" OCR/layout quality: mean={grade}, worst-5%={low_grade}."
    return IngestResult(kind="text", text=text, source_note=note,
                        ocr_grade=grade, ocr_low_grade=low_grade)


def finalize_structured(ext: ClaimExtraction, raw: str) -> DocOutcome:
    """For already-structured input (currently: X12 835): skip the LLM loop
    entirely. Extraction came straight from the EDI grammar; triage is
    derived the same mechanical way `DocumentAgent._finalize_node` overrides
    an LLM's triage -- registry lookup + arithmetic, not a model call.
    """
    report = validate(ext, raw)
    appealable, category = derive_triage(ext.denial_codes)
    at_risk = round(sum(
        li.charge_amount or 0.0 for li in ext.line_items
        if not (li.allowed_amount or 0.0)), 2)
    primary = primary_denial_code(ext.denial_codes)
    triage = Triage(
        is_appealable=appealable, denial_category=category, dollars_at_risk=at_risk,
        recommended_action=primary.typical_action if primary else "Manual review required.",
        rationale=(f"{primary.code}: {primary.description}" if primary
                   else "No registry codes resolved."),
    )
    return DocOutcome(status="ok" if report.ok else "incomplete",
                      extraction=ext, triage=triage, validation=report, steps_used=0)
