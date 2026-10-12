from __future__ import annotations

from datetime import timedelta

from governed_agent_engine.config.loader import UsageConfig
from governed_agent_engine.core.models import GrokStatus, UsageState
from governed_agent_engine.orchestrator.usage_state import (
    became_available,
    evaluate_heartbeat,
    should_probe,
    update_from_heartbeat,
)

from .conftest import T0

CFG = UsageConfig(heartbeat_stale_after_s=600, probe_interval_s=60)


def test_unknown_without_heartbeat() -> None:
    assert evaluate_heartbeat(None, T0, CFG) is GrokStatus.UNKNOWN


def test_boundary_and_skew() -> None:
    assert evaluate_heartbeat(T0 - timedelta(seconds=600), T0, CFG) is GrokStatus.AVAILABLE
    assert evaluate_heartbeat(T0 - timedelta(seconds=601), T0, CFG) is GrokStatus.EXHAUSTED
    assert evaluate_heartbeat(T0 + timedelta(seconds=30), T0, CFG) is GrokStatus.AVAILABLE


def test_transitions() -> None:
    s0 = UsageState()
    s1 = update_from_heartbeat(s0, T0, T0, CFG)
    assert s1.status is GrokStatus.AVAILABLE
    assert became_available(s0, s1)
    s2 = update_from_heartbeat(s1, T0, T0 + timedelta(hours=1), CFG)
    assert s2.status is GrokStatus.EXHAUSTED
    assert not became_available(s1, s2)
    s3 = update_from_heartbeat(s2, T0 + timedelta(hours=1), T0 + timedelta(hours=1), CFG)
    assert became_available(s2, s3)
    assert s1.status is GrokStatus.AVAILABLE  # inputs untouched


def test_should_probe() -> None:
    assert should_probe(T0, UsageState(), CFG)
    s = UsageState(last_probe=T0)
    assert not should_probe(T0 + timedelta(seconds=59), s, CFG)
    assert should_probe(T0 + timedelta(seconds=60), s, CFG)
