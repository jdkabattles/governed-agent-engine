"""Overseer GOVERNANCE + ANALYST COGNITION packages and scope gates."""
# ruff: noqa: E501

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from governed_agent_engine.core.charter import CharterError
from governed_agent_engine.core.doctrine import load_doctrine
from governed_agent_engine.core.grok_binding import bind
from governed_agent_engine.core.grok_format import parse_grok
from governed_agent_engine.core.scopes import (
    ScopeError,
    check_run_completion,
    gate,
)

REPO = Path(__file__).resolve().parent.parent
AGENTS = REPO / "config" / "agents"


def cog(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "agent": "analyst", "objective": "summarise acquired listings",
        "input_artifacts": ["artifacts/evidence/data.jsonl"],
        "output_schema": {"type": "object", "required": ["summary"]},
    }  # fmt: skip
    d.update(over)
    return d


def test_gate() -> None:
    gate("research", "owner", cog())
    gate("custom", "analyst", cog(agent=None))
    gate("governance_check", "owner", {"agent": "overseer"})
    gate("market_watch", "owner", {"x": 1})  # unscoped
    with pytest.raises(ScopeError, match="GOVERNANCE_NON_COMPLIANT") as info:
        gate("research", "owner", {"agent": "overseer"})
    assert info.value.code == "GOVERNANCE_NON_COMPLIANT"


@pytest.mark.parametrize(
    ("ttype", "inputs", "needle"),
    [
        ("market_watch", cog(), "not cognition-scoped"),
        ("research", cog(objective=""), "missing objective"),
        ("research", cog(input_artifacts=[]), "not free-form speculation"),
        ("research", cog(output_schema="text"), "must be an object"),
        ("research", cog(tool="in_repo_scraper"), "no tools or side effects"),
        ("research", cog(side_effects=True), "no tools or side effects"),
        ("research", cog(budget_usd=1), "budget_usd must be 0"),
        ("research", cog(budget_usd=False), "budget_usd must be 0"),
    ],
)
def test_cognition_gate_rejects(ttype: str, inputs: dict[str, Any], needle: str) -> None:
    with pytest.raises(ScopeError, match=needle) as info:
        gate(ttype, "owner", inputs)
    assert info.value.code == "COGNITION_NON_COMPLIANT"


def test_cognition_run_completion(tmp_path: Path) -> None:
    art = tmp_path / "artifacts"
    art.mkdir()
    for n in ("reasoning_log.md", "structured_summary.json", "refinement_notes.md"):
        (art / n).write_text("x")
    refs = [f"artifacts/{n}" for n in
            ("reasoning_log.md", "structured_summary.json", "refinement_notes.md")]  # fmt: skip
    roots = ("artifacts",)
    assert check_run_completion("x", cog(), {"artifacts": refs}, tmp_path, roots) == []
    assert check_run_completion("grok", {}, {}, tmp_path, roots) == []
    bad = check_run_completion("analyst", {}, {"artifacts": refs[:1]}, tmp_path, roots)
    assert bad == [
        "COGNITION_NON_COMPLIANT: missing run artifact: structured_summary",
        "COGNITION_NON_COMPLIANT: missing run artifact: refinement_notes",
    ]


def _root(tmp_path: Path) -> Path:
    for d in ("identities", "doctrine", "config"):
        shutil.copytree(REPO / d, tmp_path / d)
    return tmp_path


