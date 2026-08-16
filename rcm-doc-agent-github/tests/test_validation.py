"""Unit tests for src/docproc/validation.py -- the mechanical guardrails.

These reproduce, as real assertions, the two "break tests" run live during
the session (a fabricated dollar amount, and line items that don't sum to
the stated total) -- proving the validators actually reject bad data rather
than always returning ok=True.
"""

from __future__ import annotations

from src.docproc.schemas import ClaimExtraction, DocType, FieldValue, LineItem
from src.docproc.validation import (
    check_arithmetic,
    check_business_rules,
    check_code_semantics,
    check_grounding,
    validate,
)


class TestGrounding:
    def test_fabricated_value_is_rejected(self, denial_letter_text):
        """A dollar amount that never appears in the document must fail
        grounding -- the anti-hallucination guarantee this project is built
        around."""
        fake = ClaimExtraction(
            doc_type=DocType.DENIAL_LETTER,
            total_charged=FieldValue(value="9999999.99", source_text="9999999.99"),
        )
        report = validate(fake, denial_letter_text)
        assert report.ok is False
        errors = [i for i in report.issues if i.severity == "error"]
        assert any(i.field == "total_charged" and i.check == "grounding" for i in errors)

    def test_missing_source_text_is_rejected(self, denial_letter_text):
        """A populated value with NO source_text at all is also a grounding
        error, not silently accepted."""
        ext = ClaimExtraction(total_charged=FieldValue(value="6027.57", source_text=None))
        issues = check_grounding(ext, denial_letter_text)
        assert any(i.field == "total_charged" and i.severity == "error" for i in issues)

    def test_real_value_from_the_document_passes(self, denial_letter_text):
        """The positive case: a value genuinely quoted in the document
        grounds cleanly -- the check isn't just permanently failing."""
        ext = ClaimExtraction(
            claim_number=FieldValue(value="CLM5070378921", source_text="CLM5070378921"),
        )
        issues = check_grounding(ext, denial_letter_text)
        assert issues == []

    def test_whitespace_and_case_are_normalized(self, denial_letter_text):
        """Grounding tolerates whitespace collapsing and case folding -- an
        LLM re-flowing multi-line text into one line shouldn't be penalized
        as a hallucination."""
        ext = ClaimExtraction(
            patient_name=FieldValue(value="Grace Whitfield", source_text="grace   whitfield"),
        )
        issues = check_grounding(ext, denial_letter_text)
        assert issues == []


class TestOcrAwareGrounding:
    """A near-miss source_text is a hard error on clean text, but becomes a
    tolerated fuzzy-match warning under confirmed poor/fair OCR confidence
    -- exact-substring grounding on OCR text false-positives on legitimate
    OCR noise and false-negatives on confidently-wrong OCR in both
    directions, per the real Docling test that motivated this."""

    def test_near_miss_is_a_hard_error_without_ocr_grade(self, denial_letter_text):
        ext = ClaimExtraction(
            claim_number=FieldValue(value="CLM5070378921", source_text="CLMS070378921"))
        issues = check_grounding(ext, denial_letter_text, ocr_low_grade=None)
        assert any(i.severity == "error" and i.field == "claim_number" for i in issues)

    def test_same_near_miss_is_a_tolerated_warning_under_poor_ocr(self, denial_letter_text):
        ext = ClaimExtraction(
            claim_number=FieldValue(value="CLM5070378921", source_text="CLMS070378921"))
        issues = check_grounding(ext, denial_letter_text, ocr_low_grade="fair")
        assert all(i.severity != "error" for i in issues)
        assert any(i.severity == "warning" and "approximate match" in i.message for i in issues)

    def test_completely_unrelated_text_is_still_rejected_even_under_poor_ocr(
            self, denial_letter_text):
        """Poor OCR confidence widens the tolerance -- it doesn't disable
        grounding entirely. A value with nothing resembling it anywhere in
        the document must still fail."""
        ext = ClaimExtraction(
            claim_number=FieldValue(value="CLM5070378921", source_text="totally unrelated text"))
        issues = check_grounding(ext, denial_letter_text, ocr_low_grade="poor")
        assert any(i.severity == "error" for i in issues)


