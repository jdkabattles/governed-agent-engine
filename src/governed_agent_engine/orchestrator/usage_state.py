"""Grok availability inferred from heartbeat age (Grok Bot has no API to query)."""

from __future__ import annotations

from datetime import datetime, timedelta

from governed_agent_engine.config.loader import UsageConfig
from governed_agent_engine.core.models import GrokStatus, UsageState


def evaluate_heartbeat(heartbeat: datetime | None, now: datetime, cfg: UsageConfig) -> GrokStatus:
    if heartbeat is None:
        return GrokStatus.UNKNOWN
    if now - heartbeat <= timedelta(seconds=cfg.heartbeat_stale_after_s):
        return GrokStatus.AVAILABLE  # includes small future skew
    return GrokStatus.EXHAUSTED


def should_probe(now: datetime, state: UsageState, cfg: UsageConfig) -> bool:
    return state.last_probe is None or now - state.last_probe >= timedelta(
        seconds=cfg.probe_interval_s
    )


def update_from_heartbeat(
    state: UsageState, heartbeat: datetime | None, now: datetime, cfg: UsageConfig
) -> UsageState:
    """Pure transition; returns a new state."""
    del state  # transition depends only on the observation; kept for a uniform signature
    return UsageState(
        status=evaluate_heartbeat(heartbeat, now, cfg), last_probe=now, last_heartbeat=heartbeat
    )


def became_available(before: UsageState, after: UsageState) -> bool:
    return before.status is not GrokStatus.AVAILABLE and after.status is GrokStatus.AVAILABLE
