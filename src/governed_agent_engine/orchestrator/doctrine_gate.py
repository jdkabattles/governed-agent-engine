"""Doctrine enforcement.

``check_plan``: every Plan from any cognition model passes here before it may enter the
backlog. Rules are structural (step ``kind`` enum, flags, counts), not keyword matching:
a model can write "improve" in any sentence, but it cannot satisfy ``kind == improve`` after
a ``kind == verify`` step without actually planning one.

``check_execution_evidence``: steps marked ``evidence_required`` must be backed by evidence
artifacts in the executor's result, or the entry is failed.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from governed_agent_engine.core.doctrine import Doctrine
from governed_agent_engine.core.models import Plan, StepKind

EVIDENCE_KINDS = frozenset({StepKind.EXECUTE, StepKind.VERIFY})


def check_plan(plan: Plan, doctrine: Doctrine) -> list[str]:
    """Return doctrine violations (empty list = compliant)."""
    out: list[str] = []
    steps = plan.steps
    if not plan.summary.strip():
        out.append("laziness: empty summary")
    if len(steps) < doctrine.min_steps:
        out.append(f"laziness: {len(steps)} steps < min_steps {doctrine.min_steps}")
    if [s.index for s in steps] != list(range(len(steps))):
        out.append("structure: step indices must be 0..n-1 in order")
    seen: set[str] = set()
    for s in steps:
        if s.kind is None:
            out.append(f"structure: step {s.index} has no kind")
        if len(s.description.strip()) < doctrine.min_step_chars:
            out.append(f"laziness: step {s.index} description too short")
        key = " ".join(s.description.lower().split())
        if key in seen:
            out.append(f"laziness: step {s.index} duplicates an earlier step")
        seen.add(key)
    kinds = [s.kind for s in steps]
    # truth first: facts must be gathered before anything is executed
    if StepKind.GATHER not in kinds:
        out.append("truth: no gather step (facts must be collected first)")
    elif StepKind.EXECUTE in kinds and kinds.index(StepKind.EXECUTE) < kinds.index(StepKind.GATHER):
        out.append("truth: execute step before any gather step")
    if doctrine.evidence_required:
        if StepKind.VERIFY not in kinds:
            out.append("evidence: no verify step")
        for s in steps:
            if s.kind in EVIDENCE_KINDS and not s.evidence_required:
                out.append(f"evidence: {s.kind} step {s.index} must set evidence_required")
    if doctrine.failure_analysis_required:
        for s in steps:
            if s.kind is StepKind.EXECUTE and not s.failure_modes:
                out.append(f"failure_analysis: execute step {s.index} lists no failure_modes")
    if doctrine.improvement_loop:
        verify_at = [i for i, k in enumerate(kinds) if k is StepKind.VERIFY]
        improve_at = [i for i, k in enumerate(kinds) if k is StepKind.IMPROVE]
        if not improve_at:
            out.append("improvement: no step with kind=improve")
        elif verify_at and max(improve_at) < min(verify_at):
            out.append("improvement: improve step must follow a verify step")
    return out


def check_execution_evidence(
    plan: Plan, outputs: Mapping[str, Any], root: Path, write_roots: tuple[str, ...]
) -> list[str]:
    """Evidence-of-truth: every evidence_required step needs ``outputs["evidence"][str(i)]``
    naming a non-empty file under an allowed artifact root. Returns violations."""
    ev = outputs.get("evidence")
    ev_map: Mapping[str, Any] = ev if isinstance(ev, Mapping) else {}
    out: list[str] = []
    allowed = [(root / r).resolve() for r in write_roots]
    for s in plan.steps:
        if not s.evidence_required:
            continue
        ref = ev_map.get(str(s.index))
        if not isinstance(ref, str) or not ref.strip():
            out.append(f"step {s.index}: no evidence artifact reported")
            continue
        p = (root / ref).resolve()
        if not any(p.is_relative_to(a) for a in allowed):
            out.append(f"step {s.index}: evidence outside artifact roots: {ref}")
        elif not p.is_file() or p.stat().st_size == 0:
            out.append(f"step {s.index}: evidence artifact missing or empty: {ref}")
    return out
