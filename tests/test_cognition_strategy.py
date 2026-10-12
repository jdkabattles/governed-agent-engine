from __future__ import annotations

from dataclasses import replace

import pytest

from governed_agent_engine.cognition.base import ChatMessage, Completion
from governed_agent_engine.cognition.health import HealthTracker
from governed_agent_engine.cognition.rate_limit import ProviderLimiter, TokenBucket
from governed_agent_engine.cognition.registry import build_limiters
from governed_agent_engine.cognition.strategy import CognitionStrategy, select_endpoints
from governed_agent_engine.config.loader import AppConfig
from governed_agent_engine.core.errors import (
    AllEndpointsFailedError,
    AuthError,
    ProviderError,
    RateLimitedError,
)

from .conftest import Mono, make_task

MSGS = [ChatMessage("user", "x")]
LEGACY_CHAIN = ("pollinations", "openrouter", "gemini", "llm7")


class Fake:
    def __init__(self, name: str, behavior: Exception | str = "ok") -> None:
        self.name, self.model, self.behavior, self.calls = name, "fake", behavior, 0

    def complete(self, messages: list[ChatMessage]) -> Completion:
        self.calls += 1
        if isinstance(self.behavior, Exception):
            raise self.behavior
        return Completion(self.name, self.model, self.behavior)


def test_select_filters_and_orders(config: AppConfig) -> None:
    eps = dict(config.endpoints)
    eps["ovh"] = replace(eps["ovh"], task_types=("research",))
    h = HealthTracker(clock=Mono())
    chain = ("nope", "blackbox", "llm7", "pollinations", "ovh", "llm7")
    picked, skipped = select_endpoints(
        make_task(type="research", sensitive=True), chain, eps, config.governance, h
    )
    assert picked == ("ovh", "pollinations")  # affinity first; llm7 blocked (sensitive)
    assert {a.outcome for a in skipped} == {"unknown", "skipped_disabled", "skipped_policy"}


def test_health_benches_and_recovers() -> None:
    m = Mono()
    h = HealthTracker(failure_threshold=2, cooldown_s=60, clock=m)
    h.record_failure("a")
    assert h.is_healthy("a")
    h.record_failure("a")
    assert not h.is_healthy("a")
    m.t += 61
    assert h.is_healthy("a")
    h.record_success("a")
    assert h.state["a"].successes == 1


def _strategy(config: AppConfig, clients: dict[str, Fake], m: Mono) -> CognitionStrategy:
    return CognitionStrategy(
        clients, config.endpoints, build_limiters(config.endpoints, m),
        HealthTracker(failure_threshold=1, cooldown_s=600, clock=m), config.governance,
    )  # fmt: skip


def test_fallback_records_everything(config: AppConfig) -> None:
    m = Mono()
    clients = {
        "pollinations": Fake("pollinations", RateLimitedError("pollinations", "429", 30)),
        "openrouter": Fake("openrouter", AuthError("openrouter", "no key")),
        "gemini": Fake("gemini", ProviderError("gemini", "503")),
        "llm7": Fake("llm7", "plan"),
    }
    s = _strategy(config, clients, m)
    r = s.complete(make_task(), LEGACY_CHAIN, MSGS)
    assert r.completion.endpoint == "llm7"
    assert [a.outcome for a in r.attempts] == ["rate_limited", "auth_error", "error", "ok"]
    assert s.limiters["pollinations"].rate_limited_count == 1
    # pollinations now locally blocked; openrouter/gemini benched (threshold 1)
    r2 = s.complete(make_task(), LEGACY_CHAIN, MSGS)
    outcomes = {a.endpoint: a.outcome for a in r2.attempts}
    assert outcomes["openrouter"] == "skipped_unhealthy"
    assert outcomes["pollinations"] == "local_rate_limited"
    assert clients["pollinations"].calls == 1


def test_local_bucket_and_missing_client(config: AppConfig) -> None:
    m = Mono()
    s = CognitionStrategy(
        {"ovh": Fake("ovh")},
        config.endpoints,
        {"ovh": ProviderLimiter(minute=TokenBucket.per_minute(1, m))},
        HealthTracker(clock=m),
        config.governance,
    )
    assert s.complete(make_task(), ("pollinations", "ovh"), MSGS).completion.endpoint == "ovh"
    with pytest.raises(AllEndpointsFailedError) as info:
        s.complete(make_task(), ("ovh",), MSGS)
    assert info.value.attempts[-1].outcome == "local_rate_limited"


def test_token_bucket() -> None:
    m = Mono()
    b = TokenBucket.per_minute(2, m)
    assert b.try_acquire() and b.try_acquire()  # noqa: PT018
    assert not b.try_acquire()
    m.t += 30
    assert b.try_acquire()
    b.penalize(10)
    m.t += 5
    assert not b.try_acquire()
    m.t += 120
    assert b.try_acquire()
    lim = ProviderLimiter(minute=TokenBucket.per_minute(5, m), day=TokenBucket.per_day(1, m))
    assert lim.try_acquire()
    assert not lim.try_acquire()
