"""Token-bucket rate limiter — the direct fix for the real failure found when
batch concurrency was first benchmarked.

Background (see LEARNING.md, 2026-08-14): naively adding a thread pool to
batch processing made things *faster and worse* -- `--workers 6` cut wall
time from 64s to 22s while dropping the success rate from 9/9 to 0/9, because
every worker burst against the same fixed per-account budget:

    litellm.RateLimitError: ... Limit 30000, Used 30000, Requested 1318

Retrying into a wall doesn't help; the requests have to be *paced* so the
fleet as a whole stays under the account's tokens-per-minute ceiling. That's
what this does: one shared bucket across all workers in the process, refilled
continuously, with `acquire()` blocking until the estimated cost of a call
fits within budget.

Deliberately in-process: it correctly paces a single multi-threaded worker
process, which is what `docker compose up --scale worker=N` needs *per
container*. Across containers the budget has to be divided (give each of N
workers TPM/N) or moved to a shared limiter -- Redis `INCR` with a
per-minute key is the standard next step. That boundary is called out in
`TokenBucket.__init__` rather than hidden.
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    """Thread-safe token bucket for tokens-per-minute LLM quotas.

    `capacity` is the burst size and `rate_per_sec` the sustained refill.
    Sizing both from the same TPM number means the bucket can absorb one
    minute's worth of burst and then paces to exactly the sustained limit --
    matching how provider TPM quotas actually behave.
    """

    def __init__(self, tokens_per_minute: int, *, safety_factor: float = 0.85,
                 num_processes: int = 1):
        """`safety_factor` reserves headroom because the *estimated* token
        cost of a call is never exact (the response length isn't known until
        it arrives) -- running at 100% of the stated limit reliably trips it.

        `num_processes` divides the budget when more than one worker
        container shares one account: each process gets TPM/N. Crude but
        correct and dependency-free; a shared Redis-backed limiter is the
        real fix when the split becomes wasteful (see module docstring).
        """
        effective = (tokens_per_minute * safety_factor) / max(1, num_processes)
        self.capacity = max(1.0, effective)
        self.rate_per_sec = self.capacity / 60.0
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self.waits = 0
        self.total_wait_s = 0.0

    def _refill_locked(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate_per_sec)
        self._last = now

    def acquire(self, estimated_tokens: int) -> float:
        """Block until `estimated_tokens` of budget is available. Returns how
        long it waited (seconds) so callers can report real pacing overhead
        rather than guessing at it."""
        need = min(float(estimated_tokens), self.capacity)
        waited = 0.0
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= need:
                    self._tokens -= need
                    if waited:
                        self.waits += 1
                        self.total_wait_s += waited
                    return waited
                deficit = need - self._tokens
                sleep_for = min(max(deficit / self.rate_per_sec, 0.01), 5.0)
            time.sleep(sleep_for)
            waited += sleep_for


class NullLimiter:
    """No-op limiter for sequential runs / providers with no meaningful cap.
    Same interface so callers never branch on `if limiter is not None`."""

    waits = 0
    total_wait_s = 0.0

    def acquire(self, estimated_tokens: int) -> float:  # noqa: ARG002
        return 0.0


def estimate_tokens(document: str, max_steps: int = 3) -> int:
    """Rough per-document token budget: prompt + document, times the number
    of LLM turns a typical run takes, plus response allowance.

    ~4 chars/token is the standard English approximation. This only has to be
    the right order of magnitude -- the safety factor in `TokenBucket`
    absorbs the error, and being slightly pessimistic here is much cheaper
    than tripping the real limit.
    """
    doc_tokens = len(document) // 4
    system_prompt_tokens = 1200          # measured order of magnitude for DOC_SYSTEM_PROMPT
    response_tokens = 700                # typical DocStep JSON with a full extraction
    return (doc_tokens + system_prompt_tokens + response_tokens) * max_steps
