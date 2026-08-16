"""Validation tools — the agent's feedback signal.

Four independent checks, each producing actionable issues the agent can fix on
the next turn. This is what makes self-correction meaningful: the agent is not
asked to "reflect", it is handed concrete, mechanical failures.

1. Grounding   — every populated field must cite a span that actually occurs in
                 the source document. This is the anti-hallucination check.
2. Arithmetic  — line items must sum to the stated totals; paid <= allowed <=
                 charged. Catches transcription and OCR-style digit errors.
3. Business    — dates ordered correctly, codes present in the CARC/RARC
                 registry, formats sane. Catches domain-invalid output.
4. Semantic    — a resolved code can still be semantically wrong for THIS
                 document (a real code, scope-restricted to Workers' Comp/
                 Property & Casualty, with no corroborating context anywhere).
"""

from __future__ import annotations

import difflib
import re
from datetime import date

from .registry.codes import lookup_code, resolve_fuzzy
from .schemas import ClaimExtraction, ValidationIssue, ValidationReport

CENTS_TOLERANCE = 0.02
_WS = re.compile(r"\s+")
_POOR_OCR_GRADES = {"poor", "fair"}


def _norm(s: str) -> str:
    """Collapse whitespace and lowercase, for tolerant substring comparisons."""
    return _WS.sub(" ", s or "").strip().lower()


def _fuzzy_occurs(needle: str, haystack: str, min_ratio: float = 0.82) -> bool:
    """Approximate substring search: does some window of `haystack` roughly
    the length of `needle` match it above `min_ratio` (difflib's
    SequenceMatcher ratio, stdlib -- no new dependency)? Only ever called
    when the source page's OCR confidence is poor/fair (see `check_grounding`
    below) -- exact substring containment is both too strict for genuinely
    garbled-but-legitimate OCR noise and gives no partial credit; this sits
    between "exact match" and "no check at all," not a replacement for exact
    matching on clean text."""
    n = len(needle)
    if n == 0 or not haystack:
        return False
    step = max(1, n // 4)
    best = 0.0
    for start in range(0, max(1, len(haystack) - n + 1), step):
        window = haystack[start:start + n]
        ratio = difflib.SequenceMatcher(None, needle, window).ratio()
        if ratio > best:
            best = ratio
        if best >= min_ratio:
            return True
    return best >= min_ratio


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
def check_grounding(ext: ClaimExtraction, document: str,
                    ocr_low_grade: str | None = None) -> list[ValidationIssue]:
    """Every populated field must quote a span present in the document.

    `ocr_low_grade` (Docling's worst-5th-percentile quality grade, when this
    document came through the OCR path) changes what "grounded" means: on
    clean text, an exact substring miss IS a real anti-hallucination signal.
    On confirmed poor/fair OCR, exact-substring grounding is close to
    meaningless in both directions -- it false-positives on legitimate OCR
    normalization (a genuinely correct value the OCR just rendered with
    different noise) and false-negatives on confidently-wrong OCR (a
    plausible-looking but garbled span that happens to still be an exact
    substring of the -- equally garbled -- source). So under poor/fair OCR,
    an exact miss falls back to a fuzzy approximate match
    (`_fuzzy_occurs`) as a `warning`, not a hard `error` -- still checked,
    just not held to a standard the OCR layer itself can't meet.
    """
    issues: list[ValidationIssue] = []
    doc = _norm(document)
    ocr_uncertain = ocr_low_grade in _POOR_OCR_GRADES
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
        needle = _norm(fv.source_text)
        if needle in doc:
            continue
        if ocr_uncertain and _fuzzy_occurs(needle, doc):
            issues.append(ValidationIssue(
                field=name, severity="warning",
                message=f"source_text {fv.source_text!r} is not an exact match, but the source "
                        f"page's OCR confidence is {ocr_low_grade!r} -- accepted as an approximate "
                        f"match rather than rejected outright; verify against the original scan."))
            continue
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
            candidates = resolve_fuzzy(code)
            hint = (" Close registry matches: " +
                   ", ".join(f"{c} ({d[:60]}...)" if len(d) > 60 else f"{c} ({d})"
                             for c, d, _dist in candidates[:3])) if candidates else ""
            issues.append(ValidationIssue(
                field="denial_codes", severity="error",
                message=f"code {code!r} is not in the CARC/RARC registry; re-read the document.{hint}"))

    # CPT procedure codes have no registry to look up against (unlike CARC/
    # RARC denial codes above), so this is a format-plausibility check, not
    # an authoritative one -- same tier as the member_id check below. Added
    # after a real Docling OCR test on a handwritten-style document misread
    # a real CPT code ("70450" -> "F0450") with NO existing validator
    # catching it: grounding passed (the garbled text IS what's in the
    # document), and nothing else even looked at this field. A standard CPT
    # code is 5 digits (AMA CPT-4); this won't catch every OCR error, but it
    # catches exactly the digit/letter-confusion class the real test found.
    for i, li in enumerate(ext.line_items):
        if li.cpt_code and not re.fullmatch(r"\d{5}", li.cpt_code):
            issues.append(ValidationIssue(
                field=f"line_items[{i}].cpt_code", severity="warning",
                message=f"{li.cpt_code!r} is not a standard 5-digit CPT code; "
                        f"possible OCR/transcription error -- re-check against the source."))

    if ext.member_id.value and not re.fullmatch(r"[A-Za-z0-9\-]{6,20}", str(ext.member_id.value)):
        issues.append(ValidationIssue(field="member_id", severity="warning",
                                      message="member id has an unexpected format."))
    return issues


# Real, scope-restriction phrases pulled straight from the official X12
# description text (see carc_codes.py) -- NOT a fabricated keyword list.
# ~30/297 real CARC codes carry an explicit "to be used for X only" (or
# equivalent) restriction; the two checked here are the ones with a common,
# checkable corroborating signal a normal payer document would show
# somewhere if it genuinely applied. Verified against the real 9-document
# corpus: none of its ground-truth codes (CO-197/22/27/45/50/97, PR-1/2/3)
# are scope-restricted, so this adds zero false-positive risk there.
#
# Known limitation, found while testing this exact check: corroboration is
# a plain substring search, so a NEGATED mention ("this is NOT a workers'
# compensation claim") would still substring-match "workers compensation"
# and be (wrongly) treated as corroborating. Not fixed here -- real payer
# documents essentially never phrase it that way in practice, but a
# document that genuinely did would produce a false negative (missed
# semantic issue), not a false positive.
_SCOPE_SIGNALS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "workers_comp": (
        ("workers' compensation", "workers compensation", "work-related injury",
         "work related injury", "wc carrier", "workers' comp"),
        ("workers' compensation", "workers comp", "work-related", "work related",
         "on-the-job", "occupational injury", "wc claim"),
    ),
    "property_casualty": (
        ("property and casualty",),
        ("property and casualty", "auto accident", "motor vehicle accident",
         "third party liability"),
    ),
}


