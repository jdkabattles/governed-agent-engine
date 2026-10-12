"""Multi-agent charter: identities fixed in code; identities/ + doctrine/ must match."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from governed_agent_engine import cli
from governed_agent_engine.config.loader import AppConfig
from governed_agent_engine.core import identity
from governed_agent_engine.core.charter import (
    APPROVAL_ACTIONS,
    CharterError,
    charter_fingerprint,
    load_charter,
)
from governed_agent_engine.core.errors import GovernanceError
from governed_agent_engine.core.models import GovernanceConfig
from governed_agent_engine.orchestrator.governance import (
    authorize_approval,
    authorize_block,
    validate_task,
)

from .conftest import FakeClock, FakeHttp, make_task, oai
from .test_doctrine_identity import _orch as _orch_h

SHARED_SHA256 = "3dae57e54c250bafc8163b88b155117cee44b5dd5bceccbbb33b8e0061e93223"


def allow_overseer(gov: GovernanceConfig) -> GovernanceConfig:
    return replace(gov, process=replace(gov.process, overseer_may_issue_top_level=True))


def _orch(config: AppConfig, clock: FakeClock, texts: dict[str, str]) -> object:
    h = FakeHttp()
    h.add("POST", "https://", oai())
    return _orch_h(config, clock, h)


REPO = Path(__file__).resolve().parent.parent
BOT_FILES = ("acquirer", "analyst", "grok", "overseer", "owner")


def _root(tmp_path: Path) -> Path:
    for d in ("identities", "doctrine"):
        shutil.copytree(REPO / d, tmp_path / d)
    (tmp_path / "config").mkdir(exist_ok=True)
    for f in ("identity.grok", "doctrine.grok", "directive.grok"):
        shutil.copy(REPO / "config" / f, tmp_path / "config" / f)
    return tmp_path


def _edit(path: Path, **changes: object) -> None:
    raw = yaml.safe_load(path.read_text())
    raw.update(changes)
    path.write_text(yaml.safe_dump(raw, sort_keys=False))


# ------------------------------------------------------------------ identity model
def test_four_bot_identities() -> None:
    i = identity
    assert [(b.name, b.type.value, b.authority_level) for b in i.BOTS] == [
        ("owner", "human", 100), ("overseer", "overseer", 50), ("grok", "executor", 10),
        ("acquirer", "acquisition", 5), ("analyst", "cognition", 3),
    ]  # fmt: skip
    assert i.resolve("00000000-0000-4000-8000-000000000050") is i.OVERSEER_OVERSEER
    assert i.resolve("00000000-0000-4000-8000-000000000005") is i.ACQUISITION_ACQUIRER
    assert i.resolve("00000000-0000-4000-8000-000000000003") is i.COGNITION_ANALYST
    assert i.resolve("cognition:ovh").authority_level == 1
    assert i.OVERSEER_OVERSEER.is_overseer and not i.OVERSEER_OVERSEER.is_human  # noqa: PT018
    for bad in (49, 51, 100):
        with pytest.raises(i.IdentityError, match="exactly 50"):
            i.AgentIdentity("x", i.AgentType.OVERSEER, bad)
    for t in (i.AgentType.EXECUTOR, i.AgentType.ACQUISITION, i.AgentType.COGNITION):
        with pytest.raises(i.IdentityError, match="human authority"):
            i.AgentIdentity("x", t, 50)
    assert i.can_approve(i.HUMAN_OWNER)
    assert not any(i.can_approve(b) for b in i.BOTS[1:])
    assert i.can_block(i.OVERSEER_OVERSEER) and i.can_block(i.HUMAN_OWNER)  # noqa: PT018
    assert not i.can_block(i.EXECUTOR_GROK)


def test_overseer_top_level_needs_governance(config: AppConfig) -> None:
    with pytest.raises(GovernanceError, match="human issuer"):
        validate_task(make_task(issued_by="overseer"), config.governance)
    validate_task(make_task(issued_by="overseer"), allow_overseer(config.governance))
    for who in ("grok", "acquirer", "analyst"):
        with pytest.raises(GovernanceError, match="human issuer"):
            validate_task(make_task(issued_by=who), allow_overseer(config.governance))
    MODES = ("rate_limits", "bans", "bad_data", "coverage")  # noqa: N806
    acq = {"target": "https://example.org", "output_schema": {"type": "object"},
           "evidence_plan": "raw pages + counts", "tool": "in_repo_scraper",
           "failure_modes": dict.fromkeys(MODES, "handled")}  # fmt: skip
    with pytest.raises(GovernanceError, match="ACQUISITION_NON_COMPLIANT"):
        validate_task(make_task(task_id="c", depth=1, parent_task_id="r", provenance=["r"],
                                issued_by="acquirer"), config.governance)  # fmt: skip
    validate_task(make_task(task_id="c", type="research", depth=1, parent_task_id="r",
                            provenance=["r"], issued_by="acquirer", inputs=acq),
                  config.governance)  # fmt: skip


def test_only_owner_approves(config: AppConfig) -> None:
    assert authorize_approval("owner", config.governance).name == "owner"
    for who in ("overseer", "grok", "acquirer", "analyst", "cognition:groq_gpt_oss_120b", "", "x"):
        with pytest.raises(GovernanceError, match="may not approve"):
            authorize_approval(who, allow_overseer(config.governance))
    assert authorize_block("overseer").name == "overseer"
    with pytest.raises(GovernanceError, match="may not block"):
        authorize_block("grok")


# ------------------------------------------------------------------ charter files
def test_charter_loads_and_matches_code(config: AppConfig) -> None:
    c = load_charter(REPO, config.doctrine.min_steps, config.doctrine.min_step_chars)
    assert sorted(c.bots) == list(BOT_FILES)
    assert c.bot("overseer").former_name is None
    assert c.bot("acquirer").identity.agent_id == "00000000-0000-4000-8000-000000000005"
    assert c.bot("analyst").escalation_path == ("overseer", "owner")
    primary = ("governed-agent-engine", "primary: central orchestrator; executes its backlog")
    assert primary in c.bot("grok").integrations
    assert len(c.bot("overseer").integrations) == 2
    assert "approve:publish" in c.bot("overseer").forbidden_actions
    assert c.rules["wisdom"]["principle"] == "knowledge -> understanding -> improved action"
    assert c.fingerprint == charter_fingerprint(REPO) == config.charter_fingerprint


@pytest.mark.parametrize(
    ("bot", "changes", "needle"),
    [
        ("overseer", {"authority": 60}, "does not match code"),
        ("grok", {"type": "human"}, "does not match code"),
        ("acquirer", {"id": "00000000-0000-0000-0000-000000000000"}, "does not match code"),
        ("analyst", {"surprise": 1}, "keys differ"),
        ("grok", {"allowed_actions": ["approve:publish"]}, "only a human may approve"),
        ("analyst", {"forbidden_actions": ["spend"]}, "must forbid"),
        ("overseer", {"forbidden_actions": [f"approve:{a}" for a in APPROVAL_ACTIONS]},
         "override_owner"),
        ("grok", {"allowed_actions": ["spend"]}, "both allowed and forbidden"),
        ("grok", {"escalation_path": ["analyst", "owner"]}, "strictly upward"),
        ("grok", {"escalation_path": ["overseer"]}, "end with owner"),
        ("grok", {"escalation_path": ["nobody"]}, "unknown escalation"),
        ("grok", {"escalation_path": "overseer"}, "list of non-empty strings"),
        ("grok", {"integrations": []}, "integrations"),
        ("grok", {"integrations": [{"repo": "example-executor-backend", "status": "planned",
                                    "use": "x"}]}, "status must be existing"),
        ("grok", {"integrations": [{"repo": "example-planned-repo", "status": "maybe",
                                    "use": "x"}]}, "status must be planned"),
        ("grok", {"role": " "}, "role"),
        ("grok", {"former_name": 7}, "former_name"),
    ],
)  # fmt: skip
def test_identity_tamper(tmp_path: Path, bot: str, changes: dict[str, object],
                         needle: str) -> None:  # fmt: skip
    root = _root(tmp_path)
    _edit(root / "identities" / f"{bot}.yaml", **changes)
    with pytest.raises(CharterError, match=needle):
        load_charter(root)


def test_charter_files_and_doctrine_tamper(tmp_path: Path) -> None:
    root = _root(tmp_path)
    load_charter(root, 3, 12)
    with pytest.raises(CharterError, match="min_plan_steps"):
        load_charter(root, 4, 12)
    with pytest.raises(CharterError, match="min_step_chars"):
        load_charter(root, 3, 13)
    (root / "identities" / "mallory.yaml").write_text("name: mallory\n")
    with pytest.raises(CharterError, match="expected exactly"):
        load_charter(root)
    (root / "identities" / "mallory.yaml").unlink()
    (root / "identities" / "grok.yaml").write_text("- not a mapping\n")
    with pytest.raises(CharterError, match="mapping"):
        load_charter(root)
    (root / "identities" / "grok.yaml").write_text("a: [unclosed\n")
    with pytest.raises(CharterError, match="invalid YAML"):
        load_charter(root)
    shutil.copy(REPO / "identities" / "grok.yaml", root / "identities" / "grok.yaml")
    doc = root / "doctrine" / "doctrine.yaml"
    good = doc.read_text()
    raw = yaml.safe_load(good)
    mutations: list[tuple[Callable[[Any], object], str]] = [
        (lambda r: r.update(statement="Work hard."), "verbatim"),
        (lambda r: r["rules"].pop("wisdom"), "rules must have exactly"),
        (lambda r: r["rules"]["improvement"].pop("feed_lessons_into_next_plan"),
         "rules.improvement"),
        (lambda r: r["rules"]["truth_and_evidence"].update(cite_evidence=False), "must be true"),
        (lambda r: r["rules"]["anti_laziness"].update(no_filler_or_duplicate_steps=False),
         "anti_laziness flags"),
        (lambda r: r["rules"]["wisdom"].update(principle="vibes"), "wisdom"),
        (lambda r: r["rules"]["wisdom"]["stages"].reverse(), "wisdom"),
    ]  # fmt: skip
    for mutate, needle in mutations:
        r = yaml.safe_load(good)
        mutate(r)
        doc.write_text(yaml.safe_dump(r))
        with pytest.raises(CharterError, match=needle):
            load_charter(root)
    doc.write_text(good)
    assert load_charter(root).rules == raw["rules"]
    doc.unlink()
    with pytest.raises(CharterError, match="missing"):
        load_charter(root)


def test_charter_tamper_halts(root: Path, config: AppConfig, clock: FakeClock) -> None:
    o = _orch(config, clock, {})
    p = root / "identities" / "grok.yaml"
    p.write_text(p.read_text() + "\n# promoted myself\n")
    with pytest.raises(GovernanceError, match="changed while running"):
        o.submit(make_task())  # type: ignore[attr-defined]


def test_load_config_rejects_bad_charter(root: Path) -> None:
    _edit(root / "identities" / "overseer.yaml", authority=50, type="human")
    assert cli.main(["--root", str(root), "validate"]) != 0
    with pytest.raises(CharterError):
        load_charter(root)


def test_cli_approve_block(root: Path, config: AppConfig, clock: FakeClock,
                           monkeypatch: pytest.MonkeyPatch,
                           capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    from governed_agent_engine.orchestrator import main as orch_main

    o = _orch(config, clock, {})
    monkeypatch.setattr(orch_main, "build", lambda r, **kw: o)
    e = o.submit(make_task(task_id="m1", action="send_message", human_approval=True)).entry  # type: ignore[attr-defined]
    assert cli.main(["--root", str(root), "approve", e.entry_id, "--as", "overseer"]) == 2
    assert cli.main(["--root", str(root), "approve", e.entry_id]) == 0
    clock.advance(seconds=1)
    e2 = o.submit(make_task(task_id="m2", action="send_message", human_approval=True)).entry  # type: ignore[attr-defined]
    assert cli.main(["--root", str(root), "block", e2.entry_id, "--reason", "x",
                     "--as", "overseer"]) == 0  # fmt: skip
    assert '"rejected"' in capsys.readouterr().out
