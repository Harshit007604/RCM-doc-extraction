"""Unit tests for src/docproc/store.py -- the durable job queue and the
mechanical HITL review policy. Uses a temp SQLite file per test (via the
`store` fixture below), never the real data/queue/jobs.db."""

from __future__ import annotations

import pytest

from src.docproc.queue.store import JobStore, ReviewPolicy, triage_decision


@pytest.fixture
def store(tmp_path) -> JobStore:
    return JobStore(db_path=str(tmp_path / "test_jobs.db"))


class TestEnqueueAndClaim:
    def test_enqueue_is_idempotent_per_batch(self, store):
        added_first = store.enqueue(["a.txt", "b.txt"], batch="demo")
        added_second = store.enqueue(["a.txt", "b.txt", "c.txt"], batch="demo")
        assert added_first == 2
        assert added_second == 1  # only c.txt is new
        assert store.stats(batch="demo")["total"] == 3

    def test_claim_next_returns_none_when_drained(self, store):
        assert store.claim_next("worker-1") is None

    def test_claim_next_marks_processing_and_increments_attempts(self, store):
        store.enqueue(["a.txt"], batch="demo")
        job = store.claim_next("worker-1", batch="demo")
        assert job["status"] == "processing"
        assert job["attempts"] == 1
        assert job["worker_id"] == "worker-1"

    def test_two_workers_never_claim_the_same_job(self, store):
        """The concurrency-critical guarantee: claim_next is atomic."""
        store.enqueue(["only-one.txt"], batch="demo")
        first = store.claim_next("worker-1", batch="demo")
        second = store.claim_next("worker-2", batch="demo")
        assert first is not None
        assert second is None


class TestCompleteAndFail:
    def test_complete_records_full_result(self, store):
        store.enqueue(["a.txt"], batch="demo")
        job = store.claim_next("worker-1", batch="demo")
        store.complete(
            job["id"], status="auto_approved", review_reason=None,
            extraction={"doc_type": "eob"}, validation={"ok": True}, triage={"is_appealable": True},
            claim_number="CLM123", payer_name="Acme Payer", is_appealable=True,
            denial_category="coding", dollars_at_risk=100.0, ingest_kind="text",
            llm_calls=2, duration_s=1.5, trace_path="logs/run_x.jsonl",
        )
        row = store.get(job["id"])
        assert row["status"] == "auto_approved"
        assert row["claim_number"] == "CLM123"
        assert row["dollars_at_risk"] == 100.0

    def test_fail_requeues_while_attempts_remain(self, store):
        store.enqueue(["a.txt"], batch="demo")
        job = store.claim_next("worker-1", batch="demo")  # attempts=1
        new_status = store.fail(job["id"], "transient error", max_attempts=3)
        assert new_status == "pending"
        assert store.get(job["id"])["error"] == "transient error"

    def test_fail_routes_to_needs_review_after_max_attempts(self, store):
        store.enqueue(["a.txt"], batch="demo")
        for _ in range(3):
            job = store.claim_next("worker-1", batch="demo")
            new_status = store.fail(job["id"], "still failing", max_attempts=3)
            if new_status == "pending":
                continue
        assert new_status == "needs_review"

    def test_complete_persists_the_agents_own_error_message(self, store):
        """Regression test for the bug where the review queue showed
        'Agent failed' with no reason -- outcome.message must survive into
        the stored row."""
        store.enqueue(["a.txt"], batch="demo")
        job = store.claim_next("worker-1", batch="demo")
        store.complete(
            job["id"], status="needs_review", review_reason="Agent failed: boom",
            extraction=None, validation=None, triage=None, claim_number=None,
            payer_name=None, is_appealable=None, denial_category=None,
            dollars_at_risk=0.0, ingest_kind="text", llm_calls=1, duration_s=0.5,
            trace_path=None, error="LLM unavailable: could not obtain valid DocStep JSON",
        )
        row = store.get(job["id"])
        assert "could not obtain valid DocStep JSON" in row["error"]


class TestRequeue:
    def test_requeue_only_errors_by_default(self, store):
        store.enqueue(["a.txt", "b.txt"], batch="demo")
        job_a = store.claim_next("w1", batch="demo")
        job_b = store.claim_next("w1", batch="demo")
        store.complete(job_a["id"], status="needs_review", review_reason="Agent failed: x",
                       extraction=None, validation=None, triage=None, claim_number=None,
                       payer_name=None, is_appealable=None, denial_category=None,
                       dollars_at_risk=0.0, ingest_kind="text", llm_calls=1, duration_s=0.1,
                       trace_path=None, error="boom")
        store.complete(job_b["id"], status="needs_review",
                       review_reason="High value: $9,999.00 at or above threshold",
                       extraction=None, validation=None, triage=None, claim_number=None,
                       payer_name=None, is_appealable=True, denial_category="coverage",
                       dollars_at_risk=9999.0, ingest_kind="text", llm_calls=2, duration_s=1.0,
                       trace_path=None)
        n = store.requeue(batch="demo", only_errors=True)
        assert n == 1  # only the errored one, not the genuine high-value review
        assert store.get(job_a["id"])["status"] == "pending"
        assert store.get(job_b["id"])["status"] == "needs_review"


