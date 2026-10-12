from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from governed_agent_engine import cli
from governed_agent_engine.orchestrator import main as orch_main

from .conftest import FakeHttp, jr, make_task, oai


@pytest.fixture
def patched(root: Path, monkeypatch: pytest.MonkeyPatch) -> FakeHttp:
    h = FakeHttp()
    h.add("POST", "https://", oai())
    real = orch_main.build
    monkeypatch.setattr(orch_main, "build", lambda r: real(r, http=h, env={}))
    ok_rows = [{"endpoint": "x", "outcome": "ok"}]
    monkeypatch.setattr(cli, "run_smoke", lambda r, e, judge0, markdown: ok_rows)
    return h


def _run(root: Path, capsys: pytest.CaptureFixture[str], *a: str) -> tuple[int, object]:
    rc = cli.main(["--root", str(root), *a])
    return rc, json.loads(capsys.readouterr().out)


def test_commands(root: Path, patched: FakeHttp, capsys: pytest.CaptureFixture[str],
                  monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    tf = root / "t.json"
    tf.write_text(json.dumps(make_task().to_dict()))
    rc, out = _run(root, capsys, "submit", str(tf))
    assert rc == 0
    assert out["status"] == "pending"  # type: ignore[index]
    rc, out = _run(root, capsys, "submit", "--json", json.dumps(make_task(task_id="j").to_dict()))
    assert rc == 0
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(make_task(task_id="s").to_dict())))
    rc, out = _run(root, capsys, "submit", "-")
    assert rc == 0
    rc, out = _run(root, capsys, "status")
    assert out["backlog"] == {"pending": 3}  # type: ignore[index]
    assert _run(root, capsys, "replay")[0] == 0
    rc, out = _run(root, capsys, "run", "--once")
    assert out["grok_status"] == "UNKNOWN"  # type: ignore[index]
    monkeypatch.setattr(orch_main.Orchestrator, "run_forever", lambda self, max_cycles: 1)
    assert _run(root, capsys, "run", "--cycles", "1")[1] == {"cycles": 1}
    assert _run(root, capsys, "validate")[1]["ok"] is True  # type: ignore[index]
    assert _run(root, capsys, "smoke")[0] == 0


def test_error_codes(root: Path, patched: FakeHttp, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(root, capsys, "submit", "--json",
                json.dumps(make_task(action="trade").to_dict()))[0] == 2  # fmt: skip
    assert _run(root, capsys, "submit", "--json", "{bad")[0] == 4
    patched.routes.clear()
    patched.add("POST", "https://", jr(500, {}))
    patched.add("GET", "https://", jr(500, {}))
    assert _run(root, capsys, "submit", "--json", json.dumps(make_task().to_dict()))[0] == 3


def test_smoke_exit_code(root: Path, monkeypatch: pytest.MonkeyPatch,
                         capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    monkeypatch.setattr(cli, "run_smoke", lambda *a, **k: [{"outcome": "error"}])
    assert _run(root, capsys, "smoke")[0] == 1


def test_file_and_json_errors_are_one_line(
    root: Path, patched: FakeHttp, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = [("submit", str(root / "missing.json")), ("submit", str(root))]
    (root / "bad.json").write_text("{not json")
    (root / "bin.json").write_bytes(b"\xff\xfe\x00")
    cases += [("submit", str(root / "bad.json")), ("submit", str(root / "bin.json")),
              ("submit", "--json", "[1, 2")]  # fmt: skip
    for args in cases:
        rc = cli.main(["--root", str(root), *args])
        cap = capsys.readouterr()
        assert rc == 4, args
        assert cap.err.count("\n") == 1, cap.err
        assert cap.err.startswith("gae: error: input: ")
        assert "Traceback" not in cap.err + cap.out
        assert json.loads(cap.out)["error"] == "input"
    rc = cli.main(["--root", str(root), "submit", str(root / "missing.json")])
    assert "file not found" in capsys.readouterr().err
    assert rc == 4


def test_unexpected_error_no_traceback(root: Path, monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    def boom(*a: object, **k: object) -> object:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cli.orch_main, "build", boom)
    assert cli.main(["--root", str(root), "status"]) == 1
    assert capsys.readouterr().err == "gae: error: RuntimeError: kaboom\n"
    monkeypatch.setattr(cli.orch_main, "build", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    assert cli.main(["--root", str(root), "status"]) == 4
    capsys.readouterr()
    monkeypatch.setenv("GAE_DEBUG", "1")
    monkeypatch.setattr(cli.orch_main, "build", boom)
    with pytest.raises(RuntimeError):
        cli.main(["--root", str(root), "status"])


def test_example_command_and_file(root: Path, patched: FakeHttp,
                                  capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    repo_example = json.loads((Path(__file__).parent.parent / "examples" / "task.json").read_text())
    assert repo_example == cli.example_task("research")
    for t in sorted(cli.EXAMPLE_GOALS):
        assert cli.main(["--root", str(root), "example", "--type", t]) == 0
        task = json.loads(capsys.readouterr().out)
        assert task["issued_by"] == "owner"
        assert task["type"] == t
    out = root / "task.json"
    assert cli.main(["--root", str(root), "example", "--out", str(out)]) == 0
    assert "gae submit" in capsys.readouterr().out
    rc, res = _run(root, capsys, "submit", str(out))
    assert rc == 0, res
    assert res["status"] in ("pending", "awaiting_approval")  # type: ignore[index]


def test_status_explanations() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 10, 11, 20, 0, tzinfo=UTC)
    assert cli.status_explanation("UNKNOWN", None, now, 7200).startswith(
        "UNKNOWN: Executor Agent has never"
    )
    ex = cli.status_explanation("EXHAUSTED", "2026-10-11T15:00:00+00:00", now, 7200)
    assert ex == ("EXHAUSTED: Executor Agent last checked in 5.0 hours ago (limit 2 h). Tasks will "
                  "queue until its next check-in; cognition still plans them.")  # fmt: skip
    ok = cli.status_explanation("AVAILABLE", "2026-10-11T19:30:00+00:00", now, 7200)
    assert "0.5 hours ago" in ok


def test_status_cli_explains(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = _run(root, capsys, "status")
    assert rc == 0
    assert out["grok"]["explanation"].startswith("UNKNOWN")  # type: ignore[index]
