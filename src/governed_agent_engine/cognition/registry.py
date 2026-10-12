"""kind -> adapter. Adding an OpenAI-compatible endpoint is YAML-only."""

from __future__ import annotations

import time
from collections.abc import Mapping

from governed_agent_engine.cognition.ai_horde import AIHordeEndpoint
from governed_agent_engine.cognition.base import CognitionEndpoint
from governed_agent_engine.cognition.gemini import GeminiEndpoint
from governed_agent_engine.cognition.http import HttpClient
from governed_agent_engine.cognition.openai_compat import OpenAICompatEndpoint
from governed_agent_engine.cognition.rate_limit import Clock, ProviderLimiter, TokenBucket
from governed_agent_engine.core.models import EndpointConfig

ADAPTERS: dict[str, type[OpenAICompatEndpoint] | type[GeminiEndpoint] | type[AIHordeEndpoint]] = {
    "openai_compat": OpenAICompatEndpoint,
    "gemini": GeminiEndpoint,
    "ai_horde": AIHordeEndpoint,
}


def build_clients(
    endpoints: Mapping[str, EndpointConfig],
    http: HttpClient,
    env: Mapping[str, str] | None = None,
) -> dict[str, CognitionEndpoint]:
    return {
        name: ADAPTERS[ep.kind](ep, http, env)
        for name, ep in sorted(endpoints.items())
        if ep.enabled and ep.kind in ADAPTERS
    }


def build_limiters(
    endpoints: Mapping[str, EndpointConfig], clock: Clock = time.monotonic
) -> dict[str, ProviderLimiter]:
    """One limiter per rate group: endpoints with the same ``rate_group`` (e.g. several
    OpenRouter :free models behind one 50/day key) share a single bucket."""
    shared: dict[str, ProviderLimiter] = {}
    out: dict[str, ProviderLimiter] = {}
    for name, ep in endpoints.items():
        key = ep.limit_key
        if key not in shared:
            shared[key] = ProviderLimiter(
                minute=TokenBucket.per_minute(ep.rpm, clock),
                day=TokenBucket.per_day(ep.rpd, clock) if ep.rpd else None,
            )
        out[name] = shared[key]
    return out
