from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from governed_agent_engine.bench import items, plan
from governed_agent_engine.bench.runner import RunResult, Target, estimate_tokens, run_target
from governed_agent_engine.cognition.base import (
    ChatMessage,
    Completion,
    RateLimitSnapshot,
    parse_duration,
)
from governed_agent_engine.cognition.health import HealthTracker
from governed_agent_engine.cognition.openai_compat import OpenAICompatEndpoint
from governed_agent_engine.cognition.registry import build_limiters
from governed_agent_engine.cognition.strategy import CognitionStrategy
from governed_agent_engine.config.loader import AppConfig
from governed_agent_engine.core.errors import RateLimitedError

from .conftest import FakeHttp, Mono, jr, make_task

GROQ = {
    "x-ratelimit-limit-requests": "1000", "x-ratelimit-remaining-requests": "999",
    "x-ratelimit-reset-requests": "1m26.4s", "x-ratelimit-limit-tokens": "8000",
    "x-ratelimit-remaining-tokens": "7906", "x-ratelimit-reset-tokens": "705ms",
}  # fmt: skip


def test_parse_duration() -> None:
    assert parse_duration("1m26.4s") == pytest.approx(86.4)
    assert parse_duration("705ms") == pytest.approx(0.705)
    assert parse_duration("1h2m") == pytest.approx(3720)
    assert parse_duration("7") == 7.0
    assert parse_duration(None) is None
    assert parse_duration("") is None


def test_snapshot() -> None:
    s = RateLimitSnapshot.from_headers(GROQ)
    assert s is not None
    assert (s.limit_requests, s.limit_tokens, s.remaining_tokens) == (1000, 8000, 7906)
    assert s.wait_needed(100) == 0.0
    assert s.wait_needed(9000) == pytest.approx(0.705)
    assert replace(s, remaining_requests=0).wait_needed() == pytest.approx(86.4)
    assert replace(s, remaining_tokens=0, reset_tokens_s=None).wait_needed(5) == 60.0
    assert RateLimitSnapshot.from_headers({"content-type": "x"}) is None
    bad = RateLimitSnapshot.from_headers({"x-ratelimit-limit-tokens": "abc"})
    assert bad is not None and bad.limit_tokens is None  # noqa: PT018


def test_groq_endpoints_and_headers(config: AppConfig) -> None:
    ep = config.endpoints["groq_gpt_oss_120b"]
    assert (ep.rpd, ep.tpm, ep.model, ep.trust) == (
        1000,
        8000,
        "openai/gpt-oss-120b",
        "first_party",
    )
    h = FakeHttp()
    h.add("POST", "https://api.groq.com", jr(200, {"model": "openai/gpt-oss-120b",
          "choices": [{"message": {"content": "PONG"}}]}, GROQ),
          jr(429, {}, {**GROQ, "x-ratelimit-remaining-tokens": "0",
                       "x-ratelimit-reset-tokens": "7.5s"}))  # fmt: skip
    c = OpenAICompatEndpoint(ep, h, {"GROQ_API_KEY": "k"})
    assert c.complete([ChatMessage("user", "x")]).text == "PONG"
    assert c.last_ratelimit is not None and c.last_ratelimit.remaining_tokens == 7906  # noqa: PT018
    with pytest.raises(RateLimitedError) as info:
        c.complete([ChatMessage("user", "x")])
    assert info.value.retry_after_s == pytest.approx(7.5)


class Snap:
    def __init__(self, snap: RateLimitSnapshot) -> None:
        self.name, self.model, self.last_ratelimit = "groq_qwen3_8_27b", "m", snap

    def complete(self, messages: list[ChatMessage]) -> Completion:
        return Completion(self.name, self.model, "ANSWER: 403")


def test_strategy_honors_exhausted_window(config: AppConfig) -> None:
    m = Mono()
    snap = RateLimitSnapshot(remaining_requests=0, reset_requests_s=86.4)
    s = CognitionStrategy({"groq_qwen3_8_27b": Snap(snap)}, config.endpoints,
                          build_limiters(config.endpoints, m), HealthTracker(clock=m),
                          config.governance)  # fmt: skip
    chain = ("groq_qwen3_8_27b",)
    assert s.complete(make_task(), chain, []).completion.text == "ANSWER: 403"
    lim = s.limiters["groq_qwen3_8_27b"]
    assert not lim.try_acquire()  # blocked until the server-announced reset
    m.t += 90
    assert lim.try_acquire()


def test_runner_waits_on_token_window(tmp_path: Path) -> None:
    waits: list[float] = []
    snap = RateLimitSnapshot(remaining_tokens=10, reset_tokens_s=3.0)
    t = Target("g", Snap(snap), items.by_ids(("math.0",)), 0.0)
    run_target(t, RunResult(), tmp_path, clock=lambda: 0.0, sleep=waits.append)
    assert waits == [3.0]


def test_runner_uses_retry_after_hint(tmp_path: Path) -> None:
    waits: list[float] = []

    class RL:
        name, model = "g", "m"

        def __init__(self) -> None:
            self.n = 0

        def complete(self, messages: list[ChatMessage]) -> Completion:
            self.n += 1
            if self.n == 1:
                raise RateLimitedError("g", "HTTP 429", 45.0)
            return Completion("g", "m", "ANSWER: 403")

    t = Target("g", RL(), items.by_ids(("math.0",)), 0.0)
    r = RunResult()
    run_target(t, r, tmp_path, clock=lambda: 0.0, sleep=waits.append)
    assert waits == [45.0] and r.records[0].ok  # noqa: PT018


def test_plan_skips_items_over_tpm(config: AppConfig) -> None:
    ep = config.endpoints["groq_gpt_oss_20b"]
    ids = [i.item_id for i in plan.items_for("groq_gpt_oss_20b", ep)]
    assert "needle.6k" in ids and "needle.30k" not in ids  # noqa: PT018
    assert all(estimate_tokens(i) <= 8000 for i in plan.items_for("groq_gpt_oss_20b", ep))
