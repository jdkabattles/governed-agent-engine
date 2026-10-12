"""Endpoint selection + fallback chain.

``select_endpoints`` is pure given its inputs: it orders the configured chain for the task type
(by measured capability utility from config/capabilities.yaml when available, otherwise by
declared affinity) and filters by enabled, governance knowledge policy (incl. sensitive ->
first-party only) and health. ``CognitionStrategy.complete`` then walks the candidates,
consuming token buckets and recording 429s / failures.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from governed_agent_engine.bench.capabilities import Capabilities
from governed_agent_engine.cognition.base import ChatMessage, CognitionEndpoint, Completion
from governed_agent_engine.cognition.health import HealthTracker
from governed_agent_engine.cognition.rate_limit import ProviderLimiter
from governed_agent_engine.core.errors import (
    AllEndpointsFailedError,
    AuthError,
    ProviderError,
    RateLimitedError,
)
from governed_agent_engine.core.models import EndpointConfig, GovernanceConfig, Task
from governed_agent_engine.orchestrator.governance import endpoint_permitted


@dataclass(frozen=True, slots=True)
class Attempt:
    endpoint: str
    outcome: str  # ok|skipped_disabled|skipped_policy|skipped_unhealthy|local_rate_limited|
    #              rate_limited|auth_error|error|unknown
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"endpoint": self.endpoint, "outcome": self.outcome, "detail": self.detail}


def select_endpoints(
    task: Task,
    chain: Sequence[str],
    endpoints: Mapping[str, EndpointConfig],
    gov: GovernanceConfig,
    health: HealthTracker,
    capabilities: Capabilities | None = None,
    exclude: Sequence[str] = (),
    penalties: Mapping[str, float] | None = None,
) -> tuple[tuple[str, ...], tuple[Attempt, ...]]:
    """Return (candidates in order, skipped attempts with reasons).

    Ordering: measured endpoints by utility for ``task.type`` (desc), then unmeasured ones by
    declared affinity, then chain order. ``exclude`` removes endpoints (used by cross-checks to
    force a *different* model).
    """
    base = [n for n in dict.fromkeys(chain) if n not in exclude]
    pos = {n: i for i, n in enumerate(base)}

    pen = penalties or {}

    def key(n: str) -> tuple[int, float, float, int, int]:
        u = capabilities.utility(n, task.type) if capabilities is not None else None
        affinity = 0 if n in endpoints and task.type in endpoints[n].task_types else 1
        p = pen.get(n, 1.0)  # doctrine laziness penalty multiplier (1.0 = none)
        return (0 if u is not None else 1, -(u or 0.0) * p, -p, affinity, pos[n])

    ordered = sorted(base, key=key)
    picked: list[str] = []
    skipped: list[Attempt] = []
    for name in ordered:
        ep = endpoints.get(name)
        if ep is None:
            skipped.append(Attempt(name, "unknown"))
        elif not ep.enabled:
            skipped.append(Attempt(name, "skipped_disabled", ep.status))
        elif not endpoint_permitted(task, ep, gov):
            skipped.append(Attempt(name, "skipped_policy", f"trust={ep.trust}"))
        elif not health.is_healthy(name):
            skipped.append(Attempt(name, "skipped_unhealthy"))
        else:
            picked.append(name)
    return tuple(picked), tuple(skipped)


@dataclass(frozen=True, slots=True)
class StrategyResult:
    completion: Completion
    attempts: tuple[Attempt, ...]


class CognitionStrategy:
    def __init__(
        self,
        clients: Mapping[str, CognitionEndpoint],
        endpoints: Mapping[str, EndpointConfig],
        limiters: Mapping[str, ProviderLimiter],
        health: HealthTracker,
        gov: GovernanceConfig,
        capabilities: Capabilities | None = None,
    ) -> None:
        self.clients, self.endpoints, self.limiters = clients, endpoints, limiters
        self.health, self.gov, self.capabilities = health, gov, capabilities
        self.penalties: dict[str, float] = {}

    def penalize(self, endpoint: str, factor: float) -> None:
        """Doctrine rejection: multiply the endpoint's routing utility by ``factor``."""
        self.penalties[endpoint] = self.penalties.get(endpoint, 1.0) * factor

    def candidates(
        self, task: Task, chain: Sequence[str], exclude: Sequence[str] = ()
    ) -> tuple[str, ...]:
        return select_endpoints(
            task,
            chain,
            self.endpoints,
            self.gov,
            self.health,
            self.capabilities,
            exclude,
            self.penalties,
        )[0]

    def complete(
        self,
        task: Task,
        chain: Sequence[str],
        messages: list[ChatMessage],
        exclude: Sequence[str] = (),
    ) -> StrategyResult:
        candidates, skipped = select_endpoints(
            task,
            chain,
            self.endpoints,
            self.gov,
            self.health,
            self.capabilities,
            exclude,
            self.penalties,
        )
        attempts = list(skipped)
        for name in candidates:
            client = self.clients.get(name)
            if client is None:
                attempts.append(Attempt(name, "unknown"))
                continue
            limiter = self.limiters.get(name)
            if limiter is not None and not limiter.try_acquire():
                attempts.append(Attempt(name, "local_rate_limited"))
                continue
            try:
                completion = client.complete(messages)
            except RateLimitedError as exc:
                if limiter is not None:
                    limiter.record_429(exc.retry_after_s)
                attempts.append(Attempt(name, "rate_limited", str(exc)))
                continue
            except ProviderError as exc:
                self.health.record_failure(name)
                outcome = "auth_error" if isinstance(exc, AuthError) else "error"
                attempts.append(Attempt(name, outcome, str(exc)[:200]))
                continue
            self.health.record_success(name)
            snap = getattr(client, "last_ratelimit", None)
            if limiter is not None and snap is not None:
                limiter.observe(snap.wait_needed(1))
            attempts.append(Attempt(name, "ok"))
            return StrategyResult(completion, tuple(attempts))
        raise AllEndpointsFailedError(tuple(attempts))