class TestTriageDecision:
    def test_agent_error_forces_review(self):
        status, reason = triage_decision("error", True, "coding", 0.0, ReviewPolicy(),
                                         message="boom")
        assert status == "needs_review"
        assert "boom" in reason

    def test_high_value_forces_review_regardless_of_cleanliness(self):
        status, _ = triage_decision("ok", True, "coverage", 10_000.0, ReviewPolicy())
        assert status == "needs_review"

    def test_unresolved_denial_category_forces_review(self):
        status, _ = triage_decision("ok", True, "unknown", 100.0, ReviewPolicy())
        assert status == "needs_review"

    def test_clean_low_value_result_auto_approves(self):
        status, reason = triage_decision("ok", True, "coding", 100.0, ReviewPolicy())
        assert status == "auto_approved"
        assert reason is None

    def test_grounding_only_failure_on_deterministic_source_is_not_flagged(self):
        """The EDI-review-flood fix: a grounding-only error on parser output
        (e.g. total_allowed with no source span) must NOT force review."""
        issues = [{"field": "total_allowed", "severity": "error", "check": "grounding"}]
        status, _ = triage_decision("ok", False, "coding", 100.0, ReviewPolicy(),
                                    validation_issues=issues, deterministic=True)
        assert status == "auto_approved"

    def test_grounding_failure_on_llm_output_still_forces_review(self):
        """The same failure on LLM-generated text IS a real hallucination
        signal and must still force review."""
        issues = [{"field": "total_charged", "severity": "error", "check": "grounding"}]
        status, _ = triage_decision("ok", False, "coding", 100.0, ReviewPolicy(),
                                    validation_issues=issues, deterministic=False)
        assert status == "needs_review"

    def test_arithmetic_failure_forces_review_even_when_deterministic(self):
        """Arithmetic/business-rule failures indicate a genuine parser bug
        or bad source data -- these must force review for ANY source."""
        issues = [{"field": "total_charged", "severity": "error", "check": "arithmetic"}]
        status, _ = triage_decision("ok", False, "coding", 100.0, ReviewPolicy(),
                                    validation_issues=issues, deterministic=True)
        assert status == "needs_review"

    def test_poor_ocr_grade_forces_review_regardless_of_extraction_cleanliness(self):
        """Docling confidence gate: a poor/fair OCR grade forces review even
        if the downstream LLM extraction looks perfectly clean, because
        grounding can only confirm the extraction matches the (possibly
        wrong) transcription, not the real document."""
        status, reason = triage_decision("ok", True, "coding", 100.0, ReviewPolicy(),
                                         ocr_low_grade="fair")
        assert status == "needs_review"
        assert "fair" in reason

    def test_good_ocr_grade_does_not_force_review(self):
        status, _ = triage_decision("ok", True, "coding", 100.0, ReviewPolicy(),
                                    ocr_low_grade="good")
        assert status == "auto_approved"


class TestQaSampleRate:
    """Random audit sampling: even a document that clears every mechanical
    gate can still be routed to review, at a fixed rate, purely to monitor
    real-world accuracy over time. This is what keeps human review doing
    something meaningful once extraction quality is already high."""

    def test_zero_rate_never_samples(self):
        for job_id in range(50):
            status, _ = triage_decision("ok", True, "coding", 100.0,
                                        ReviewPolicy(qa_sample_rate=0.0), job_id=job_id)
            assert status == "auto_approved"

    def test_full_rate_always_samples(self):
        status, reason = triage_decision("ok", True, "coding", 100.0,
                                          ReviewPolicy(qa_sample_rate=1.0), job_id=42)
        assert status == "needs_review"
        assert "QA audit" in reason

    def test_same_job_id_samples_the_same_way_every_time(self):
        """Stability: a re-run of the same job must not flip in/out of the
        audit sample, or the sample is meaningless for tracking outcomes."""
        policy = ReviewPolicy(qa_sample_rate=0.3)
        first = triage_decision("ok", True, "coding", 100.0, policy, job_id=7)
        second = triage_decision("ok", True, "coding", 100.0, policy, job_id=7)
        assert first == second

    def test_sample_rate_is_only_reached_after_all_other_gates_pass(self):
        """A high-value document is still flagged for the high-value reason,
        not silently reclassified as a QA sample."""
        status, reason = triage_decision("ok", True, "coding", 10_000.0,
                                         ReviewPolicy(qa_sample_rate=0.0))
        assert status == "needs_review"
        assert "High value" in reason