@pytest.mark.parametrize(
    ("bot", "old", "new", "code"),
    [
        ("overseer", "AUDIT_AT_STARTUP: true", "AUDIT_AT_STARTUP: false", "GOVERNANCE_MISMATCH"),
        ("overseer", "MAY_APPROVE_HUMAN_ACTIONS: false", "MAY_APPROVE_HUMAN_ACTIONS: true",
         "GOVERNANCE_MISMATCH"),
        ("overseer", '  CLARIFY_WITH:\n    - "Owner"\n',
         '  CLARIFY_WITH:\n    - "Owner"\n    - "Overseer Agent"\n',
         "CLARIFICATION_RULE_MISSING"),
        ("overseer", "GOVERNANCE:\n", "OTHER:\n", "IDENTITY_MISMATCH"),
        ("analyst", "SIDE_EFFECTS: false", "SIDE_EFFECTS: true", "COGNITION_MISMATCH"),
        ("analyst", '    - "refinement_notes"\n', "", "COGNITION_MISMATCH"),
        ("analyst", "ask Overseer Agent", "guess", "CLARIFICATION_RULE_MISSING"),
        ("analyst", 'DIRECTIVE_SOURCE: "owner"', 'DIRECTIVE_SOURCE: "derived"',
         "DIRECTIVE_MISMATCH"),
    ],
)  # fmt: skip
def test_package_mismatch(tmp_path: Path, bot: str, old: str, new: str, code: str) -> None:
    root = _root(tmp_path)
    p = root / "config" / "agents" / f"{bot}.grok"
    s = p.read_text()
    assert old in s
    p.write_text(s.replace(old, new, 1))
    with pytest.raises(CharterError) as info:
        bind(root, load_doctrine(root / "config" / "doctrine.yaml"))
    assert info.value.code == code


def test_overseer_identity_override_and_oversight(tmp_path: Path) -> None:
    from governed_agent_engine.core.grok_binding import (
        audit,
        check_override,
        effective_rules,
        laziness_factor,
    )
    from governed_agent_engine.core.scopes import (
        OVERSEER_DOCTRINE_OVERRIDE,
        OVERSIGHT_ENFORCEMENT,
        OVERSIGHT_REQUIREMENTS,
    )

    h = parse_grok((AGENTS / "overseer.grok").read_text())
    desc = h["IDENTITY"]["DESCRIPTION"]  # type: ignore[index,call-overload]
    assert str(desc).startswith("Audits agents for doctrine")
    assert h["DOCTRINE_OVERRIDE"] == {"RULES": OVERSEER_DOCTRINE_OVERRIDE}
    assert h["OVERSIGHT_REQUIREMENTS"] == OVERSIGHT_REQUIREMENTS
    assert len(OVERSIGHT_REQUIREMENTS) == 8
    for status, checks in OVERSIGHT_ENFORCEMENT.values():
        assert status in ("enforced", "structural")
        assert checks
    shared = parse_grok((REPO / "config" / "doctrine.grok").read_text())
    rules = effective_rules(shared, h)
    assert rules["TRUTH_PRIORITY"] == "highest"
    assert laziness_factor(rules) == 0.25
    assert laziness_factor(effective_rules(shared, {})) == 0.5
    assert laziness_factor({}) == 1.0
    assert check_override(shared, {"RULES": {"LAZINESS_PENALTY": "maximum"}}) == []
    for bad in ({"RULES": {"LAZINESS_PENALTY": "low"}}, {"RULES": {"EVIDENCE_REQUIRED": False}},
                {"RULES": {"IMPROVEMENT_LOOP": "optional"}}, {"RULES": {"NEW": 1}},
                {"RULES": {"TRUTH_PRIORITY": "medium"}}, {"X": 1}, None):  # fmt: skip
        found = check_override(shared, bad)  # type: ignore[arg-type]
        assert found
        assert found[0][0] == "DOCTRINE_MISMATCH"
    assert check_override({}, {"RULES": {}}) != []
    root = _root(tmp_path)
    from datetime import UTC, datetime

    rep = audit(root, load_doctrine(root / "config" / "doctrine.yaml"), datetime.now(UTC))
    assert rep.evaluator["laziness_factor"] == 0.25
    p = root / "config" / "agents" / "analyst.grok"
    p.write_text(p.read_text().replace("AUTHORITY_LEVEL: 3", "AUTHORITY_LEVEL: 4", 1))
    rep = audit(root, load_doctrine(root / "config" / "doctrine.yaml"), datetime.now(UTC))
    assert rep.agents["analyst"]["penalty_factor"] == 0.25
    assert rep.agents["grok"]["penalty_factor"] == 1.0


