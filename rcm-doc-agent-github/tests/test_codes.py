"""Unit tests for src/docproc/registry/codes.py -- the CARC/RARC registry, the
two-tier lookup (curated 16 + real X12 fallback), and the mechanical triage
derivation.
"""

from __future__ import annotations

from src.docproc.registry.codes import (
    NON_APPEALABLE_CATEGORIES,
    _bare_carc_number,
    _categorize_carc,
    derive_triage,
    lookup_code,
    lookup_codes,
    primary_denial_code,
)


class TestCuratedRegistry:
    def test_known_curated_code_resolves(self):
        info = lookup_code("CO-45")
        assert info is not None
        assert info.category == "contractual"
        assert info.appealable is False

    def test_tolerates_missing_dash(self):
        """CO45 and CO-45 must resolve to the same entry."""
        assert lookup_code("CO45").code == lookup_code("CO-45").code

    def test_unknown_code_returns_none(self):
        assert lookup_code("CO-XYZ999") is None

    def test_empty_or_none_code_returns_none(self):
        assert lookup_code("") is None


class TestRealX12Fallback:
    """Codes NOT in the curated 16 must still resolve via the real,
    x12.org-fetched registry (297 codes) -- this is the fix that replaced
    "None" with a genuine, sourced description for ~280 real codes."""

    def test_code_outside_curated_set_resolves_via_real_registry(self):
        info = lookup_code("CO-96")
        assert info is not None
        assert "non-covered" in info.description.lower()

    def test_group_prefix_pr_resolves_the_same_bare_carc(self):
        """PR-96 and CO-96 are the same underlying CARC (96); only the group
        code (who's liable) differs -- both must resolve to the real
        description."""
        assert lookup_code("PR-96").description == lookup_code("CO-96").description

    def test_invalid_code_still_returns_none(self):
        """The fallback must never invent a meaning for a genuinely
        nonexistent code."""
        assert lookup_code("CO-XYZ999") is None

    def test_letter_prefixed_real_carc_is_not_confused_with_a_group_code(self):
        """Regression test for a real bug caught before it shipped: some
        real CARC codes have their own letter prefix (A1, B4, P12...). A
        naive `lstrip` on group-code characters would silently corrupt these.
        `_bare_carc_number` must only strip CO-/PR-/OA-/PI-/CR- on an EXPLICIT
        dash, and never touch a code with no dash."""
        assert _bare_carc_number("A1") == "A1"
        assert _bare_carc_number("CO-A1") == "A1"
        assert _bare_carc_number("PR-96") == "96"
        assert _bare_carc_number("P12") == "P12"

    def test_deductible_coinsurance_copay_always_contractual(self):
        """CARC 1/2/3 (deductible/coinsurance/copay) are patient cost-share,
        never a real denial, regardless of the exact wording."""
        assert _categorize_carc("1", "Deductible Amount") == "contractual"
        assert _categorize_carc("2", "Coinsurance Amount") == "contractual"
        assert _categorize_carc("3", "Co-payment Amount") == "contractual"

    def test_unclassifiable_description_defaults_to_documentation(self):
        """When no keyword matches, the heuristic must default to a
        manual-review category rather than guessing a specific one."""
        assert _categorize_carc("999", "Some entirely novel adjustment text.") == "documentation"


class TestLookupCodes:
    def test_batch_form_is_keyed_by_original_string(self):
        results = lookup_codes(["CO-45", "CO-XYZ999"])
        assert set(results.keys()) == {"CO-45", "CO-XYZ999"}
        assert results["CO-45"] is not None
        assert results["CO-XYZ999"] is None


class TestDeriveTriage:
    def test_appealable_code_wins_over_non_appealable(self):
        """CO-45 (contractual, non-appealable) and CO-197 (authorization,
        appealable) together -> the appealable one should drive the verdict."""
        appealable, category = derive_triage(["CO-45", "CO-197"])
        assert appealable is True
        assert category == "authorization"

    def test_only_non_appealable_codes_present(self):
        appealable, category = derive_triage(["CO-45", "PR-1"])
        assert appealable is False
        assert category in NON_APPEALABLE_CATEGORIES

    def test_no_resolvable_codes_returns_unknown(self):
        appealable, category = derive_triage(["CO-XYZ999"])
        assert appealable is False
        assert category == "unknown"

    def test_empty_list_returns_unknown(self):
        assert derive_triage([]) == (False, "unknown")

    def test_primary_denial_code_prefers_first_appealable_in_order(self):
        primary = primary_denial_code(["CO-45", "CO-197", "CO-97"])
        assert primary is not None
        assert primary.code == "CO-197"  # first appealable, in the given order
