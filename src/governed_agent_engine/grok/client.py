"""Grok Bot client. Grok Bot has no inbound API, so the default transport is a file/git queue.

Contract:
* submit(): write ``artifacts/backlog/<entry_id>.json`` (optionally git commit + push).
* a scheduled Grok Bot routine pulls the repo, executes ``pending``/``approved`` entries
  (never ``awaiting_approval``), writes ``artifacts/evidence/<entry_id>.json`` and refreshes
  ``artifacts/evidence/heartbeat.json``, then pushes.
* probe(): heartbeat freshness -> UNKNOWN / AVAILABLE / EXHAUSTED.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from governed_agent_engine.config.loader import UsageConfig
from governed_agent_engine.core.errors import TransportError, ValidationError
from governed_agent_engine.core.models import (
    BacklogEntry,
    GrokResponse,
    GrokResult,
    GrokStatus,
    Heartbeat,
)
from governed_agent_engine.orchestrator.backlog_manager import atomic_write_json
from governed_agent_engine.orchestrator.usage_state import evaluate_heartbeat


@runtime_checkable
class GrokTransport(Protocol):
    name: str

    def submit(self, entry: BacklogEntry) -> GrokResponse: ...

    def fetch_result(self, entry_id: str) -> GrokResult | None: ...

    def last_heartbeat(self) -> datetime | None: ...


class FileQueueTransport:
    name = "file_queue"

    def __init__(self, backlog_dir: Path, evidence_dir: Path) -> None:
        self.backlog_dir = backlog_dir
        self.evidence_dir = evidence_dir

    def submit(self, entry: BacklogEntry) -> GrokResponse:
        path = self.backlog_dir / f"{entry.entry_id}.json"
        atomic_write_json(path, entry.to_dict())
        return GrokResponse(accepted=True, reference=str(path), transport=self.name)

    def _read(self, path: Path) -> object | None:
        if not path.is_file():
            return None
        try:
            data: object = json.loads(path.read_text(encoding="utf-8"))
            return data
        except json.JSONDecodeError:
            return None

    def fetch_result(self, entry_id: str) -> GrokResult | None:
        raw = self._read(self.evidence_dir / f"{entry_id}.json")
        return None if raw is None else GrokResult.from_dict(raw)

    def last_heartbeat(self) -> datetime | None:
        raw = self._read(self.evidence_dir / "heartbeat.json")
        if raw is None:
            return None
        try:
            return Heartbeat.from_dict(raw).ts
        except ValidationError:
            return None


class GitSync:
    """Optional: pull before reading evidence, push after writing backlog.

    Uses the user's own git credentials/helper; this package never handles tokens.
    """

    def __init__(self, repo_dir: Path, remote: str = "origin", branch: str = "main") -> None:
        git = shutil.which("git")
        if git is None:
            raise TransportError("git executable not found")
        self._git, self.repo_dir, self.remote, self.branch = git, repo_dir, remote, branch

    def _run(self, *args: str) -> str:
        proc = subprocess.run(  # noqa: S603
            [self._git, *args], cwd=self.repo_dir, capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            raise TransportError(f"git {args[0]} failed: {proc.stderr.strip()[:300]}")
        return proc.stdout

    def pull(self) -> None:
        self._run("pull", "--ff-only", self.remote, self.branch)

    def push_paths(self, paths: list[Path], message: str) -> bool:
        self._run("add", "--", *[str(p) for p in paths])
        if not self._run("status", "--porcelain").strip():
            return False
        self._run("commit", "-m", message)
        self._run("push", self.remote, self.branch)
        return True


class GitQueueTransport(FileQueueTransport):
    name = "git_queue"

    def __init__(self, backlog_dir: Path, evidence_dir: Path, git: GitSync) -> None:
        super().__init__(backlog_dir, evidence_dir)
        self.git = git

    def submit(self, entry: BacklogEntry) -> GrokResponse:
        resp = super().submit(entry)
        self.git.push_paths([Path(resp.reference)], f"backlog: {entry.entry_id}")
        return GrokResponse(accepted=True, reference=resp.reference, transport=self.name)

    def last_heartbeat(self) -> datetime | None:
        self.git.pull()
        return super().last_heartbeat()


class GrokClient:
    def __init__(self, transport: GrokTransport, usage: UsageConfig) -> None:
        self.transport = transport
        self._usage = usage

    def probe(self, now: datetime) -> tuple[GrokStatus, datetime | None]:
        hb = self.transport.last_heartbeat()
        return evaluate_heartbeat(hb, now, self._usage), hb
