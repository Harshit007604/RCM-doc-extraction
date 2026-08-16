"""Unit tests for src/docproc/validation.py -- the mechanical guardrails.

These reproduce, as real assertions, the two "break tests" run live during
the session (a fabricated dollar amount, and line items that don't sum to
the stated total) -- proving the validators actually reject bad data rather
than always returning ok=True.
"""

from __future__ import annotations

from src.docproc.schemas import ClaimExtraction, DocType, FieldValue, LineItem
from src.docproc.validation import check_arithmetic, check_grounding, validate


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
