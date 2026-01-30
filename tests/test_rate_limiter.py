"""Tests for the token-bucket rate limiter."""

import time
from unittest.mock import patch

import pytest

from solmesh.rate_limiter import RateLimiter, _TokenBucket


class TestTokenBucket:
    def test_burst_allowed(self):
        bucket = _TokenBucket(max_tokens=3, refill_rate=1.0)
        assert bucket.consume() is True
        assert bucket.consume() is True
        assert bucket.consume() is True

    def test_burst_exceeded(self):
        bucket = _TokenBucket(max_tokens=2, refill_rate=0.1)
        bucket.consume()
        bucket.consume()
        assert bucket.consume() is False

    def test_refill(self):
        bucket = _TokenBucket(max_tokens=1, refill_rate=10.0)
        bucket.consume()
        assert bucket.consume() is False
        # Simulate time passing (0.2s at 10 tokens/s = 2 tokens refilled)
        bucket.last_refill = time.time() - 0.2
        assert bucket.consume() is True

    def test_refill_capped_at_max(self):
        bucket = _TokenBucket(max_tokens=2, refill_rate=100.0)
        # Wait a long time - should cap at max_tokens
        bucket.last_refill = time.time() - 100
        bucket.tokens = 0
        bucket.consume()  # triggers refill
        # Should have had max_tokens - 1 left after consuming one
        assert bucket.tokens <= 1.0


class TestRateLimiter:
    def test_allows_burst(self):
        rl = RateLimiter(max_per_minute=6.0, burst=3)
        assert rl.is_allowed("sender1") is True
        assert rl.is_allowed("sender1") is True
        assert rl.is_allowed("sender1") is True

    def test_blocks_after_burst(self):
        rl = RateLimiter(max_per_minute=6.0, burst=2)
        rl.is_allowed("sender1")
        rl.is_allowed("sender1")
        assert rl.is_allowed("sender1") is False

    def test_independent_senders(self):
        rl = RateLimiter(max_per_minute=6.0, burst=1)
        assert rl.is_allowed("alice") is True
        assert rl.is_allowed("bob") is True
        # Alice is now rate-limited, but bob is independent
        assert rl.is_allowed("alice") is False
        assert rl.is_allowed("bob") is False

    def test_refill_allows_again(self):
        rl = RateLimiter(max_per_minute=60.0, burst=1)
        rl.is_allowed("sender1")
        assert rl.is_allowed("sender1") is False
        # Simulate 2 seconds passing (60/min = 1/sec, so 2 tokens refilled)
        rl._buckets["sender1"].last_refill = time.time() - 2
        assert rl.is_allowed("sender1") is True

    def test_cleanup_stale(self):
        rl = RateLimiter(max_per_minute=10.0, burst=3)
        rl.is_allowed("fresh")
        rl.is_allowed("stale")
        # Make stale bucket old
        rl._buckets["stale"].last_refill = time.time() - 700
        removed = rl.cleanup_stale(max_age=600)
        assert removed == 1
        assert "stale" not in rl._buckets
        assert "fresh" in rl._buckets

    def test_cleanup_none_stale(self):
        rl = RateLimiter(max_per_minute=10.0, burst=3)
        rl.is_allowed("recent")
        assert rl.cleanup_stale() == 0
