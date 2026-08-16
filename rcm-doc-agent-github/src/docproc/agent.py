r"""Document-processing agent (LangGraph).

    START -> decide --lookup_code--> lookup ---------\
                    \--extract-----> extract -> validate --(fail, budget left)--\
                     \--finalize---> finalize -> END                            |
                      \--budget----> give_up  -> END                            |
                                          ^-------------------------------------/

Loop: reason -> act (tool) -> observe (validation report) -> self-correct ->
respond. The distinctive property is that the "observe" step is a *mechanical*
validator (grounding + arithmetic + business rules), not the model grading
itself — so self-correction is driven by verifiable failures.
"""

from __future__ import annotations

from typing import Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from ..common import _extract_json
from ..config import Settings
from ..llm import LLMClient, LLMError
from ..logging_utils import RunTrace
from .prompts import DOC_SYSTEM_PROMPT, build_doc_task_message, build_validation_message
from .registry.codes import derive_triage, lookup_codes, primary_denial_code, render_lookup
from .schemas import DocAction, DocOutcome, DocStep, Triage, ValidationReport
from .validation import validate


class DocState(TypedDict, total=False):
    document: str
    filename: str
    ocr_low_grade: Optional[str]
    messages: list
    step: int
    fix_rounds: int
    decision: DocStep
    extraction: Optional[object]
    validation: Optional[ValidationReport]
    outcome: Optional[DocOutcome]


