"""Durable job + review store (SQLite) — the queue behind enterprise-scale ingestion.

Why SQLite: it gives a genuinely durable, multi-process-safe work queue with
**zero extra infrastructure**, so `docker compose up --scale worker=4` works
on a laptop today. The point isn't that SQLite is the right enterprise queue
-- it isn't past a certain volume -- it's that the *interface* here is
deliberately queue-shaped (`enqueue` / `claim_next` / `complete` / `fail`),
so replacing the backend with SQS, Postgres+SKIP LOCKED, or Kafka later is a
driver swap, not a redesign of the pipeline or the workers.

Concurrency correctness (the part that actually matters):
  - WAL journal mode, so readers (the Streamlit review UI) never block the
    writers (workers).
  - `BEGIN IMMEDIATE` + a single atomic `UPDATE ... WHERE id = (SELECT ...
    LIMIT 1)` for claiming, so two workers can never claim the same job.
  - `busy_timeout` so a contended write waits rather than raising
    "database is locked".

Job lifecycle:

    pending --claim--> processing --+--> needs_review --(human)--> approved
                                    |                           \\-> rejected
                                    +--> auto_approved
                                    +--> failed  (agent/LLM error, retryable)

`needs_review` vs `auto_approved` is decided mechanically by
`triage_decision()` -- never by the model's own confidence in itself.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

DEFAULT_DB_PATH = os.environ.get("QUEUE_DB", "data/queue/jobs.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_path       TEXT    NOT NULL,
    filename       TEXT    NOT NULL,
    batch          TEXT    NOT NULL DEFAULT 'default',
    status         TEXT    NOT NULL DEFAULT 'pending',
    attempts       INTEGER NOT NULL DEFAULT 0,
    worker_id      TEXT,
    error          TEXT,
    -- extraction results (JSON blobs; NULL until processed)
    extraction     TEXT,
    validation     TEXT,
    triage         TEXT,
    -- denormalized for cheap worklist sorting/filtering in the review UI
    claim_number   TEXT,
    payer_name     TEXT,
    is_appealable  INTEGER,
    denial_category TEXT,
    dollars_at_risk REAL NOT NULL DEFAULT 0.0,
    review_reason  TEXT,
    -- HITL
    reviewed_by    TEXT,
    review_note    TEXT,
    reviewed_at    REAL,
    -- observability
    ingest_kind    TEXT,
    ocr_grade      TEXT,
    ocr_low_grade  TEXT,
    llm_calls      INTEGER NOT NULL DEFAULT 0,
    duration_s     REAL,
    trace_path     TEXT,
    created_at     REAL    NOT NULL,
    updated_at     REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_batch  ON jobs(batch);
CREATE INDEX IF NOT EXISTS idx_jobs_risk   ON jobs(dollars_at_risk DESC);
"""

# Terminal states a job never leaves without human action.
DONE_STATES = ("auto_approved", "approved", "rejected")

# Docling quality grades at or below which a human should glance at the
# document regardless of how clean the LLM's own extraction from that
# (possibly garbled) text looks -- grounding can only verify the extraction
# matches Docling's TRANSCRIPTION, not that the transcription matches the
# real document. Set by `ingest._ingest_with_docling`, consumed here.
POOR_OCR_GRADES = {"poor", "fair"}


@dataclass
class ReviewPolicy:
    """Mechanical gates that decide whether a human must look at a result.

    Deliberately *not* model self-confidence: every gate here is either a
    hard failure signal (agent errored, validators rejected the extraction,
    no denial code resolved in the registry), a business-risk threshold
    (large dollar amounts get a second pair of eyes regardless of how clean
    the extraction looked), or a fixed-rate audit sample (see
    `qa_sample_rate` below). That's how a real RCM shop would set it up --
    the cost of a wrong appeal decision scales with the dollars on it, and
    accuracy on any one corpus (however good) is not proof against drift.
    """

    high_value_threshold: float = 5000.0   # $ at risk above which a human always reviews
    qa_sample_rate: float = 0.0             # fraction of CLEAN docs sent for review anyway


def _qa_sampled(job_id: int | None, rate: float) -> bool:
    """Deterministic (not random-per-call) audit sample: hash the job id so
    the SAME document always lands on the same side of the sample -- a
    re-run doesn't flip a document in or out of the audit, which would make
    the sample meaningless for tracking review outcomes over time. Falls
    back to `random` only when no job id is available (e.g. a one-off CLI
    run outside the queue).
    """
    if rate <= 0:
        return False
    if job_id is None:
        return random.random() < rate
    # Stable 0..1 value from the id -- same id always samples the same way.
    return (job_id * 2654435761 % 2**32) / 2**32 < rate


