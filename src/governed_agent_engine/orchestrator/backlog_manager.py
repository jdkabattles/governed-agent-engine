"""Durable, auditable backlog: immutable entries, forward-only status, ordered replay."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from governed_agent_engine.core.errors import BacklogError
from governed_agent_engine.core.models import (
    STATUS_TRANSITIONS,
    BacklogEntry,
    BacklogStatus,
    Executor,
    Plan,
    Task,
)


def atomic_write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def make_entry_id(task: Task, created_at: datetime) -> str:
    digest = hashlib.sha256((task.canonical_json() + created_at.isoformat()).encode()).hexdigest()
    return f"{created_at.strftime('%Y%m%dT%H%M%SZ')}-{task.task_id}-{digest[:12]}"


class BacklogManager:
    def __init__(self, artifacts_dir: Path) -> None:
        self.artifacts_dir = artifacts_dir
        self.backlog_dir = artifacts_dir / "backlog"
        self.plans_dir = artifacts_dir / "plans"

    def _path(self, entry_id: str) -> Path:
        return self.backlog_dir / f"{entry_id}.json"

    def create(
        self,
        task: Task,
        now: datetime,
        *,
        executor: Executor,
        requires_human_approval: bool,
        plan: Plan | None = None,
    ) -> BacklogEntry:
        entry_id = make_entry_id(task, now)
        if self._path(entry_id).exists():
            raise BacklogError(f"entry exists (immutable): {entry_id}")
        plan_path: str | None = None
        if plan is not None:
            p = self.plans_dir / f"{entry_id}.plan.json"
            atomic_write_json(p, plan.to_dict())
            plan_path = p.relative_to(self.artifacts_dir).as_posix()
        status = (
            BacklogStatus.AWAITING_APPROVAL if requires_human_approval else BacklogStatus.PENDING
        )
        entry = BacklogEntry(
            entry_id=entry_id,
            task=task,
            created_at=now,
            status=status,
            requires_human_approval=requires_human_approval,
            executor=executor,
            plan_path=plan_path,
            status_history=((status, now),),
        )
        atomic_write_json(self._path(entry_id), entry.to_dict())
        return entry

    def get(self, entry_id: str) -> BacklogEntry:
        path = self._path(entry_id)
        if not path.is_file():
            raise BacklogError(f"no such entry: {entry_id}")
        return BacklogEntry.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def update_status(self, entry_id: str, new: BacklogStatus, now: datetime) -> BacklogEntry:
        current = self.get(entry_id)
        if new not in STATUS_TRANSITIONS[current.status]:
            raise BacklogError(f"illegal transition {current.status} -> {new} ({entry_id})")
        updated = current.with_status(new, now)
        atomic_write_json(self._path(entry_id), updated.to_dict())
        return updated

    def entries(self) -> list[BacklogEntry]:
        out = [
            BacklogEntry.from_dict(json.loads(p.read_text(encoding="utf-8")))
            for p in self.backlog_dir.glob("*.json")
        ]
        return sorted(out, key=BacklogEntry.sort_key)

    def replay(self, statuses: frozenset[BacklogStatus] | None = None) -> list[BacklogEntry]:
        """Ordered by (created_at, priority rank, entry_id): deterministic."""
        wanted = statuses or frozenset({BacklogStatus.PENDING, BacklogStatus.APPROVED})
        return [e for e in self.entries() if e.status in wanted]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.entries():
            out[e.status.value] = out.get(e.status.value, 0) + 1
        return dict(sorted(out.items()))
