"""Token-bucket rate limiter for gateway requests."""

from __future__ import annotations
import time
from dataclasses import dataclass, field


@dataclass
class _TokenBucket:
    """Per-sender token bucket."""

    max_tokens: int
    refill_rate: float  # tokens per second
    tokens: float = 0.0
    last_refill: float = field(default_factory=time.time)

    def __post_init__(self):
        self.tokens = float(self.max_tokens)

    def consume(self) -> bool:
        """Try to consume one token. Returns True if allowed."""
        now = time.time()
        elapsed = now - self.last_refill
        self.last_refill = now

        # Refill tokens
        self.tokens = min(self.max_tokens, self.tokens + elapsed * self.refill_rate)

        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class RateLimiter:
    """Per-sender rate limiter using token buckets."""

    def __init__(self, max_per_minute: float = 10.0, burst: int = 3):
        self._max_per_minute = max_per_minute
        self._burst = burst
        self._refill_rate = max_per_minute / 60.0
        self._buckets: dict[str, _TokenBucket] = {}

    def is_allowed(self, sender_id: str) -> bool:
        """Check if a request from sender_id is allowed."""
        if sender_id not in self._buckets:
            self._buckets[sender_id] = _TokenBucket(
                max_tokens=self._burst,
                refill_rate=self._refill_rate,
            )
        return self._buckets[sender_id].consume()

    def cleanup_stale(self, max_age: float = 600.0) -> int:
        """Remove buckets not used in max_age seconds. Returns count removed."""
        now = time.time()
        stale = [
            sid for sid, bucket in self._buckets.items()
            if (now - bucket.last_refill) > max_age
        ]
        for sid in stale:
            del self._buckets[sid]
        return len(stale)
