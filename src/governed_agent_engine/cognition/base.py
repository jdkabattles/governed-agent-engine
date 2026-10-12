"""Cognition protocol. Free LLM endpoints produce plans only; they never execute actions."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from governed_agent_engine.cognition.http import HttpClient, HttpResponse, HttpTransportError
from governed_agent_engine.core.errors import AuthError, ProviderError, RateLimitedError
from governed_agent_engine.core.models import EndpointConfig

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Role
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class Completion:
    endpoint: str
    model: str
    text: str


@runtime_checkable
class CognitionEndpoint(Protocol):
    name: str
    model: str

    def complete(self, messages: list[ChatMessage]) -> Completion: ...


def parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def resolve_key(cfg: EndpointConfig, env: Mapping[str, str] | None) -> str | None:
    if cfg.auth == "none":
        return None
    source = os.environ if env is None else env
    key = (source.get(cfg.api_key_env, "") if cfg.api_key_env else "") or (
        cfg.default_api_key or ""
    )
    if not key:
        raise AuthError(cfg.name, f"env var {cfg.api_key_env} not set")
    return key


_DUR = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")


def parse_duration(value: str | None) -> float | None:
    """Parse Groq/OpenAI-style durations: '1m26.4s', '705ms', '2s', '1h2m' -> seconds."""
    if not value:
        return None
    parts = _DUR.findall(value.strip())
    if not parts:
        return parse_retry_after(value)
    mult = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    return sum(float(n) * mult[u] for n, u in parts)


@dataclass(frozen=True, slots=True)
class RateLimitSnapshot:
    """x-ratelimit-* response headers (OpenAI/Groq convention). None = header absent."""

    limit_requests: int | None = None
    remaining_requests: int | None = None
    reset_requests_s: float | None = None
    limit_tokens: int | None = None
    remaining_tokens: int | None = None
    reset_tokens_s: float | None = None

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> RateLimitSnapshot | None:
        h = {k.lower(): v for k, v in headers.items()}
        if not any(k.startswith("x-ratelimit-") for k in h):
            return None

        def num(k: str) -> int | None:
            try:
                return int(float(h[k])) if k in h else None
            except ValueError:
                return None

        return cls(
            num("x-ratelimit-limit-requests"), num("x-ratelimit-remaining-requests"),
            parse_duration(h.get("x-ratelimit-reset-requests")),
            num("x-ratelimit-limit-tokens"), num("x-ratelimit-remaining-tokens"),
            parse_duration(h.get("x-ratelimit-reset-tokens")),
        )  # fmt: skip

    def wait_needed(self, tokens_needed: int = 0) -> float:
        """Seconds to wait before a request needing ~tokens_needed tokens (0 = go now)."""
        if self.remaining_requests is not None and self.remaining_requests <= 0:
            return self.reset_requests_s or 60.0
        if (
            self.remaining_tokens is not None
            and tokens_needed > 0
            and self.remaining_tokens < tokens_needed
        ):
            return self.reset_tokens_s or 60.0
        return 0.0


def _hget(headers: Mapping[str, str], key: str) -> str | None:
    for k, v in headers.items():
        if k.lower() == key:
            return v
    return None


class HttpEndpoint:
    """Shared plumbing: status-code mapping and JSON decoding."""

    def __init__(
        self, cfg: EndpointConfig, http: HttpClient, env: Mapping[str, str] | None = None
    ) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.model = cfg.model
        self.http = http
        self.env = env
        self.last_ratelimit: RateLimitSnapshot | None = None

    def call(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: object | None = None,
        timeout: float | None = None,
    ) -> Any:
        try:
            resp: HttpResponse = self.http.request(
                method, url, headers=headers, json_body=body, timeout=timeout or self.cfg.timeout_s
            )
        except HttpTransportError as exc:
            raise ProviderError(self.name, f"transport error: {exc}") from exc
        snap = RateLimitSnapshot.from_headers(resp.headers)
        if snap is not None:
            self.last_ratelimit = snap
        if resp.status == 429:
            retry = parse_retry_after(_hget(resp.headers, "retry-after"))
            if retry is None and snap is not None:
                retry = snap.wait_needed(1) or snap.reset_tokens_s
            raise RateLimitedError(self.name, "HTTP 429", retry)
        if resp.status in (401, 403):
            raise AuthError(self.name, f"HTTP {resp.status}")
        if resp.status >= 300:
            raise ProviderError(self.name, f"HTTP {resp.status}: {resp.text()[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(self.name, "non-JSON response") from exc
