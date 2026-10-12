"""Acquisition Agent package + acquisition gate (the owner's directive, 2026-10-11)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from governed_agent_engine.core.acquisition import (
    AcquisitionError,
    check_task,
    gate,
    is_acquisition,
    missing_run_artifacts,
)
from governed_agent_engine.core.charter import CharterError
from governed_agent_engine.core.doctrine import load_doctrine
from governed_agent_engine.core.grok_binding import bind

REPO = Path(__file__).resolve().parent.parent
WF = REPO / "config" / "agents" / "acquirer.grok"


def good(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "agent": "acquirer", "target": "https://example.org/listings",
        "output_schema": {"type": "object", "required": ["title"]},
        "evidence_plan": "store raw pages, row counts and source URLs",
        "failure_modes": {"rate_limits": "backoff", "bans": "rotate actor", "bad_data":
                          "schema validate", "coverage": "compare to sitemap"},
        "tool": "apify_actor_via_connector",
    }  # fmt: skip
    d.update(over)
    return d


def test_gate_accepts_compliant_task() -> None:
    gate("research", "owner", good(), False)
    gate("market_watch", "acquirer", good(agent=None, tool="in_repo_scraper"), False)
    gate("custom", "owner", {"x": 1}, False)  # not an acquisition task
    assert not is_acquisition("research", "grok", {})
    assert check_task("research", good(budget_usd=0), False) == []
    assert check_task("research", good(publish_apify_store=True), True) == []


@pytest.mark.parametrize(
    ("ttype", "inputs", "needle"),
    [
        ("custom", good(), "not acquisition-scoped"),
        ("research", good(target=" "), "missing target"),
        ("research", good(output_schema="rows"), "output_schema must be an object"),
        ("research", good(evidence_plan=[]), "missing evidence_plan"),
        ("research", good(failure_modes=None), "missing failure_modes"),
        ("research", good(failure_modes="none"), "must map each failure mode"),
        ("research", good(failure_modes={"rate_limits": "x"}), "failure_modes.bans"),
        ("research", good(tool="selenium_farm"), "not in approved tools"),
        ("research", good(budget_usd=5), "budget_usd must be 0"),
        ("research", good(budget_usd=True), "budget_usd must be 0"),
        ("research", good(budget_usd="0"), "budget_usd must be 0"),
        ("research", good(publish_apify_store=True), "requires human_approval"),
        ("research", good(output_schema=7), "missing output_schema"),
    ],
)
def test_gate_rejects(ttype: str, inputs: dict[str, Any], needle: str) -> None:
    with pytest.raises(AcquisitionError, match=needle) as info:
        gate(ttype, "owner", inputs, False)
    assert str(info.value).startswith("ACQUISITION_NON_COMPLIANT")
    assert info.value.problems


def test_run_artifacts() -> None:
    assert missing_run_artifacts(["out/data.jsonl", "logs.txt", "acquisition_report.md"]) == []
    assert missing_run_artifacts(["data.csv"]) == ["logs", "acquisition_report"]


def _root(tmp_path: Path) -> Path:
    for d in ("identities", "doctrine", "config"):
        shutil.copytree(REPO / d, tmp_path / d)
    return tmp_path


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("BUDGET_USD: 0", "BUDGET_USD: 5", "ACQUISITION_MISMATCH"),
        ('    - "in_repo_scraper"\n', '    - "anything"\n', "ACQUISITION_MISMATCH"),
        ('DIRECTIVE_SOURCE: "owner"', 'DIRECTIVE_SOURCE: "derived"', "DIRECTIVE_MISMATCH"),
        ('    - "apify_usage_pattern"\n', "", "CLARIFICATION_RULE_MISSING"),
        ("you must ask Owner", "you must decide", "CLARIFICATION_RULE_MISSING"),
        ('  TEXT: "', '  TEXT: 1\n  X: "', "CLARIFICATION_RULE_MISSING"),
        ("ACQUISITION:\n", "OTHER:\n", "IDENTITY_MISMATCH"),
    ],
)
def test_acquirer_package_mismatch(tmp_path: Path, old: str, new: str, code: str) -> None:
    root = _root(tmp_path)
    p = root / "config" / "agents" / "acquirer.grok"
    s = p.read_text()
    assert old in s
    p.write_text(s.replace(old, new, 1))
    with pytest.raises(CharterError) as info:
        bind(root, load_doctrine(root / "config" / "doctrine.yaml"))
    assert info.value.code == code


def test_check_run_completion(tmp_path: Path) -> None:
    from governed_agent_engine.core.acquisition import check_run_completion

    art = tmp_path / "artifacts"
    art.mkdir()
    for n in ("data.json", "logs.log", "acquisition_report.md"):
        (art / n).write_text("x")
    (tmp_path / "outside.txt").write_text("x")
    refs = {"d": "artifacts/data.json", "l": "artifacts/logs.log",
            "r": "artifacts/acquisition_report.md"}  # fmt: skip
    roots = ("artifacts",)
    assert check_run_completion("research", "x", good(), {"artifacts": refs}, tmp_path, roots) == []
    assert check_run_completion("custom", "x", {}, {}, tmp_path, roots) == []
    bad = check_run_completion(
        "research", "acquirer", good(agent=None),
        {"artifacts": ["outside.txt", 3, "artifacts/data.json", "artifacts/nope.txt"]},
        tmp_path, roots,
    )  # fmt: skip
    assert all(b.startswith("ACQUISITION_NON_COMPLIANT: ") for b in bad)
    text = " | ".join(bad)
    for needle in (
        "outside artifact roots",
        "invalid artifact reference",
        "missing or empty",
        "missing run artifact: logs",
        "missing run artifact: acquisition_report",
    ):
        assert needle in text  # fmt: skip
    none = check_run_completion("research", "x", good(), {"artifacts": "x"}, tmp_path, roots)
    assert len(none) == 3
