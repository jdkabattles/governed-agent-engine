from __future__ import annotations

import json
from pathlib import Path

import pytest

from governed_agent_engine.cognition.health import HealthTracker
from governed_agent_engine.cognition.planner import parse_plan
from governed_agent_engine.cognition.registry import build_clients, build_limiters
from governed_agent_engine.cognition.strategy import CognitionStrategy
from governed_agent_engine.config.loader import AppConfig
from governed_agent_engine.core.errors import (
    AllEndpointsFailedError,
    GovernanceError,
    GovernanceTamperError,
)
from governed_agent_engine.core.models import BacklogStatus, Executor, GrokResult, Heartbeat
from governed_agent_engine.orchestrator.main import Orchestrator, build, normalize

from .conftest import T0, FakeClock, FakeHttp, Mono, jr, make_task, oai


def _http_ok() -> FakeHttp:
    h = FakeHttp()
    h.add("POST", "https://", oai())
    return h


def _orch(config: AppConfig, clock: FakeClock, http: FakeHttp | None = None) -> Orchestrator:
    en = config.enabled_endpoints()
    m = Mono()
    strat = CognitionStrategy(
        build_clients(en, http or _http_ok(), {"OPENROUTER_API_KEY": "k", "GEMINI_API_KEY": "g"}),
        en, build_limiters(en, m), HealthTracker(clock=m), config.governance,
    )  # fmt: skip
    return Orchestrator(config, strat, clock=clock)


def _hb(root: Path) -> None:
    (root / "artifacts/evidence/heartbeat.json").write_text(json.dumps(Heartbeat(T0).to_dict()))


def _evidence(root: Path, entry_id: str, status: str = "done") -> None:
    r = GrokResult(entry_id, "t1", status, T0)
    (root / f"artifacts/evidence/{entry_id}.json").write_text(json.dumps(r.to_dict()))


def test_normalize() -> None:
    t = normalize({"task": {"task_id": "a", "type": " custom ", "action": " Plan "}})
    assert (t.type, t.action) == ("custom", "plan")


def test_parse_plan() -> None:
    p = parse_plan(make_task(), "Sum.\n1. a\n2) b\n- c\nnot", "e", "m", T0)
    assert [s.description for s in p.steps] == ["a", "b", "c"]
    assert parse_plan(make_task(), "", "e", "m", T0).summary == ""


def test_cognition_path(root: Path, config: AppConfig, clock: FakeClock) -> None:
    o = _orch(config, clock)
    out = o.submit(make_task())
    assert out.decision.executor is Executor.COGNITION
    assert out.entry.plan_path is not None
    assert (root / "artifacts" / out.entry.plan_path).is_file()
    assert o.metrics.counters["endpoint.pollinations.ok"] == 1
    phases = [json.loads(x)["phase"] for x in
              (root / "artifacts/logs/2026-10-10.jsonl").read_text().splitlines()]  # fmt: skip
    assert phases == ["validate", "route", "cognition", "backlog"]


def test_grok_path_and_replay(root: Path, config: AppConfig, clock: FakeClock) -> None:
    _hb(root)
    o = _orch(config, clock)
    out = o.submit(make_task())
    assert out.decision.executor is Executor.GROK
    assert [e.entry_id for e in o.replay()] == [out.entry.entry_id]
    _evidence(root, out.entry.entry_id)
    assert o.replay() == []
    assert o.backlog.get(out.entry.entry_id).status is BacklogStatus.DONE


def test_failed_evidence(root: Path, config: AppConfig, clock: FakeClock) -> None:
    _hb(root)
    o = _orch(config, clock)
    e = o.submit(make_task()).entry
    o.backlog.update_status(e.entry_id, BacklogStatus.IN_PROGRESS, T0)
    _evidence(root, e.entry_id, "failed")
    o.replay()
    assert o.backlog.get(e.entry_id).status is BacklogStatus.FAILED


def test_human_gate(root: Path, config: AppConfig, clock: FakeClock) -> None:
    _hb(root)
    o = _orch(config, clock)
    e = o.submit(make_task(action="send_message", human_approval=True)).entry
    assert e.status is BacklogStatus.AWAITING_APPROVAL
    assert o.replay() == []


def test_denials(root: Path, config: AppConfig, clock: FakeClock) -> None:
    o = _orch(config, clock)
    with pytest.raises(GovernanceError):
        o.submit(make_task(action="trade"))
    assert o.metrics.counters["governance.denied"] == 1
    gf = root / "config/governance.yaml"
    gf.write_text(gf.read_text() + "# tamper\n")
    with pytest.raises(GovernanceTamperError):
        o.submit(make_task())


def test_all_failed(config: AppConfig, clock: FakeClock) -> None:
    h = FakeHttp()
    h.add("POST", "https://", jr(500, {}))
    h.add("GET", "https://", jr(500, {}))
    o = _orch(config, clock, h)
    with pytest.raises(AllEndpointsFailedError):
        o.submit(make_task())
    assert o.metrics.counters["cognition.all_failed"] == 1


def test_run_cycle_inbox_and_replay_on_available(
    root: Path, config: AppConfig, clock: FakeClock
) -> None:
    o = _orch(config, clock)
    inbox = root / "artifacts/inbox"
    (inbox / "a.json").write_text(json.dumps(make_task(task_id="a", requires_grok=True).to_dict()))
    (inbox / "b.json").write_text("{not json")
    (inbox / "c.json").write_text(json.dumps(make_task(task_id="c", action="purchase").to_dict()))
    r = o.run_cycle()
    assert (r.intake_ok, r.intake_rejected) == (1, 2)
    assert r.grok_status == "UNKNOWN"
    assert not r.replay_triggered
    assert r.pending_for_grok == 1
    assert sorted(p.name for p in (inbox / "rejected").iterdir()) == ["b.json", "c.json"]
    assert (inbox / "processed/a.json").is_file()
    # Grok comes alive -> replay triggered, evidence reconciled
    _hb(root)
    entry = o.backlog.entries()[0]
    _evidence(root, entry.entry_id)
    r2 = o.run_cycle()
    assert r2.replay_triggered
    assert r2.reconciled == 1
    assert r2.pending_for_grok == 0
    assert (root / "artifacts/metrics/metrics.json").is_file()


def test_run_forever(config: AppConfig, clock: FakeClock) -> None:
    o = _orch(config, clock)
    sleeps: list[float] = []
    assert o.run_forever(max_cycles=2, sleep=sleeps.append) == 2
    assert sleeps == [config.usage.loop_interval_s]


def test_build_uses_real_config(root: Path) -> None:
    o = build(root, http=_http_ok(), env={})
    assert "pollinations" in o.strategy.clients
