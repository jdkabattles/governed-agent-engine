"""Acquisition Agent acquisition gate (the owner's directive, 2026-10-11).

The ``ACQUISITION`` block of ``config/agents/acquirer.grok`` encodes the owner's operational
rules as checkable constraints. ``ACQUISITION_POLICY`` below is the native (executable) copy;
the binding check requires the .grok block to equal it, so neither can drift silently.

A task is an *acquisition task* when ``inputs.agent == "acquirer"`` or it is issued by
acquirer. Such a task must: use an acquisition-scoped task type; declare ``target``,
``output_schema``, ``evidence_plan`` and ``failure_modes`` covering rate limits, bans, bad data
and coverage; use only approved tools; spend $0; and set ``human_approval`` when it publishes
to the Apify Store. Every run must emit data + logs + a brief acquisition report.
Kept identical (apart from imports) in example-executor-backend and governed-agent-engine.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path, PurePath
from typing import Any

AGENT = "acquirer"
ACQUISITION_POLICY: dict[str, Any] = {
    "TASK_TYPES": ["research", "market_watch"],
    "REQUIRED_INPUTS": ["target", "output_schema", "evidence_plan", "failure_modes"],
    "REQUIRED_FAILURE_MODES": ["rate_limits", "bans", "bad_data", "coverage"],
    "RUN_ARTIFACTS": ["data", "logs", "acquisition_report"],
    "APPROVED_TOOLS": ["apify_actor_via_connector", "in_repo_scraper"],
    "HUMAN_APPROVAL_REQUIRED": ["publish_apify_store"],
    "BUDGET_USD": 0,
}


class AcquisitionError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("ACQUISITION_NON_COMPLIANT: " + "; ".join(problems))
        self.problems = problems


def is_acquisition(task_type: str, issued_by: str, inputs: Mapping[str, Any]) -> bool:
    del task_type
    return inputs.get("agent") == AGENT or issued_by == AGENT


def _filled(v: object) -> bool:
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, tuple, dict)):
        return bool(v)
    return False


def check_task(task_type: str, inputs: Mapping[str, Any], human_approval: bool) -> list[str]:
    """Problems with one acquisition task (empty list = compliant)."""
    p = ACQUISITION_POLICY
    out: list[str] = []
    if task_type not in p["TASK_TYPES"]:
        out.append(f"task type {task_type!r} is not acquisition-scoped {p['TASK_TYPES']}")
    for key in p["REQUIRED_INPUTS"]:
        if not _filled(inputs.get(key)):
            out.append(f"missing {key}")
    if "output_schema" in inputs and not isinstance(inputs["output_schema"], dict):
        out.append("output_schema must be an object (JSON schema)")
    fm = inputs.get("failure_modes")
    if isinstance(fm, dict):
        for mode in p["REQUIRED_FAILURE_MODES"]:
            if not _filled(fm.get(mode)):
                out.append(f"failure_modes.{mode} not analysed")
    elif fm is not None:
        out.append("failure_modes must map each failure mode to its mitigation")
    tool = inputs.get("tool")
    if tool not in p["APPROVED_TOOLS"]:
        out.append(f"tool {tool!r} not in approved tools {p['APPROVED_TOOLS']}")
    budget = inputs.get("budget_usd", 0)
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget != 0:
        out.append("budget_usd must be 0 ($0 only)")
    if inputs.get("publish_apify_store") and not human_approval:
        out.append("publishing to the Apify Store requires human_approval (Owner)")
    return out


def gate(task_type: str, issued_by: str, inputs: Mapping[str, Any], human_approval: bool) -> None:
    """Raise ``AcquisitionError`` for a non-compliant acquisition task; no-op otherwise."""
    if not is_acquisition(task_type, issued_by, inputs):
        return
    problems = check_task(task_type, inputs, human_approval)
    if problems:
        raise AcquisitionError(problems)


def missing_run_artifacts(paths: Iterable[str]) -> list[str]:
    """Which required run artifacts (data, logs, acquisition_report) are absent, judged by
    file stem prefix (e.g. ``data.jsonl``, ``logs.txt``, ``acquisition_report.md``)."""
    stems = {PurePath(x).name.split(".", 1)[0] for x in paths}
    return [a for a in ACQUISITION_POLICY["RUN_ARTIFACTS"] if a not in stems]


def artifact_problems(
    outputs: Mapping[str, Any], root: Path, write_roots: Iterable[str], required: Iterable[str]
) -> list[str]:
    """Generic run-completion check: ``outputs["artifacts"]`` (list of paths, or mapping
    name -> path) must name non-empty files under an artifact root whose stems cover
    ``required``. Returns problems (empty list = compliant)."""
    raw = outputs.get("artifacts")
    refs: list[object] = list(raw.values()) if isinstance(raw, Mapping) else (
        list(raw) if isinstance(raw, (list, tuple)) else []
    )  # fmt: skip
    allowed = [(root / r).resolve() for r in write_roots]
    out: list[str] = []
    stems: set[str] = set()
    for ref in refs:
        if not isinstance(ref, str) or not ref.strip():
            out.append(f"invalid artifact reference: {ref!r}")
            continue
        path = (root / ref).resolve()
        if not any(path.is_relative_to(a) for a in allowed):
            out.append(f"artifact outside artifact roots: {ref}")
        elif not path.is_file() or path.stat().st_size == 0:
            out.append(f"artifact missing or empty: {ref}")
        else:
            stems.add(PurePath(ref).name.split(".", 1)[0])
    out += [f"missing run artifact: {name}" for name in required if name not in stems]
    return out


def check_run_completion(
    task_type: str,
    issued_by: str,
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Any],
    root: Path,
    write_roots: Iterable[str],
) -> list[str]:
    """Run-completion rule for acquisition tasks: non-empty ``data.*``, ``logs.*`` and
    ``acquisition_report.*`` artifacts under an artifact root, reported in
    ``outputs["artifacts"]``. Returns ``ACQUISITION_NON_COMPLIANT: ...`` reasons."""
    if not is_acquisition(task_type, issued_by, inputs):
        return []
    req = ACQUISITION_POLICY["RUN_ARTIFACTS"]
    return [f"ACQUISITION_NON_COMPLIANT: {x}"
            for x in artifact_problems(outputs, root, write_roots, req)]  # fmt: skip
