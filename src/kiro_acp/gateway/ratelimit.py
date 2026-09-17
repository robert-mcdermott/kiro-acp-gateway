"""Per-key request rate limiting (token bucket) and turn-slot queueing helpers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    tokens: float
    updated: float


@dataclass
class RateLimiter:
    """Simple token bucket: ``rpm`` requests per minute per key with burst ``rpm``."""

    rpm: int
    _buckets: dict[str, _Bucket] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.rpm > 0

    def acquire(self, key: str) -> float:
        """Return 0 when allowed, else seconds until the next token."""
        if not self.enabled:
            return 0.0
        now = time.monotonic()
        rate = self.rpm / 60.0
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=float(self.rpm), updated=now)
            self._buckets[key] = bucket
        bucket.tokens = min(float(self.rpm), bucket.tokens + (now - bucket.updated) * rate)
        bucket.updated = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return 0.0
        return (1.0 - bucket.tokens) / rate