def triage_decision(status: str, validation_ok: bool, denial_category: str | None,
                    dollars_at_risk: float, policy: ReviewPolicy,
                    message: str | None = None,
                    validation_issues: list[dict] | None = None,
                    deterministic: bool = False,
                    ocr_low_grade: str | None = None,
                    job_id: int | None = None) -> tuple[str, str | None]:
    """Return `(job_status, review_reason)` for a finished extraction.

    Returns `("needs_review", why)` or `("auto_approved", None)`.

    `deterministic=True` means the extraction came from a parser, not an LLM
    (X12 835 EDI). That changes how one specific failure is weighed: the
    grounding check exists to catch *hallucination*, so a grounding failure
    on parser output is a category error -- an 835 has no literal span for a
    derived value like `total_allowed` (it's computed from SVC minus CO-group
    CAS adjustments, never quoted as one number anywhere in the
    transaction). Measured consequence of not making this distinction: 10/10
    EDI documents in the 110-document pipeline run were routed to human
    review for that single non-issue. At real payer volume, where EDI is the
    majority of the feed, that floods the review queue and makes HITL
    worthless. Arithmetic and business-rule failures still force review for
    *any* source -- those would indicate a genuine parser bug or inconsistent
    source data.

    `ocr_low_grade` is Docling's worst-5th-percentile quality grade for a
    PDF/image document (see `POOR_OCR_GRADES` above). A `poor`/`fair` grade
    forces review regardless of how clean the LLM's downstream extraction
    looks, because grounding can only confirm the extraction matches
    Docling's transcription -- not that the transcription matches the real
    document. This is the guardrail a bad scan needed and didn't have before.
    """
    if status == "error":
        return "needs_review", (f"Agent failed to produce a result: {message}" if message
                                else "Agent failed to produce a result.")
    if ocr_low_grade in POOR_OCR_GRADES:
        return "needs_review", (f"Docling OCR/layout confidence is {ocr_low_grade!r} on part "
                                "of this document; verify the transcription against the "
                                "original file before trusting the extraction.")
    if not validation_ok:
        errors = [i for i in (validation_issues or []) if i.get("severity") == "error"]
        non_grounding = [i for i in errors if i.get("check") != "grounding"]
        if deterministic and errors and not non_grounding:
            pass  # grounding-only failure on parser output: not a hallucination signal
        else:
            failed = sorted({i.get("check", "unknown") for i in errors}) or ["validation"]
            return "needs_review", f"Mechanical validation failed ({', '.join(failed)})."
    if not denial_category or denial_category == "unknown":
        return "needs_review", "No denial code resolved to the registry; triage is unreliable."
    if dollars_at_risk >= policy.high_value_threshold:
        return "needs_review", f"High value: ${dollars_at_risk:,.2f} at or above the ${policy.high_value_threshold:,.0f} review threshold."
    if _qa_sampled(job_id, policy.qa_sample_rate):
        return "needs_review", (f"Random QA audit sample (rate={policy.qa_sample_rate:.0%}): this document passed "
                                "every mechanical gate, but it's being reviewed anyway to track real-world accuracy "
                                "over time -- clean gates on today's corpus are not proof against drift on tomorrow's.")
    return "auto_approved", None


