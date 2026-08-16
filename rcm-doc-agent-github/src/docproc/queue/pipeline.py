"""Pipeline control CLI — enqueue work, watch status, reset.

The operator-facing half of the enterprise ingestion demo (`worker.py` is the
compute half). Deliberately separated: in a real deployment the thing that
*submits* work (an SFTP poller, an API endpoint, a clearinghouse feed
listener) is a different service from the thing that *processes* it, and
splitting them here keeps that boundary honest rather than hiding it behind
one "run everything" function.

    # 1. queue 100 documents
    python -m src.docproc.queue.pipeline enqueue --docs data/docs_100 --batch demo

    # 2. process them (run several of these, or `docker compose up --scale worker=4`)
    python -m src.docproc.queue.worker --batch demo --threads 4

    # 3. watch it
    python -m src.docproc.queue.pipeline status --batch demo

    # 4. review what the policy flagged, in the Streamlit HITL queue
    streamlit run ui/streamlit_app.py   ->  "Review queue (HITL)"
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.docproc.queue.store import DONE_STATES, JobStore

INGESTIBLE = ("*.txt", "*.edi", "*.835", "*.x12", "*.pdf", "*.png", "*.jpg", "*.jpeg", "*.docx")


def discover(docs_dir: str) -> list[str]:
    """Every ingestible document in a directory -- all the formats the
    ingestion router knows how to route, not just `.txt`."""
    paths: list[str] = []
    for pattern in INGESTIBLE:
        paths.extend(glob.glob(os.path.join(docs_dir, pattern)))
    return sorted(paths)


def cmd_enqueue(args) -> int:
    paths = discover(args.docs)
    if not paths:
        print(f"No ingestible documents in '{args.docs}'.", file=sys.stderr)
        return 2
    added = JobStore(args.db).enqueue(paths, batch=args.batch)
    print(f"Queued {added} new documents into batch '{args.batch}' "
          f"({len(paths) - added} already present).")
    return 0


def cmd_status(args) -> int:
    store = JobStore(args.db)
    s = store.stats(batch=args.batch)
    by = s["by_status"]
    print(f"\n=== PIPELINE STATUS (batch={args.batch or 'all'}) ===")
    print(f"  total queued            {s['total']}")
    for state in ("pending", "processing", "needs_review", "auto_approved",
                  "approved", "rejected"):
        if by.get(state):
            print(f"  {state:<24}{by[state]}")
    done = sum(by.get(st, 0) for st in DONE_STATES)
    print(f"\n  completed (no human needed)  {by.get('auto_approved', 0)}")
    print(f"  awaiting human review        {by.get('needs_review', 0)}")
    print(f"  fully resolved               {done}")
    print(f"\n  total LLM calls         {s['llm_calls']}")
    print(f"  processed WITHOUT an LLM {s['processed_without_llm']}  "
          f"(EDI fast path -- $0 inference cost)")
    print(f"  avg seconds/document    {s['avg_duration_s']}")
    print(f"  appealable $ at risk    ${s['appealable_dollars']:,.2f}")

    if args.top:
        rows = [r for r in store.list_jobs(batch=args.batch, limit=args.top)
                if r["status"] in (*DONE_STATES, "needs_review")]
        if rows:
            print(f"\n  Top {len(rows)} by dollars at risk:")
            for r in rows:
                flag = "REVIEW" if r["status"] == "needs_review" else r["status"]
                print(f"    {r['filename']:<36} {flag:<14} ${r['dollars_at_risk']:>10,.2f}  "
                      f"{r['denial_category'] or ''}")
    return 0


def cmd_reset(args) -> int:
    n = JobStore(args.db).reset(batch=args.batch)
    print(f"Deleted {n} jobs" + (f" from batch '{args.batch}'." if args.batch else "."))
    return 0


def cmd_requeue(args) -> int:
    n = JobStore(args.db).requeue(batch=args.batch, only_errors=not args.all)
    scope = "all flagged jobs" if args.all else "failed jobs only"
    print(f"Requeued {n} jobs ({scope}) -- start a worker to reprocess them.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Control the document ingestion pipeline.")
    p.add_argument("--db", default=os.environ.get("QUEUE_DB", "data/queue/jobs.db"))
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enqueue", help="Add a directory of documents to the queue.")
    e.add_argument("--docs", required=True)
    e.add_argument("--batch", default="default")
    e.set_defaults(func=cmd_enqueue)

    s = sub.add_parser("status", help="Show pipeline counters.")
    s.add_argument("--batch", default=None)
    s.add_argument("--top", type=int, default=10, help="Show top N by $ at risk (0 to hide).")
    s.set_defaults(func=cmd_status)

    r = sub.add_parser("reset", help="Delete jobs (a batch, or everything).")
    r.add_argument("--batch", default=None)
    r.set_defaults(func=cmd_reset)

    q = sub.add_parser("requeue",
                       help="Send failed jobs back to pending (retry after a fix).")
    q.add_argument("--batch", default=None)
    q.add_argument("--all", action="store_true",
                   help="Also requeue genuine human-review items, not just failures.")
    q.set_defaults(func=cmd_requeue)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
