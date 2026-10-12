"""Pure router: (task, usage, routing) -> RouteDecision. No I/O, no clock, no randomness."""

from __future__ import annotations

from governed_agent_engine.config.loader import RoutingConfig
from governed_agent_engine.core.models import (
    Executor,
    GrokStatus,
    RouteDecision,
    Task,
    UsageState,
)


def chain_for(task: Task, routing: RoutingConfig) -> tuple[str, ...]:
    return routing.task_types.get(task.type) or routing.default_chain


def route_task(task: Task, usage: UsageState, routing: RoutingConfig) -> RouteDecision:
    chain = chain_for(task, routing)
    if task.requires_grok:
        # Never downgraded to free cognition; queued for Grok even when it is down.
        why = "grok available" if usage.status is GrokStatus.AVAILABLE else "queued for grok"
        return RouteDecision(task.task_id, Executor.GROK, f"requires_grok; {why}")
    if task.type in routing.cognition_only_types:
        return RouteDecision(task.task_id, Executor.COGNITION, "cognition-only task type", chain)
    if routing.default_executor is Executor.COGNITION:
        return RouteDecision(task.task_id, Executor.COGNITION, "default executor", chain)
    if usage.status is GrokStatus.AVAILABLE:
        return RouteDecision(task.task_id, Executor.GROK, "grok available")
    return RouteDecision(
        task.task_id,
        Executor.COGNITION,
        f"grok {usage.status.value.lower()} -> free cognition",
        chain,
    )
