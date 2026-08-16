"""Unit tests for src/docproc/schemas.py -- specifically the FieldValue
numeric-coercion validator, the fix for the dominant real parse failure
(129 of 198 occurrences, see LEARNING.md 2026-08-14)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.docproc.schemas import ClaimExtraction, DocStep, FieldValue


class TestFieldValueCoercion:
    def test_bare_float_is_coerced_to_string(self):
        """Real captured failure: models emit `"value": 2306.72` (a JSON
        number) instead of `"value": "2306.72"` (a string) even though the
        prompt asks for a string. This must be coerced, not rejected."""
        fv = FieldValue(value=2306.72)
        assert fv.value == "2306.72"
        assert isinstance(fv.value, str)

    def test_bare_int_is_coerced_to_string(self):
        fv = FieldValue(value=42)
        assert fv.value == "42"

    def test_string_value_is_unaffected(self):
        fv = FieldValue(value="2306.72")
        assert fv.value == "2306.72"

    def test_none_value_is_unaffected(self):
        fv = FieldValue(value=None)
        assert fv.value is None

    def test_bool_is_NOT_silently_coerced(self):
        """Guard case: bool is an int subclass in Python. A stray
        true/false must still fail loudly, not become the string "True"."""
        with pytest.raises(ValidationError):
            FieldValue(value=True)

    def test_coercion_applies_through_nested_claim_extraction(self):
        """The validator must fire when FieldValue is nested inside
        ClaimExtraction (the real path -- DocStep.extraction.total_charged),
        not just when constructed standalone."""
        ext = ClaimExtraction(total_charged=FieldValue(value=5737.13))
        assert ext.total_charged.value == "5737.13"

    def test_coercion_applies_through_full_doc_step_json_parse(self):
        """End-to-end: a DocStep parsed from raw JSON text with a bare
        number in a value field must not raise."""
        raw = (
            '{"thought": "t", "action": "extract", '
            '"extraction": {"doc_type": "eob", '
            '"total_charged": {"value": 2306.72, "source_text": "$2,306.72"}}}'
        )
        step = DocStep.model_validate_json(raw)
        assert step.extraction.total_charged.value == "2306.72"