class TestArithmetic:
    def test_line_items_not_summing_to_total_is_rejected(self):
        """Dropping a line item (or a wrong total) must fail arithmetic even
        when every individual number looks plausible in isolation."""
        bad = ClaimExtraction(
            total_charged=FieldValue(value="6027.57", source_text="6,027.57"),
            line_items=[
                LineItem(cpt_code="70450", charge_amount=3837.01, allowed_amount=0, paid_amount=0),
                LineItem(cpt_code="80053", charge_amount=1043.09, allowed_amount=500.77, paid_amount=413.36),
            ],  # missing the third line item (93010, $1,147.47)
        )
        issues = check_arithmetic(bad)
        assert any(i.field == "total_charged" for i in issues)

    def test_correct_arithmetic_passes(self):
        good = ClaimExtraction(
            total_charged=FieldValue(value="6027.57"),
            total_paid=FieldValue(value="942.81"),
            line_items=[
                LineItem(cpt_code="70450", charge_amount=3837.01, allowed_amount=0.0, paid_amount=0.0),
                LineItem(cpt_code="80053", charge_amount=1043.09, allowed_amount=500.77, paid_amount=413.36),
                LineItem(cpt_code="93010", charge_amount=1147.47, allowed_amount=737.67, paid_amount=529.45),
            ],
        )
        assert check_arithmetic(good) == []

    def test_paid_exceeding_allowed_is_rejected(self):
        bad = ClaimExtraction(
            line_items=[LineItem(cpt_code="99214", charge_amount=100.0,
                                 allowed_amount=50.0, paid_amount=75.0)],
        )
        issues = check_arithmetic(bad)
        assert any("paid exceeds allowed" in i.message for i in issues)

    def test_allowed_exceeding_charged_is_rejected(self):
        bad = ClaimExtraction(
            total_charged=FieldValue(value="100.00"),
            total_allowed=FieldValue(value="150.00"),
        )
        issues = check_arithmetic(bad)
        assert any(i.field == "total_allowed" for i in issues)


class TestBusinessRules:
    def test_denial_code_not_in_registry_is_rejected(self):
        """A denial code that doesn't resolve in the CARC/RARC registry is a
        real, mechanical error -- not a soft warning -- because the registry
        is authoritative (unlike the CPT format check below, which is only a
        shape heuristic)."""
        bad = ClaimExtraction(denial_codes=["CO-999999"])
        issues = check_business_rules(bad)
        assert any(i.field == "denial_codes" and i.severity == "error" for i in issues)

    def test_misread_cpt_code_is_flagged(self):
        """Regression test for a real finding: a synthetic handwritten-style
        document run through Docling's actual OCR misread the CPT code
        '70450' as 'F0450' -- grounding passed (the garbled text IS what's
        in the document) and nothing else caught it, since CPT codes had no
        validator at all before this check was added."""
        bad = ClaimExtraction(
            line_items=[LineItem(cpt_code="F0450", charge_amount=3837.01)],
        )
        issues = check_business_rules(bad)
        assert any("F0450" in i.message and i.field == "line_items[0].cpt_code"
                   for i in issues)

    def test_invalid_code_error_message_includes_fuzzy_candidates(self):
        """An unresolvable code's error message should hand back close
        registry matches, not just a bare rejection -- this is what lets
        the self-correction loop pick from a bounded list instead of
        guessing in the open."""
        bad = ClaimExtraction(denial_codes=["CO-19F"])
        issues = check_business_rules(bad)
        msg = next(i.message for i in issues if i.field == "denial_codes")
        assert "CO-197" in msg


