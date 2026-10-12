"""Which items each endpoint gets, honoring quotas. Deterministic."""

from __future__ import annotations

from collections.abc import Mapping

from governed_agent_engine.bench.items import (
    ALL_ITEMS,
    MINI_IDS,
    SUBSET_IDS,
    Item,
    by_ids,
)
from governed_agent_engine.bench.runner import estimate_tokens
from governed_agent_engine.core.models import EndpointConfig

# Rough context limits (chars) for needle items; anything larger is not attempted.
CONTEXT_CHAR_LIMITS: dict[str, int] = {"ai_horde": 6_500}


def items_for(name: str, ep: EndpointConfig, mode: str = "auto") -> tuple[Item, ...]:
    if mode == "mini" or (mode == "auto" and name == "openrouter"):
        return by_ids(MINI_IDS)  # auto-router: model varies per call, keep spend tiny
    if mode == "subset" or (mode == "auto" and ep.rpd is not None and ep.rpd <= 100):
        return by_ids(SUBSET_IDS)
    limit = CONTEXT_CHAR_LIMITS.get(name)
    return tuple(
        i
        for i in ALL_ITEMS
        if not (limit and i.dimension == "long_context" and int(str(i.params["chars"])) > limit)
        and not (ep.tpm and estimate_tokens(i) > ep.tpm)  # can never fit one minute's tokens
    )


def budget(
    plan: Mapping[str, tuple[Item, ...]], endpoints: Mapping[str, EndpointConfig]
) -> dict[str, int]:
    """Calls per rate group (to check against daily quotas before running)."""
    out: dict[str, int] = {}
    for name, items in plan.items():
        key = endpoints[name].limit_key
        out[key] = out.get(key, 0) + len(items)
    return out
