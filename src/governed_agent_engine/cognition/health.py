"""Per-endpoint health: consecutive failures bench an endpoint for a cooldown (circuit breaker)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from governed_agent_engine.cognition.rate_limit import Clock


@dataclass
class EndpointHealth:
    consecutive_failures: int = 0
    benched_until: float = 0.0
    successes: int = 0
    failures: int = 0


@dataclass
class HealthTracker:
    failure_threshold: int = 3
    cooldown_s: float = 600.0
    clock: Clock = time.monotonic
    state: dict[str, EndpointHealth] = field(default_factory=dict)

    def _get(self, name: str) -> EndpointHealth:
        return self.state.setdefault(name, EndpointHealth())

    def is_healthy(self, name: str) -> bool:
        return self.clock() >= self._get(name).benched_until

    def record_success(self, name: str) -> None:
        h = self._get(name)
        h.consecutive_failures = 0
        h.successes += 1

    def record_failure(self, name: str) -> None:
        h = self._get(name)
        h.failures += 1
        h.consecutive_failures += 1
        if h.consecutive_failures >= self.failure_threshold:
            h.benched_until = self.clock() + self.cooldown_s
            h.consecutive_failures = 0