class TestCodeSemantics:
    """Regression tests for the real gap found via a Docling OCR test: a
    misread denial code can drift, across self-correction attempts, to a
    DIFFERENT real registry code that's semantically wrong for the document
    (CO-197, a precertification denial, misread and re-guessed as CO-19,
    Workers' Compensation liability). check_business_rules alone can't catch
    this because CO-19 IS a real code; only a scope-consistency check can.
    """

    def test_workers_comp_scoped_code_with_no_corroborating_context_is_flagged(
            self, denial_letter_text):
        """The exact real failure case: CO-19 resolves (it's a real code)
        but nothing in a normal payer document mentions workers' comp."""
        ext = ClaimExtraction(denial_codes=["CO-19"])
        issues = check_code_semantics(ext, denial_letter_text)
        assert any("CO-19" in i.message and "workers comp" in i.message
                   for i in issues)

    def test_workers_comp_code_with_corroborating_context_is_not_flagged(self):
        """The other side of the check: if the document DOES mention workers'
        compensation context, the same code must NOT be flagged -- this is a
        consistency check, not a blanket ban on Workers' Comp codes."""
        ext = ClaimExtraction(denial_codes=["CO-19"])
        doc = "This claim was submitted under Workers' Compensation coverage."
        issues = check_code_semantics(ext, doc)
        assert issues == []

    def test_real_corpus_denial_codes_produce_zero_false_positives(self, denial_letter_text):
        """Every denial code this project's own synthetic generator actually
        uses (none are Workers' Comp/Property & Casualty scoped, confirmed
        against ground_truth.json) must never trigger this check -- a real
        false positive here would flag every clean document."""
        real_codes = ["CO-197", "CO-22", "CO-27", "CO-45", "CO-50", "CO-97",
                     "PR-1", "PR-2", "PR-3"]
        ext = ClaimExtraction(denial_codes=real_codes)
        issues = check_code_semantics(ext, denial_letter_text)
        assert issues == []

    def test_unresolvable_code_is_silently_skipped(self):
        """An invalid code is already flagged by check_business_rules --
        check_code_semantics must not also try to reason about it (and
        crash or double-report)."""
        ext = ClaimExtraction(denial_codes=["CO-999999"])
        assert check_code_semantics(ext, "any document text") == []

    def test_scope_mismatch_is_a_warning_without_ocr_grade_but_an_error_under_poor_ocr(self):
        """A scope mismatch alone is a soft signal (severity=warning); the
        SAME mismatch combined with confirmed poor/fair OCR confidence is a
        much stronger one, since it's exactly the failure mode a real
        Docling test produced -- escalated to a hard error in that case."""
        ext = ClaimExtraction(denial_codes=["CO-19"])
        doc = "Dear Provider, your claim has been denied per code CO-19."
        clean = check_code_semantics(ext, doc, ocr_low_grade=None)
        ocr = check_code_semantics(ext, doc, ocr_low_grade="poor")
        assert all(i.severity == "warning" for i in clean) and clean
        assert all(i.severity == "error" for i in ocr) and ocr

    def test_real_5_digit_cpt_code_passes(self):
        good = ClaimExtraction(
            line_items=[LineItem(cpt_code="70450", charge_amount=3837.01)],
        )
        issues = check_business_rules(good)
        assert not any("cpt_code" in i.field for i in issues)

    def test_missing_cpt_code_is_not_flagged(self):
        """A line item with no CPT code at all (None) is a different concern
        (or simply absent) -- this check only fires on a code that IS
        present but doesn't match the expected shape."""
        ext = ClaimExtraction(line_items=[LineItem(cpt_code=None, charge_amount=100.0)])
        issues = check_business_rules(ext)
        assert not any("cpt_code" in i.field for i in issues)


class TestValidateIntegration:
    def test_ok_is_false_only_on_errors_not_warnings(self, denial_letter_text):
        """A document with zero denial codes triggers a WARNING (missing
        codes), which must not by itself flip `ok` to False -- only errors
        gate finalize."""
        ext = ClaimExtraction(
            claim_number=FieldValue(value="CLM5070378921", source_text="CLM5070378921"),
        )
        report = validate(ext, denial_letter_text)
        assert any(i.severity == "warning" for i in report.issues)
        assert report.ok is True

    def test_every_issue_is_tagged_with_its_check(self, denial_letter_text):
        """Regression test for the EDI-review-flood bug: every issue must
        carry a `check` so callers (store.triage_decision) can weigh
        grounding differently from arithmetic/business-rule failures."""
        fake = ClaimExtraction(total_charged=FieldValue(value="1.00", source_text="not-in-doc"))
        report = validate(fake, denial_letter_text)
        assert all(i.check in ("grounding", "arithmetic", "business_rules") for i in report.issues)
