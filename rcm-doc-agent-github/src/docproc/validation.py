"""Validation tools — the agent's feedback signal.

Three independent checks, each producing actionable issues the agent can fix on
the next turn. This is what makes self-correction meaningful: the agent is not
asked to "reflect", it is handed concrete, mechanical failures.

1. Grounding   — every populated field must cite a span that actually occurs in
                 the source document. This is the anti-hallucination check.
2. Arithmetic  — line items must sum to the stated totals; paid <= allowed <=
                 charged. Catches transcription and OCR-style digit errors.
3. Business    — dates ordered correctly, codes present in the CARC/RARC
                 registry, formats sane. Catches domain-invalid output.
"""

from __future__ import annotations

import re
from datetime import date

from .registry.codes import lookup_code
from .schemas import ClaimExtraction, ValidationIssue, ValidationReport

CENTS_TOLERANCE = 0.02
_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    """Collapse whitespace and lowercase, for tolerant substring comparisons."""
    return _WS.sub(" ", s or "").strip().lower()


def _num(v) -> float | None:
    """Parse a currency-ish value; return None if not numeric."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    cleaned = str(v).replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_date(v) -> date | None:
    """Try a handful of formats the corpus/documents actually use; None if
    none match (that itself becomes a validation error, not a crash)."""
    if not v:
        return None
    text = str(v).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%d-%m-%Y"):
        try:
            from datetime import datetime
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
def check_grounding(ext: ClaimExtraction, document: str) -> list[ValidationIssue]:
    """Every populated field must quote a span present in the document."""
    issues: list[ValidationIssue] = []
    doc = _norm(document)
    fields = ["payer_name", "claim_number", "member_id", "patient_name",
              "provider_name", "date_of_service", "total_charged", "total_allowed",
              "total_paid", "patient_responsibility", "appeal_deadline"]
    for name in fields:
        fv = getattr(ext, name, None)
        if fv is None or fv.value in (None, ""):
            continue
        if not fv.source_text:
            issues.append(ValidationIssue(
                field=name, severity="error",
                message="value provided without source_text; cite the exact document span."))
            continue
        if _norm(fv.source_text) not in doc:
            issues.append(ValidationIssue(
                field=name, severity="error",
                message=f"source_text {fv.source_text!r} does not occur in the document "
                        "(possible hallucination); quote text verbatim."))
    return issues


def check_arithmetic(ext: ClaimExtraction) -> list[ValidationIssue]:
    """Line items must sum to the stated totals, and paid <= allowed <= charged
    both at the claim level and per line -- catches transcription/OCR-style
    digit errors that a grounding check alone would miss (a wrong-but-quoted
    number still "occurs" in the document)."""
    issues: list[ValidationIssue] = []
    charged = _num(ext.total_charged.value)
    allowed = _num(ext.total_allowed.value)
    paid = _num(ext.total_paid.value)

    if ext.line_items:
        s_charge = sum(li.charge_amount or 0.0 for li in ext.line_items)
        if charged is not None and abs(s_charge - charged) > CENTS_TOLERANCE:
            issues.append(ValidationIssue(
                field="total_charged", severity="error",
                message=f"line items sum to {s_charge:.2f} but total_charged is {charged:.2f}."))
        s_paid = sum(li.paid_amount or 0.0 for li in ext.line_items)
        if paid is not None and abs(s_paid - paid) > CENTS_TOLERANCE:
            issues.append(ValidationIssue(
                field="total_paid", severity="error",
                message=f"line items sum to {s_paid:.2f} but total_paid is {paid:.2f}."))

    if charged is not None and allowed is not None and allowed - charged > CENTS_TOLERANCE:
        issues.append(ValidationIssue(field="total_allowed", severity="error",
                                      message="allowed exceeds charged."))
    if allowed is not None and paid is not None and paid - allowed > CENTS_TOLERANCE:
        issues.append(ValidationIssue(field="total_paid", severity="error",
                                      message="paid exceeds allowed."))
    for i, li in enumerate(ext.line_items):
        if (li.paid_amount or 0) - (li.allowed_amount or 0) > CENTS_TOLERANCE:
            issues.append(ValidationIssue(
                field=f"line_items[{i}]", severity="error",
                message=f"line {li.cpt_code}: paid exceeds allowed."))
    return issues


def check_business_rules(ext: ClaimExtraction) -> list[ValidationIssue]:
    """Domain-validity checks that arithmetic and grounding can't express:
    dates parse and are ordered correctly, denial codes exist in the
    CARC/RARC registry, member id looks like a real identifier."""
    issues: list[ValidationIssue] = []

    dos = _parse_date(ext.date_of_service.value)
    deadline = _parse_date(ext.appeal_deadline.value)
    if ext.date_of_service.value and dos is None:
        issues.append(ValidationIssue(field="date_of_service", severity="error",
                                      message="unparseable date; normalize to YYYY-MM-DD."))
    if ext.appeal_deadline.value and deadline is None:
        issues.append(ValidationIssue(field="appeal_deadline", severity="error",
                                      message="unparseable date; normalize to YYYY-MM-DD."))
    if dos and deadline and deadline <= dos:
        issues.append(ValidationIssue(field="appeal_deadline", severity="error",
                                      message="appeal deadline must fall after the date of service."))

    if not ext.denial_codes:
        issues.append(ValidationIssue(field="denial_codes", severity="warning",
                                      message="no adjustment codes captured; payer documents normally carry at least one."))
    for code in ext.denial_codes:
        if lookup_code(code) is None:
            issues.append(ValidationIssue(
                field="denial_codes", severity="error",
                message=f"code {code!r} is not in the CARC/RARC registry; re-read the document."))

    if ext.member_id.value and not re.fullmatch(r"[A-Za-z0-9\-]{6,20}", str(ext.member_id.value)):
        issues.append(ValidationIssue(field="member_id", severity="warning",
                                      message="member id has an unexpected format."))
    return issues


def validate(ext: ClaimExtraction, document: str) -> ValidationReport:
    """Run all three checks and combine them into one report. `ok` is False if
    any issue is severity='error' (warnings don't block finalize).

    Each issue is tagged with the validator that produced it (`check`) so a
    caller can weigh them differently by provenance -- see
    `ValidationIssue.check` and `store.triage_decision`.
    """
    issues = ([i.model_copy(update={"check": "grounding"}) for i in check_grounding(ext, document)]
              + [i.model_copy(update={"check": "arithmetic"}) for i in check_arithmetic(ext)]
              + [i.model_copy(update={"check": "business_rules"}) for i in check_business_rules(ext)])
    ok = not any(i.severity == "error" for i in issues)
    return ValidationReport(ok=ok, issues=issues)
