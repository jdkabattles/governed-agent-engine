from __future__ import annotations

from dataclasses import replace

from governed_agent_engine.config.loader import AppConfig
from governed_agent_engine.core.models import Executor, GrokStatus, UsageState
from governed_agent_engine.orchestrator.router import route_task

from .conftest import make_task

AV = UsageState(status=GrokStatus.AVAILABLE)
EX = UsageState(status=GrokStatus.EXHAUSTED)
UN = UsageState(status=GrokStatus.UNKNOWN)


def test_available_goes_to_grok(config: AppConfig) -> None:
    d = route_task(make_task(), AV, config.routing)
    assert d.executor is Executor.GROK
    assert d.endpoints == ()


def test_exhausted_and_unknown_go_to_cognition(config: AppConfig) -> None:
    for u in (EX, UN):
        d = route_task(make_task(), u, config.routing)
        assert d.executor is Executor.COGNITION
        assert d.endpoints == config.routing.default_chain


def test_task_type_chain(config: AppConfig) -> None:
    d = route_task(make_task(type="research"), EX, config.routing)
    assert d.endpoints[0] == "ovh"


def test_requires_grok_never_downgraded(config: AppConfig) -> None:
    for u in (AV, EX, UN):
        assert (
            route_task(make_task(requires_grok=True), u, config.routing).executor is Executor.GROK
        )
    assert "queued" in route_task(make_task(requires_grok=True), EX, config.routing).reason


def test_cognition_only_types(config: AppConfig) -> None:
    d = route_task(make_task(type="governance_check"), AV, config.routing)
    assert d.executor is Executor.COGNITION


def test_default_executor_cognition(config: AppConfig) -> None:
    r = replace(config.routing, default_executor=Executor.COGNITION)
    assert route_task(make_task(), AV, r).executor is Executor.COGNITION


def test_pure(config: AppConfig) -> None:
    t = make_task()
    assert route_task(t, EX, config.routing) == route_task(t, EX, config.routing)
