"""Observability: console logging + a machine-readable JSONL trace per run.

Every agent step (thought, action, code, observation) is appended to a trace so
a full run is inspectable after the fact — required for debugging non-deterministic
agents and for the eval report.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger console format used by every CLI entrypoint."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


class RunTrace:
    """Collects structured events for one agent run and writes them to JSONL."""

    def __init__(self, log_dir: str, question: str):
        """Start a new trace file `logs/run_<id>.jsonl` for one agent run."""
        self.run_id = uuid.uuid4().hex[:8]
        self.question = question
        self.started = time.time()
        self.events: list[dict] = []
        self._log = logging.getLogger("agent.trace")
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, f"run_{self.run_id}.jsonl")

    def record(self, step: int, kind: str, **fields) -> None:
        """Append one structured event to the JSONL trace and echo a short,
        human-readable line to the console for the matching event kinds."""
        event = {"run_id": self.run_id, "step": step, "kind": kind,
                 "ts": round(time.time() - self.started, 3), **fields}
        self.events.append(event)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str) + "\n")

        # Human-readable console echo.
        if kind == "decision":
            self._log.info("step %d | %s | %s", step, fields.get("action"),
                           _truncate(fields.get("thought", "")))
        elif kind == "observation":
            ok = fields.get("ok")
            tail = fields.get("result_repr") or fields.get("error") or ""
            self._log.info("step %d | observation ok=%s | %s", step, ok, _truncate(tail))
        elif kind == "final":
            self._log.info("step %d | FINAL | %s", step, _truncate(fields.get("summary", "")))
        elif kind == "error":
            self._log.error("step %d | %s", step, fields.get("message"))

    def as_dict(self) -> dict:
        """Full trace as a plain dict (used by callers that want it in-memory
        rather than re-reading the JSONL file)."""
        return {"run_id": self.run_id, "question": self.question, "events": self.events}


def _truncate(s: str, n: int = 120) -> str:
    """Collapse whitespace and clip to `n` chars for a readable console line."""
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"
