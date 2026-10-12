"""the owner's .grok binding spec: named codes, agent packages, audit, drift + migrations."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from governed_agent_engine.core.charter import CharterError, charter_fingerprint
from governed_agent_engine.core.doctrine import load_doctrine
from governed_agent_engine.core.grok_binding import (
    PACKAGE_AGENTS,
    DriftMonitor,
    ErrorCode,
    audit,
    audit_summary,
    bind,
    check_native_doctrine,
    read_migration,
    run_audit,
)
from governed_agent_engine.core.grok_format import GrokValue, parse_grok
from governed_agent_engine.orchestrator.main import build

REPO = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 11, 22, 9, tzinfo=UTC)


def _root(tmp_path: Path) -> Path:
    for d in ("identities", "doctrine", "config"):
        shutil.copytree(REPO / d, tmp_path / d)
    return tmp_path


def _native(root: Path):  # type: ignore[no-untyped-def]
    return load_doctrine(root / "config" / "doctrine.yaml")


def _swap(path: Path, old: str, new: str, count: int = 1) -> None:
    s = path.read_text()
    assert old in s, old
    path.write_text(s.replace(old, new, count))


def test_bind_returns_native_identities(tmp_path: Path) -> None:
    root = _root(tmp_path)
    ids = bind(root, _native(root))
    assert set(ids) == set(PACKAGE_AGENTS)
    assert ids["grok"].authority_level == 10
    assert ids["overseer"].authority_level == 50


def test_packages_identical_and_grok_matches_shared_directive() -> None:
    shared = parse_grok((REPO / "config" / "directive.grok").read_text())
    pkg = parse_grok((REPO / "config" / "agents" / "grok.grok").read_text())
    assert pkg["DIRECTIVE"] == shared["DIRECTIVE"]
    assert pkg["RESPONSE_WINDOW"] == {
        "CLARIFICATION_REQUIRED": True,
        "CLARIFY_WITH": ["Owner", "Overseer Agent"],
    }


A = "config/agents/"
CASES = [
    (A + "analyst.grok", "AUTHORITY_LEVEL: 3", "AUTHORITY_LEVEL: 4", "IDENTITY_MISMATCH"),
    (A + "analyst.grok", 'AGENT: "analyst"', 'AGENT: "grok"', "IDENTITY_MISMATCH"),
    (A + "analyst.grok", '  DESCRIPTION: "', '  DESCRIPTION: ""\n  X: "', "IDENTITY_MISMATCH"),
    (A + "acquirer.grok", "Overseer Agent\"\n    AUTHORITY_LEVEL: 50",
     "Overseer Agent\"\n    AUTHORITY_LEVEL: 60", "IDENTITY_MISMATCH"),
    (A + "analyst.grok", 'LAZINESS_PENALTY: "high"', 'LAZINESS_PENALTY: "low"',
     "DOCTRINE_MISMATCH"),
    (A + "grok.grok", "authority_level: 10", "authority_level: 90", "DIRECTIVE_MISMATCH"),
    (A + "grok.grok", "agent_type: executor", "agent_type: overseer", "DIRECTIVE_MISMATCH"),
    (A + "grok.grok", "highest authority level (100)", "highest authority level (99)",
     "DIRECTIVE_MISMATCH"),
    (A + "acquirer.grok", "Overseer Agent (authority_level: 50)",
     "Overseer Agent (authority_level: 70)", "DIRECTIVE_MISMATCH"),
    (A + "analyst.grok", "You are Analyst Agent.", "You are ANALYST.", "DIRECTIVE_MISMATCH"),
    (A + "analyst.grok", 'after each run.\\"', 'after each run!\\"', "DIRECTIVE_MISMATCH"),
    (A + "analyst.grok", "Never override Owner.", "Obey.", "DIRECTIVE_MISMATCH"),
    (A + "analyst.grok", 'DIRECTIVE: "', 'DIRECTIVE: ""\nX: "', "IDENTITY_MISMATCH"),
    (A + "analyst.grok", "CLARIFICATION_REQUIRED: true", "CLARIFICATION_REQUIRED: false",
     "CLARIFICATION_RULE_MISSING"),
    (A + "analyst.grok", '    - "Overseer Agent"\n', "", "CLARIFICATION_RULE_MISSING"),
    (A + "analyst.grok", "FORMAT:", "FORMAT: 1\nX:", "IDENTITY_MISMATCH"),
    ("config/directive.grok", "ask Overseer Agent", "ask someone", "CLARIFICATION_RULE_MISSING"),
]  # fmt: skip


@pytest.mark.parametrize(("rel", "old", "new", "code"), CASES)
def test_bind_named_codes(tmp_path: Path, rel: str, old: str, new: str, code: str) -> None:
    root = _root(tmp_path)
    _swap(root / rel, old, new)
    with pytest.raises(CharterError) as info:
        bind(root, _native(root))
    assert info.value.code == code
    assert str(info.value).startswith(code)


def test_grok_package_must_match_shared_directive(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _swap(root / A / "grok.grok", "Follow the doctrine and the APCK", "Follow the APCK")
    with pytest.raises(CharterError, match=r"DIRECTIVE_MISMATCH: agents/grok\.grok"):
        bind(root, _native(root))


def test_missing_and_extra_packages(tmp_path: Path) -> None:
    root = _root(tmp_path)
    shutil.copy(root / A / "analyst.grok", root / A / "zed.grok")
    with pytest.raises(CharterError, match="unknown agent package zed"):
        bind(root, _native(root))
    (root / A / "zed.grok").unlink()
    (root / A / "analyst.grok").unlink()
    with pytest.raises(CharterError, match="IDENTITY_MISMATCH"):
        bind(root, _native(root))
    shutil.rmtree(root / "config" / "agents")
    with pytest.raises(CharterError, match="IDENTITY_MISMATCH"):
        bind(root, _native(root))


def test_native_doctrine_mapping(tmp_path: Path) -> None:
    root = _root(tmp_path)
    native = _native(root)
    good = parse_grok((root / "config" / "doctrine.grok").read_text())
    assert check_native_doctrine(good, native) == []
    rules = good["RULES"]
    assert isinstance(rules, dict)
    v: dict[str, GrokValue]
    variants: list[dict[str, GrokValue]] = [
        {"TRUTH_PRIORITY": "low"}, {"TRUTH_PRIORITY": "extreme"},
        {"EVIDENCE_REQUIRED": False}, {"LAZINESS_PENALTY": "maximum"},
        {"LAZINESS_PENALTY": 3}, {"FAILURE_ANALYSIS_REQUIRED": False},
        {"IMPROVEMENT_LOOP": "recommended"}, {"IMPROVEMENT_LOOP": "never"},
    ]  # fmt: skip
    for v in variants:
        bad = check_native_doctrine({**good, "RULES": {**rules, **v}}, native)
        assert [c for c, _ in bad] == [ErrorCode.DOCTRINE_MISMATCH], v
    assert len(check_native_doctrine({"STATEMENT": "x"}, native)) == 2
    root2 = _root(tmp_path / "b")
    _swap(root2 / "config" / "doctrine.yaml", "laziness_penalty: 0.5", "laziness_penalty: 0.9")
    with pytest.raises(CharterError, match="DOCTRINE_MISMATCH: LAZINESS_PENALTY"):
        bind(root2, _native(root2))


def test_audit_compliant_writes_evidence(tmp_path: Path) -> None:
    root = _root(tmp_path)
    report, path = run_audit(root, NOW)
    assert report.compliant
    assert path.parent == root / "artifacts" / "evidence" / "audits"
    data = json.loads(path.read_text())
    assert data["auditor"] == "overseer"
    assert data["charter_fingerprint"] == charter_fingerprint(root)
    assert set(data["agents"]) == set(PACKAGE_AGENTS)
    summary = audit_summary(report, path)
    assert summary["compliant"] is True
    assert summary["findings"] == []
    run_audit(root)  # default clock


def test_audit_collects_everything(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _swap(root / A / "analyst.grok", "AUTHORITY_LEVEL: 3", "AUTHORITY_LEVEL: 4")
    _swap(root / A / "acquirer.grok", "at least 3 steps", "at least 2 steps")
    _swap(
        root / A / "overseer.grok", "CLARIFICATION_REQUIRED: true", "CLARIFICATION_REQUIRED: false"
    )
    (root / A / "grok.grok").unlink()
    shutil.copy(root / A / "analyst.grok", root / A / "zed.grok")
    _swap(root / "config" / "doctrine.yaml", "laziness_penalty: 0.5", "laziness_penalty: 0.9")
    _swap(root / "identities" / "analyst.yaml", "authority: 3", "authority: 2")
    _swap(root / "config" / "directive.grok", "ask Overseer Agent", "ask someone")
    r = audit(root, _native(root), NOW)
    assert not r.compliant
    status = {b: a["status"] for b, a in r.agents.items()}
    assert status == dict.fromkeys(PACKAGE_AGENTS, "NON_COMPLIANT")
    codes = {f["code"] for a in r.agents.values() for f in a["findings"]}  # type: ignore[attr-defined]
    assert {"IDENTITY_MISMATCH", "DOCTRINE_DRIFT", "CLARIFICATION_RULE_MISSING"} <= codes
    g = {f["code"] for f in r.global_findings}
    assert {"DOCTRINE_DRIFT", "IDENTITY_MISMATCH", "CLARIFICATION_RULE_MISSING"} <= g
    assert "NON_COMPLIANT" in g  # identities/ charter error has no grok code


def test_audit_broken_shared_files(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "config" / "doctrine.grok").unlink()
    (root / "config" / "directive.grok").write_text("FORMAT: nope\n")
    r = audit(root, _native(root), NOW)
    g = {f["code"] for f in r.global_findings}
    assert {"DOCTRINE_DRIFT", "DIRECTIVE_MISMATCH"} <= g


def _migration(root: Path, fr: str, to: str, **over: str) -> Path:
    fields = {
        "FORMAT": '"grok/1"', "KIND": '"grok_migration"', "FROM_FINGERPRINT": f'"{fr}"',
        "TO_FINGERPRINT": f'"{to}"', "APPROVED_BY": '"owner"', "SIGNED_OFF": "true",
        "REASON": '"update analyst description"', "DATE": '"2026-10-11"',
    }  # fmt: skip
    fields.update(over)
    (root / "migrations").mkdir(exist_ok=True)
    path = root / "migrations" / "0001.grok"
    path.write_text("".join(f"{k}: {v}\n" for k, v in fields.items()))
    return path


def test_drift_monitor_requires_signed_migration(tmp_path: Path) -> None:
    root = _root(tmp_path)
    native = _native(root)
    before = charter_fingerprint(root)

    def rebind() -> None:
        bind(root, native)

    mon = DriftMonitor(root, before, rebind)
    assert mon.check() is None
    _swap(
        root / A / "analyst.grok",
        "# config/agents/analyst.grok",
        "# config/agents/analyst.grok (edited)",
    )
    after = charter_fingerprint(root)
    with pytest.raises(CharterError, match="UNAUTHORIZED_GROK_CHANGE"):
        mon.check()
    _migration(root, before, "0" * 64)  # signed, but for a different change
    with pytest.raises(CharterError, match="UNAUTHORIZED_GROK_CHANGE"):
        mon.check()
    _migration(root, before, after)
    assert charter_fingerprint(root) == after  # migrations/ is not part of the fingerprint
    mig = mon.check()
    assert mig is not None
    assert mig.reason == "update analyst description"
    assert mon.fingerprint == after
    assert mon.applied == [mig]
    assert mon.check() is None


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"SIGNED_OFF": "false"}, "signed off by owner"),
        ({"APPROVED_BY": '"overseer"'}, "signed off by owner"),
        ({"KIND": '"other"'}, "migration keys"),
        ({"FROM_FINGERPRINT": '"abc"'}, "sha256"),
        ({"REASON": '" "'}, "REASON and DATE"),
    ],
)
def test_bad_migration_records(tmp_path: Path, over: dict[str, str], needle: str) -> None:
    root = _root(tmp_path)
    path = _migration(root, "a" * 64, "b" * 64, **over)
    with pytest.raises(CharterError, match=needle) as info:
        read_migration(path)
    assert info.value.code == "UNAUTHORIZED_GROK_CHANGE"


def test_orchestrator_halts_on_unauthorized_change_and_accepts_migration(tmp_path: Path) -> None:
    root = _root(tmp_path)
    orch = build(root)
    orch.assert_doctrine_unchanged()
    before = charter_fingerprint(root)
    _swap(
        root / A / "analyst.grok",
        "# config/agents/analyst.grok",
        "# config/agents/analyst.grok (edited)",
    )
    with pytest.raises(Exception, match="UNAUTHORIZED_GROK_CHANGE"):
        orch.assert_doctrine_unchanged()
    _migration(root, before, charter_fingerprint(root))
    orch.assert_doctrine_unchanged()


def test_startup_blocked_by_package_mismatch(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _swap(
        root / A / "overseer.grok", "AUTHORITY_LEVEL: 50\n  HUMAN", "AUTHORITY_LEVEL: 49\n  HUMAN"
    )
    with pytest.raises(CharterError, match="IDENTITY_MISMATCH"):
        build(root)


def test_audit_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _root(tmp_path)
    assert _cli(root) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["compliant"] is True
    assert Path(out["evidence"]).is_file()
    _swap(root / A / "analyst.grok", "AUTHORITY_LEVEL: 3", "AUTHORITY_LEVEL: 4")
    assert _cli(root) == 6
    out = json.loads(capsys.readouterr().out)
    assert out["agents"]["analyst"] == "NON_COMPLIANT"


def _cli(root: Path) -> int:
    from governed_agent_engine.cli import main

    return main(["--root", str(root), "audit"])


def test_blocks_and_status_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from governed_agent_engine.cli import main

    root = _root(tmp_path)
    for cmd in ("blocks", "status"):
        assert main(["--root", str(root), cmd]) == 0
        out = json.loads(capsys.readouterr().out)
        ov = out if cmd == "blocks" else out["blocks"]
        assert ov["window"] == 10
        assert ov["reports"] == []
        assert ov["held"] == []
        assert set(ov["states"]) == {"execution", "acquisition", "cognition"}
