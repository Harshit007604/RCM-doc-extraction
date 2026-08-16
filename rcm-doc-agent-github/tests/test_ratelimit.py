"""Unit tests for src/docproc/queue/ratelimit.py -- the token-bucket rate
limiter, the direct fix for the measured RateLimitError failures
(LEARNING.md, 2026-08-14: --workers 6 went from 64s/9-9 success to 22s/0-9
success)."""

from __future__ import annotations

import time

import pytest

from src.docproc.queue.ratelimit import NullLimiter, TokenBucket, estimate_tokens


class TestTokenBucket:
    def test_acquire_within_capacity_does_not_block(self):
        bucket = TokenBucket(tokens_per_minute=60_000, safety_factor=1.0)
        started = time.monotonic()
        waited = bucket.acquire(1000)
        assert waited == 0.0
        assert time.monotonic() - started < 0.1

    def test_acquire_exceeding_capacity_blocks_and_reports_real_wait(self):
        """A request larger than what's currently in the bucket must
        actually wait (not silently proceed) and report a nonzero wait.
        Sized so the real wait is ~0.1s -- fast, but a genuine sleep+refill
        cycle, not a mocked clock."""
        bucket = TokenBucket(tokens_per_minute=6000, safety_factor=1.0)  # 100 tokens/sec
        bucket.acquire(5990)                 # leaves ~10 tokens
        waited = bucket.acquire(20)          # needs 10 more -> ~0.1s to refill
        assert waited > 0.0
        assert bucket.waits >= 1
        assert bucket.total_wait_s > 0.0

    def test_safety_factor_reduces_usable_capacity(self):
        """0.85 safety factor means the bucket never claims the full stated
        TPM -- headroom for the fact that response length isn't known until
        it arrives."""
        full = TokenBucket(tokens_per_minute=1000, safety_factor=1.0)
        safe = TokenBucket(tokens_per_minute=1000, safety_factor=0.85)
        assert safe.capacity < full.capacity
        assert safe.capacity == 850.0

    def test_num_processes_splits_the_budget_evenly(self):
        """WORKER_REPLICAS divides the account's TPM across containers so
        the fleet as a whole (not each container individually) respects the
        real provider limit."""
        one = TokenBucket(tokens_per_minute=10_000, num_processes=1)
        four = TokenBucket(tokens_per_minute=10_000, num_processes=4)
        assert four.capacity == pytest.approx(one.capacity / 4)


class TestNullLimiter:
    def test_never_blocks_and_reports_no_wait(self):
        limiter = NullLimiter()
        assert limiter.acquire(1_000_000) == 0.0
        assert limiter.waits == 0


class TestEstimateTokens:
    def test_longer_document_estimates_more_tokens(self):
        short = estimate_tokens("a" * 100, max_steps=1)
        long = estimate_tokens("a" * 10_000, max_steps=1)
        assert long > short

    def test_more_steps_scales_the_estimate(self):
        one_step = estimate_tokens("some document text", max_steps=1)
        three_steps = estimate_tokens("some document text", max_steps=3)
        assert three_steps == one_step * 3
