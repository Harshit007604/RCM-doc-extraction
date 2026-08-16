"""Streamlit UI for the document-extraction agent (primary).

This is the primary agent's UI — the CSV data-analysis agent's UI moved to
`legacy/ui/streamlit_app.py` when that domain was deprioritized (see
`LEARNING.md`).

Run:
    pip install streamlit
    streamlit run ui/streamlit_app.py

Three modes, one for each capability built so far:
  - Single document   : extract + validate + triage one payer document.
  - Portfolio triage   : rank a batch of documents by dollars at risk
                         (multi-agent A — src/docproc/workflows/portfolio.py).
  - Reconciliation     : cross-check a claim's denial letter / EOB / 835
                         remittance advice against each other
                         (multi-agent B — src/docproc/workflows/reconcile.py).
  - Review queue (HITL): the human-in-the-loop step of the enterprise
                         pipeline — work the queue of documents the
                         mechanical review policy flagged, and
                         approve/edit/reject each one
                         (src/docproc/queue/store.py + worker.py).
"""

from __future__ import annotations

import glob
import json
import os
import sys

# Make `src` importable when launched via `streamlit run ui/streamlit_app.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from src.config import get_settings
from src.llm import LLMClient
from src.docproc.agent import DocumentAgent
from src.docproc.workflows.portfolio import PortfolioOrchestrator
from src.docproc.workflows.reconcile import ClaimReconciler
from src.docproc.queue.store import DONE_STATES, JobStore

st.set_page_config(page_title="Document Extraction Agent", page_icon="🧾", layout="wide")
st.title("🧾 Agentic Document-Extraction Assistant")
st.caption("Denial letter / EOB / 835 remittance advice → validated claim record → appeal triage.")

with st.sidebar:
    st.header("Configuration")
    # Pre-fill from the real configured environment (.env's MODEL, or the
    # config.py default if unset) -- NOT a hardcoded literal. A hardcoded
    # default here previously showed "gemini/gemini-2.5-flash" regardless of
    # what MODEL was actually set to (this project's own .env has been
    # `openai/gpt-4.1` throughout every real run in LEARNING.md), so anyone
    # using the UI without manually retyping the field silently ran a
    # different, untested model -- and would hit a missing-API-key error if
    # that provider's key wasn't configured. Found via a real end-to-end
    # docker-compose UI test (see LEARNING.md).
    model = st.text_input("Model (LiteLLM string)", value=get_settings().model)
    temperature = st.slider("Temperature", 0.0, 1.0, 0.0, 0.1)
    stream = st.toggle("Stream reasoning steps live", value=True)
    st.caption("Requires a valid API key for the chosen provider, set in `.env` "
               "(e.g. `OPENAI_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`). "
               "Use a model string like `openai/gpt-4.1` or `groq/llama-3.3-70b-versatile`.")
    mode = st.radio("Mode", ["Single document", "Portfolio triage (batch)",
                             "Cross-document reconciliation", "Review queue (HITL)"])


def make_on_event(log_box):
    """Return an on_event callback that renders an accumulating trace into `log_box`."""
    log_lines: list[str] = []

    def on_event(ev: dict):
        kind = ev.get("kind")
        if kind == "decision":
            log_lines.append(f"🧠 step {ev['step']} · `{ev['action']}` — {ev.get('thought', '')}")
        elif kind == "tool_call":
            log_lines.append(f"🔧 tool `{ev.get('tool')}` found={ev.get('found')} "
                             f"not_found={ev.get('not_found')}")
        elif kind == "validation":
            icon = "✅" if ev.get("ok") else "⚠️"
            log_lines.append(f"{icon} validate · ok={ev.get('ok')} errors={ev.get('errors')}")
        elif kind == "final":
            log_lines.append(f"🏁 **final** — {ev.get('summary') or ''}")
        elif kind in ("error", "parse_error"):
            log_lines.append(f"❌ {kind} — {ev.get('message', '')}")
        elif kind == "delegate":
            label = ev.get("file") or f"{ev.get('doc_type')}: {ev.get('file')}"
            log_lines.append(f"➡️ delegate — {label}")
        log_box.markdown("\n\n".join(log_lines))

    return on_event


