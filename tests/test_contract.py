"""Shared handoff contract with example-executor-backend: the golden files in schemas/contract/ are
byte-identical in both repos and must parse and round-trip here (see schemas/CONTRACT.md)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from governed_agent_engine.core.models import (
    BacklogEntry,
    Executor,
    GrokResult,
    Heartbeat,
    Plan,
    Task,
)

DIR = Path(__file__).resolve().parent.parent / "schemas" / "contract"
MODELS: dict[str, Any] = {
    "task": Task,
    "plan": Plan,
    "backlog_entry": BacklogEntry,
    "evidence_result": GrokResult,
    "heartbeat": Heartbeat,
}
FILES = sorted(DIR.glob("*.json"))


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_golden_roundtrip(path: Path) -> None:
    model = MODELS[path.stem.split("_", 1)[1]]
    obj = model.from_dict(json.loads(path.read_text(encoding="utf-8")))
    again = model.from_dict(json.loads(json.dumps(obj.to_dict())))
    assert again == obj


def test_v1_files_upgrade() -> None:
    e = BacklogEntry.from_dict(json.loads((DIR / "v1_backlog_entry.json").read_text()))
    assert e.executor is Executor.GROK
    assert len(e.status_history) == 1
    p = Plan.from_dict(json.loads((DIR / "v1_plan.json").read_text()))
    assert p.endpoint == "pollinations"
    assert p.steps[0].kind is None


def test_v2_shapes() -> None:
    e = BacklogEntry.from_dict(json.loads((DIR / "v2_backlog_entry.json").read_text()))
    d = e.to_dict()
    assert d["schema_version"] == 2
    assert isinstance(d["status_history"][0], dict)
    assert d["task"]["provenance"] == ["root-1"]
    r = GrokResult.from_dict(json.loads((DIR / "v2_evidence_result.json").read_text()))
    assert r.started_at is not None
    assert r.to_dict()["started_at"]
    assert len(FILES) == 7
