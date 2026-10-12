"""Agent-level routing penalty."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from governed_agent_engine.core import routing_penalty as rp
from governed_agent_engine.core.charter import CharterError, charter_fingerprint
from governed_agent_engine.core.doctrine import load_doctrine
from governed_agent_engine.core.grok_binding import DriftMonitor, audit, bind, penalty_table
from governed_agent_engine.core.grok_format import GrokValue

REPO = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 11, 23, 43, tzinfo=UTC)


def _root(tmp_path: Path) -> Path:
    for d in ("identities", "doctrine", "config", "migrations"):
        shutil.copytree(REPO / d, tmp_path / d)
    return tmp_path


def _override(root: Path, bot: str, level: str) -> None:
    p = root / "config" / "agents" / f"{bot}.grok"
    s = p.read_text()
    assert "DOCTRINE_OVERRIDE" not in s
    p.write_text(
        s.replace(
            "\nDIRECTIVE: ",
            f'\nDOCTRINE_OVERRIDE:\n  RULES:\n    LAZINESS_PENALTY: "{level}"\nDIRECTIVE: ',
            1,
        )
    )


def test_table_from_grok_and_overseer_never_routed() -> None:
    t = penalty_table(REPO)
    assert set(t) == {"grok", "acquirer", "analyst"}  # overseer absent
    assert all((p.level, p.value, p.source) == ("high", 0.5, "shared") for p in t.values())
    assert "maximum" not in rp.ROUTING_LEVELS
    overseer = rp.Penalty("overseer", *rp.OVERSEER_LEVEL, "agent-specific")
    with pytest.raises(rp.PenaltyError):
        rp.score(overseer, 1.0, True)


@pytest.mark.parametrize(
    ("agent", "base", "final"), [("grok", 1.0, 0.5), ("acquirer", 0.8, 0.4), ("analyst", 0.6, 0.3)]
)
def test_spec_examples(agent: str, base: float, final: float) -> None:
    p = penalty_table(REPO)[agent]
    assert rp.score(p, base, True) == final
    assert rp.score(p, base, False) == base


def test_overseer_audit_only(tmp_path: Path) -> None:
    root = _root(tmp_path)
    rep = audit(root, load_doctrine(root / "config" / "doctrine.yaml"), NOW).to_dict()
    assert rep["evaluator"]["laziness_factor"] == 0.25  # type: ignore[index]
    assert rep["evaluator"]["enters_routing"] is False  # type: ignore[index]
    assert "overseer" not in rep["routing_penalties"]  # type: ignore[operator]
    assert rep["compliant"] is True


def test_stricter_allowed_looser_or_maximum_stops_startup(tmp_path: Path) -> None:
    root = _root(tmp_path / "a")
    _override(root, "analyst", "high")  # equal: shared source
    assert penalty_table(root)["analyst"].source == "shared"
    shared: dict[str, GrokValue] = {"RULES": {"LAZINESS_PENALTY": "medium"}}
    strict = rp.resolve(
        shared, {"analyst": {"DOCTRINE_OVERRIDE": {"RULES": {"LAZINESS_PENALTY": "high"}}}}
    )
    assert (strict["analyst"].value, strict["analyst"].source) == (0.5, "agent-specific")
    assert strict["grok"].value == 0.75
    for bad in ("low", "maximum", "bogus"):
        with pytest.raises(rp.PenaltyError) as info:
            rp.resolve(
                shared, {"grok": {"DOCTRINE_OVERRIDE": {"RULES": {"LAZINESS_PENALTY": bad}}}}
            )
        assert info.value.code == rp.PENALTY_MISMATCH
    with pytest.raises(rp.PenaltyError):
        rp.resolve({"RULES": {"LAZINESS_PENALTY": "maximum"}}, {})
    root = _root(tmp_path / "b")
    _override(root, "acquirer", "medium")
    with pytest.raises(CharterError) as ce:
        penalty_table(root)
    assert ce.value.code == "PENALTY_MISMATCH"
    with pytest.raises(CharterError):
        bind(root, load_doctrine(root / "config" / "doctrine.yaml"))


def test_ledger_hash_chain_and_audit(tmp_path: Path) -> None:
    root = _root(tmp_path)
    table = penalty_table(root)
    led = root.joinpath("artifacts", *rp.LEDGER)
    r1 = rp.record(led, table["grok"], True, NOW.isoformat(), "t1")
    r2 = rp.record(led, table["analyst"], False, NOW.isoformat(), "t2", 0.6)
    assert (r1["final_score"], r2["final_score"], r2["prev_hash"]) == (0.5, 0.6, r1["hash"])
    assert set(r1) >= {
        "agent",
        "laziness_detected",
        "penalty_level",
        "penalty_value",
        "base_score",
        "final_score",
        "doctrinal_source",
        "timestamp",
    }
    assert rp.verify(led, table) == []
    assert rp.current_scores(led) == {"grok": 0.5, "acquirer": 1.0, "analyst": 0.6}
    native = load_doctrine(root / "config" / "doctrine.yaml")
    assert audit(root, native, NOW).compliant
    rows = led.read_text().splitlines()
    tampered = json.loads(rows[0]) | {"final_score": 1.0}
    led.write_text(json.dumps(tampered) + "\n" + rows[1] + "\n")
    codes = {c for c, _ in rp.verify(led, table)}
    assert "UNAUTHORIZED_PENALTY_CHANGE" in codes
    rep = audit(root, native, NOW)
    assert not rep.compliant
    assert any(f["code"] == "UNAUTHORIZED_PENALTY_CHANGE" for f in rep.global_findings)
    # resealed but inconsistent / overseer injected / incomplete
    led.unlink()
    bad = rp.Penalty("grok", "high", 0.5, "shared")
    rec = rp.record(led, bad, True, NOW.isoformat(), "t3")
    rec["final_score"] = 0.9
    rec["hash"] = rp._digest(rec)
    led.write_text(json.dumps(rec) + "\n")
    assert [c for c, _ in rp.verify(led, table)] == ["PENALTY_MISMATCH"]
    led.unlink()
    rp.record(led, rp.Penalty("grok", "low", 0.9, "shared"), False, NOW.isoformat(), "t4")
    assert [c for c, _ in rp.verify(led, table)] == ["UNAUTHORIZED_PENALTY_CHANGE"]
    with led.open("a") as fh:
        fh.write(json.dumps({"agent": "overseer"}) + "\n")
    overseer_row = {
        "agent": "overseer",
        **{k: 0 for k in rp.RECORD_KEYS if k != "agent"},
        "prev_hash": "",
        "hash": "",
    }
    assert any(c == "PENALTY_MISMATCH" for c, _ in rp.verify(led, table))
    led.write_text(json.dumps(overseer_row) + "\n")
    assert any("never enters routing" in m for _, m in rp.verify(led, table))
    led.write_text("{not json\n")
    assert [c for c, _ in rp.verify(led, table)] == ["UNAUTHORIZED_PENALTY_CHANGE"]


def test_penalty_drift_requires_migration(tmp_path: Path) -> None:
    root = _root(tmp_path)
    mon = DriftMonitor(root, charter_fingerprint(root), lambda: None)
    assert mon.penalties == penalty_table(root)
    before = charter_fingerprint(root)
    p = root / "config" / "agents" / "grok.grok"
    p.write_text(
        p.read_text().replace(
            "\nDIRECTIVE: ",
            '\nDOCTRINE_OVERRIDE:\n  RULES:\n    TRUTH_PRIORITY: "highest"\nDIRECTIVE: ',
            1,
        )
    )
    with pytest.raises(CharterError) as info:  # penalties unchanged -> generic change code
        mon.check()
    assert info.value.code == "UNAUTHORIZED_GROK_CHANGE"
    p.write_text((REPO / "config" / "agents" / "grok.grok").read_text())
    assert mon.check() is None
    shared = root / "config" / "doctrine.grok"
    shared.write_text(
        shared.read_text().replace('LAZINESS_PENALTY: "high"', 'LAZINESS_PENALTY: "medium"', 1)
    )
    with pytest.raises(CharterError) as info:  # penalty numbers moved: routing stops
        mon.check()
    assert info.value.code == "PENALTY_DRIFT_DETECTED"
    assert mon.penalties is not None
    assert mon.penalties["grok"].value == 0.5
    after = charter_fingerprint(root)
    (root / "migrations" / "zz-test.grok").write_text(
        'FORMAT: "grok/1"\nKIND: "grok_migration"\n'
        f'FROM_FINGERPRINT: "{before}"\nTO_FINGERPRINT: "{after}"\n'
        'APPROVED_BY: "owner"\nSIGNED_OFF: true\nREASON: "test"\nDATE: "2026-10-11"\n'
    )
    assert mon.check() is not None  # Owner-signed migration: accepted, table reloaded
    assert mon.penalties is not None
    assert mon.penalties["grok"].value == 0.75
