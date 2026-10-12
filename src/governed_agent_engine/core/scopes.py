"""Overseer Agent GOVERNANCE and Analyst Agent COGNITION scopes (the owner's
directives, 2026-10-11).

``GOVERNANCE_POLICY`` / ``COGNITION_POLICY`` are the native (executable) copies of the
``GOVERNANCE`` block in ``config/agents/overseer.grok`` and the ``COGNITION`` block in
``config/agents/analyst.grok``; the binding check requires the .grok blocks to equal them.

* Overseer governs; it does not execute, acquire or perform cognition. A task routed to Overseer
  (``inputs.agent == "overseer"``) must be a ``governance_check``.
* A cognition task (``inputs.agent == "analyst"`` or issued by analyst) must be cognition-scoped,
  structured rather than speculative (declares objective, input artifacts and an output schema),
  side-effect free, $0, and its run must emit a reasoning log, a structured summary and
  refinement notes.
Kept byte-identical in example-executor-backend and governed-agent-engine.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .acquisition import artifact_problems

GOVERNANCE_POLICY: dict[str, Any] = {
    "AUDIT_AT_STARTUP": True,
    "AUDIT_ON_DEMAND": True,
    "DRIFT_DETECTION": ["identity", "doctrine", "directive", "unauthorized_change"],
    "BLOCK_NON_COMPLIANT_UNTIL": "owner_approved_migration",
    "AUDIT_RECORDS": "artifacts/evidence/audits",
    "OVERSEES": ["grok", "acquirer", "analyst"],
    "TASK_TYPES": ["governance_check"],
    "FORBIDDEN_FUNCTIONS": ["execute", "acquire", "cognition"],
    "MAY_APPROVE_HUMAN_ACTIONS": False,
}
# Overseer-only doctrine override: stricter than the shared
# doctrine; applies when Overseer evaluates/audits (laziness multiplier 0.25).
OVERSEER_DOCTRINE_OVERRIDE: dict[str, Any] = {
    "TRUTH_PRIORITY": "highest",
    "EVIDENCE_REQUIRED": True,
    "LAZINESS_PENALTY": "maximum",
    "FAILURE_ANALYSIS_REQUIRED": True,
    "IMPROVEMENT_LOOP": "mandatory",
}
# OVERSIGHT_REQUIREMENTS (the owner's wording, verbatim) -> the checks that enforce each one.
# "enforced": a mechanical gate/audit rejects violations. "structural": the rule is required
# to be present everywhere, but whether an agent actually follows it cannot be verified by code.
OVERSIGHT_ENFORCEMENT: dict[str, tuple[str, list[str]]] = {
    "Agents produce multi-step plans.": (
        "enforced",
        ["doctrine_gate.check_plan: min_steps, distinct non-trivial steps"],
    ),
    "Agents back claims and actions with evidence.": (
        "enforced",
        [
            "doctrine_gate.check_plan: evidence_required on execute/verify steps",
            "reconciliation: evidence-of-truth (done without evidence => failed)",
            "acquisition/cognition run-artifact checks",
        ],
    ),
    "Agents list failure_modes when planning.": (
        "enforced",
        [
            "doctrine_gate.check_plan: execute steps must list failure_modes",
            "acquisition gate: failure_modes for rate_limits/bans/bad_data/coverage",
        ],
    ),
    "Agents include an improvement step.": (
        "enforced",
        ["doctrine_gate.check_plan: kind=improve step after verify"],
    ),
    "Agents follow the APCK lifecycle.": (
        "enforced",
        [
            "governance.validate_task (allowlists, depth, provenance, issuer)",
            "governance.assert_allowed (human-approval actions never automatic)",
            "governance/doctrine/charter tamper checks",
        ],
    ),
    "Agents follow routing decisions.": (
        "enforced",
        [
            "router.route_task decides the executor deterministically",
            "backlog records the routed executor; only its evidence is reconciled",
            "scope gates: acquirer/analyst/overseer tasks must match their scope",
        ],
    ),
    "Agents ask the owner when something is unclear.": (
        "structural",
        [
            "binding: every package RESPONSE_WINDOW requires clarification with "
            "Owner (CLARIFICATION_RULE_MISSING otherwise)"
        ],
    ),
    "Agents stay consistent with their .grok packages.": (
        "enforced",
        ["startup binding (IDENTITY_MISMATCH etc.)", "DriftMonitor", "audit (NON_COMPLIANT)"],
    ),
}
OVERSIGHT_REQUIREMENTS: list[str] = list(OVERSIGHT_ENFORCEMENT)
# COGNITION_REQUIREMENTS -> checks.
# "partial": the declared shape is enforced but the quality of the content is not verifiable.
COGNITION_ENFORCEMENT: dict[str, tuple[str, list[str]]] = {
    "Reasoning is multi-step and structured.": (
        "enforced",
        [
            "doctrine_gate.check_plan: min_steps, typed step kinds",
            "cognition gate: output_schema (JSON schema object) required",
        ],
    ),
    "Each task states its goal, source files and output format.": (
        "enforced",
        ["cognition gate: objective, input_artifacts, output_schema required"],
    ),
    "Each completed task includes a reasoning log, a summary and refinement notes.": (
        "enforced",
        [
            "run completion: reasoning_log, structured_summary, refinement_notes "
            "artifacts (missing/empty => failed)"
        ],
    ),
    "Reasoning rests on evidence, not speculation.": (
        "partial",
        [
            "cognition gate: input_artifacts required",
            "reconciliation: evidence-of-truth for evidence_required steps",
            "not verifiable: whether the reasoning content actually follows from it",
        ],
    ),
    "The analyst uses no tools and spends nothing.": (
        "enforced",
        [
            "cognition gate: no tool/side_effects, budget_usd 0",
            "not visible: actions taken outside the governed task queue",
        ],
    ),
    "The analyst asks the owner when something is unclear.": (
        "structural",
        ["binding: analyst RESPONSE_WINDOW requires clarification with Owner"],
    ),
    "The analyst follows routing decisions inside the governed system.": (
        "enforced",
        [
            "router.route_task decides the executor; backlog records it",
            "scope gates: analyst-addressed tasks must be cognition-scoped",
        ],
    ),
}
COGNITION_REQUIREMENTS: list[str] = list(COGNITION_ENFORCEMENT)
COGNITION_POLICY: dict[str, Any] = {
    "TASK_TYPES": ["research", "custom"],
    "REQUIRED_INPUTS": ["objective", "input_artifacts", "output_schema"],
    "RUN_ARTIFACTS": ["reasoning_log", "structured_summary", "refinement_notes"],
    "SIDE_EFFECTS": False,
    "BUDGET_USD": 0,
}


class ScopeError(ValueError):
    def __init__(self, code: str, problems: list[str]) -> None:
        super().__init__(f"{code}: " + "; ".join(problems))
        self.code = code
        self.problems = problems


def _filled(v: object) -> bool:
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, tuple, dict)):
        return bool(v)
    return False


def is_cognition(issued_by: str, inputs: Mapping[str, Any]) -> bool:
    return inputs.get("agent") == "analyst" or issued_by == "analyst"


def check_cognition_task(task_type: str, inputs: Mapping[str, Any]) -> list[str]:
    p = COGNITION_POLICY
    out: list[str] = []
    if task_type not in p["TASK_TYPES"]:
        out.append(f"task type {task_type!r} is not cognition-scoped {p['TASK_TYPES']}")
    for key in p["REQUIRED_INPUTS"]:
        if not _filled(inputs.get(key)):
            out.append(f"missing {key} (structured reasoning, not free-form speculation)")
    if "output_schema" in inputs and not isinstance(inputs["output_schema"], dict):
        out.append("output_schema must be an object (JSON schema)")
    if "tool" in inputs or inputs.get("side_effects"):
        out.append("cognition is advisory: no tools or side effects")
    budget = inputs.get("budget_usd", 0)
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget != 0:
        out.append("budget_usd must be 0 ($0 only)")
    return out


def gate(task_type: str, issued_by: str, inputs: Mapping[str, Any]) -> None:
    """Validation gate; raises ``ScopeError`` (GOVERNANCE_/COGNITION_NON_COMPLIANT)."""
    if inputs.get("agent") == "overseer" and task_type not in GOVERNANCE_POLICY["TASK_TYPES"]:
        raise ScopeError(
            "GOVERNANCE_NON_COMPLIANT",
            [f"Overseer Agent governs only (governance_check), not {task_type!r}: it does not "
             "execute, acquire data or perform cognition"],
        )  # fmt: skip
    if is_cognition(issued_by, inputs):
        problems = check_cognition_task(task_type, inputs)
        if problems:
            raise ScopeError("COGNITION_NON_COMPLIANT", problems)


def check_run_completion(
    issued_by: str,
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Any],
    root: Path,
    write_roots: Iterable[str],
) -> list[str]:
    """Cognition runs must emit non-empty reasoning_log.*, structured_summary.* and
    refinement_notes.* artifacts. Returns ``COGNITION_NON_COMPLIANT: ...`` reasons."""
    if not is_cognition(issued_by, inputs):
        return []
    req = COGNITION_POLICY["RUN_ARTIFACTS"]
    return [f"COGNITION_NON_COMPLIANT: {x}"
            for x in artifact_problems(outputs, root, write_roots, req)]  # fmt: skip
