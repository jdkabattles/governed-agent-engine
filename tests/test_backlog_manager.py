from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from governed_agent_engine.core.errors import BacklogError
from governed_agent_engine.core.models import BacklogStatus, Executor, Plan
from governed_agent_engine.orchestrator.backlog_manager import BacklogManager, make_entry_id

from .conftest import T0, make_task

G = Executor.GROK


@pytest.fixture
def bm(root: Path) -> BacklogManager:
    return BacklogManager(root / "artifacts")


def test_create_and_get(bm: BacklogManager) -> None:
    e = bm.create(make_task(), T0, executor=G, requires_human_approval=False)
    assert e.status is BacklogStatus.PENDING
    assert bm.get(e.entry_id) == e


def test_plan_path(bm: BacklogManager) -> None:
    plan = Plan("t1", "e", "m", "s", T0)
    e = bm.create(make_task(), T0, executor=Executor.COGNITION, requires_human_approval=False,
                  plan=plan)  # fmt: skip
    assert e.plan_path is not None
    assert (bm.artifacts_dir / e.plan_path).is_file()


def test_awaiting_approval_not_replayed_until_approved(bm: BacklogManager) -> None:
    e = bm.create(make_task(action="publish", human_approval=True), T0, executor=G,
                  requires_human_approval=True)  # fmt: skip
    assert e.status is BacklogStatus.AWAITING_APPROVAL
    assert bm.replay() == []
    bm.update_status(e.entry_id, BacklogStatus.APPROVED, T0)
    assert [x.entry_id for x in bm.replay()] == [e.entry_id]


def test_immutability(bm: BacklogManager) -> None:
    e = bm.create(make_task(inputs={"a": 1}), T0, executor=G, requires_human_approval=False)
    with pytest.raises(BacklogError, match="immutable"):
        bm.create(make_task(inputs={"a": 1}), T0, executor=G, requires_human_approval=False)
    u = bm.update_status(e.entry_id, BacklogStatus.REJECTED, T0)
    strip = {"status", "status_history"}
    assert {k: v for k, v in u.to_dict().items() if k not in strip} == {
        k: v for k, v in e.to_dict().items() if k not in strip
    }


def test_forward_only(bm: BacklogManager) -> None:
    e = bm.create(make_task(), T0, executor=G, requires_human_approval=False)
    with pytest.raises(BacklogError, match="illegal"):
        bm.update_status(e.entry_id, BacklogStatus.DONE, T0)
    bm.update_status(e.entry_id, BacklogStatus.IN_PROGRESS, T0)
    done = bm.update_status(e.entry_id, BacklogStatus.DONE, T0)
    assert [s for s, _ in done.status_history] == [
        BacklogStatus.PENDING, BacklogStatus.IN_PROGRESS, BacklogStatus.DONE,
    ]  # fmt: skip
    with pytest.raises(BacklogError):
        bm.update_status(e.entry_id, BacklogStatus.PENDING, T0)
    with pytest.raises(BacklogError, match="no such"):
        bm.get("missing")


def test_replay_order(bm: BacklogManager) -> None:
    late = T0 + timedelta(minutes=1)
    for tid, pr, ts in [("late-high", "high", late), ("early-low", "low", T0),
                        ("early-high", "high", T0), ("early-norm", "normal", T0)]:  # fmt: skip
        bm.create(
            make_task(task_id=tid, priority=pr), ts, executor=G, requires_human_approval=False
        )
    order = [e.task_id for e in bm.replay()]
    assert order == ["early-high", "early-norm", "early-low", "late-high"]
    assert order == [e.task_id for e in bm.replay()]
    assert bm.counts() == {"pending": 4}


def test_entry_id_deterministic() -> None:
    assert make_entry_id(make_task(), T0) == make_entry_id(make_task(), T0)
    assert make_entry_id(make_task(), T0) != make_entry_id(make_task(task_id="x"), T0)