def check_code_semantics(ext: ClaimExtraction, document: str,
                         ocr_low_grade: str | None = None) -> list[ValidationIssue]:
    """Domain-consistency check `check_business_rules` structurally can't
    express: a code can be a REAL registry entry and still be semantically
    wrong for THIS document -- the exact gap a real Docling OCR test found
    (a misread `CO-197` drifted, across self-correction attempts, to `CO-19`
    -- a real, valid code, but "Workers' Compensation liability," completely
    unrelated to the actual precertification denial).

    Approach: the official X12 description for ~30/297 codes explicitly
    restricts scope ("to be used for Workers' Compensation only", etc.).
    If a resolved code carries one of those restrictions, a document that
    genuinely warrants it would show SOME corroborating mention of that
    scope somewhere (workers' comp, an auto/property claim) -- if the whole
    document shows zero such signal, that's a real, checkable inconsistency,
    not a guess. Silent (no issue) when a code has no scope restriction at
    all, which is the overwhelming majority -- this deliberately does NOT
    try to match narrative text near the code, because this project's own
    document templates are terse, label-only tables with no such narrative
    to match against (verified against all 3 real templates); a check that
    required narrative agreement would never fire on THIS corpus at all.

    `ocr_low_grade`: on confirmed poor/fair OCR, a scope mismatch is a much
    stronger signal (it's exactly the failure mode a real Docling test
    produced) -- escalated to `error` instead of `warning` in that case,
    since "real code, wrong scope, AND unreliable transcription" together
    are past the point of a soft warning.
    """
    issues: list[ValidationIssue] = []
    doc_lower = document.lower()
    severity = "error" if ocr_low_grade in _POOR_OCR_GRADES else "warning"
    for code in ext.denial_codes:
        info = lookup_code(code)
        if info is None:
            continue  # already flagged by check_business_rules
        desc_lower = info.description.lower()
        for scope, (desc_phrases, doc_phrases) in _SCOPE_SIGNALS.items():
            if not any(p in desc_lower for p in desc_phrases):
                continue
            if any(p in doc_lower for p in doc_phrases):
                continue  # document corroborates this scope -- consistent
            issues.append(ValidationIssue(
                field="denial_codes", severity=severity,
                message=(f"code {code!r} resolves to a real registry entry restricted to "
                         f"{scope.replace('_', ' ')} claims ({info.description[:100]}...), but "
                         f"nothing else in the document indicates this is a {scope.replace('_', ' ')} "
                         f"claim -- possible OCR/transcription error (a similar-looking code may "
                         f"have been misread); re-check against the source.")))
    return issues


def validate(ext: ClaimExtraction, document: str,
            ocr_low_grade: str | None = None) -> ValidationReport:
    """Run all four checks and combine them into one report. `ok` is False if
    any issue is severity='error' (warnings don't block finalize).

    `ocr_low_grade` (Docling's worst-5th-percentile confidence grade, `None`
    for non-OCR text) makes grounding fuzzy-tolerant and the semantic check
    stricter when the source page's own OCR confidence is poor/fair -- see
    each check's docstring for why those are opposite directions, not a
    contradiction: grounding EXACT-match is too strict on garbled text;
    a SCOPE mismatch is a stronger signal, not a weaker one, once OCR
    confidence is already known to be unreliable.

    Each issue is tagged with the validator that produced it (`check`) so a
    caller can weigh them differently by provenance -- see
    `ValidationIssue.check` and `store.triage_decision`.
    """
    issues = ([i.model_copy(update={"check": "grounding"}) for i in check_grounding(ext, document, ocr_low_grade)]
              + [i.model_copy(update={"check": "arithmetic"}) for i in check_arithmetic(ext)]
              + [i.model_copy(update={"check": "business_rules"}) for i in check_business_rules(ext)]
              + [i.model_copy(update={"check": "semantic"}) for i in check_code_semantics(ext, document, ocr_low_grade)])
    ok = not any(i.severity == "error" for i in issues)
    return ValidationReport(ok=ok, issues=issues)