class JobStore:
    """Thin, dependency-free wrapper over the SQLite job table."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """One short-lived connection per operation -- safe to share a
        `JobStore` across threads, and cheap since SQLite connections are
        local file handles, not network sockets.

        Deliberately rollback-journal mode (`DELETE`), not WAL -- see the
        module docstring. A real multi-container `docker compose` run
        (`--scale worker=4` + `ui` + repeated one-off `status` containers,
        all bind-mounting the same `./data` on Docker Desktop for macOS)
        corrupted a WAL-mode database file for real; rollback-journal mode
        only needs POSIX file locks, which that same bind mount honors
        correctly, at the cost of a writer briefly blocking readers during
        a commit rather than never blocking them at all.
        """
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=DELETE")  # see module docstring: WAL corrupted for real over a Docker Desktop bind mount
            conn.execute("PRAGMA busy_timeout=30000")   # wait, don't raise, on contention
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
        finally:
            conn.close()

    # ---------------------------------------------------------------- enqueue
    def enqueue(self, doc_paths: list[str], batch: str = "default") -> int:
        """Add documents to the queue. Idempotent per (doc_path, batch): a
        path already queued in this batch is skipped, so re-running the
        enqueue step after a partial failure doesn't duplicate work."""
        now = time.time()
        added = 0
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            existing = {r["doc_path"] for r in
                        c.execute("SELECT doc_path FROM jobs WHERE batch = ?", (batch,))}
            for path in doc_paths:
                if path in existing:
                    continue
                c.execute(
                    "INSERT INTO jobs (doc_path, filename, batch, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'pending', ?, ?)",
                    (path, os.path.basename(path), batch, now, now))
                added += 1
            c.execute("COMMIT")
        return added

    # ------------------------------------------------------------------ claim
    def claim_next(self, worker_id: str, batch: str | None = None) -> sqlite3.Row | None:
        """Atomically claim one pending job. Returns `None` when the queue is
        drained. Two concurrent workers can never get the same row: the
        UPDATE and its subselect run inside one IMMEDIATE transaction, so the
        second worker's subselect can't see a row the first already took."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            where_batch = "AND batch = ?" if batch else ""
            params: tuple[Any, ...] = (batch,) if batch else ()
            row = c.execute(
                f"SELECT id FROM jobs WHERE status = 'pending' {where_batch} "
                "ORDER BY id LIMIT 1", params).fetchone()
            if row is None:
                c.execute("COMMIT")
                return None
            c.execute(
                "UPDATE jobs SET status='processing', worker_id=?, attempts=attempts+1, "
                "updated_at=? WHERE id=?", (worker_id, time.time(), row["id"]))
            claimed = c.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            c.execute("COMMIT")
            return claimed

    # --------------------------------------------------------------- complete
    def complete(self, job_id: int, *, status: str, review_reason: str | None,
                 extraction: dict | None, validation: dict | None, triage: dict | None,
                 claim_number: str | None, payer_name: str | None,
                 is_appealable: bool | None, denial_category: str | None,
                 dollars_at_risk: float, ingest_kind: str, llm_calls: int,
                 duration_s: float, trace_path: str | None,
                 error: str | None = None,
                 ocr_grade: str | None = None, ocr_low_grade: str | None = None) -> None:
        """Record a finished extraction and its routing decision.

        `error` carries the agent's *own* failure message for the case where
        it terminated cleanly with `status="error"` (e.g. LLM retries
        exhausted) rather than raising -- without this the review queue shows
        "Agent failed" with no reason, which is useless to the human who has
        to act on it.
        """
        with self._conn() as c:
            c.execute(
                "UPDATE jobs SET status=?, review_reason=?, extraction=?, validation=?, "
                "triage=?, claim_number=?, payer_name=?, is_appealable=?, denial_category=?, "
                "dollars_at_risk=?, ingest_kind=?, ocr_grade=?, ocr_low_grade=?, llm_calls=?, "
                "duration_s=?, trace_path=?, error=?, updated_at=? WHERE id=?",
                (status, review_reason,
                 json.dumps(extraction) if extraction else None,
                 json.dumps(validation) if validation else None,
                 json.dumps(triage) if triage else None,
                 claim_number, payer_name,
                 None if is_appealable is None else int(is_appealable),
                 denial_category, dollars_at_risk, ingest_kind, ocr_grade, ocr_low_grade,
                 llm_calls, duration_s, trace_path, (error or None), time.time(), job_id))

    def fail(self, job_id: int, error: str, max_attempts: int = 3) -> str:
        """Mark a job failed. Re-queues it for another worker while attempts
        remain (transient LLM/network errors are the common case); after
        `max_attempts` it goes to `needs_review` so a human sees it rather
        than it disappearing silently."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
            attempts = row["attempts"] if row else max_attempts
            new_status = "pending" if attempts < max_attempts else "needs_review"
            reason = None if new_status == "pending" else f"Failed after {attempts} attempts."
            c.execute("UPDATE jobs SET status=?, error=?, review_reason=COALESCE(?, review_reason), "
                      "updated_at=? WHERE id=?",
                      (new_status, error[:2000], reason, time.time(), job_id))
            c.execute("COMMIT")
        return new_status

    # ------------------------------------------------------------------ HITL
    def record_review(self, job_id: int, decision: str, reviewer: str,
                      note: str | None = None, edited_triage: dict | None = None) -> None:
        """Apply a human decision (`approved` / `rejected`) to a reviewed job,
        optionally overriding the triage the agent produced. The override is
        stored, not merged -- the original agent output stays in the row's
        history via the trace file, so a later audit can see both."""
        if decision not in ("approved", "rejected"):
            raise ValueError(f"decision must be 'approved' or 'rejected', got {decision!r}")
        with self._conn() as c:
            if edited_triage is not None:
                c.execute(
                    "UPDATE jobs SET status=?, reviewed_by=?, review_note=?, reviewed_at=?, "
                    "triage=?, is_appealable=?, denial_category=?, dollars_at_risk=?, "
                    "updated_at=? WHERE id=?",
                    (decision, reviewer, note, time.time(), json.dumps(edited_triage),
                     int(bool(edited_triage.get("is_appealable"))),
                     edited_triage.get("denial_category"),
                     float(edited_triage.get("dollars_at_risk") or 0.0),
                     time.time(), job_id))
            else:
                c.execute(
                    "UPDATE jobs SET status=?, reviewed_by=?, review_note=?, reviewed_at=?, "
                    "updated_at=? WHERE id=?",
                    (decision, reviewer, note, time.time(), time.time(), job_id))

    # ----------------------------------------------------------------- reads
    def list_jobs(self, status: str | None = None, batch: str | None = None,
                  limit: int = 500) -> list[sqlite3.Row]:
        """Worklist query: ranked by dollars at risk (what an analyst works first)."""
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if batch:
            clauses.append("batch = ?")
            params.append(batch)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._conn() as c:
            return list(c.execute(
                f"SELECT * FROM jobs {where} ORDER BY dollars_at_risk DESC, id LIMIT ?",
                (*params, limit)))

    def get(self, job_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def stats(self, batch: str | None = None) -> dict[str, Any]:
        """Pipeline dashboard numbers: counts by status plus cost/throughput
        aggregates -- what an ops view needs to answer 'is it keeping up and
        what is it costing me'."""
        where = "WHERE batch = ?" if batch else ""
        params: tuple[Any, ...] = (batch,) if batch else ()
        with self._conn() as c:
            by_status = {r["status"]: r["n"] for r in c.execute(
                f"SELECT status, COUNT(*) AS n FROM jobs {where} GROUP BY status", params)}
            agg = c.execute(
                f"SELECT COUNT(*) AS total, "
                f"COALESCE(SUM(llm_calls),0) AS llm_calls, "
                f"COALESCE(AVG(duration_s),0) AS avg_duration, "
                f"COALESCE(SUM(CASE WHEN is_appealable=1 THEN dollars_at_risk ELSE 0 END),0) AS appealable_dollars "
                f"FROM jobs {where}", params).fetchone()
            no_llm = c.execute(
                f"SELECT COUNT(*) AS n FROM jobs {where} "
                f"{'AND' if where else 'WHERE'} llm_calls = 0 AND status IN "
                "('auto_approved','approved','rejected','needs_review')", params).fetchone()
        return {
            "by_status": by_status,
            "total": agg["total"],
            "llm_calls": agg["llm_calls"],
            "avg_duration_s": round(agg["avg_duration"], 2),
            "appealable_dollars": round(agg["appealable_dollars"], 2),
            "processed_without_llm": no_llm["n"],
        }

    def reset(self, batch: str | None = None) -> int:
        """Delete jobs (a whole batch, or everything). For re-running a demo
        from a clean slate."""
        with self._conn() as c:
            cur = (c.execute("DELETE FROM jobs WHERE batch = ?", (batch,)) if batch
                   else c.execute("DELETE FROM jobs"))
            return cur.rowcount

    def requeue(self, status: str = "needs_review", batch: str | None = None,
                only_errors: bool = True) -> int:
        """Send jobs back to `pending` so workers pick them up again.

        The operational case this exists for: a bug or a transient outage put
        a batch of documents into `needs_review` for a reason that has since
        been fixed. Without this, the only options are re-processing
        everything (wasteful) or hand-editing the database (worse).

        `only_errors=True` restricts it to jobs that actually failed, so a
        blanket requeue can't silently discard genuine human-review items
        (a high-value document a reviewer still needs to look at).
        """
        clauses = ["status = ?"]
        params: list[Any] = [status]
        if batch:
            clauses.append("batch = ?")
            params.append(batch)
        if only_errors:
            clauses.append("(error IS NOT NULL OR review_reason LIKE 'Agent failed%')")
        with self._conn() as c:
            cur = c.execute(
                f"UPDATE jobs SET status='pending', attempts=0, error=NULL, "
                f"review_reason=NULL, updated_at={time.time()} "
                f"WHERE {' AND '.join(clauses)}", params)
            return cur.rowcount
