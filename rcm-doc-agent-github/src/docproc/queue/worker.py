"""Pipeline worker — the horizontally-scalable unit of enterprise ingestion.

One worker process pulls documents off the durable queue (`store.JobStore`),
routes each through the ingestion router (`ingest.py` — EDI goes down the
zero-LLM deterministic path, PDF/image through Docling, prose to the LLM
agent), then applies a mechanical review policy that decides whether a human
must look at the result before it counts.

    docker compose up --scale worker=4

...runs four of these against the same queue. Nothing here is worker-aware:
jobs are claimed atomically, `DocumentAgent` instances are per-document and
stateless, and each document writes its own trace file. That's what makes
"add another container" a valid scaling move rather than a race condition.

Threads inside a worker handle I/O concurrency (LLM latency); containers
handle everything above that. The shared `TokenBucket` paces the whole
process so concurrency doesn't just convert into rate-limit errors -- the
real failure mode measured before this existed (see LEARNING.md).
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.config import get_settings
from src.llm import LLMClient
from src.docproc.agent import DocumentAgent
from src.docproc.ingestion.ingest import finalize_structured, ingest
from src.docproc.queue.ratelimit import NullLimiter, TokenBucket, estimate_tokens
from src.docproc.queue.store import JobStore, ReviewPolicy, triage_decision


class Worker:
    """Claims jobs from the queue until it's drained (or `--follow` forever)."""

    def __init__(self, store: JobStore, settings, llm: LLMClient,
                 limiter=None, policy: ReviewPolicy | None = None,
                 worker_id: str | None = None):
        self.store = store
        self.settings = settings
        self.llm = llm
        self.limiter = limiter or NullLimiter()
        self.policy = policy or ReviewPolicy()
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self._processed = 0
        self._lock = threading.Lock()

    def run_once(self, batch: str | None = None) -> bool:
        """Claim and process a single job. Returns False when the queue is empty."""
        job = self.store.claim_next(self.worker_id, batch=batch)
        if job is None:
            return False
        self._process(job)
        return True

    def run_until_drained(self, batch: str | None = None, threads: int = 1,
                          follow: bool = False, poll_interval: float = 2.0) -> int:
        """Drain the queue. With `threads > 1`, several documents are in
        flight at once inside this process; the shared limiter keeps the
        aggregate request rate under the account's quota.

        `follow=True` keeps polling after the queue empties (long-running
        service mode, e.g. under docker compose) instead of exiting.
        """
        stop = threading.Event()

        def loop() -> None:
            while not stop.is_set():
                if not self.run_once(batch=batch):
                    if not follow:
                        return
                    time.sleep(poll_interval)

        if threads <= 1:
            loop()
        else:
            with ThreadPoolExecutor(max_workers=threads) as pool:
                futures = [pool.submit(loop) for _ in range(threads)]
                for f in futures:
                    f.result()
        return self._processed

    # ------------------------------------------------------------------ core
    def _process(self, job) -> None:
        """Ingest → extract → validate → triage → route. Any exception is
        recorded against the job (retryable) rather than killing the worker:
        one poison document must not stop a fleet."""
        started = time.monotonic()
        try:
            ingested = ingest(job["doc_path"])

            if ingested.kind == "structured":
                # Already-structured EDI: no LLM call at all, so no rate-limit
                # budget is consumed. At real payer volumes this is the
                # majority path and the main reason cost doesn't scale
                # linearly with document count.
                raw = open(job["doc_path"], encoding="utf-8").read()
                outcome = finalize_structured(ingested.extraction, raw)
                llm_calls = 0
                ingest_kind = "edi"
                ocr_grade = ocr_low_grade = None
            else:
                text = ingested.text or ""
                self.limiter.acquire(estimate_tokens(text, max_steps=3))
                agent = DocumentAgent(self.settings, self.llm)
                outcome = agent.run(text, job["filename"], ocr_low_grade=ingested.ocr_low_grade)
                llm_calls = outcome.steps_used
                ingest_kind = "docling" if "Docling" in ingested.source_note else "text"
                ocr_grade, ocr_low_grade = ingested.ocr_grade, ingested.ocr_low_grade

            ext, tri, val = outcome.extraction, outcome.triage, outcome.validation
            dollars = float(getattr(tri, "dollars_at_risk", 0.0) or 0.0)
            category = getattr(tri, "denial_category", None)
            val_dict = val.model_dump(mode="json") if val else None
            status, reason = triage_decision(
                outcome.status, bool(val and val.ok), category, dollars, self.policy,
                message=outcome.message,
                validation_issues=(val_dict or {}).get("issues"),
                deterministic=(ingest_kind == "edi"),
                ocr_low_grade=ocr_low_grade,
                job_id=job["id"])

            self.store.complete(
                job["id"], status=status, review_reason=reason,
                extraction=ext.model_dump(mode="json") if ext else None,
                validation=val_dict,
                triage=tri.model_dump(mode="json") if tri else None,
                claim_number=ext.claim_number.value if ext else None,
                payer_name=ext.payer_name.value if ext else None,
                is_appealable=getattr(tri, "is_appealable", None),
                denial_category=category, dollars_at_risk=dollars,
                ingest_kind=ingest_kind, ocr_grade=ocr_grade, ocr_low_grade=ocr_low_grade,
                llm_calls=llm_calls,
                duration_s=round(time.monotonic() - started, 2),
                trace_path=outcome.trace_path,
                error=outcome.message if outcome.status == "error" else None)
            with self._lock:
                self._processed += 1
            print(f"[{self.worker_id}] job {job['id']} {job['filename']} -> {status} "
                  f"({ingest_kind}, {llm_calls} llm calls, "
                  f"{time.monotonic() - started:.1f}s)", flush=True)

        except Exception as exc:  # noqa: BLE001 - a bad document must not kill the fleet
            new_status = self.store.fail(job["id"], f"{type(exc).__name__}: {exc}")
            print(f"[{self.worker_id}] job {job['id']} {job['filename']} FAILED "
                  f"({type(exc).__name__}) -> {new_status}", flush=True)


