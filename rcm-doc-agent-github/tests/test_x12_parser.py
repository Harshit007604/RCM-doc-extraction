"""Unit tests for src/docproc/x12_parser.py::parse_835 against the real
bundled sample X12 835 EDI transaction (data/real_world/sample_835.edi) --
no LLM involved, this is a pure deterministic parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.docproc.ingestion.x12_parser import parse_835

SAMPLE_PATH = Path(__file__).resolve().parent.parent / "data" / "real_world" / "sample_835.edi"


@pytest.fixture
def raw_edi() -> str:
    return SAMPLE_PATH.read_text()


class TestParse835:
    def test_payer_and_provider_names(self, raw_edi):
        ext = parse_835(raw_edi)
        assert ext.payer_name.value == "Meridian Health Plan"
        assert ext.payer_name.source_text == "MERIDIAN HEALTH PLAN"
        assert ext.provider_name.value == "Riverbend Regional Medical Center"

    def test_claim_summary_fields(self, raw_edi):
        ext = parse_835(raw_edi)
        assert ext.claim_number.value == "CLM2234510745"
        assert ext.total_charged.value == "2306.72"
        assert ext.total_paid.value == "761.56"
        assert ext.patient_responsibility.value == "274.40"

    def test_patient_name_and_member_id(self, raw_edi):
        ext = parse_835(raw_edi)
        assert ext.patient_name.value == "Elena Ferraro"
        assert ext.member_id.value == "Z365874400"

    def test_date_of_service_normalized_to_iso(self, raw_edi):
        ext = parse_835(raw_edi)
        assert ext.date_of_service.value == "2026-04-25"
        assert ext.date_of_service.source_text == "20260425"

    def test_denial_codes_include_both_co_and_pr_prefixed(self, raw_edi):
        """Real bug precedent (LEARNING.md): a worked example that only
        showed CO-prefixed codes taught a model to under-extract PR-codes.
        The parser itself must not have that blind spot."""
        ext = parse_835(raw_edi)
        assert ext.denial_codes == ["CO-45", "CO-97", "PR-1"]

    def test_line_items_parsed_with_co_adjustment_applied_to_allowed(self, raw_edi):
        ext = parse_835(raw_edi)
        assert len(ext.line_items) == 2
        line1, line2 = ext.line_items
        assert line1.cpt_code == "99214"
        assert line1.charge_amount == 450.00
        assert line1.allowed_amount == pytest.approx(315.00)  # 450.00 - CO-45 (135.00)
        assert line1.paid_amount == 315.00

    def test_pr_group_code_does_not_reduce_allowed_amount(self, raw_edi):
        """Only CO (contractual obligation) adjustments reduce the allowed
        amount; PR (patient responsibility) is a separate liability, not a
        write-off, and must not be subtracted from `allowed_amount`."""
        ext = parse_835(raw_edi)
        line2 = ext.line_items[1]
        assert line2.cpt_code == "97110"
        # 1856.72 - CO-97 (1135.76) = 720.96; the PR-1 (274.40) CAS must NOT
        # also be subtracted here.
        assert line2.allowed_amount == pytest.approx(720.96)

    def test_total_allowed_is_derived_with_no_source_text(self, raw_edi):
        """An 835 never literally states one number for total_allowed -- it
        is computed from SVC minus CO-group CAS adjustments. Honestly
        reflect that: no source_text, because there is none to quote."""
        ext = parse_835(raw_edi)
        assert ext.total_allowed.value == "1035.96"  # 315.00 + 720.96
        assert ext.total_allowed.source_text is None

    def test_appeal_deadline_is_genuinely_absent(self, raw_edi):
        """An 835 is a payment transaction, not a denial letter -- there is
        no appeal-rights language in the EDI itself."""
        ext = parse_835(raw_edi)
        assert ext.appeal_deadline.value is None