@pytest.mark.parametrize(
    ("bot", "old", "new", "code"),
    [
        ("overseer", 'LAZINESS_PENALTY: "maximum"', 'LAZINESS_PENALTY: "medium"', "DOCTRINE_MISMATCH"),
        ("overseer", 'TRUTH_PRIORITY: "highest"', 'TRUTH_PRIORITY: "high"', "GOVERNANCE_MISMATCH"),
        ("overseer", "  - \"Agents follow routing decisions.\"\n", "", "GOVERNANCE_MISMATCH"),
        ("overseer", "Audits agents for doctrine", "Audits agents on doctrine", "IDENTITY_MISMATCH"),
        ("analyst", "DIRECTIVE: ", 'DOCTRINE_OVERRIDE:\n  RULES:\n    IMPROVEMENT_LOOP: "optional"\nDIRECTIVE: ',
         "DOCTRINE_MISMATCH"),
        ("overseer", "OVERSIGHT_REQUIREMENTS:\n", "OVERSIGHT: \"x\"\nOTHER:\n", "IDENTITY_MISMATCH"),
    ],
)  # fmt: skip
def test_overseer_override_mismatch(
    tmp_path: Path, bot: str, old: str, new: str, code: str
) -> None:
    root = _root(tmp_path)
    p = root / "config" / "agents" / f"{bot}.grok"
    s = p.read_text()
    assert old in s
    p.write_text(s.replace(old, new, 1))
    with pytest.raises(CharterError) as info:
        bind(root, load_doctrine(root / "config" / "doctrine.yaml"))
    assert info.value.code == code


def test_analyst_stricter_override_is_allowed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    p = root / "config" / "agents" / "analyst.grok"
    s = p.read_text().replace(
        "DIRECTIVE: ", 'DOCTRINE_OVERRIDE:\n  RULES:\n    TRUTH_PRIORITY: "highest"\nDIRECTIVE: ', 1
    )
    p.write_text(s)
    bind(root, load_doctrine(root / "config" / "doctrine.yaml"))


def test_analyst_identity_and_cognition_requirements(tmp_path: Path) -> None:
    from governed_agent_engine.core.grok_binding import check_override
    from governed_agent_engine.core.scopes import COGNITION_ENFORCEMENT, COGNITION_REQUIREMENTS

    d = parse_grok((AGENTS / "analyst.grok").read_text())
    desc = d["IDENTITY"]["DESCRIPTION"]  # type: ignore[index,call-overload]
    assert str(desc).startswith("Produces structured reasoning")
    assert d["COGNITION_REQUIREMENTS"] == COGNITION_REQUIREMENTS
    assert len(COGNITION_REQUIREMENTS) == 7
    assert "DOCTRINE_OVERRIDE" not in d
    statuses = {s for s, _ in COGNITION_ENFORCEMENT.values()}
    assert statuses == {"enforced", "partial", "structural"}
    shared = parse_grok((REPO / "config" / "doctrine.grok").read_text())
    rules = shared["RULES"]
    assert check_override(shared, {"RULES": rules}) == []  # equal to baseline is accepted
    root = _root(tmp_path)
    p = root / "config" / "agents" / "analyst.grok"
    for old, new in [('  - "The analyst uses no tools and spends nothing."\n', ""),
                     ("advises only and never executes", "advises and executes")]:  # fmt: skip
        p.write_text((AGENTS / "analyst.grok").read_text().replace(old, new, 1))
        with pytest.raises(CharterError) as info:
            bind(root, load_doctrine(root / "config" / "doctrine.yaml"))
        assert info.value.code in ("COGNITION_MISMATCH", "IDENTITY_MISMATCH")
    p.write_text(
        (AGENTS / "analyst.grok")
        .read_text()
        .replace(
            "DIRECTIVE: ",
            'DOCTRINE_OVERRIDE:\n  RULES:\n    TRUTH_PRIORITY: "high"\nDIRECTIVE: ',
            1,
        )
    )
    bind(root, load_doctrine(root / "config" / "doctrine.yaml"))  # equal override accepted
