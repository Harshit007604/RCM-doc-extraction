"""Denial-code registry (CARC / RARC).

This is the agent's domain-knowledge tool. Payer documents carry claim
adjustment reason codes; knowing what a code *means* — and whether it is
appealable — is what turns raw extraction into an actionable triage decision.

Modelled on the public X12 CARC/RARC code sets. Trimmed to a representative
subset; in production this would be the full published set loaded from a
maintained table, refreshed on the X12 update cadence.
"""

from __future__ import annotations

from pydantic import BaseModel

from .carc_codes import RAW_CARC


class DenialCode(BaseModel):
    code: str
    kind: str            # "CARC" | "RARC"
    description: str
    category: str        # coverage | coding | authorization | eligibility | timely_filing | duplicate | contractual | documentation
    appealable: bool
    typical_action: str


_CURATED_CODES: list[DenialCode] = [
    DenialCode(code="CO-4", kind="CARC",
               description="Procedure code inconsistent with the modifier used or a required modifier is missing.",
               category="coding", appealable=True,
               typical_action="Review modifier usage; correct and resubmit as a corrected claim."),
    DenialCode(code="CO-11", kind="CARC",
               description="The diagnosis is inconsistent with the procedure.",
               category="coding", appealable=True,
               typical_action="Verify diagnosis-to-procedure linkage against the medical record; correct and resubmit."),
    DenialCode(code="CO-16", kind="CARC",
               description="Claim/service lacks information or has submission/billing errors.",
               category="documentation", appealable=True,
               typical_action="Identify the missing element from the accompanying RARC and resubmit."),
    DenialCode(code="CO-18", kind="CARC",
               description="Exact duplicate claim or service.",
               category="duplicate", appealable=False,
               typical_action="No appeal. Verify the original claim's status and post the original payment."),
    DenialCode(code="CO-22", kind="CARC",
               description="This care may be covered by another payer per coordination of benefits.",
               category="coverage", appealable=True,
               typical_action="Confirm primary payer; rebill to the correct payer with the primary EOB."),
    DenialCode(code="CO-29", kind="CARC",
               description="The time limit for filing has expired.",
               category="timely_filing", appealable=True,
               typical_action="Appeal only with proof of timely submission; otherwise write off."),
    DenialCode(code="CO-45", kind="CARC",
               description="Charge exceeds fee schedule/maximum allowable or contracted amount.",
               category="contractual", appealable=False,
               typical_action="Contractual adjustment. Post the write-off; not patient responsibility."),
    DenialCode(code="CO-50", kind="CARC",
               description="Non-covered service because it is not deemed a medical necessity by the payer.",
               category="coverage", appealable=True,
               typical_action="Appeal with clinical documentation supporting medical necessity."),
    DenialCode(code="CO-97", kind="CARC",
               description="Benefit for this service is included in the payment for another service already adjudicated.",
               category="coding", appealable=True,
               typical_action="Review bundling edits; appeal with modifier justification if unbundling is supported."),
    DenialCode(code="CO-197", kind="CARC",
               description="Precertification/authorization/notification absent.",
               category="authorization", appealable=True,
               typical_action="Appeal with retro-authorization request and documentation of the auth attempt."),
    DenialCode(code="PR-1", kind="CARC",
               description="Deductible amount.",
               category="contractual", appealable=False,
               typical_action="Patient responsibility. Bill the patient for the deductible."),
    DenialCode(code="PR-2", kind="CARC",
               description="Coinsurance amount.",
               category="contractual", appealable=False,
               typical_action="Patient responsibility. Bill the patient for coinsurance."),
    DenialCode(code="PR-3", kind="CARC",
               description="Co-payment amount.",
               category="contractual", appealable=False,
               typical_action="Patient responsibility. Bill the patient for the copay."),
    DenialCode(code="CO-27", kind="CARC",
               description="Expenses incurred after coverage terminated.",
               category="eligibility", appealable=True,
               typical_action="Verify eligibility on the date of service; appeal if coverage was active."),
    DenialCode(code="N130", kind="RARC",
               description="Consult plan benefit documents for information about restrictions for this service.",
               category="coverage", appealable=True,
               typical_action="Review the plan's benefit language before appealing."),
    DenialCode(code="M127", kind="RARC",
               description="Missing patient medical record for this service.",
               category="documentation", appealable=True,
               typical_action="Submit the requested medical records with the appeal."),
]

# Categories where an appeal is generally not the right action. Kept as a
# module-level name too (backward-compatible with every existing import).
NON_APPEALABLE_CATEGORIES = {"contractual", "duplicate"}