def render_extraction(outcome) -> None:
    """Render one `DocOutcome` as Streamlit widgets: the extraction table,
    validation result, and triage card -- shared by the single-document mode
    (kept as one function so the CLI and UI never drift in what they show)."""
    ext, tri, val = outcome.extraction, outcome.triage, outcome.validation

    st.subheader(f"Extraction ({outcome.status}, {outcome.steps_used} steps)")
    if ext:
        fields = ["payer_name", "provider_name", "patient_name", "member_id",
                  "claim_number", "date_of_service", "total_charged", "total_allowed",
                  "total_paid", "patient_responsibility", "appeal_deadline"]
        st.table({"value": {f: getattr(ext, f).value for f in fields}})
        st.write(f"**denial_codes**: {', '.join(ext.denial_codes) or '(none)'}")
        if ext.line_items:
            st.write("**line items**")
            st.dataframe([li.model_dump() for li in ext.line_items], width="stretch")

    if val:
        if val.ok:
            st.success("Validation passed — grounding, arithmetic, and business rules all check out.")
        else:
            st.error("Validation failed:")
            for i in val.issues:
                st.write(f"- [{i.severity}] **{i.field}**: {i.message}")

    if tri:
        st.subheader("Triage")
        col1, col2, col3 = st.columns(3)
        col1.metric("Appealable", "Yes" if tri.is_appealable else "No")
        col2.metric("Category", tri.denial_category)
        col3.metric("Dollars at risk", f"${tri.dollars_at_risk:,.2f}")
        st.write(f"**Recommended action:** {tri.recommended_action}")
        st.write(f"**Rationale:** {tri.rationale}")

    if outcome.trace_path:
        st.caption(f"Trace: `{outcome.trace_path}`")


# --------------------------------------------------------------------------- #
if mode == "Single document":
    st.subheader("Extract + validate + triage one document")
    corpus_files = sorted(glob.glob("data/docs/*.txt")) + sorted(glob.glob("data/real_world/*"))
    options = ["(upload instead)"] + corpus_files
    choice = st.selectbox("Pick a sample document", options, index=1 if corpus_files else 0)
    uploaded = st.file_uploader("...or upload a document", type=["txt", "edi"])

    document = filename = None
    if uploaded is not None:
        document = uploaded.getvalue().decode("utf-8")
        filename = uploaded.name
    elif choice != "(upload instead)":
        filename = os.path.basename(choice)
        document = open(choice, encoding="utf-8").read()

    if document:
        with st.expander("Document text", expanded=False):
            st.text(document)

    run = st.button("Extract & triage", type="primary", disabled=document is None)

    if run and document:
        settings = get_settings(model=model, temperature=temperature)
        llm = LLMClient(settings)
        on_event = make_on_event(st.empty()) if stream else None

        with st.status("Running agent…", expanded=True):
            outcome = DocumentAgent(settings, llm).run(document, filename, on_event=on_event)

        render_extraction(outcome)

# --------------------------------------------------------------------------- #
elif mode == "Portfolio triage (batch)":
    st.subheader("Rank a batch of documents by dollars at risk")
    st.caption("Multi-agent A: a fresh DocumentAgent per document, synthesized into a "
               "worklist ranked by $ at risk.")
    batch_dir = st.text_input("Directory of documents", value="data/docs")
    run = st.button("Run portfolio triage", type="primary")

    if run:
        paths = sorted(glob.glob(os.path.join(batch_dir, "*.txt")))
        if not paths:
            st.warning(f"No .txt documents found in `{batch_dir}`.")
        else:
            settings = get_settings(model=model, temperature=temperature)
            llm = LLMClient(settings)
            on_event = make_on_event(st.empty()) if stream else None

            with st.status(f"Triaging {len(paths)} documents…", expanded=True):
                outcome = PortfolioOrchestrator(settings, llm).run(paths, on_event=on_event)

            st.subheader("Ranked worklist")
            rows = [{"file": it.filename, "status": it.status, "appealable": it.is_appealable,
                     "category": it.denial_category, "$ at risk": it.dollars_at_risk,
                     "claim #": it.claim_number} for it in outcome.items]
            st.dataframe(rows, width="stretch")

            col1, col2 = st.columns(2)
            col1.metric("Total appealable $ at risk", f"${outcome.total_dollars_at_risk:,.2f}")
            col2.metric("Appealable claims", f"{outcome.appealable_count}/{len(outcome.items)}")
            if outcome.by_category:
                st.write("**By category**")
                st.bar_chart(outcome.by_category)

