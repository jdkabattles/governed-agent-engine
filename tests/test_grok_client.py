from __future__ import annotations

import json
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from governed_agent_engine.config.loader import UsageConfig
from governed_agent_engine.core.errors import TransportError
from governed_agent_engine.core.models import (
    BacklogEntry,
    BacklogStatus,
    Executor,
    GrokResult,
    GrokStatus,
    Heartbeat,
)
from governed_agent_engine.grok.client import (
    FileQueueTransport,
    GitQueueTransport,
    GitSync,
    GrokClient,
    GrokTransport,
)
from governed_agent_engine.grok.executor_interface import GrokExecutor

from .conftest import T0, make_task


def _entry() -> BacklogEntry:
    return BacklogEntry("e1", make_task(), T0, BacklogStatus.PENDING, False, Executor.GROK)


def test_file_queue(tmp_path: Path) -> None:
    t = FileQueueTransport(tmp_path / "b", tmp_path / "ev")
    assert isinstance(t, GrokTransport)
    client = GrokClient(t, UsageConfig(heartbeat_stale_after_s=60))
    ex = GrokExecutor(client)
    resp = ex.dispatch(_entry())
    assert resp.accepted
    assert Path(resp.reference).is_file()
    assert ex.collect("e1") is None
    assert client.probe(T0) == (GrokStatus.UNKNOWN, None)
    ev = tmp_path / "ev"
    ev.mkdir()
    (ev / "e1.json").write_text(json.dumps(GrokResult("e1", "t1", "done", T0).to_dict()))
    assert ex.collect("e1") is not None
    (ev / "heartbeat.json").write_text(json.dumps(Heartbeat(T0).to_dict()))
    assert client.probe(T0) == (GrokStatus.AVAILABLE, T0)
    assert client.probe(T0 + timedelta(minutes=5))[0] is GrokStatus.EXHAUSTED
    (ev / "heartbeat.json").write_text("{bad")
    assert client.probe(T0)[0] is GrokStatus.UNKNOWN
    (ev / "heartbeat.json").write_text('{"ts": "nope"}')
    assert client.probe(T0)[0] is GrokStatus.UNKNOWN


def _git(cwd: Path, *a: str) -> None:
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def test_git_queue_with_local_bare_remote(tmp_path: Path) -> None:
    bare = tmp_path / "r.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    work = tmp_path / "w"
    _git(tmp_path, "clone", "-q", str(bare), str(work))
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        _git(work, "config", k, v)
    (work / "seed").write_text("x")
    _git(work, "add", "seed")
    _git(work, "commit", "-qm", "seed")
    _git(work, "push", "-q", "origin", "main")
    gs = GitSync(work)
    t = GitQueueTransport(work / "artifacts/backlog", work / "artifacts/evidence", gs)
    assert t.submit(_entry()).transport == "git_queue"
    assert gs.push_paths([work / "seed"], "noop") is False
    assert t.last_heartbeat() is None  # pulls first
    with pytest.raises(TransportError, match="failed"):
        GitSync(work, remote="nope").pull()


def test_git_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("shutil.which", lambda _n: None)
    with pytest.raises(TransportError, match="not found"):
        GitSync(tmp_path)
