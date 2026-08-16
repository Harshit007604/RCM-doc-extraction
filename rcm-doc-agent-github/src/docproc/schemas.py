"""Structured extraction contracts for healthcare RCM correspondence.

The agent's job: turn an unstructured payer document (denial letter / EOB /
remittance advice) into a validated `ClaimExtraction`. Every field the agent
emits must be traceable to text it actually saw — that is enforced structurally
by `FieldValue.source_text` and checked mechanically by the grounding validator.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator


class DocType(str, Enum):
    """Which of the three surface formats a document is (or `UNKNOWN` before
    the agent has looked)."""

    DENIAL_LETTER = "denial_letter"
    EOB = "eob"
    REMITTANCE = "remittance_advice"
    UNKNOWN = "unknown"


class FieldValue(BaseModel):
    """A single extracted value plus the span it came from.

    `source_text` is the hallucination guard: the verbatim substring of the
    document that supports `value`. A value with no matching span in the source
    is flagged by validation, not silently trusted.
    """

    value: str | None = Field(default=None, description="Normalized value, or null if absent.")
    source_text: str | None = Field(
        default=None, description="Verbatim snippet from the document supporting this value."
    )
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_numeric_to_str(cls, v):
        """Real, measured failure mode: models frequently emit money fields as
        a bare JSON number (`2306.72`) instead of a quoted string (`"2306.72"`)
        even though the prompt asks for a string -- 129 of 198 parse failures
        across this project's trace logs were exactly this ('Input should be
        a valid string ... input_type=float'). Retrying the whole LLM call
        for something this mechanical wastes a paid call and still sometimes
        exhausts the retry budget (the real cause of the ~6% hard failure
        rate found running 110 documents through the pipeline -- see
        LEARNING.md). Coercing here fixes it for every case, not just the
        ones caught by luck within 3 retries, and preserves the original
        formatting via `repr`-free `str()` (e.g. `2306.72`, not `2306.7200`).
        Booleans are excluded (`isinstance(v, bool)` before `int`, since
        `bool` is a subclass of `int` in Python) -- a stray `true`/`false`
        should still fail loudly rather than silently becoming `"True"`.
        """
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return str(v)
        return v


class LineItem(BaseModel):
    """One billed service line (a CPT code plus its charged/allowed/paid
    amounts and the adjustment code that applied to it)."""

    cpt_code: str | None = None
    description: str | None = None
    charge_amount: float | None = None
    allowed_amount: float | None = None
    paid_amount: float | None = None
    denial_code: str | None = None


class ClaimExtraction(BaseModel):
    """The structured record extracted from one document."""

    doc_type: DocType = DocType.UNKNOWN
    payer_name: FieldValue = Field(default_factory=FieldValue)
    claim_number: FieldValue = Field(default_factory=FieldValue)
    member_id: FieldValue = Field(default_factory=FieldValue)
    patient_name: FieldValue = Field(default_factory=FieldValue)
    provider_name: FieldValue = Field(default_factory=FieldValue)
    date_of_service: FieldValue = Field(default_factory=FieldValue)   # ISO YYYY-MM-DD
    total_charged: FieldValue = Field(default_factory=FieldValue)
    total_allowed: FieldValue = Field(default_factory=FieldValue)
    total_paid: FieldValue = Field(default_factory=FieldValue)
    patient_responsibility: FieldValue = Field(default_factory=FieldValue)
    denial_codes: list[str] = Field(default_factory=list)
    appeal_deadline: FieldValue = Field(default_factory=FieldValue)   # ISO YYYY-MM-DD
    line_items: list[LineItem] = Field(default_factory=list)


class ValidationIssue(BaseModel):
    """One concrete, actionable failure from a validator -- what the agent is
    handed back instead of a vague "try again".

    `check` records WHICH validator produced the issue. That matters
    downstream: a `grounding` failure is an anti-hallucination signal and is
    only meaningful for text an LLM generated -- a deterministic parser
    (e.g. `x12_parser`) can legitimately produce a *derived* value with no
    quotable span, and treating that as suspicious would route every EDI
    document to human review for no reason. `arithmetic` and
    `business_rules` failures are meaningful regardless of source.
    """

    field: str
    severity: str  # "error" | "warning"
    message: str
    check: str = "unknown"  # "grounding" | "arithmetic" | "business_rules"


class ValidationReport(BaseModel):
    ok: bool
    issues: list[ValidationIssue] = Field(default_factory=list)

    def render(self) -> str:
        if self.ok:
            return "VALIDATION: passed, no issues."
        lines = ["VALIDATION: failed. Fix these and re-emit the extraction:"]
        lines += [f"  [{i.severity}] {i.field}: {i.message}" for i in self.issues]
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Agent control protocol
# --------------------------------------------------------------------------- #
class DocAction(str, Enum):
    LOOKUP_CODE = "lookup_code"   # call the CARC/RARC registry tool
    EXTRACT = "extract"           # emit / re-emit the structured record
    FINALIZE = "finalize"         # validated record + triage recommendation


class Triage(BaseModel):
    """The business output — what an RCM analyst would actually act on."""

    is_appealable: bool
    denial_category: str = Field(default="unknown")
    recommended_action: str = ""
    rationale: str = ""
    dollars_at_risk: float = 0.0


class DocStep(BaseModel):
    """One reasoning turn of the extraction agent."""

    thought: str
    action: DocAction
    codes_to_look_up: list[str] = Field(default_factory=list)
    extraction: ClaimExtraction | None = None
    triage: Triage | None = None


class DocOutcome(BaseModel):
    """What `DocumentAgent.run()` returns: the terminal status plus whatever
    extraction/triage/validation state exists at that point."""

    status: str = "ok"  # ok | incomplete | error
    extraction: ClaimExtraction | None = None
    triage: Triage | None = None
    validation: ValidationReport | None = None
    steps_used: int = 0
    trace_path: str | None = None
    message: str | None = None
    token_usage: dict = Field(default_factory=dict)  # real prompt/completion/total tokens, this document


# --------------------------------------------------------------------------- #
# Multi-agent: portfolio triage (batch of documents -> ranked worklist)
# --------------------------------------------------------------------------- #
class WorklistItem(BaseModel):
    """One row of the portfolio worklist -- a single document's outcome,
    reduced to what an RCM analyst needs to prioritize it."""

    filename: str
    status: str
    claim_number: str | None = None
    payer_name: str | None = None
    is_appealable: bool | None = None
    denial_category: str | None = None
    dollars_at_risk: float = 0.0
    recommended_action: str | None = None


class PortfolioOutcome(BaseModel):
    """The full batch result: every `WorklistItem`, ranked, plus the
    aggregate totals the synthesizer computed."""

    items: list[WorklistItem] = Field(default_factory=list)
    total_dollars_at_risk: float = 0.0
    appealable_count: int = 0
    by_category: dict[str, float] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Multi-agent: cross-document reconciliation (same claim, N document formats)
# --------------------------------------------------------------------------- #
class ReconciliationIssue(BaseModel):
    """One field that disagrees across two or more documents in a claim
    group, with the actual value each document reported."""

    field: str
    values: dict[str, str]   # doc_type -> the value each document extraction reported
    message: str


class ReconciliationReport(BaseModel):
    """Result of cross-checking one claim's documents: which fields agree,
    which don't, and the individual extraction behind each document."""

    claim_number: str | None = None
    ok: bool = True
    issues: list[ReconciliationIssue] = Field(default_factory=list)
    per_doc: dict[str, ClaimExtraction] = Field(default_factory=dict)