def build_worker(args) -> Worker:
    """Assemble a worker from CLI args -- shared by `__main__` here and by
    the enqueue+drain convenience path in `pipeline.py`."""
    settings = get_settings(model=args.model)
    store = JobStore(args.db)
    limiter = (TokenBucket(args.tpm, num_processes=args.processes)
               if args.tpm > 0 else NullLimiter())
    policy = ReviewPolicy(high_value_threshold=args.review_threshold, qa_sample_rate=args.qa_sample_rate)
    return Worker(store, settings, LLMClient(settings), limiter=limiter, policy=policy)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pipeline worker: drain the document queue.")
    p.add_argument("--db", default=os.environ.get("QUEUE_DB", "data/queue/jobs.db"))
    p.add_argument("--batch", default=None, help="Only process this batch.")
    p.add_argument("--threads", type=int, default=int(os.environ.get("WORKER_THREADS", "2")),
                   help="Concurrent documents in flight inside this process.")
    p.add_argument("--tpm", type=int, default=int(os.environ.get("LLM_TPM", "30000")),
                   help="Account tokens-per-minute budget shared by this process. 0 disables pacing.")
    p.add_argument("--processes", type=int, default=int(os.environ.get("WORKER_REPLICAS", "1")),
                   help="How many worker containers share the TPM budget (splits it evenly).")
    p.add_argument("--review-threshold", type=float,
                   default=float(os.environ.get("REVIEW_THRESHOLD", "5000")),
                   help="Dollars at risk at/above which a human always reviews.")
    p.add_argument("--qa-sample-rate", type=float,
                   default=float(os.environ.get("QA_SAMPLE_RATE", "0")),
                   help="Fraction (0-1) of otherwise-clean documents routed to human review "
                        "anyway, as an ongoing accuracy audit independent of any single "
                        "corpus's measured accuracy.")
    p.add_argument("--model", default=None, help="LiteLLM model string override.")
    p.add_argument("--follow", action="store_true",
                   help="Keep polling after the queue drains (service mode).")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    worker = build_worker(args)
    print(f"[{worker.worker_id}] worker up: db={args.db} threads={args.threads} "
          f"tpm={args.tpm}/{args.processes} follow={args.follow}", flush=True)
    n = worker.run_until_drained(batch=args.batch, threads=args.threads, follow=args.follow)
    limiter = worker.limiter
    print(f"[{worker.worker_id}] drained: {n} documents processed; "
          f"rate-limit waits: {limiter.waits} ({limiter.total_wait_s:.1f}s total)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
