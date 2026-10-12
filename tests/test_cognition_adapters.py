"""Adapters against a fake HttpClient (no network)."""

from __future__ import annotations

import pytest

from governed_agent_engine.cognition.ai_horde import AIHordeEndpoint, render_prompt
from governed_agent_engine.cognition.base import ChatMessage, parse_retry_after
from governed_agent_engine.cognition.gemini import GeminiEndpoint
from governed_agent_engine.cognition.http import HttpResponse, HttpTransportError
from governed_agent_engine.cognition.judge0 import Judge0Client
from governed_agent_engine.cognition.openai_compat import OpenAICompatEndpoint
from governed_agent_engine.cognition.rate_limit import TokenBucket
from governed_agent_engine.cognition.registry import build_clients
from governed_agent_engine.config.loader import AppConfig
from governed_agent_engine.core.errors import AuthError, ProviderError, RateLimitedError

from .conftest import FakeHttp, Mono, jr, oai

MSGS = [ChatMessage("system", "sys"), ChatMessage("user", "hi")]


def test_registry_only_enabled(config: AppConfig) -> None:
    clients = build_clients(config.endpoints, FakeHttp(), {})
    assert set(clients) == set(config.enabled_endpoints())
    assert isinstance(clients["gemini"], GeminiEndpoint)
    assert isinstance(clients["ai_horde"], AIHordeEndpoint)


def test_pollinations_keyless(config: AppConfig) -> None:
    h = FakeHttp()
    h.add("POST", "https://text.pollinations.ai/openai", oai("PONG"))
    c = OpenAICompatEndpoint(config.endpoints["pollinations"], h, {}).complete(MSGS)
    assert (c.text, c.model) == ("PONG", "m1")
    assert "Authorization" not in h.calls[0][2]
    assert h.calls[0][3]["temperature"] == 0


def test_openrouter_bearer(config: AppConfig) -> None:
    h = FakeHttp()
    h.add("POST", "https://openrouter.ai/api/v1/chat/completions", oai())
    OpenAICompatEndpoint(config.endpoints["openrouter"], h, {"OPENROUTER_API_KEY": "k"}).complete(
        MSGS
    )
    assert h.calls[0][2]["Authorization"] == "Bearer k"
    with pytest.raises(AuthError, match="OPENROUTER_API_KEY"):
        OpenAICompatEndpoint(config.endpoints["openrouter"], h, {}).complete(MSGS)


@pytest.mark.parametrize(
    ("resp", "exc"),
    [
        (HttpResponse(429, b"", {"retry-after": "7"}), RateLimitedError),
        (HttpResponse(401, b""), AuthError),
        (HttpResponse(403, b""), AuthError),
        (HttpResponse(500, b"boom"), ProviderError),
        (HttpResponse(302, b""), ProviderError),
        (HttpResponse(200, b"<html>"), ProviderError),
        (jr(200, {"x": 1}), ProviderError),
        (jr(200, {"choices": [{"message": {"content": " "}}]}), ProviderError),
        (HttpTransportError("dns"), ProviderError),
    ],
)
def test_error_mapping(config: AppConfig, resp: HttpResponse | Exception, exc: type) -> None:
    h = FakeHttp()
    h.add("POST", "https://api.llm7.io", resp)
    with pytest.raises(exc) as info:
        OpenAICompatEndpoint(config.endpoints["llm7"], h, {}).complete(MSGS)
    if isinstance(info.value, RateLimitedError):
        assert info.value.retry_after_s == 7.0


GEM = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent"


def test_gemini(config: AppConfig) -> None:
    h = FakeHttp()
    h.add(
        "POST",
        GEM,
        jr(200, {"candidates": [{"content": {"parts": [{"text": "a"}, {"text": "b"}]}}]}),
    )
    g = GeminiEndpoint(config.endpoints["gemini"], h, {"GEMINI_API_KEY": "g"})
    assert g.complete([*MSGS, ChatMessage("assistant", "prev")]).text == "ab"
    _method, url, headers, body = h.calls[0]
    assert headers["x-goog-api-key"] == "g"
    assert "key=" not in url
    assert body["systemInstruction"]["parts"][0]["text"] == "sys"
    assert body["contents"][-1]["role"] == "model"


@pytest.mark.parametrize("payload", [{"x": 1}, {"candidates": [{"content": {"parts": [{}]}}]}])
def test_gemini_bad(config: AppConfig, payload: object) -> None:
    h = FakeHttp()
    h.add("POST", GEM, jr(200, payload))
    with pytest.raises(ProviderError):
        GeminiEndpoint(config.endpoints["gemini"], h, {"GEMINI_API_KEY": "g"}).complete(
            [ChatMessage("user", "q")]
        )


H = "https://aihorde.net/api/v2"


def test_ai_horde_flow(config: AppConfig) -> None:
    h = FakeHttp()
    h.add("POST", f"{H}/generate/text/async", jr(202, {"id": "j"}))
    h.add("GET", f"{H}/generate/text/status/j", jr(200, {"done": False}),
          jr(200, {"done": True, "generations": [{"text": "ok", "model": "L"}]}))  # fmt: skip
    sleeps: list[float] = []
    c = AIHordeEndpoint(config.endpoints["ai_horde"], h, {}, sleep=sleeps.append).complete(MSGS)
    assert (c.text, c.model) == ("ok", "L")
    assert h.calls[0][2]["apikey"] == "0000000000"
    assert sleeps == [5.0]


@pytest.mark.parametrize(
    ("sub", "st"),
    [
        ({"msg": "x"}, {}),
        ({"id": "j"}, {"faulted": True}),
        ({"id": "j"}, {"done": True, "generations": []}),
        ({"id": "j"}, {"done": False}),
    ],
)
def test_ai_horde_failures(config: AppConfig, sub: object, st: object) -> None:
    h = FakeHttp()
    h.add("POST", f"{H}/generate/text/async", jr(202, sub))
    h.add("GET", f"{H}/generate/text/status/j", jr(200, st))
    ep = AIHordeEndpoint(config.endpoints["ai_horde"], h, {}, sleep=lambda _s: None,
                         poll_interval_s=1000.0)  # fmt: skip
    with pytest.raises(ProviderError):
        ep.complete(MSGS)


def test_helpers() -> None:
    assert render_prompt(MSGS).endswith("### Assistant:\n")
    assert parse_retry_after(None) is None
    assert parse_retry_after("x") is None
    assert parse_retry_after("-1") == 0.0


def test_judge0(config: AppConfig) -> None:
    h = FakeHttp()
    h.add("POST", "https://ce.judge0.com/submissions",
          jr(200, {"status": {"description": "Accepted"}, "stdout": "42\n"}))  # fmt: skip
    j = Judge0Client(config.endpoints["judge0"], h, TokenBucket.per_minute(1, Mono()))
    r = j.run("print(6*7)")
    assert r.ok
    assert r.stdout == "42\n"
    assert j.run("x").status == "local_rate_limited"
    with pytest.raises(ValueError, match="unsupported"):
        j.run("x", language="cobol")
