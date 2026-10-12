from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from governed_agent_engine.config.loader import AppConfig
from governed_agent_engine.core.errors import GovernanceError, GovernanceTamperError
from governed_agent_engine.core.models import HUMAN_APPROVAL_ACTIONS, GovernanceConfig
from governed_agent_engine.orchestrator.governance import (
    assert_allowed,
    assert_unchanged,
    assert_write_path,
    endpoint_permitted,
    requires_human,
    validate_task,
)

from .conftest import make_task

GATED = sorted(HUMAN_APPROVAL_ACTIONS - {"modify_governance"})


def test_plan_allowed(config: AppConfig) -> None:
    validate_task(make_task(), config.governance)


def test_deny_by_default(config: AppConfig) -> None:
    with pytest.raises(GovernanceError, match="deny-by-default"):
        validate_task(make_task(action="launch"), config.governance)
    with pytest.raises(GovernanceError, match="deny-by-default"):
        assert_allowed("launch", {}, config.governance)


def test_task_type_not_allowed() -> None:
    g = GovernanceConfig.from_dict({"process": {"allowed_task_types": ["research"]}})
    with pytest.raises(GovernanceError, match="task type"):
        validate_task(make_task(), g)


@pytest.mark.parametrize("action", GATED)
def test_human_actions(config: AppConfig, action: str) -> None:
    with pytest.raises(GovernanceError, match="human_approval"):
        validate_task(make_task(action=action), config.governance)
    validate_task(make_task(action=action, human_approval=True), config.governance)
    with pytest.raises(GovernanceError, match="never auto-executed"):
        assert_allowed(action, {"auto": True}, config.governance)
    with pytest.raises(GovernanceError, match="never auto-executed"):
        assert_allowed(action, {}, config.governance)
    assert_allowed(action, {"auto": False}, config.governance)


def test_governance_mutation_always_refused(config: AppConfig) -> None:
    with pytest.raises(GovernanceError, match="mutation is disallowed"):
        validate_task(make_task(action="modify_governance", human_approval=True), config.governance)


def test_hard_floor_survives_config() -> None:
    g = GovernanceConfig.from_dict(
        {
            "process": {"human_approval_required": []},
            "constraints": {"allowed_actions": ["plan", "trade"]},
        }
    )
    assert requires_human("trade", g)
    with pytest.raises(GovernanceError):
        assert_allowed("trade", {"auto": True}, g)


def test_recursion_depth(config: AppConfig) -> None:
    mx = config.governance.constraints.max_recursion_depth
    chain = [f"a{i}" for i in range(mx - 1)] + ["p"]
    validate_task(make_task(depth=mx, parent_task_id="p", issued_by="grok", provenance=chain),
                  config.governance)  # fmt: skip
    with pytest.raises(GovernanceError, match="max_recursion_depth"):
        validate_task(make_task(depth=mx + 1, parent_task_id="p", provenance=[*chain, "x"]),
                      config.governance)  # fmt: skip
    with pytest.raises(GovernanceError, match="parent_task_id"):
        validate_task(make_task(depth=1), config.governance)


def test_inputs_limit() -> None:
    g = GovernanceConfig.from_dict({"constraints": {"max_inputs_bytes": 10}})
    with pytest.raises(GovernanceError, match="too large"):
        validate_task(make_task(inputs={"x": "y" * 40}), g)


def test_write_roots(config: AppConfig, root: Path) -> None:
    assert_write_path(root / "artifacts" / "backlog", root, config.governance)
    with pytest.raises(GovernanceError, match="outside"):
        assert_write_path(root / "config", root, config.governance)


def test_knowledge_policy(config: AppConfig) -> None:
    eps = config.endpoints
    g = config.governance
    assert endpoint_permitted(make_task(), eps["llm7"], g)
    assert not endpoint_permitted(make_task(sensitive=True), eps["llm7"], g)
    assert endpoint_permitted(make_task(sensitive=True), eps["gemini"], g)
    assert not endpoint_permitted(make_task(), eps["blackbox"], g)
    lax = replace(g, knowledge=replace(g.knowledge, allow_sensitive_to_third_party=True))
    assert endpoint_permitted(make_task(sensitive=True), eps["llm7"], lax)


def test_tamper_detection(config: AppConfig, root: Path) -> None:
    f = root / "config" / "governance.yaml"
    assert_unchanged(config.governance, f)
    f.write_text(f.read_text() + "\n# edited\n")
    with pytest.raises(GovernanceTamperError):
        assert_unchanged(config.governance, f)
