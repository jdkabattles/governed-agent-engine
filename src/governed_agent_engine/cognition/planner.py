"""Task -> Plan via the cognition strategy. Planning only; nothing is executed.

Every plan, from whichever model produced it, goes through the doctrine gate. A non-compliant
plan is rejected, its violations are handed to ``on_reject`` (the orchestrator writes them as
failure-analysis evidence), the model's routing utility is penalised, and the next model in
the fallback chain is asked. The doctrine statement is injected into the system prompt.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from governed_agent_engine.cognition.base import ChatMessage
from governed_agent_engine.cognition.crosscheck import repair_json
from governed_agent_engine.cognition.strategy import CognitionStrategy, StrategyResult
from governed_agent_engine.core.doctrine import Doctrine
from governed_agent_engine.core.errors import AllEndpointsFailedError, DoctrineError
from governed_agent_engine.core.models import Plan, PlanStep, StepKind, Task
from governed_agent_engine.orchestrator.doctrine_gate import check_plan

PLAN_INSTRUCTIONS = (
    "You are a planning assistant. You never execute anything; you only plan.\n"
    "Output ONLY a JSON object (no markdown) of the form:\n"
    '{"summary": str, "steps": [{"index": int from 0, "kind": one of '
    '"gather"|"analyze"|"execute"|"verify"|"improve", "description": str, '
    '"evidence_required": bool, "failure_modes": [str]}]}\n'
    "Rules: start by gathering facts (kind gather) before any execute step; every execute and "
    "verify step sets evidence_required=true; every execute step lists concrete failure_modes; "
    "include a verify step and, after it, an improve step that uses what was learned; "
    "3 to 10 specific steps, no filler, no duplicates."
)
_STEP = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+(.*\S)\s*$")

RejectHook = Callable[[Plan, list[str], int], None]


def system_prompt(doctrine: Doctrine) -> str:
    return doctrine.system_prompt() + "\n\n" + PLAN_INSTRUCTIONS


def build_messages(task: Task, doctrine: Doctrine | None = None) -> list[ChatMessage]:
    payload = json.dumps(
        {"type": task.type, "action": task.action, "inputs": task.inputs},
        sort_keys=True,
        default=str,
    )
    sys_text = system_prompt(doctrine or Doctrine())
    return [ChatMessage("system", sys_text), ChatMessage("user", f"Task:\n{payload}")]


def _steps_from_json(obj: Any) -> tuple[str, tuple[PlanStep, ...]] | None:
    if not isinstance(obj, dict) or not isinstance(obj.get("steps"), list):
        return None
    steps: list[PlanStep] = []
    for i, st in enumerate(obj["steps"]):
        if not isinstance(st, dict):
            return None
        kind_raw = str(st.get("kind", "")).strip().lower()
        fm = st.get("failure_modes", [])
        steps.append(
            PlanStep(
                index=st["index"] if isinstance(st.get("index"), int) else i,
                description=str(st.get("description", "")).strip(),
                kind=StepKind(kind_raw) if kind_raw in StepKind._value2member_map_ else None,
                evidence_required=st.get("evidence_required") is True,
                failure_modes=tuple(str(x) for x in fm if str(x).strip())
                if isinstance(fm, list)
                else (),
            )
        )
    return str(obj.get("summary", "")).strip(), tuple(steps)


def parse_plan(task: Task, text: str, endpoint: str, model: str, now: datetime) -> Plan:
    """Structured JSON plan if possible; otherwise a legacy text plan (steps without kind,
    which the doctrine gate will reject)."""
    parsed = None
    try:
        parsed = _steps_from_json(repair_json(text))
    except ValueError:
        parsed = None
    if parsed is not None:
        summary, steps = parsed
    else:
        lines = [ln for ln in text.strip().splitlines() if ln.strip()]
        matches = [m.group(1) for m in (_STEP.match(ln) for ln in lines) if m]
        summary = lines[0].strip() if lines else ""
        steps = tuple(PlanStep(i, d) for i, d in enumerate(matches))
    return Plan(task.task_id, endpoint, model, summary, now, steps, text)


def plan_task(
    task: Task,
    strategy: CognitionStrategy,
    chain: Sequence[str],
    now: datetime,
    doctrine: Doctrine | None = None,
    on_reject: RejectHook | None = None,
    max_models: int = 4,
) -> tuple[Plan, StrategyResult]:
    doc = doctrine or Doctrine()
    msgs = build_messages(task, doc)
    tried: list[str] = []
    rejections: list[dict[str, object]] = []
    last_exc: AllEndpointsFailedError | None = None
    for attempt in range(max_models):
        try:
            result = strategy.complete(task, chain, msgs, exclude=tried)
        except AllEndpointsFailedError as exc:
            last_exc = exc
            break
        c = result.completion
        plan = parse_plan(task, c.text, c.endpoint, c.model, now)
        violations = check_plan(plan, doc)
        if not violations:
            return plan, result
        tried.append(c.endpoint)
        strategy.penalize(c.endpoint, doc.laziness_penalty)
        rejections.append({"endpoint": c.endpoint, "model": c.model, "violations": violations})
        if on_reject is not None:
            on_reject(plan, violations, attempt)
    if not rejections and last_exc is not None:
        raise last_exc
    raise DoctrineError(tuple(rejections))
