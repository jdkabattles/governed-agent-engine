"""Agent selection."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from governed_agent_engine.core import agent_selection as sel
from governed_agent_engine.core import block_report
from governed_agent_engine.core import routing_penalty as rp
from governed_agent_engine.core.charter import CharterError
from governed_agent_engine.core.doctrine import load_doctrine
from governed_agent_engine.core.grok_binding import (
    audit,
    bind,
    ledger_findings,
    penalty_table,
    selection_context,
)
from governed_agent_engine.core.grok_format import parse_grok

REPO = Path(__file__).resolve().parent.parent
TS = "2026-10-11T19:19:00-05:00"


def _root(tmp_path: Path) -> Path:
    for d in ("identities", "doctrine", "config", "migrations"):
        shutil.copytree(REPO / d, tmp_path / d)
    return tmp_path


def _runs(root: Path, agent: str, pattern: str) -> list[dict[str, object]]:
    """pattern: 'L' lazy, '.' clean, one run each, appended to the run ledger."""
    led = root.joinpath("artifacts", *rp.LEDGER)
    p = penalty_table(root)[agent]
    n = len(rp.read_rows(led))
    for i, c in enumerate(pattern):
        rp.record(led, p, c == "L", TS, f"{agent}-{n + i}")
    return rp.read_rows(led)


def _state(root: Path, agent: str = "acquirer") -> sel.AgentState:
    ctx = selection_context(root)
    scope = {v: k for k, v in sel.SCOPES.items()}[agent]
    return sel.select(scope, ctx.table, rp.read_rows(root.joinpath("artifacts", *rp.LEDGER)),
                      ctx.window, ctx.unblocks)  # fmt: skip


def _unblock(root: Path, agent: str, reset: str, name: str = "u1") -> None:
    d = root.joinpath(*sel.UNBLOCK_DIR)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.grok").write_text(
        f'FORMAT: "grok/1"\nKIND: "agent_unblock"\nAGENT: "{agent}"\nRESET_AFTER_HASH: "{reset}"\n'
        'APPROVED_BY: "owner"\nSIGNED_OFF: true\nREASON: "test"\nDATE: "2026-10-11"\n'
    )


def test_one_lazy_is_half_and_eligible(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _runs(root, "acquirer", "..L..")
    st = _state(root)
    assert (st.score, st.lazy_count, st.eligibility) == (0.5, 1, "eligible")
    assert st.window_size == sel.DEFAULT_WINDOW == 10


def test_two_lazy_in_ten_blocks(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _runs(root, "analyst", "L.......L")
    st = _state(root, "analyst")
    assert (st.score, st.eligibility) == (0.25, "blocked")
    assert st.blocked_by == ["analyst-0", "analyst-8"]
    assert _state(root, "grok").eligibility == "eligible"  # per-agent windows


def test_aging_out_restores_only_if_never_blocked(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _runs(root, "grok", "L" + "." * 10)  # one lazy, then ages out
    st = _state(root, "grok")
    assert (st.score, st.eligibility, len(st.run_ids)) == (1.0, "eligible", 10)
    _runs(root, "acquirer", "LL" + "." * 10)  # blocked, then lazy runs age out
    st = _state(root)
    assert (st.score, st.eligibility) == (1.0, "blocked")  # stays blocked until migration


def test_unblock_record_resets_window(tmp_path: Path) -> None:
    root = _root(tmp_path)
    runs = _runs(root, "acquirer", ".LL")
    _unblock(root, "acquirer", str(runs[-1]["hash"]))
    st = _state(root)
    assert (st.score, st.eligibility, st.run_ids) == (1.0, "eligible", [])
    _runs(root, "acquirer", "L.L")  # lazy again after the reset -> blocked again
    assert _state(root).eligibility == "blocked"
    _unblock(root, "acquirer", rp.GENESIS, "u0")  # genesis never moves the start back
    assert _state(root).eligibility == "blocked"
    _unblock(root, "acquirer", "f" * 64, "u2")
    with pytest.raises(rp.PenaltyError) as pe:
        _state(root)
    assert pe.value.code == "UNAUTHORIZED_PENALTY_CHANGE"
    _unblock(root, "acquirer", "f" * 64, "u2")
    (root.joinpath(*sel.UNBLOCK_DIR) / "u2.grok").write_text('KIND: "agent_unblock"\n')
    with pytest.raises(CharterError) as info:
        selection_context(root)
    assert info.value.code == "UNAUTHORIZED_PENALTY_CHANGE"
    (root.joinpath(*sel.UNBLOCK_DIR) / "u2.grok").write_text("bad: [\n")
    with pytest.raises(CharterError):
        selection_context(root)


def test_overseer_never_selected(tmp_path: Path) -> None:
    root = _root(tmp_path)
    assert "overseer" not in sel.SCOPES.values()
    table = penalty_table(root)
    with pytest.raises(rp.PenaltyError):
        sel.agent_state("overseer", [], table["grok"], 10, [])
    with pytest.raises(rp.PenaltyError):
        sel.select("governance", table, [], 10, [])
    assert sel.scope_for(True, False) == "acquisition"
    assert sel.scope_for(False, True) == "cognition"
    assert sel.scope_for(False, False) == "execution"


def test_window_configurable_only_via_grok(tmp_path: Path) -> None:
    root = _root(tmp_path)
    doc = root / "config" / "doctrine.grok"
    doc.write_text(doc.read_text() + "AGENT_SELECTION:\n  WINDOW: 3\n")
    _runs(root, "grok", "L...")
    assert (_state(root, "grok").score, _state(root, "grok").window_size) == (1.0, 3)
    for bad in ("0", "true", '"10"'):
        doc.write_text(
            doc.read_text().rsplit("AGENT_SELECTION", 1)[0] + f"AGENT_SELECTION:\n  WINDOW: {bad}\n"
        )
        with pytest.raises(CharterError) as info:
            selection_context(root)
        assert info.value.code == "PENALTY_MISMATCH"
    with pytest.raises(CharterError):
        bind(root, load_doctrine(root / "config" / "doctrine.yaml"))
    with pytest.raises(rp.PenaltyError):
        sel.window_size({"AGENT_SELECTION": "x"})


def test_ledger_audit_catches_routing_while_blocked_and_tampering(tmp_path: Path) -> None:
    root = _root(tmp_path)
    art = root / "artifacts"
    ctx = selection_context(root)
    runs = _runs(root, "acquirer", "L")
    st = _state(root)
    led = art.joinpath(*sel.SELECTION_LEDGER)
    sel.record(led, "t1", "acquisition", st, ctx.table["acquirer"], runs, TS)
    runs = _runs(root, "acquirer", "L")
    blocked = _state(root)
    assert blocked.block_trigger == runs[-1]["hash"]
    rec = sel.record(led, "t2", "acquisition", blocked, ctx.table["acquirer"], runs, TS)
    # block without a block report -> audit finding
    assert any("without block report" in m for _, m in ledger_findings(root, art, ctx))
    led.write_text("".join(json.dumps(r) + "\n" for r in rp.read_rows(led)[:1]))
    doctrine_grok = parse_grok((root / "config" / "doctrine.grok").read_text())
    now = datetime(2026, 10, 12, 0, 19, tzinfo=UTC)
    ref = block_report.build(root, art, blocked, ctx.table["acquirer"], ["t2"], now, doctrine_grok)
    sel.append_block(art, "acquirer", str(blocked.block_trigger), ref, now.isoformat())
    assert sel.find_report(art, str(blocked.block_trigger)) == ref
    rep = json.loads((art / ref["json"]).read_text())
    assert rep["who"] == {
        "agent_blocked": "acquirer",
        "issuer": "overseer/routing-kernel",
        "approver_needed": "owner",
    }
    assert rep["what"]["lazy_count"] == 2
    assert rep["what"]["threshold"] == 0.5
    assert rep["when"]["blocked_at"]["ct"].startswith("2026-10-11T19:19")
    assert rep["why"]["doctrine_statement"].startswith("Example doctrine")
    assert (
        "All actions must produce artifacts: logs, evidence, structured outputs."
        in rep["why"]["rules_violated"]
    )
    assert "## Why" in (art / ref["md"]).read_text()
    assert "BLOCKED acquirer" in block_report.summary(rep)
    rec = sel.record(led, "t2", "acquisition", blocked, ctx.table["acquirer"], runs, TS, (), ref)
    assert (rec["eligibility"], rec["routing_score"], rec["window"]["lazy_count"]) == (
        "blocked",
        0.25,
        2,
    )
    assert ledger_findings(root, art, ctx) == []
    native = load_doctrine(root / "config" / "doctrine.yaml")
    rep = audit(root, native, __import__("datetime").datetime(2026, 10, 12))
    assert rep.compliant
    md_bytes = (art / ref["md"]).read_bytes()
    (art / ref["md"]).write_text("edited")
    assert any("altered" in m for _, m in ledger_findings(root, art, ctx))
    (art / ref["md"]).unlink()
    assert any("missing" in m for _, m in ledger_findings(root, art, ctx))
    (art / ref["md"]).write_bytes(md_bytes)
    assert ledger_findings(root, art, ctx) == []
    states = rep.routing["agent_selection"]["states"]  # type: ignore[index]
    assert states["acquisition"]["blocked"] is True  # Overseer logs the block
    # forge: routed while blocked (resealed so the chain itself is intact)
    rows = rp.read_rows(led)
    forged = rows[1] | {"eligibility": "eligible", "prev_hash": rows[0]["hash"]}
    forged["hash"] = rp.digest(forged)
    led.write_text(json.dumps(rows[0]) + "\n" + json.dumps(forged) + "\n")
    msgs = [m for _, m in sel.verify(led, runs, ctx.table, ctx.window, ctx.unblocks)]
    assert any("routed while blocked" in m for m in msgs)
    assert not audit(root, native, __import__("datetime").datetime(2026, 10, 12)).compliant
    # tampered (not resealed), wrong score, overseer, bad head, incomplete, unreadable
    led.write_text(json.dumps(rows[0] | {"routing_score": 1.0}) + "\n")
    codes = {c for c, _ in sel.verify(led, runs, ctx.table, ctx.window, ctx.unblocks)}
    assert codes == {"UNAUTHORIZED_PENALTY_CHANGE", "PENALTY_MISMATCH"}
    for change in (
        {"agent_selected": "overseer"},
        {"run_log_head": "e" * 64},
        {"scope": "governance"},
    ):
        r = rows[0] | change
        r["hash"] = rp.digest(r)
        led.write_text(json.dumps(r) + "\n")
        assert sel.verify(led, runs, ctx.table, ctx.window, ctx.unblocks)
    led.write_text(json.dumps({"task_id": "x"}) + "\n")
    assert [c for c, _ in sel.verify(led, runs, ctx.table, ctx.window, ctx.unblocks)] == [
        "PENALTY_MISMATCH"
    ]
    led.write_text("{nope\n")
    assert [c for c, _ in sel.verify(led, runs, ctx.table, ctx.window, ctx.unblocks)] == [
        "UNAUTHORIZED_PENALTY_CHANGE"
    ]
    (art.joinpath(*rp.LEDGER)).write_text("{nope\n")
    led.unlink()
    assert ledger_findings(root, art, ctx)
