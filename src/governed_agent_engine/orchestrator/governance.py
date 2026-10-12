"""APCK governance gate. Deny-by-default; every task and every side effect passes here.

* A (Artifacts): writes only under configured roots; DONE requires evidence.
* P (Process): allowed task types; human-approval actions only as backlog entries.
* C (Constraints): action allowlist, max recursion depth, input size, $0, no mutation.
* K (Knowledge): which endpoints may be consulted, and never sensitive -> third party.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from governed_agent_engine.config.loader import file_fingerprint
from governed_agent_engine.core import acquisition, identity, scopes
from governed_agent_engine.core.errors import GovernanceError, GovernanceTamperError
from governed_agent_engine.core.models import (
    HUMAN_APPROVAL_ACTIONS,
    EndpointConfig,
    GovernanceConfig,
    Task,
)


def requires_human(action: str, gov: GovernanceConfig) -> bool:
    return action in HUMAN_APPROVAL_ACTIONS or action in gov.process.human_approval_required


ParentLookup = Callable[[str], Task | None]


def validate_issuer(task: Task, gov: GovernanceConfig, lookup: ParentLookup | None = None) -> None:
    """Authority + provenance.

    * depth 0 (top-level): issuer must be a human with authority >= min_top_level_authority.
    * depth > 0 (child): any known identity may issue it, but ``provenance`` must be the full
      ancestor chain (root first, ending in parent_task_id, len == depth). With ``lookup`` the
      chain is verified against the backlog: parent exists, parent's chain + parent == ours,
      and the root task was issued by a human with sufficient authority.
    """
    who = identity.resolve(task.issued_by)
    need = gov.process.min_top_level_authority
    if task.depth == 0:
        if task.provenance:
            raise GovernanceError("C: top-level task must have empty provenance")
        if not may_issue_top_level(who, need, gov.process.overseer_may_issue_top_level):
            raise GovernanceError(
                f"P: top-level tasks need a human issuer with authority >= {need}; "
                f"got {who.name!r} ({who.type.value}, {who.authority_level})"
            )
        return
    if who is identity.UNKNOWN:
        raise GovernanceError(f"P: unknown issuer {task.issued_by!r} for child task")
    if len(task.provenance) != task.depth or task.provenance[-1] != task.parent_task_id:
        raise GovernanceError("C: provenance must list every ancestor (root first) up to parent")
    if task.task_id in task.provenance:
        raise GovernanceError("C: provenance cycle")
    if lookup is None:
        return
    parent = lookup(task.parent_task_id or "")
    if parent is None:
        raise GovernanceError(f"C: parent task {task.parent_task_id!r} not found in backlog")
    if (*parent.provenance, parent.task_id) != task.provenance:
        raise GovernanceError("C: provenance does not match the parent's recorded chain")
    root = parent if parent.depth == 0 else lookup(task.provenance[0])
    root_who = identity.resolve(root.issued_by if root else None)
    if not may_issue_top_level(root_who, need, gov.process.overseer_may_issue_top_level):
        raise GovernanceError("P: provenance root was not issued by sufficient human authority")


def validate_task(task: Task, gov: GovernanceConfig, lookup: ParentLookup | None = None) -> None:
    if task.type not in gov.process.allowed_task_types:
        raise GovernanceError(f"P: task type not allowed: {task.type}")
    if task.action not in gov.constraints.allowed_actions:
        raise GovernanceError(f"C: action not in allowlist (deny-by-default): {task.action}")
    if task.depth > gov.constraints.max_recursion_depth:
        raise GovernanceError(
            f"C: recursion depth {task.depth} exceeds max_recursion_depth "
            f"{gov.constraints.max_recursion_depth}"
        )
    if task.depth > 0 and task.parent_task_id is None:
        raise GovernanceError("C: sub-task (depth>0) must name parent_task_id")
    size = len(json.dumps(task.inputs, sort_keys=True, default=str).encode())
    if size > gov.constraints.max_inputs_bytes:
        raise GovernanceError(f"C: inputs too large ({size} bytes)")
    if task.action == "modify_governance":
        raise GovernanceError("C: governance mutation is disallowed (governance_mutable=false)")
    if requires_human(task.action, gov) and not task.human_approval:
        raise GovernanceError(f"P: action '{task.action}' requires human_approval=true")
    validate_issuer(task, gov, lookup)
    try:  # Acquisition Agent acquisition gate (config/agents/acquirer.grok ACQUISITION)
        acquisition.gate(task.type, task.issued_by, task.inputs, task.human_approval)
    except acquisition.AcquisitionError as exc:
        raise GovernanceError(str(exc)) from exc
    try:  # Overseer GOVERNANCE scope + ANALYST COGNITION scope (config/agents/*.grok)
        scopes.gate(task.type, task.issued_by, task.inputs)
    except scopes.ScopeError as exc:
        raise GovernanceError(str(exc)) from exc


def assert_allowed(action: str, context: dict[str, Any], gov: GovernanceConfig) -> None:
    """Gate for side effects the agent itself performs. Human actions are never automatic."""
    if action not in gov.constraints.allowed_actions:
        raise GovernanceError(f"C: action not in allowlist (deny-by-default): {action}")
    if requires_human(action, gov) and context.get("auto", True):
        raise GovernanceError(f"P: action '{action}' is never auto-executed")


def assert_write_path(path: Path, root: Path, gov: GovernanceConfig) -> None:
    resolved = path.resolve()
    for r in gov.artifacts.write_roots:
        if resolved.is_relative_to((root / r).resolve()):
            return
    raise GovernanceError(f"A: write outside allowed roots: {path}")


def endpoint_permitted(task: Task, ep: EndpointConfig, gov: GovernanceConfig) -> bool:
    if ep.name not in gov.knowledge.allowed_endpoints:
        return False
    return not (
        task.sensitive
        and ep.trust != "first_party"
        and not gov.knowledge.allow_sensitive_to_third_party
    )


def assert_unchanged(gov: GovernanceConfig, governance_file: Path) -> None:
    if file_fingerprint(governance_file) != gov.fingerprint:
        raise GovernanceTamperError(
            "governance.yaml changed while running; mutation is disallowed. Restart required."
        )


def may_issue_top_level(who: identity.AgentIdentity, need: int, overseer_ok: bool) -> bool:
    """Humans with enough authority; the overseer only when governance explicitly allows it."""
    if who.is_human:
        return who.authority_level >= need
    return who.is_overseer and overseer_ok


def authorize_approval(approver: str | None, gov: GovernanceConfig) -> identity.AgentIdentity:
    """Human-approval actions are approved by a human only. The overseer can review, flag and
    block, but never approve; no configuration changes that."""
    who = identity.resolve(approver)
    if not identity.can_approve(who, gov.process.min_top_level_authority):
        raise GovernanceError(
            f"approval requires a human with authority >= {gov.process.min_top_level_authority}; "
            f"{who.name!r} ({who.type.value}) may not approve"
        )
    return who


def authorize_block(actor: str | None) -> identity.AgentIdentity:
    who = identity.resolve(actor)
    if not identity.can_block(who):
        raise GovernanceError(f"{who.name!r} ({who.type.value}) may not block tasks")
    return who