class CarcRegistry:
    """The agent's denial-code domain knowledge, as a single cohesive object
    instead of a loose collection of module functions + globals.

    Two tiers, both encapsulated here:
      1. `curated` -- hand-written category/action text for ~16 codes seen
         most often in this project's sample documents.
      2. A real, full X12-published CARC list (`carc_codes.RAW_CARC`, 297
         current codes fetched from x12.org) as a fallback, with category and
         typical-action *heuristically* derived from the authoritative
         description text (see `_categorize`). This means a real payer
         document using any of the ~280 CARC codes outside the curated set
         still resolves to a genuine, sourced description instead of `None`
         -- only truly unrecognized/invalid codes (RARCs not in the curated
         set, typos, deactivated codes) return `None`.

    A module-level singleton (`_default_registry` below) backs the plain
    functions (`lookup_code`, `derive_triage`, etc.) that the rest of the
    codebase already imports, so this refactor is a pure internal
    reorganization -- no call site elsewhere changes.
    """

    # Templated action per category -- used only for the real-X12 fallback,
    # where we have a genuine description but no analyst-authored action (the
    # curated entries above have their own hand-written actions).
    _ACTION_BY_CATEGORY: dict[str, str] = {
        "duplicate": "No appeal. Verify the original claim's status; do not resubmit.",
        "timely_filing": "Appeal only with proof of timely submission; otherwise write off.",
        "authorization": "Appeal with a retro-authorization request and documentation of the authorization attempt.",
        "contractual": "Contractual adjustment or patient responsibility. Post the write-off or bill the patient; not appealable.",
        "coding": "Review coding/modifier usage against the medical record; correct and resubmit, or appeal with justification.",
        "eligibility": "Verify eligibility on the date of service; appeal if coverage was active.",
        "coverage": "Appeal with clinical documentation supporting medical necessity or coverage.",
        "documentation": "Identify and submit the missing/requested information; resubmit or appeal.",
    }

    # Keyword -> category, checked in order (first match wins) against the
    # real X12 description text. This is OUR heuristic, not X12's -- the
    # official code list has no category field at all; category/appealable/
    # action are a domain judgment this project adds on top of the
    # authoritative description.
    _CATEGORY_KEYWORDS: list[tuple[str, str]] = [
        ("duplicate", "duplicate"),
        ("time limit for filing", "timely_filing"),
        ("precertification", "authorization"),
        ("pre-certification", "authorization"),
        ("authorization", "authorization"),
        ("notification", "authorization"),
        ("referral", "authorization"),
        ("fee schedule", "contractual"),
        ("contracted", "contractual"),
        ("legislated fee", "contractual"),
        ("maximum allowable", "contractual"),
        ("already adjudicated", "coding"),
        ("included in the payment", "coding"),
        ("inconsistent with", "coding"),
        ("diagnosis", "coding"),
        ("procedure code", "coding"),
        ("modifier", "coding"),
        ("revenue code", "coding"),
        ("not an eligible", "eligibility"),
        ("eligib", "eligibility"),
        ("coverage terminated", "eligibility"),
        ("expenses incurred", "eligibility"),
        ("dependent coverage", "eligibility"),
        ("missing", "documentation"),
        ("incomplete", "documentation"),
        ("invalid", "documentation"),
        ("documentation", "documentation"),
        ("medical record", "documentation"),
        ("not covered", "coverage"),
        ("non-covered", "coverage"),
        ("medical necessity", "coverage"),
        ("experimental", "coverage"),
        ("cosmetic", "coverage"),
        ("benefit", "coverage"),
    ]

    # CARC 1/2/3 (deductible/coinsurance/copayment) are patient cost-share,
    # not real denials -- always contractual/non-appealable regardless of
    # wording, matching the curated PR-1/PR-2/PR-3 entries above.
    _ALWAYS_CONTRACTUAL_CARC = {"1", "2", "3"}

    def __init__(self, curated: list[DenialCode] | None = None,
                 raw_carc: dict[str, str] | None = None) -> None:
        self._curated: dict[str, DenialCode] = {
            c.code: c for c in (curated if curated is not None else _CURATED_CODES)
        }
        self._raw_carc: dict[str, str] = raw_carc if raw_carc is not None else RAW_CARC

    def lookup(self, code: str) -> DenialCode | None:
        """Look up one CARC/RARC code. Tolerates minor formatting variance
        (missing dash, e.g. "CO45" vs "CO-45")."""
        if not code:
            return None
        normalized = code.strip().upper().replace(" ", "")
        if normalized in self._curated:
            return self._curated[normalized]
        stripped = normalized.replace("-", "")
        for key, val in self._curated.items():
            if key.replace("-", "") == stripped:
                return val

        bare = self._bare_carc_number(code)
        description = self._raw_carc.get(bare)
        if description is None:
            return None
        category = self._categorize(bare, description)
        return DenialCode(
            code=normalized, kind="CARC", description=description, category=category,
            appealable=category not in NON_APPEALABLE_CATEGORIES,
            typical_action=self._ACTION_BY_CATEGORY[category],
        )

    def lookup_many(self, codes: list[str]) -> dict[str, DenialCode | None]:
        """Batch form of `lookup`, keyed by the original (un-normalized) code
        string -- what the `lookup_code` tool call actually returns to the
        agent."""
        return {c: self.lookup(c) for c in codes}

    def known_codes(self) -> list[str]:
        """All curated codes, sorted -- used to seed the system prompt so the
        model knows a representative sample without guessing."""
        return sorted(self._curated)

    def derive_triage(self, denial_codes: list[str]) -> tuple[bool, str]:
        """Mechanically derive (is_appealable, denial_category) from a list
        of denial codes -- the same rule an RCM analyst applies, and the same
        rule `evaluate.py`'s scoring uses to grade the LLM's triage.

        Why this exists: a real cross-tier comparison (gpt-4.1 vs
        gpt-4.1-mini, see reports/cheap_extraction_research.md #5) showed a
        model can extract every field correctly and still get triage wrong --
        paraphrasing a registry category instead of copying it verbatim, or
        inventing a category that isn't one of the registry's real ones.
        None of the three mechanical validators (grounding/arithmetic/
        business-rules) catch that, because it's a reasoning error over
        already-correct data, not a grounding or arithmetic violation. This
        method is the fourth check: is_appealable and denial_category should
        never be trusted from the model's own words -- they're fully
        determined by which codes were extracted, so derive them here and let
        the LLM's output be overridden by this at finalize time.

        Rule: pick the first code (in the given order) whose category is NOT
        in NON_APPEALABLE_CATEGORIES ("contractual", "duplicate"); if none
        qualify, fall back to the first resolvable code. Returns
        (False, "unknown") if no code resolves in the registry at all.
        """
        primary = self.primary_denial_code(denial_codes)
        if primary is None:
            return False, "unknown"
        return bool(primary.appealable), primary.category

    def primary_denial_code(self, denial_codes: list[str]) -> DenialCode | None:
        """Resolve the single code that drives the triage decision (first
        appealable code in order, else first resolvable code) -- exposed
        separately from `derive_triage` so a caller can also pull
        `.description`/`.typical_action` for registry-grounded explanatory
        text, not just the bare (bool, category) verdict."""
        infos = [i for i in (self.lookup(c) for c in denial_codes) if i]
        actionable = [i for i in infos if i.category not in NON_APPEALABLE_CATEGORIES]
        return actionable[0] if actionable else (infos[0] if infos else None)

    @staticmethod
    def render_lookup(results: dict[str, DenialCode | None]) -> str:
        """Render registry results as a tool OBSERVATION for the agent."""
        lines = ["CODE_LOOKUP:"]
        for code, info in results.items():
            if info is None:
                lines.append(f"  {code}: NOT FOUND in registry (do not invent a meaning).")
            else:
                lines.append(
                    f"  {info.code} [{info.kind}] category={info.category} "
                    f"appealable={info.appealable}\n"
                    f"    meaning: {info.description}\n"
                    f"    action: {info.typical_action}"
                )
        return "\n".join(lines)

    @classmethod
    def _categorize(cls, bare_code: str, description: str) -> str:
        """Heuristic category for a real X12 CARC code that isn't in the
        curated set. Keyword match against the authoritative description;
        falls back to "documentation" (implying manual review, not an
        auto-write-off) when nothing matches rather than guessing a specific
        category."""
        if bare_code in cls._ALWAYS_CONTRACTUAL_CARC:
            return "contractual"
        lower = description.lower()
        for keyword, category in cls._CATEGORY_KEYWORDS:
            if keyword in lower:
                return category
        return "documentation"

    @staticmethod
    def _bare_carc_number(code: str) -> str:
        """Strip a group-code prefix (CO-/PR-/OA-/PI-/CR-, the 5 official
        Claim Adjustment Group Codes) to get the bare CARC code the real X12
        registry is keyed by. Only strips on an explicit "GROUP-CODE" dash
        format -- plenty of real CARCs have their own letter prefix (A1, B4,
        P12), which must NOT be confused with a group-code prefix."""
        normalized = code.strip().upper().replace(" ", "")
        if "-" in normalized:
            prefix, rest = normalized.split("-", 1)
            if prefix in {"CO", "OA", "PI", "PR", "CR"}:
                return rest
        return normalized


# Module-level singleton + thin function wrappers: every existing caller
# (agent.py, evaluate.py, ingest.py, validation.py, tests) imports these
# plain functions, so the class above is an internal reorganization, not a
# breaking API change.
_default_registry = CarcRegistry()


def lookup_code(code: str) -> DenialCode | None:
    return _default_registry.lookup(code)


def lookup_codes(codes: list[str]) -> dict[str, DenialCode | None]:
    return _default_registry.lookup_many(codes)


def known_codes() -> list[str]:
    return _default_registry.known_codes()


def derive_triage(denial_codes: list[str]) -> tuple[bool, str]:
    return _default_registry.derive_triage(denial_codes)


def primary_denial_code(denial_codes: list[str]) -> DenialCode | None:
    return _default_registry.primary_denial_code(denial_codes)


def render_lookup(results: dict[str, DenialCode | None]) -> str:
    return CarcRegistry.render_lookup(results)


def _categorize_carc(bare_code: str, description: str) -> str:
    return CarcRegistry._categorize(bare_code, description)


def _bare_carc_number(code: str) -> str:
    return CarcRegistry._bare_carc_number(code)