# --------------------------------------------------------------------------- #
elif mode == "Cross-document reconciliation":
    st.subheader("Cross-check a claim's denial letter / EOB / remittance advice")
    st.caption("Multi-agent B: a fresh DocumentAgent per document in the SAME claim, then a "
               "field-by-field diff — the case no single-document validator can catch.")
    recon_dir = st.text_input("Directory of matched claim triads", value="data/matched_claims")
    n_claims = st.number_input("Claims to generate if missing", min_value=1, max_value=20, value=4)
    col_a, col_b = st.columns(2)
    gen = col_a.button("Generate matched triads")
    run = col_b.button("Run reconciliation", type="primary")

    if gen:
        from src.docproc.generator import generate_triads
        manifest_path = generate_triads(recon_dir, n_claims=int(n_claims))
        st.success(f"Generated: `{manifest_path}`")

    if run:
        manifest_path = os.path.join(recon_dir, "manifest.json")
        if not os.path.exists(manifest_path):
            st.warning(f"No manifest.json in `{recon_dir}`. Click 'Generate matched triads' first.")
        else:
            with open(manifest_path, encoding="utf-8") as fh:
                manifest = json.load(fh)
            settings = get_settings(model=model, temperature=temperature)
            llm = LLMClient(settings)
            reconciler = ClaimReconciler(settings, llm)

            n_flagged = n_correct = 0
            for group in manifest:
                doc_paths = {doc_type: os.path.join(recon_dir, fname)
                            for doc_type, fname in group["files"].items()}
                on_event = make_on_event(st.empty()) if stream else None
                with st.status(f"Reconciling claim group {group['group']}…"):
                    report = reconciler.run(doc_paths, on_event=on_event)

                st.markdown(f"### Claim {report.claim_number or '(unknown)'}")
                if report.ok:
                    st.success("All cross-checked fields agree.")
                else:
                    st.error("Discrepancies found:")
                    for issue in report.issues:
                        st.write(f"- **{issue.field}**: {issue.message}")

                expected = set(group.get("discrepancy_fields", []))
                if expected:
                    n_flagged += 1
                    found = {i.field for i in report.issues}
                    if found >= expected:
                        n_correct += 1

            if n_flagged:
                st.info(f"Caught {n_correct}/{n_flagged} injected discrepancies across "
                       f"{len(manifest)} claim groups.")