class DocumentAgent:
    """The document-extraction agent: a compiled LangGraph `StateGraph` plus
    the LLM client it decides with. One instance processes one document; the
    multi-agent orchestrators (`portfolio.py`, `reconcile.py`) create a fresh
    instance per document so there is never shared state between claims.
    """

    def __init__(self, settings: Settings, llm: LLMClient):
        """Build and compile the graph once; `run()` is what actually executes it."""
        self.s = settings
        self.llm = llm
        self._app = self._build()
        self._trace: RunTrace | None = None
        self._on_event = None
        self._on_token = None
        self._token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def _build(self):
        """Wire the five nodes and their conditional routing (see the module
        docstring's ASCII diagram) into a compiled, runnable graph."""
        g = StateGraph(DocState)
        g.add_node("decide", self._decide_node)
        g.add_node("lookup", self._lookup_node)
        g.add_node("extract", self._extract_node)
        g.add_node("finalize", self._finalize_node)
        g.add_node("give_up", self._give_up_node)

        g.add_edge(START, "decide")
        g.add_conditional_edges("decide", self._route_decide, {
            "lookup_code": "lookup", "extract": "extract",
            "finalize": "finalize", "give_up": "give_up", "end": END,
        })
        g.add_conditional_edges("lookup", self._loop, {"decide": "decide", "end": END})
        g.add_conditional_edges("extract", self._loop, {"decide": "decide", "end": END})
        g.add_conditional_edges("finalize", self._loop, {"decide": "decide", "end": END})
        g.add_edge("give_up", END)
        return g.compile()

    # ------------------------------------------------------------------ #
    def run(self, document: str, filename: str = "document.txt",
            on_event=None, on_token=None, ocr_low_grade: str | None = None) -> DocOutcome:
        """Run the full loop on one document to completion (ok / incomplete /
        error). `on_event`/`on_token` are optional callbacks for live UIs
        (CLI `--stream`, the Streamlit app) to render progress as it happens.

        `ocr_low_grade`: Docling's worst-5th-percentile OCR confidence grade
        for this document (`None` for plain text / non-OCR input, see
        `ingest.IngestResult.ocr_low_grade`). Threaded through to `validate()`
        so grounding/semantic checks can be OCR-aware (see `validation.py`).
        """
        self._trace = RunTrace(self.s.log_dir, f"extract:{filename}")
        self._on_event, self._on_token = on_event, on_token
        # Reset per-document: instances are reused across documents in some
        # callers (e.g. evaluate.py's loop), so token counts must not leak
        # from a previous document into this one.
        self._token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        # Input guard-rail: reject an oversized document BEFORE it's ever
        # sent to the LLM, rather than silently forwarding an arbitrarily
        # large payload (a misrouted binary file, an accidentally-
        # concatenated multi-document dump). Zero LLM calls spent on a
        # rejected document -- this check happens before the graph runs at
        # all. `store.triage_decision` routes `status="error"` outcomes to
        # `needs_review` (see queue/store.py), so a rejected document still
        # gets a human, not a silent drop.
        if len(document) > self.s.max_input_chars:
            self._emit(0, "error",
                       message=(f"Input too large: {len(document):,} chars exceeds the "
                                f"{self.s.max_input_chars:,}-char guard-rail (config: "
                                f"max_input_chars); rejected before any LLM call."))
            return DocOutcome(
                status="error", steps_used=0, trace_path=self._trace.path,
                message=(f"Document too large ({len(document):,} chars > "
                         f"{self.s.max_input_chars:,}-char limit); rejected before "
                         f"sending to the LLM."),
                token_usage=dict(self._token_usage))

        init: DocState = {
            "document": document, "filename": filename, "ocr_low_grade": ocr_low_grade,
            "messages": [{"role": "user",
                          "content": build_doc_task_message(document, filename)}],
            "step": 0, "fix_rounds": 0,
        }
        final = self._app.invoke(init, config={"recursion_limit": self.s.max_steps * 4 + 10})
        out = final.get("outcome")
        if out is None:
            out = DocOutcome(status="error", steps_used=final.get("step", 0),
                             trace_path=self._trace.path,
                             message="graph ended without an outcome")
        # Set regardless of which node produced the outcome -- one exit point,
        # not four call sites to keep in sync.
        out.token_usage = dict(self._token_usage)
        return out

    def _emit(self, step: int, kind: str, **fields):
        """Write one event to the JSONL trace and forward it to the live
        `on_event` callback, if any -- the single choke point every node
        uses to stay observable."""
        self._trace.record(step, kind, **fields)
        if self._on_event:
            self._on_event({"step": step, "kind": kind, **fields})

    # -- nodes ---------------------------------------------------------- #
    def _decide_node(self, state: DocState) -> dict:
        """Ask the LLM for the next `DocStep` (reason + chosen action) and
        route on it. An `LLMError` here ends the run with status='error'
        rather than raising, so the graph always terminates cleanly."""
        step = state["step"] + 1
        messages = list(state["messages"])
        try:
            decision = self._decide(messages, step)
        except LLMError as exc:
            self._emit(step, "error", message=f"LLM failure: {exc}")
            return {"step": step,
                    "outcome": DocOutcome(status="error", steps_used=step,
                                          trace_path=self._trace.path,
                                          message=f"LLM unavailable: {exc}")}
        messages.append({"role": "assistant", "content": decision.model_dump_json()})
        self._emit(step, "decision", action=decision.action.value, thought=decision.thought,
                   codes=decision.codes_to_look_up or None)
        return {"step": step, "decision": decision, "messages": messages}

    def _lookup_node(self, state: DocState) -> dict:
        """The agent's one tool call: resolve the requested CARC/RARC codes
        against the registry and feed the result back as an observation."""
        step = state["step"]
        codes = state["decision"].codes_to_look_up
        results = lookup_codes(codes)
        found = [c for c, v in results.items() if v]
        missing = [c for c, v in results.items() if not v]
        self._emit(step, "tool_call", tool="lookup_code", requested=codes,
                   found=found, not_found=missing)
        messages = list(state["messages"])
        messages.append({"role": "user", "content": render_lookup(results)})
        return {"messages": messages}

    def _extract_node(self, state: DocState) -> dict:
        """The "observe" step: run the mechanical validator (grounding,
        arithmetic, business rules) against the just-emitted extraction and
        feed the report back. This is what makes self-correction meaningful --
        the agent is handed concrete failures, not asked to "reflect"."""
        step = state["step"]
        ext = state["decision"].extraction
        messages = list(state["messages"])
        if ext is None:
            messages.append({"role": "user",
                             "content": "EXTRACT requires a non-null `extraction` object. Re-emit it."})
            return {"messages": messages}

        report = validate(ext, state["document"], state.get("ocr_low_grade"))
        n_err = sum(1 for i in report.issues if i.severity == "error")
        self._emit(step, "validation", ok=report.ok, errors=n_err,
                   issues=[f"{i.field}: {i.message}" for i in report.issues][:6])
        messages.append({"role": "user", "content": build_validation_message(report)})

        fix_rounds = state["fix_rounds"]
        if not report.ok:
            fix_rounds += 1
            if fix_rounds > self.s.max_code_retries:
                return {"messages": messages, "extraction": ext, "validation": report,
                        "fix_rounds": fix_rounds,
                        "outcome": DocOutcome(
                            status="incomplete", extraction=ext, validation=report,
                            steps_used=step, trace_path=self._trace.path,
                            message="validation still failing after correction budget")}
        return {"messages": messages, "extraction": ext,
                "validation": report, "fix_rounds": fix_rounds}

    def _finalize_node(self, state: DocState) -> dict:
        """Terminal success path: package the validated extraction plus the
        triage decision into the returned `DocOutcome`.

        `is_appealable`/`denial_category`/`dollars_at_risk` are NOT trusted
        from the model's own words -- they are mechanically re-derived from
        `ext.denial_codes` (via `derive_triage`) and `ext.line_items`, and
        overwrite whatever the model said. A real cross-tier comparison
        (gpt-4.1 vs gpt-4.1-mini, see reports/cheap_extraction_research.md #5)
        showed a model can extract every field correctly and still paraphrase
        or invent a wrong category/appealability -- a reasoning error over
        already-correct data that none of the three validators catch. Only
        `recommended_action`/`rationale` remain the model's own text.
        """
        step = state["step"]
        triage = state["decision"].triage
        ext = state.get("extraction")
        report = state.get("validation")
        if ext is None:
            messages = list(state["messages"])
            messages.append({"role": "user",
                             "content": "Cannot finalize before a validated extraction. "
                                        "Emit action=extract first."})
            return {"messages": messages}

        mech_appealable, mech_category = derive_triage(ext.denial_codes)
        mech_at_risk = round(sum(
            li.charge_amount or 0.0 for li in ext.line_items
            if not (li.allowed_amount or 0.0)), 2)
        primary = primary_denial_code(ext.denial_codes)
        if triage is None:
            # Real bug found by testing gpt-4.1-nano on a fresh 10-doc sample
            # (see LEARNING.md): a weaker model sometimes emits a `finalize`
            # DocStep with NO `triage` object at all (the schema allows it,
            # `Triage | None = None`) -- even when its own `thought` text
            # shows it reasoned about appealability correctly and the
            # extraction (denial_codes) was 100% right. The old code only
            # OVERRODE an existing triage; if the model omitted one entirely,
            # the guard skipped the whole block and `DocOutcome.triage`
            # stayed `None` on an otherwise perfectly-extracted document.
            # Build one from the registry instead of leaving it absent --
            # the numeric/category fields never depended on the model
            # anyway.
            self._emit(step, "triage_missing",
                       message="Model finalized with no triage object; constructing one "
                               "from the registry instead of leaving it absent.")
            triage = Triage(
                is_appealable=mech_appealable, denial_category=mech_category,
                dollars_at_risk=mech_at_risk,
                recommended_action=primary.typical_action if primary else "Manual review required.",
                rationale=(f"{primary.code}: {primary.description}" if primary
                          else "No registry codes resolved."))
        else:
            disagreed = (triage.is_appealable != mech_appealable
                        or triage.denial_category != mech_category)
            if disagreed:
                self._emit(step, "triage_override",
                           llm_appealable=triage.is_appealable, llm_category=triage.denial_category,
                           mech_appealable=mech_appealable, mech_category=mech_category)
                # The LLM's own action/rationale text was built on the wrong
                # verdict, so it can't be trusted either -- replace it with
                # registry-grounded text rather than leave prose that now
                # contradicts the corrected category/appealability.
                if primary is not None:
                    triage.recommended_action = primary.typical_action
                    triage.rationale = f"{primary.code}: {primary.description}"
            triage.is_appealable = mech_appealable
            triage.denial_category = mech_category
            triage.dollars_at_risk = mech_at_risk

        self._emit(step, "final", appealable=getattr(triage, "is_appealable", None),
                   category=getattr(triage, "denial_category", None),
                   at_risk=getattr(triage, "dollars_at_risk", None))
        return {"outcome": DocOutcome(status="ok", extraction=ext, triage=triage,
                                      validation=report, steps_used=step,
                                      trace_path=self._trace.path)}

    def _give_up_node(self, state: DocState) -> dict:
        """Terminal failure path: the step budget ran out before the agent
        reached `finalize`. Returns whatever partial extraction/validation
        exists rather than raising."""
        step = state["step"]
        self._emit(step, "final", summary="gave up: step budget exhausted")
        return {"outcome": DocOutcome(status="incomplete", extraction=state.get("extraction"),
                                      validation=state.get("validation"), steps_used=step,
                                      trace_path=self._trace.path, message="reached max steps")}

    # -- routers -------------------------------------------------------- #
    def _route_decide(self, state: DocState) -> str:
        """Enforce the step budget, then route to the node matching the
        model's chosen action (lookup_code / extract / finalize)."""
        if state.get("outcome"):
            return "end"
        decision = state["decision"]
        if state["step"] >= self.s.max_steps and decision.action != DocAction.FINALIZE:
            return "give_up"
        return decision.action.value

    @staticmethod
    def _loop(state: DocState) -> str:
        """After lookup/extract/finalize: end if a terminal outcome was set,
        otherwise go back to `decide` for the next turn."""
        return "end" if state.get("outcome") else "decide"

    # -- helper --------------------------------------------------------- #
    def _decide(self, messages: list[dict], step: int) -> DocStep:
        """Get one validated `DocStep` from the model, self-correcting up to
        3 attempts if it emits invalid JSON (the same pattern the CSV agent's
        `AgentStep` parsing uses)."""
        for _ in range(3):
            raw = self.llm.complete(DOC_SYSTEM_PROMPT, messages, on_token=self._on_token)
            if self.llm.last_usage:
                for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    self._token_usage[k] += self.llm.last_usage.get(k, 0)
            try:
                return DocStep.model_validate_json(_extract_json(raw))
            except (ValidationError, ValueError) as exc:
                self._emit(step, "parse_error", message=str(exc)[:300])
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user",
                                 "content": f"That was not a valid DocStep JSON object "
                                            f"({str(exc)[:200]}). Re-emit ONLY the JSON."})
        raise LLMError("could not obtain valid DocStep JSON after retries")
