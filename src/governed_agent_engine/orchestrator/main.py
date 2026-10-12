"""Orchestrator: explicit control flow, all side effects behind governance.

Loop (one cycle):
  intake (CLI + artifacts/inbox/*.json) -> normalize -> validate (APCK) -> route
  -> execute (Grok handoff) | cognition (plan) -> backlog
  -> replay when Grok becomes AVAILABLE -> evidence reconciliation.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from governed_agent_engine.bench.capabilities import load_capabilities
from governed_agent_engine.cognition.health import HealthTracker
from governed_agent_engine.cognition.http import HttpClient, UrllibHttpClient
from governed_agent_engine.cognition.planner import plan_task
from governed_agent_engine.cognition.registry import build_clients, build_limiters
from governed_agent_engine.cognition.strategy import CognitionStrategy
from governed_agent_engine.config.loader import AppConfig, file_fingerprint, load_config
from governed_agent_engine.core import (
    acquisition,
    agent_selection,
    block_report,
    routing_penalty,
    scopes,
)
from governed_agent_engine.core.charter import CharterError, load_charter, read_grok
from governed_agent_engine.core.errors import (
    AllEndpointsFailedError,
    DoctrineError,
    GovernanceError,
    GovernanceTamperError,
    ValidationError,
)
from governed_agent_engine.core.grok_binding import (
    DriftMonitor,
    audit,
    bind,
    ledger_findings,
    selection_context,
    write_audit,
)
from governed_agent_engine.core.models import (
    BacklogEntry,
    BacklogStatus,
    Executor,
    Plan,
    RouteDecision,
    Task,
    UsageState,
)
from governed_agent_engine.grok.client import FileQueueTransport, GrokClient, GrokTransport
from governed_agent_engine.grok.executor_interface import GrokExecutor
from governed_agent_engine.observability.logging import JsonLogger
from governed_agent_engine.observability.metrics import Metrics
from governed_agent_engine.orchestrator import governance
from governed_agent_engine.orchestrator.backlog_manager import BacklogManager, atomic_write_json
from governed_agent_engine.orchestrator.doctrine_gate import check_execution_evidence
from governed_agent_engine.orchestrator.router import route_task
from governed_agent_engine.orchestrator.usage_state import (
    became_available,
    should_probe,
    update_from_heartbeat,
)

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Outcome:
    decision: RouteDecision
    entry: BacklogEntry


@dataclass
class CycleReport:
    intake_ok: int = 0
    intake_rejected: int = 0
    reconciled: int = 0
    pending_for_grok: int = 0
    grok_status: str = "UNKNOWN"
    replay_triggered: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def normalize(raw: object) -> Task:
    """Accepts a Task dict, or {"task": {...}} envelope; trims strings in action/type."""
    if isinstance(raw, dict) and set(raw) == {"task"}:
        raw = raw["task"]
    if isinstance(raw, dict):
        raw = {k: (val.strip() if isinstance(val, str) and k in ("type", "action") else val)
               for k, val in raw.items()}  # fmt: skip
    return Task.from_dict(raw)


class Orchestrator:
    def __init__(
        self,
        config: AppConfig,
        strategy: CognitionStrategy,
        transport: GrokTransport | None = None,
        *,
        clock: Clock = utcnow,
        logger: JsonLogger | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.config = config
        self.art = config.root / "artifacts"
        self.drift = DriftMonitor(config.root, config.charter_fingerprint, self._rebind)
        self.strategy = strategy
        self.clock = clock
        # Overseer GOVERNANCE: audit every agent at startup and keep the record
        self.startup_audit = write_audit(
            audit(config.root, config.doctrine, self.clock(), self.art), self.art
        )
        self.backlog = BacklogManager(self.art)
        self.grok = GrokClient(
            transport or FileQueueTransport(self.art / "backlog", self.art / "evidence"),
            config.usage,
        )
        self.executor = GrokExecutor(self.grok)
        self.logger = logger or JsonLogger(self.art / "logs", stream=None, clock=clock)
        self.metrics = metrics or Metrics()
        self.usage = UsageState()
        self.governance_file = config.root / "config" / "governance.yaml"

    # ------------------------------------------------------------------ helpers
    def _log(self, task_id: str, phase: Any, executor: str, status: str, **kw: Any) -> None:
        self.logger.event(task_id=task_id, phase=phase, executor=executor, status=status, **kw)

    def refresh_usage(self, force: bool = False) -> tuple[UsageState, bool]:
        now = self.clock()
        before = self.usage
        if force or should_probe(now, self.usage, self.config.usage):
            _, hb = self.grok.probe(now)
            self.usage = update_from_heartbeat(self.usage, hb, now, self.config.usage)
        return self.usage, became_available(before, self.usage)

    # ------------------------------------------------------------------ submit
    def submit(self, task: Task) -> Outcome:
        gov = self.config.governance
        t0 = time.perf_counter()
        governance.assert_unchanged(gov, self.governance_file)
        self.assert_doctrine_unchanged()
        self._log(task.task_id, "validate", "-", "start", depth=task.depth)
        try:
            governance.validate_task(task, gov, self.find_task)
        except GovernanceError as exc:
            self.metrics.incr("governance.denied")
            self._log(task.task_id, "validate", "-", "denied", reason=str(exc))
            raise
        needs_human = governance.requires_human(task.action, gov)

        usage, _ = self.refresh_usage()
        decision = route_task(task, usage, self.config.routing)
        self._log(task.task_id, "route", decision.executor.value, "routed",
                  reason=decision.reason, grok=usage.status.value)  # fmt: skip

        governance.assert_allowed("write_artifact", {"auto": True}, gov)
        governance.assert_write_path(self.backlog.backlog_dir, self.config.root, gov)

        plan = None
        if decision.executor is Executor.COGNITION:
            try:
                plan, result = plan_task(
                    task, self.strategy, decision.endpoints, self.clock(),
                    self.config.doctrine, self._record_rejection,
                )  # fmt: skip
            except DoctrineError as exc:
                self.metrics.incr("doctrine.all_rejected")
                self._log(task.task_id, "cognition", "cognition", "doctrine_rejected",
                          rejections=list(exc.rejections))  # fmt: skip
                raise
            except AllEndpointsFailedError as exc:
                self.metrics.incr("cognition.all_failed")
                self._log(
                    task.task_id,
                    "cognition",
                    "cognition",
                    "failed",
                    attempts=[a.to_dict() for a in exc.attempts],
                )
                raise
            for a in result.attempts:
                self.metrics.incr(f"endpoint.{a.endpoint}.{a.outcome}")
            self.metrics.incr("tasks.cognition")
            self._log(task.task_id, "cognition", "cognition", "planned",
                      endpoint=plan.endpoint, steps=len(plan.steps))  # fmt: skip
        else:
            self.metrics.incr("tasks.grok")

        selection = self._select_agent(task)

        entry = self.backlog.create(
            task, self.clock(), executor=decision.executor,
            requires_human_approval=needs_human, plan=plan,
        )  # fmt: skip
        if selection.blocked:
            entry = self._hold(entry, selection)
        elif decision.executor is Executor.GROK:
            self.executor.dispatch(entry)
        self._log(task.task_id, "backlog", decision.executor.value, entry.status.value,
                  entry_id=entry.entry_id)  # fmt: skip
        self.metrics.incr("backlog.created")
        self.metrics.observe("submit", (time.perf_counter() - t0) * 1000)
        return Outcome(decision, entry)

    def _rebind(self) -> None:
        d = self.config.doctrine
        load_charter(self.config.root, d.min_steps, d.min_step_chars)
        bind(self.config.root, d)

    def assert_doctrine_unchanged(self) -> None:
        path = self.config.root / "config" / "doctrine.yaml"
        if path.is_file() and file_fingerprint(path) != self.config.doctrine.fingerprint:
            raise GovernanceTamperError("doctrine.yaml changed while running. Restart required.")
        try:  # periodic drift check: only a migration signed off by owner may change it
            self.drift.check()
        except CharterError as exc:
            raise GovernanceTamperError(
                f"identities/, doctrine/ or .grok changed while running: {exc}"
            ) from exc

    def approve(self, entry_id: str, approver: str) -> BacklogEntry:
        """AWAITING_APPROVAL -> APPROVED. Human only (never the overseer)."""
        self.assert_doctrine_unchanged()
        who = governance.authorize_approval(approver, self.config.governance)
        entry = self.backlog.update_status(entry_id, BacklogStatus.APPROVED, self.clock())
        self._decision_record(entry, "approved", who.name, "")
        return entry

    def block(self, entry_id: str, actor: str, reason: str) -> BacklogEntry:
        """Review/flag/block: human or overseer moves a not-yet-running entry to REJECTED."""
        self.assert_doctrine_unchanged()
        who = governance.authorize_block(actor)
        if not reason.strip():
            raise GovernanceError("blocking requires a reason")
        entry = self.backlog.update_status(entry_id, BacklogStatus.REJECTED, self.clock())
        self._decision_record(entry, "blocked", who.name, reason)
        return entry

    def _decision_record(self, entry: BacklogEntry, kind: str, by: str, reason: str) -> None:
        atomic_write_json(self._evidence_path("decisions", f"{entry.entry_id}.{kind}.json"), {
            "kind": kind, "entry_id": entry.entry_id, "task_id": entry.task_id, "by": by,
            "reason": reason, "at": self.clock().isoformat(),
        })  # fmt: skip

    def find_task(self, task_id: str) -> Task | None:
        for e in self.backlog.entries():
            if e.task.task_id == task_id:
                return e.task
        return None

    def _evidence_path(self, *parts: str) -> Path:
        p = self.art.joinpath("evidence", *parts)
        governance.assert_write_path(p, self.config.root, self.config.governance)
        return p

    def _record_rejection(self, plan: Plan, violations: list[str], attempt: int) -> None:
        """Failure analysis evidence for a doctrine-rejected plan."""
        self.metrics.incr("doctrine.rejected")
        self.metrics.incr(f"doctrine.rejected.{plan.endpoint}")
        path = self._evidence_path(
            "doctrine", f"{plan.task_id}.{attempt}.{plan.endpoint}.rejected.json"
        )
        atomic_write_json(path, {
            "kind": "doctrine_rejection", "task_id": plan.task_id, "attempt": attempt,
            "endpoint": plan.endpoint, "model": plan.model, "violations": violations,
            "doctrine_fingerprint": self.config.doctrine.fingerprint,
            "at": self.clock().isoformat(), "plan": plan.to_dict(),
        })  # fmt: skip
        self._log(plan.task_id, "doctrine", "cognition", "rejected",
                  endpoint=plan.endpoint, violations=violations)  # fmt: skip

    # ------------------------------------------------------------ agent selection
    def _selection_scope(self, task: Task) -> str:
        return agent_selection.scope_for(
            acquisition.is_acquisition(task.type, task.issued_by, task.inputs),
            scopes.is_cognition(task.issued_by, task.inputs),
        )

    def _select_agent(self, task: Task) -> agent_selection.AgentState:
        """Agent selection (docs/agent_selection.md). Routing stops (GovernanceError) on any
        penalty/ledger finding: drift, looser .grok, tampered or inconsistent logs."""
        try:
            ctx = selection_context(self.config.root)
        except CharterError as exc:
            raise GovernanceError(f"agent selection stopped: {exc.code}: {exc}") from exc
        problems = ledger_findings(self.config.root, self.art, ctx)
        if problems:
            code, msg = problems[0]
            raise GovernanceError(f"agent selection stopped: {code.value}: {msg}")
        runs = routing_penalty.read_rows(self.art.joinpath(*routing_penalty.LEDGER))
        scope = self._selection_scope(task)
        try:
            st = agent_selection.select(scope, ctx.table, runs, ctx.window, ctx.unblocks)
        except routing_penalty.PenaltyError as exc:
            raise GovernanceError(f"agent selection stopped: {exc.code}: {exc}") from exc
        ref = (agent_selection.find_report(self.art, st.block_trigger)
               if st.blocked and st.block_trigger else None)  # fmt: skip
        agent_selection.record(
            self.art.joinpath(*agent_selection.SELECTION_LEDGER), task.task_id, scope, st,
            ctx.table[st.agent], runs, self.clock().isoformat(), ctx.unblocks, ref,
        )  # fmt: skip
        return st

    def _hold(self, entry: BacklogEntry, st: agent_selection.AgentState) -> BacklogEntry:
        """Blocked agent: keep the entry in the backlog as blocked_agent, never dispatch."""
        now = self.clock()
        entry = self.backlog.update_status(entry.entry_id, BacklogStatus.BLOCKED_AGENT, now)
        reason = (f"{st.agent} blocked: score {st.score} < {agent_selection.THRESHOLD} "
                  f"(lazy {st.blocked_by}); needs a Owner-signed unblock")  # fmt: skip
        path = self.art / "evidence" / f"{entry.entry_id}.blocked_agent.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "kind": "blocked_agent", "entry_id": entry.entry_id, "task_id": entry.task_id,
            "agent": st.agent, "reason": reason, "state": st.summary(), "at": now.isoformat(),
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")  # fmt: skip
        self.metrics.incr(f"agent_selection.blocked.{st.agent}")
        return entry

    def _maybe_block_report(self, agent: str, trigger: str, now: Any) -> None:
        """A run that just caused a block gets its block report + a chat notification."""
        ctx = selection_context(self.config.root)
        runs = routing_penalty.read_rows(self.art.joinpath(*routing_penalty.LEDGER))
        st = agent_selection.agent_state(agent, runs, ctx.table[agent], ctx.window, ctx.unblocks)
        if st.block_trigger != trigger:
            return
        scope = {v: k for k, v in agent_selection.SCOPES.items()}[agent]
        held = [e.task_id for e in self.backlog.replay(frozenset({BacklogStatus.BLOCKED_AGENT}))
                if self._selection_scope(e.task) == scope]  # fmt: skip
        doctrine_grok = read_grok(self.config.root / "config" / "doctrine.grok", "PENALTY_MISMATCH")
        ref = block_report.build(
            self.config.root, self.art, st, ctx.table[agent], held, now, doctrine_grok
        )
        agent_selection.append_block(self.art, agent, trigger, ref, now.isoformat())
        rep = json.loads((self.art / ref["json"]).read_text(encoding="utf-8"))
        note = self.art / "outbox" / "notifications" / (Path(ref["json"]).stem + ".json")
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(json.dumps({
            "kind": "block_notification", "to": "owner", "channel": "chat",
            "summary": block_report.summary(rep), "report_json": ref["json"],
            "report_md": ref["md"], "at": now.isoformat(), "delivered": False,
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")  # fmt: skip
        self.metrics.incr(f"agent_selection.block_report.{agent}")

    def blocks_overview(self) -> dict[str, Any]:
        """CLI ``blocks`` / ``status``: block reports, live agent states, held tasks."""
        ctx = selection_context(self.config.root)
        runs = routing_penalty.read_rows(self.art.joinpath(*routing_penalty.LEDGER))
        reports = []
        for r in routing_penalty.read_rows(self.art.joinpath(*agent_selection.BLOCK_LEDGER)):
            rep = json.loads((self.art / r["report"]["json"]).read_text(encoding="utf-8"))
            reports.append({"agent": r["agent"], "summary": block_report.summary(rep),
                            "json": r["report"]["json"], "md": r["report"]["md"]})  # fmt: skip
        states = {s: agent_selection.select(s, ctx.table, runs, ctx.window, ctx.unblocks).summary()
                  for s in agent_selection.SCOPES}  # fmt: skip
        held = [{"entry_id": e.entry_id, "task_id": e.task_id}
                for e in self.backlog.replay(frozenset({BacklogStatus.BLOCKED_AGENT}))]  # fmt: skip
        return {"window": ctx.window, "states": states, "reports": reports, "held": held}

    def release_blocked(self) -> list[BacklogEntry]:
        """Re-select held entries; release those whose agent is eligible again."""
        released: list[BacklogEntry] = []
        for entry in self.backlog.replay(frozenset({BacklogStatus.BLOCKED_AGENT})):
            st = self._select_agent(entry.task)
            if st.blocked:
                continue
            nxt = (BacklogStatus.AWAITING_APPROVAL if entry.requires_human_approval
                   else BacklogStatus.PENDING)  # fmt: skip
            entry = self.backlog.update_status(entry.entry_id, nxt, self.clock())
            if nxt is BacklogStatus.PENDING and entry.executor is Executor.GROK:
                self.executor.dispatch(entry)
            self.metrics.incr("agent_selection.released")
            released.append(entry)
        return released

    def _routing_record(self, entry: BacklogEntry, laziness_detected: bool, now: Any) -> None:
        """Agent-level routing penalty (docs/routing_penalty.md); Overseer never enters routing."""
        t = entry.task
        agent = routing_penalty.agent_for(
            acquisition.is_acquisition(t.type, t.issued_by, t.inputs),
            scopes.is_cognition(t.issued_by, t.inputs),
        )
        table = self.drift.penalties
        if table is None:
            raise GovernanceError("routing penalties unavailable (PENALTY_MISMATCH)")
        ev = f"artifacts/evidence/{entry.entry_id}.evidence_check.json" if laziness_detected else ""
        rec = routing_penalty.record(
            self.art.joinpath(*routing_penalty.LEDGER), table[agent], laziness_detected,
            now.isoformat(), t.task_id, evidence_path=ev,
        )  # fmt: skip
        if laziness_detected:
            self._maybe_block_report(agent, str(rec["hash"]), now)
        if laziness_detected:
            self.metrics.incr(f"routing.penalty.{agent}")

    def _evidence_of_truth(self, entry: BacklogEntry, outputs: dict[str, Any]) -> list[str]:
        t = entry.task  # acquisition run-completion rule (data + logs + acquisition report)
        acq = acquisition.check_run_completion(
            t.type,
            t.issued_by,
            t.inputs,
            outputs,
            self.config.root,
            self.config.governance.artifacts.write_roots,
        )
        if acq:
            self.metrics.incr("acquisition.noncompliant")
        cog = scopes.check_run_completion(
            t.issued_by, t.inputs, outputs, self.config.root,
            self.config.governance.artifacts.write_roots,
        )  # fmt: skip
        if cog:
            self.metrics.incr("cognition.noncompliant")
        acq += cog
        return acq + self._plan_evidence(entry, outputs)

    def _plan_evidence(self, entry: BacklogEntry, outputs: dict[str, Any]) -> list[str]:
        if not entry.plan_path:
            return []
        plan_file = self.config.root / entry.plan_path
        if not plan_file.is_file():
            plan_file = self.art / entry.plan_path
        if not plan_file.is_file():
            return [f"plan file missing: {entry.plan_path}"]
        plan = Plan.from_dict(json.loads(plan_file.read_text(encoding="utf-8")))
        return check_execution_evidence(
            plan, outputs, self.config.root, self.config.governance.artifacts.write_roots
        )

    # ------------------------------------------------------------------ intake
    def intake_inbox(self, report: CycleReport) -> None:
        inbox = self.art / "inbox"
        for path in sorted(inbox.glob("*.json")):
            dest = "processed"
            try:
                task = normalize(json.loads(path.read_text(encoding="utf-8")))
                self._log(task.task_id, "intake", "-", "received", source=path.name)
                self.submit(task)
                report.intake_ok += 1
            except (
                ValueError,
                ValidationError,
                GovernanceError,
                AllEndpointsFailedError,
                DoctrineError,
            ) as exc:
                dest = "rejected"
                report.intake_rejected += 1
                report.errors.append(f"{path.name}: {exc}")
                self._log(path.stem, "intake", "-", "rejected", reason=str(exc)[:300])
            target = inbox / dest / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            path.replace(target)

    # ------------------------------------------------------------------ replay
    def replay(self) -> list[BacklogEntry]:
        """Reconcile evidence -> status; re-dispatch outstanding Grok entries; return them."""
        self.release_blocked()
        now = self.clock()
        reconciled = 0
        outstanding = frozenset(
            {BacklogStatus.PENDING, BacklogStatus.APPROVED, BacklogStatus.IN_PROGRESS}
        )
        for entry in self.backlog.replay(outstanding):
            result = self.executor.collect(entry.entry_id)
            if result is None:
                continue
            if entry.status is not BacklogStatus.IN_PROGRESS:
                self.backlog.update_status(entry.entry_id, BacklogStatus.IN_PROGRESS, now)
            final = BacklogStatus.DONE if result.status == "done" else BacklogStatus.FAILED
            missing = (
                self._evidence_of_truth(entry, result.outputs)
                if final is BacklogStatus.DONE
                else []
            )
            if missing:  # evidence-of-truth: claimed done without the required evidence
                final = BacklogStatus.FAILED
                self.metrics.incr("evidence.missing")
                atomic_write_json(self._evidence_path(f"{entry.entry_id}.evidence_check.json"), {
                    "kind": "evidence_of_truth_failure", "entry_id": entry.entry_id,
                    "task_id": entry.task_id, "violations": missing,
                    "at": now.isoformat(),
                })  # fmt: skip
            self.backlog.update_status(entry.entry_id, final, now)
            if final is BacklogStatus.DONE or missing:
                self._routing_record(entry, laziness_detected=bool(missing), now=now)
            reconciled += 1
            self._log(entry.task_id, "evidence", "grok", final.value, entry_id=entry.entry_id)
        self.metrics.incr("replay.reconciled", reconciled)
        remaining = [e for e in self.backlog.replay() if e.executor is Executor.GROK]
        for e in remaining:
            self.executor.dispatch(e)  # idempotent for the file queue
            self._log(e.task_id, "replay", "grok", "redispatched", entry_id=e.entry_id)
        return remaining

    # ------------------------------------------------------------------ loop
    def run_cycle(self) -> CycleReport:
        report = CycleReport()
        governance.assert_unchanged(self.config.governance, self.governance_file)
        self.assert_doctrine_unchanged()
        self.intake_inbox(report)
        usage, came_up = self.refresh_usage(force=True)
        report.grok_status = usage.status.value
        before = self.metrics.counters["replay.reconciled"]
        if came_up or usage.status.value == "AVAILABLE":
            report.replay_triggered = True
            report.pending_for_grok = len(self.replay())
        else:
            report.pending_for_grok = sum(
                1 for e in self.backlog.replay() if e.executor is Executor.GROK
            )
        report.reconciled = self.metrics.counters["replay.reconciled"] - before
        self._log("-", "loop", "-", "cycle", **report.to_dict())
        self.metrics.flush(self.art / "metrics")
        return report

    def run_forever(
        self,
        *,
        max_cycles: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> int:
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            self.run_cycle()
            cycles += 1
            if max_cycles is None or cycles < max_cycles:
                sleep(self.config.usage.loop_interval_s)
        return cycles


def build(
    root: Path,
    *,
    http: HttpClient | None = None,
    clock: Clock = utcnow,
    env: dict[str, str] | None = None,
) -> Orchestrator:
    config = load_config(root / "config", root)
    enabled = config.enabled_endpoints()
    strategy = CognitionStrategy(
        build_clients(enabled, http or UrllibHttpClient(), env),
        enabled,
        build_limiters(enabled),
        HealthTracker(config.health.failure_threshold, config.health.cooldown_s),
        config.governance,
        load_capabilities(root / "config" / "capabilities.yaml"),
    )
    return Orchestrator(config, strategy, clock=clock)
