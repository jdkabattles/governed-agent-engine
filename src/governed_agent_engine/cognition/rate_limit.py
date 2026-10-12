"""Deterministic token buckets (injectable clock) for per-minute and per-day limits."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

Clock = Callable[[], float]


@dataclass
class TokenBucket:
    capacity: float
    refill_per_s: float
    clock: Clock = time.monotonic
    tokens: float = field(init=False)
    updated: float = field(init=False)
    blocked_until: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self.tokens = self.capacity
        self.updated = self.clock()

    def _refill(self) -> float:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill_per_s)
        self.updated = now
        return now

    def try_acquire(self, n: float = 1.0) -> bool:
        now = self._refill()
        if now < self.blocked_until or self.tokens < n:
            return False
        self.tokens -= n
        return True

    def penalize(self, retry_after_s: float | None) -> None:
        """Record a server-side 429: drain tokens and honor Retry-After (default 60s)."""
        now = self._refill()
        self.tokens = 0.0
        self.blocked_until = max(self.blocked_until, now + (retry_after_s or 60.0))

    @classmethod
    def per_minute(cls, rpm: int, clock: Clock = time.monotonic) -> TokenBucket:
        return cls(capacity=float(rpm), refill_per_s=rpm / 60.0, clock=clock)

    @classmethod
    def per_day(cls, rpd: int, clock: Clock = time.monotonic) -> TokenBucket:
        return cls(capacity=float(rpd), refill_per_s=rpd / 86400.0, clock=clock)


@dataclass
class ProviderLimiter:
    minute: TokenBucket
    day: TokenBucket | None = None
    rate_limited_count: int = 0

    def try_acquire(self) -> bool:
        if not self.minute.try_acquire():
            return False
        if self.day is not None and not self.day.try_acquire():
            self.minute.tokens += 1.0  # refund
            return False
        return True

    def observe(self, wait_s: float) -> None:
        """Server says the window is spent (x-ratelimit remaining = 0): block until reset."""
        if wait_s > 0:
            self.minute.penalize(wait_s)

    def record_429(self, retry_after_s: float | None) -> None:
        self.rate_limited_count += 1
        self.minute.penalize(retry_after_s)
