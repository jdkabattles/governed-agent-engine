from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from governed_agent_engine.core.errors import ValidationError
from governed_agent_engine.core.models import (
    BacklogEntry,
    BacklogStatus,
    EndpointConfig,
    Executor,
    GovernanceConfig,
    GrokResult,
    Heartbeat,
    Plan,
    PlanStep,
    Priority,
    RouteDecision,
    StepKind,
    Task,
    UsageState,
)

from .conftest import REPO, T0, make_task


def test_task_defaults_and_normalization() -> None:
    t = make_task(action=" PLAN ")
    assert t.action == "plan"
    assert t.priority is Priority.NORMAL
    assert Task.from_dict(t.to_dict()) == t


def test_task_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        make_task().task_id = "x"  # type: ignore[misc]


@pytest.mark.parametrize(
    "bad",
    [
        {"task_id": "", "type": "custom"},
        {"task_id": "a b", "type": "custom"},
        {"task_id": "ok", "type": "nope"},
        {"task_id": "ok", "type": "custom", "extra": 1},
        {"task_id": "ok", "type": "custom", "priority": "urgent"},
        {"task_id": "ok", "type": "custom", "depth": -1},
        {"task_id": "ok", "type": "custom", "depth": True},
        {"task_id": "ok", "type": "custom", "requires_grok": "yes"},
        {"task_id": "ok", "type": "custom", "inputs": []},
        {"task_id": "ok", "type": "custom", "parent_task_id": "bad id"},
        {"type": "custom"},
        "not a dict",
    ],
)
def test_task_rejects(bad: object) -> None:
    with pytest.raises(ValidationError):
        Task.from_dict(bad)


def test_plan_roundtrip_and_errors() -> None:
    p = Plan("t", "e", "m", "s", T0, (PlanStep(0, "a"),), "raw")
    assert Plan.from_dict(json.loads(json.dumps(p.to_dict()))) == p
    with pytest.raises(ValidationError):
        Plan.from_dict({**p.to_dict(), "steps": "x"})
    with pytest.raises(ValidationError):
        Plan.from_dict({**p.to_dict(), "created_at": "2026-10-10T00:00:00"})  # naive
    with pytest.raises(ValidationError):
        Plan.from_dict({**p.to_dict(), "created_at": "garbage"})
    with pytest.raises(ValidationError):
        Plan.from_dict({**p.to_dict(), "created_at": 5})


def _entry() -> BacklogEntry:
    return BacklogEntry(
        entry_id="e1",
        task=make_task(priority="high"),
        created_at=T0,
        status=BacklogStatus.PENDING,
        requires_human_approval=False,
        executor=Executor.GROK,
        status_history=((BacklogStatus.PENDING, T0),),
    )


def test_backlog_entry_roundtrip() -> None:
    e = _entry()
    assert BacklogEntry.from_dict(json.loads(json.dumps(e.to_dict()))) == e
    assert e.sort_key() == (T0, 0, "e1")
    assert e.task_id == "t1"
    assert e.with_status(BacklogStatus.DONE, T0).status_history[-1][0] is BacklogStatus.DONE
    with pytest.raises(ValidationError):
        BacklogEntry.from_dict({**e.to_dict(), "status_history": {}})
    with pytest.raises(ValidationError):
        BacklogEntry.from_dict({**e.to_dict(), "status": "weird"})


def test_evidence_models() -> None:
    r = GrokResult("e", "t", "done", T0, outputs={"k": 1})
    assert GrokResult.from_dict(r.to_dict()) == r
    with pytest.raises(ValidationError):
        GrokResult.from_dict({**r.to_dict(), "status": "maybe"})
    hb = Heartbeat(T0)
    assert Heartbeat.from_dict(hb.to_dict()) == hb


def test_small_models_to_dict() -> None:
    assert UsageState().to_dict()["status"] == "UNKNOWN"
    assert RouteDecision("t", Executor.COGNITION, "r", ("a",)).to_dict()["endpoints"] == ["a"]


def _ep(**kw: object) -> EndpointConfig:
    base: dict[str, object] = {"kind": "openai_compat", "base_url": "https://x/", "model": "m",
                               "rpm": 1}  # fmt: skip
    base.update(kw)
    return EndpointConfig.from_dict("x", base)


def test_endpoint_rules() -> None:
    assert _ep().base_url == "https://x"
    for bad in (
        {"cost_usd": 0.5},
        {"base_url": "http://x"},
        {"auth": "bearer"},
        {"kind": "magic"},
        {"rpm": 0},
        {"task_types": ["nope"]},
        {"surprise": 1},
        {"trust": "maybe"},
        {"rpm": "1"},
        {"timeout_s": "x"},
    ):
        with pytest.raises(ValidationError):
            _ep(**bad)
    assert _ep(auth="bearer", api_key_env="K", rpd=5).rpd == 5


def test_governance_rules() -> None:
    g = GovernanceConfig.from_dict({})
    assert "purchase" in g.process.human_approval_required
    for bad in (
        {"constraints": {"governance_mutable": True}},
        {"constraints": {"cost_ceiling_usd": 1}},
        {"knowledge": {"cognition_is_advisory": False}},
        {"constraints": {"max_recursion_depth": 99}},
        {"process": {"allowed_task_types": ["bogus"]}},
        {"oops": 1},
        {"artifacts": {"x": 1}},
    ):
        with pytest.raises(ValidationError):
            GovernanceConfig.from_dict(bad)


SCHEMAS = REPO / "schemas"


@pytest.mark.parametrize(
    ("schema", "instance"),
    [
        ("task.schema.json", make_task().to_dict()),
        ("backlog_entry.schema.json", _entry().to_dict()),
        ("evidence_result.schema.json", GrokResult("e", "t", "done", T0).to_dict()),
        ("evidence_heartbeat.schema.json", Heartbeat(T0).to_dict()),
        (
            "plan.schema.json",
            Plan(
                "t1",
                "e",
                "m",
                "s",
                T0,
                (
                    PlanStep(0, "gather facts", StepKind.GATHER, False, ()),
                    PlanStep(1, "do it", StepKind.EXECUTE, True, ("x",)),
                    PlanStep(2, "legacy"),
                ),
            ).to_dict(),
        ),
    ],
)
def test_json_schemas_accept_model_output(schema: str, instance: dict[str, object]) -> None:
    # jsonschema pulls rpds-py (Rust) and so is unavailable on Termux; skip there.
    jsonschema = pytest.importorskip("jsonschema")
    s = json.loads(Path(SCHEMAS / schema).read_text())
    jsonschema.Draft202012Validator.check_schema(s)
    jsonschema.validate(instance, s)