# --------------------------------------------------------------------------- #
# Review queue (HITL) — the human step of the enterprise ingestion pipeline.
#
# Workers (src/docproc/queue/worker.py) drain a durable queue and apply a MECHANICAL
# review policy (store.triage_decision): anything that errored, failed
# validation, resolved no denial code, or exceeds the dollar threshold is
# routed to `needs_review` instead of being auto-approved. This page is where
# a human actually works that queue — the thing that makes the pipeline
# deployable rather than just fast.
# --------------------------------------------------------------------------- #
else:
    st.subheader("Human-in-the-loop review queue")
    st.caption("Documents the pipeline's mechanical review policy flagged. "
               "Workers auto-approve only what passes every gate; everything else lands here.")

    with st.sidebar:
        st.divider()
        st.subheader("Queue")
        db_path = st.text_input("Queue DB", value=os.environ.get("QUEUE_DB", "data/queue/jobs.db"))
        batch = st.text_input("Batch", value="demo")
        reviewer = st.text_input("Reviewer name", value=os.environ.get("USER", "analyst"))

    if not os.path.exists(db_path):
        st.warning(
            f"No queue database at `{db_path}`. Start the pipeline first:\n\n"
            "```bash\n"
            "python -m src.docproc.generator --out data/docs_100 --n 100\n"
            "python -m src.docproc.queue.pipeline enqueue --docs data/docs_100 --batch demo\n"
            "python -m src.docproc.queue.worker --batch demo --threads 4\n"
            "```")
        st.stop()

    store = JobStore(db_path)
    stats = store.stats(batch=batch or None)
    by = stats["by_status"]

    # ---- pipeline dashboard (the ops view) --------------------------------
    st.markdown("#### Pipeline status")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Queued", stats["total"])
    c2.metric("Pending", by.get("pending", 0) + by.get("processing", 0))
    c3.metric("Auto-approved", by.get("auto_approved", 0),
              help="Passed every mechanical gate — no human needed.")
    c4.metric("Needs review", by.get("needs_review", 0),
              help="Flagged by the review policy. This is your worklist.")
    c5.metric("Resolved", sum(by.get(s, 0) for s in DONE_STATES))

    c6, c7, c8 = st.columns(3)
    c6.metric("Appealable $ at risk", f"${stats['appealable_dollars']:,.0f}")
    c7.metric("LLM calls", stats["llm_calls"])
    c8.metric("Processed with $0 inference", stats["processed_without_llm"],
              help="X12 EDI documents routed to the deterministic parser — no LLM call at all.")

    if stats["total"]:
        done = sum(by.get(s, 0) for s in (*DONE_STATES, "needs_review"))
        st.progress(done / stats["total"], text=f"{done}/{stats['total']} documents processed")

    st.divider()

    # ---- the worklist ------------------------------------------------------
    queue_filter = st.radio("Show", ["Needs review", "Auto-approved", "All"],
                            horizontal=True, label_visibility="collapsed")
    status_filter = {"Needs review": "needs_review",
                     "Auto-approved": "auto_approved", "All": None}[queue_filter]
    rows = store.list_jobs(status=status_filter, batch=batch or None, limit=300)

    if not rows:
        st.success("Nothing in this queue. 🎉")
        st.stop()

    st.markdown(f"#### {len(rows)} documents — ranked by dollars at risk")
    st.dataframe(
        [{"id": r["id"], "file": r["filename"], "status": r["status"],
          "$ at risk": r["dollars_at_risk"], "appealable": bool(r["is_appealable"])
          if r["is_appealable"] is not None else None,
          "category": r["denial_category"], "claim #": r["claim_number"],
          "why flagged": r["review_reason"], "path": r["ingest_kind"],
          "ocr grade": r["ocr_grade"]} for r in rows],
        width="stretch", hide_index=True)

    # ---- review one document ----------------------------------------------
    st.divider()
    st.markdown("#### Review a document")
    labels = {f"#{r['id']} · {r['filename']} · ${r['dollars_at_risk']:,.2f}": r["id"]
              for r in rows}
    picked = st.selectbox("Document", list(labels), label_visibility="collapsed")
    job = store.get(labels[picked])

    if job["review_reason"]:
        st.warning(f"**Flagged because:** {job['review_reason']}")

    left, right = st.columns([3, 2])

    with left:
        st.markdown("**Source document**")
        try:
            st.code(open(job["doc_path"], encoding="utf-8").read(), language=None,
                    height=380)
        except OSError as exc:
            st.error(f"Could not read source: {exc}")

        if job["extraction"]:
            ext = json.loads(job["extraction"])
            st.markdown("**Extracted fields** (with the span each value was grounded in)")
            fields = []
            for name in ["payer_name", "provider_name", "patient_name", "member_id",
                         "claim_number", "date_of_service", "total_charged",
                         "total_allowed", "total_paid", "patient_responsibility",
                         "appeal_deadline"]:
                fv = ext.get(name) or {}
                fields.append({"field": name, "value": fv.get("value"),
                               "source_text": fv.get("source_text")})
            st.dataframe(fields, width="stretch", hide_index=True)
            st.write(f"**Denial codes:** {', '.join(ext.get('denial_codes') or []) or '—'}")

    with right:
        if job["validation"]:
            val = json.loads(job["validation"])
            if val.get("ok"):
                st.success("Mechanical validation passed")
            else:
                st.error("Mechanical validation failed")
                for issue in val.get("issues", []):
                    st.write(f"- `[{issue.get('severity')}]` **{issue.get('field')}** — "
                             f"{issue.get('message')}")
        if job["error"]:
            st.error(f"Agent error: {job['error']}")

        st.markdown("**Agent's triage**")
        tri = json.loads(job["triage"]) if job["triage"] else {}
        st.json(tri or {"(none)": "agent produced no triage"})

        st.markdown("**Your decision**")
        with st.form(f"review_{job['id']}"):
            override = st.checkbox("Override the triage before approving")
            cats = ["coverage", "coding", "authorization", "eligibility",
                    "timely_filing", "duplicate", "contractual", "documentation", "unknown"]
            cur_cat = tri.get("denial_category") or "unknown"
            new_appealable = st.checkbox("Appealable",
                                         value=bool(tri.get("is_appealable")),
                                         disabled=not override)
            new_category = st.selectbox("Category", cats,
                                        index=cats.index(cur_cat) if cur_cat in cats else len(cats) - 1,
                                        disabled=not override)
            new_dollars = st.number_input("Dollars at risk",
                                          value=float(tri.get("dollars_at_risk") or 0.0),
                                          step=100.0, disabled=not override)
            note = st.text_area("Reviewer note", placeholder="Why you approved or rejected this…")

            b1, b2 = st.columns(2)
            approve = b1.form_submit_button("✅ Approve", type="primary", width="stretch")
            reject = b2.form_submit_button("❌ Reject", width="stretch")

            if approve or reject:
                edited = None
                if override:
                    edited = {**tri, "is_appealable": new_appealable,
                              "denial_category": new_category,
                              "dollars_at_risk": new_dollars}
                store.record_review(job["id"], "approved" if approve else "rejected",
                                    reviewer=reviewer, note=note or None,
                                    edited_triage=edited)
                st.success(f"Recorded: {'approved' if approve else 'rejected'} by {reviewer}.")
                st.rerun()
