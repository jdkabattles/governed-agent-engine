"""Typed, immutable domain models (stdlib dataclasses + explicit validation).

Every model offers ``from_dict`` (strict: unknown fields rejected) and ``to_dict``
(JSON-ready). Datetimes are always timezone-aware UTC.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from governed_agent_engine.core import validate as v
from governed_agent_engine.core.errors import ValidationError

SCHEMA_VERSION = 2  # contract v2, shared with example-executor-backend (schemas/CONTRACT.md)
TASK_TYPES: tuple[str, ...] = (
    "market_watch",
    "repo_maintain",
    "email_triage",
    "governance_check",
    "research",
    "custom",
)
TASK_ID_PATTERN = r"[A-Za-z0-9_.\-]{1,128}"

# Hard floor: these actions always need a human and are never auto-executed.
HUMAN_APPROVAL_ACTIONS: frozenset[str] = frozenset(
    {"send_message", "delete", "purchase", "publish", "trade", "modify_governance"}
)


def _iso(d: datetime | None) -> str | None:
    return None if d is None else d.isoformat()


class Priority(StrEnum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {"high": 0, "normal": 1, "low": 2}[self.value]


class Executor(StrEnum):
    GROK = "grok"
    COGNITION = "cognition"


class GrokStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    AVAILABLE = "AVAILABLE"
    EXHAUSTED = "EXHAUSTED"


class BacklogStatus(StrEnum):
    PENDING = "pending"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    REJECTED = "rejected"
    BLOCKED_AGENT = "blocked_agent"  # agent selection: held, not dropped, until eligible


STATUS_TRANSITIONS: dict[BacklogStatus, frozenset[BacklogStatus]] = {
    BacklogStatus.PENDING: frozenset(
        {
            BacklogStatus.IN_PROGRESS,
            BacklogStatus.REJECTED,
            BacklogStatus.FAILED,
            BacklogStatus.BLOCKED_AGENT,
        }
    ),
    BacklogStatus.AWAITING_APPROVAL: frozenset(
        {BacklogStatus.APPROVED, BacklogStatus.REJECTED, BacklogStatus.BLOCKED_AGENT}
    ),
    BacklogStatus.BLOCKED_AGENT: frozenset(
        {BacklogStatus.PENDING, BacklogStatus.AWAITING_APPROVAL, BacklogStatus.REJECTED}
    ),
    BacklogStatus.APPROVED: frozenset(
        {BacklogStatus.IN_PROGRESS, BacklogStatus.REJECTED, BacklogStatus.FAILED}
    ),
    BacklogStatus.IN_PROGRESS: frozenset({BacklogStatus.DONE, BacklogStatus.FAILED}),
    BacklogStatus.DONE: frozenset(),
    BacklogStatus.FAILED: frozenset(),
    BacklogStatus.REJECTED: frozenset(),
}

# ------------------------------------------------------------------------------- Task


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    type: str
    action: str = "plan"
    inputs: dict[str, Any] = field(default_factory=dict)
    requires_grok: bool = False
    priority: Priority = Priority.NORMAL
    human_approval: bool = False
    sensitive: bool = False
    depth: int = 0
    parent_task_id: str | None = None
    issued_by: str = ""  # identity name (core/identity.py); "" = unknown, authority 0
    provenance: tuple[str, ...] = ()  # ancestor task ids, root first; len == depth

    FIELDS = (
        "task_id", "type", "action", "inputs", "requires_grok", "priority",
        "human_approval", "sensitive", "depth", "parent_task_id", "issued_by", "provenance",
    )  # fmt: skip

    @classmethod
    def from_dict(cls, raw: object) -> Task:
        d = v.as_mapping(raw, "Task")
        v.reject_unknown(d, cls.FIELDS, "Task")
        parent = d.get("parent_task_id")
        return cls(
            task_id=v.s(v.req(d, "task_id", "Task"), "Task.task_id", pattern=TASK_ID_PATTERN),
            type=v.choice(v.req(d, "type", "Task"), TASK_TYPES, "Task.type"),
            action=v.s(d.get("action", "plan"), "Task.action", min_len=1, max_len=64)
            .strip()
            .lower(),
            inputs=v.json_obj(d.get("inputs", {}), "Task.inputs"),
            requires_grok=v.b(d.get("requires_grok", False), "Task.requires_grok"),
            priority=v.enum(Priority, d.get("priority", "normal"), "Task.priority"),
            human_approval=v.b(d.get("human_approval", False), "Task.human_approval"),
            sensitive=v.b(d.get("sensitive", False), "Task.sensitive"),
            depth=v.i(d.get("depth", 0), "Task.depth", ge=0),
            parent_task_id=None
            if parent is None
            else v.s(parent, "Task.parent_task_id", pattern=TASK_ID_PATTERN),
            issued_by=v.s(d.get("issued_by", ""), "Task.issued_by", max_len=128),
            provenance=tuple(
                v.s(x, "Task.provenance[]", pattern=TASK_ID_PATTERN)
                for x in v.str_list(d.get("provenance", []), "Task.provenance")
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "type": self.type,
            "action": self.action,
            "inputs": self.inputs,
            "requires_grok": self.requires_grok,
            "priority": self.priority.value,
            "human_approval": self.human_approval,
            "sensitive": self.sensitive,
            "depth": self.depth,
            "parent_task_id": self.parent_task_id,
            "issued_by": self.issued_by,
            "provenance": list(self.provenance),
        }

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), default=str)


# ------------------------------------------------------------------------------- Plan


class StepKind(StrEnum):
    """Plan step kinds. The doctrine's improvement loop is enforced on ``kind``, never on
    words in the description (substring checks like 'improve' in text are trivially gamed)."""

    GATHER = "gather"
    ANALYZE = "analyze"
    EXECUTE = "execute"
    VERIFY = "verify"
    IMPROVE = "improve"


@dataclass(frozen=True, slots=True)
class PlanStep:
    index: int
    description: str
    kind: StepKind | None = None  # None only for legacy plans; doctrine rejects it
    evidence_required: bool = False
    failure_modes: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: object) -> PlanStep:
        d = v.as_mapping(raw, "PlanStep")
        v.reject_unknown(
            d,
            ("index", "description", "kind", "evidence_required", "failure_modes", "action"),
            "PlanStep",  # "action": contract v1 field, accepted and ignored
        )
        kind = d.get("kind")
        return cls(
            index=v.i(d.get("index"), "PlanStep.index", ge=0),
            description=v.s(d.get("description"), "PlanStep.description"),
            kind=None if kind is None else v.enum(StepKind, kind, "PlanStep.kind"),
            evidence_required=v.b(d.get("evidence_required", False), "evidence_required"),
            failure_modes=v.str_list(d.get("failure_modes", []), "PlanStep.failure_modes"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "description": self.description,
            "kind": None if self.kind is None else self.kind.value,
            "evidence_required": self.evidence_required,
            "failure_modes": list(self.failure_modes),
        }


@dataclass(frozen=True, slots=True)
class Plan:
    task_id: str
    endpoint: str
    model: str
    summary: str
    created_at: datetime
    steps: tuple[PlanStep, ...] = ()
    raw_text: str = ""

    @classmethod
    def from_dict(cls, raw: object) -> Plan:
        d = v.as_mapping(raw, "Plan")
        v.reject_unknown(
            d,
            ("task_id", "endpoint", "provider", "model", "summary", "created_at", "steps",
             "raw_text"),
            "Plan",
        )  # fmt: skip
        if "endpoint" not in d and "provider" in d:  # contract v1 (example-executor-backend) name
            d = {**d, "endpoint": d["provider"]}
        steps_raw = d.get("steps", [])
        if not isinstance(steps_raw, list):
            raise ValidationError("Plan.steps: expected list")
        steps = tuple(PlanStep.from_dict(st) for st in steps_raw)
        return cls(
            task_id=v.s(v.req(d, "task_id", "Plan"), "Plan.task_id"),
            endpoint=v.s(v.req(d, "endpoint", "Plan"), "Plan.endpoint"),
            model=v.s(v.req(d, "model", "Plan"), "Plan.model"),
            summary=v.s(d.get("summary", ""), "Plan.summary"),
            created_at=v.dt(v.req(d, "created_at", "Plan"), "Plan.created_at"),
            steps=steps,
            raw_text=v.s(d.get("raw_text", ""), "Plan.raw_text", max_len=200_000),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "endpoint": self.endpoint,
            "model": self.model,
            "summary": self.summary,
            "created_at": _iso(self.created_at),
            "steps": [st.to_dict() for st in self.steps],
            "raw_text": self.raw_text,
        }


# ------------------------------------------------------------------------- BacklogEntry


@dataclass(frozen=True, slots=True)
class BacklogEntry:
    """Durable handoff record. Immutable except for forward ``status`` transitions."""

    entry_id: str
    task: Task
    created_at: datetime
    status: BacklogStatus
    requires_human_approval: bool
    executor: Executor
    plan_path: str | None = None
    status_history: tuple[tuple[BacklogStatus, datetime], ...] = ()
    schema_version: int = SCHEMA_VERSION

    @property
    def task_id(self) -> str:
        return self.task.task_id

    def sort_key(self) -> tuple[datetime, int, str]:
        return (self.created_at, self.task.priority.rank, self.entry_id)

    def with_status(self, new: BacklogStatus, now: datetime) -> BacklogEntry:
        return replace(self, status=new, status_history=(*self.status_history, (new, now)))

    @classmethod
    def from_dict(cls, raw: object) -> BacklogEntry:
        d = v.as_mapping(raw, "BacklogEntry")
        v.reject_unknown(
            d,
            (
                "entry_id", "task", "created_at", "status", "requires_human_approval",
                "executor", "plan_path", "status_history", "schema_version",
            ),
            "BacklogEntry",
        )  # fmt: skip
        hist_raw = d.get("status_history", [])
        if not isinstance(hist_raw, list):
            raise ValidationError("BacklogEntry.status_history: expected list")
        history = tuple(
            (
                v.enum(BacklogStatus, h.get("status"), "history.status"),
                v.dt(h.get("at"), "history.at"),
            )
            for h in (
                # contract v1 (example-executor-backend 0.1) wrote [status, at] pairs
                {"status": x[0], "at": x[1]}
                if isinstance(x, list) and len(x) == 2
                else v.as_mapping(x, "history")
                for x in hist_raw
            )
        )
        return cls(
            entry_id=v.s(v.req(d, "entry_id", "BacklogEntry"), "entry_id", min_len=1),
            task=Task.from_dict(v.req(d, "task", "BacklogEntry")),
            created_at=v.dt(v.req(d, "created_at", "BacklogEntry"), "created_at"),
            status=v.enum(BacklogStatus, v.req(d, "status", "BacklogEntry"), "status"),
            requires_human_approval=v.b(
                v.req(d, "requires_human_approval", "BacklogEntry"), "requires_human_approval"
            ),
            executor=v.enum(Executor, d.get("executor", "grok"), "executor"),  # v1: absent
            plan_path=v.opt_s(d.get("plan_path"), "plan_path"),
            status_history=history,
            schema_version=v.i(d.get("schema_version", SCHEMA_VERSION), "schema_version", ge=1),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "entry_id": self.entry_id,
            "task": self.task.to_dict(),
            "created_at": _iso(self.created_at),
            "status": self.status.value,
            "requires_human_approval": self.requires_human_approval,
            "executor": self.executor.value,
            "plan_path": self.plan_path,
            "status_history": [
                {"status": st.value, "at": _iso(at)} for st, at in self.status_history
            ],
        }


# ------------------------------------------------------------------- Evidence / Grok I/O


@dataclass(frozen=True, slots=True)
class GrokResult:
    """Evidence written by Grok Bot: artifacts/evidence/<entry_id>.json."""

    entry_id: str
    task_id: str
    status: str  # "done" | "failed"
    finished_at: datetime
    summary: str = ""
    outputs: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    started_at: datetime | None = None

    @classmethod
    def from_dict(cls, raw: object) -> GrokResult:
        d = v.as_mapping(raw, "GrokResult")
        v.reject_unknown(
            d,
            (
                "entry_id",
                "task_id",
                "status",
                "finished_at",
                "summary",
                "outputs",
                "schema_version",
                "started_at",
            ),
            "GrokResult",
        )
        started = d.get("started_at")
        return cls(
            entry_id=v.s(v.req(d, "entry_id", "GrokResult"), "entry_id", min_len=1),
            task_id=v.s(v.req(d, "task_id", "GrokResult"), "task_id", min_len=1),
            status=v.choice(v.req(d, "status", "GrokResult"), ("done", "failed"), "status"),
            finished_at=v.dt(v.req(d, "finished_at", "GrokResult"), "finished_at"),
            summary=v.s(d.get("summary", ""), "summary"),
            outputs=v.json_obj(d.get("outputs", {}), "outputs"),
            schema_version=v.i(d.get("schema_version", SCHEMA_VERSION), "schema_version", ge=1),
            started_at=None if started is None else v.dt(started, "started_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "entry_id": self.entry_id,
            "task_id": self.task_id,
            "status": self.status,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "summary": self.summary,
            "outputs": self.outputs,
        }


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """Liveness written by Grok Bot: artifacts/evidence/heartbeat.json."""

    ts: datetime
    agent: str = "grok-bot"
    note: str = ""

    @classmethod
    def from_dict(cls, raw: object) -> Heartbeat:
        d = v.as_mapping(raw, "Heartbeat")
        v.reject_unknown(d, ("ts", "agent", "note", "schema_version"), "Heartbeat")
        return cls(
            ts=v.dt(v.req(d, "ts", "Heartbeat"), "Heartbeat.ts"),
            agent=v.s(d.get("agent", "grok-bot"), "Heartbeat.agent"),
            note=v.s(d.get("note", ""), "Heartbeat.note"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "ts": _iso(self.ts), "agent": self.agent,
                "note": self.note}  # fmt: skip


@dataclass(frozen=True, slots=True)
class GrokResponse:
    """What a GrokTransport returns for a submission (file path / commit ref / etc.)."""

    accepted: bool
    reference: str
    transport: str
    result: GrokResult | None = None


# ----------------------------------------------------------------- Usage / routing


@dataclass(frozen=True, slots=True)
class UsageState:
    status: GrokStatus = GrokStatus.UNKNOWN
    last_probe: datetime | None = None
    last_heartbeat: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "last_probe": _iso(self.last_probe),
            "last_heartbeat": _iso(self.last_heartbeat),
        }


@dataclass(frozen=True, slots=True)
class RouteDecision:
    task_id: str
    executor: Executor
    reason: str
    endpoints: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "executor": self.executor.value,
            "reason": self.reason,
            "endpoints": list(self.endpoints),
        }


# ------------------------------------------------------------------- EndpointConfig

ENDPOINT_KINDS = ("openai_compat", "gemini", "ai_horde", "judge0")
AUTH_KINDS = ("none", "bearer", "header_key", "header_apikey")
TRUST_LEVELS = ("first_party", "third_party_proxy", "community")


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    name: str
    kind: str
    base_url: str
    model: str
    rpm: int
    enabled: bool = True
    status: str = "verified"
    verified: str = ""
    chat_path: str = "/chat/completions"
    auth: str = "none"
    api_key_env: str | None = None
    default_api_key: str | None = None
    rpd: int | None = None
    timeout_s: float = 60.0
    cost_usd: float = 0.0
    trust: str = "first_party"
    task_types: tuple[str, ...] = ()
    notes: str = ""
    rate_group: str = ""  # endpoints sharing one upstream quota (e.g. all OpenRouter models)
    tpm: int | None = None  # tokens/minute cap (e.g. Groq x-ratelimit-limit-tokens)

    FIELDS = (
        "kind", "base_url", "model", "rpm", "enabled", "status", "verified", "chat_path",
        "auth", "api_key_env", "default_api_key", "rpd", "timeout_s", "cost_usd", "trust",
        "task_types", "notes", "rate_group", "tpm",
    )  # fmt: skip

    @property
    def limit_key(self) -> str:
        return self.rate_group or self.name

    @classmethod
    def from_dict(cls, name: str, raw: object) -> EndpointConfig:
        where = f"endpoints.{name}"
        d = v.as_mapping(raw, where)
        v.reject_unknown(d, cls.FIELDS, where)
        cost = v.f(d.get("cost_usd", 0.0), f"{where}.cost_usd")
        if cost != 0.0:
            raise ValidationError(f"{where}: HARD RULE: every endpoint must be $0 (cost_usd: 0.0)")
        base_url = v.s(v.req(d, "base_url", where), f"{where}.base_url").rstrip("/")
        if not base_url.startswith("https://"):
            raise ValidationError(f"{where}: base_url must be https")
        auth = v.choice(d.get("auth", "none"), AUTH_KINDS, f"{where}.auth")
        key_env = v.opt_s(d.get("api_key_env"), f"{where}.api_key_env")
        default_key = v.opt_s(d.get("default_api_key"), f"{where}.default_api_key")
        if auth != "none" and not (key_env or default_key):
            raise ValidationError(f"{where}: auth requires api_key_env or default_api_key")
        task_types = v.str_list(d.get("task_types", []), f"{where}.task_types")
        for t in task_types:
            v.choice(t, TASK_TYPES, f"{where}.task_types")
        return cls(
            name=name,
            kind=v.choice(v.req(d, "kind", where), ENDPOINT_KINDS, f"{where}.kind"),
            base_url=base_url,
            model=v.s(v.req(d, "model", where), f"{where}.model", min_len=1),
            rpm=v.i(v.req(d, "rpm", where), f"{where}.rpm", ge=1),
            enabled=v.b(d.get("enabled", True), f"{where}.enabled"),
            status=v.s(d.get("status", "verified"), f"{where}.status"),
            verified=str(d.get("verified", "")),
            chat_path=v.s(d.get("chat_path", "/chat/completions"), f"{where}.chat_path"),
            auth=auth,
            api_key_env=key_env,
            default_api_key=default_key,
            rpd=v.opt_i(d.get("rpd"), f"{where}.rpd", ge=1),
            timeout_s=v.f(d.get("timeout_s", 60.0), f"{where}.timeout_s", ge=1.0),
            cost_usd=cost,
            trust=v.choice(d.get("trust", "first_party"), TRUST_LEVELS, f"{where}.trust"),
            task_types=task_types,
            notes=v.s(d.get("notes", ""), f"{where}.notes"),
            rate_group=v.s(d.get("rate_group", ""), f"{where}.rate_group"),
            tpm=None if d.get("tpm") is None else v.i(d.get("tpm"), f"{where}.tpm", ge=1),
        )


# ------------------------------------------------------------- GovernanceConfig (APCK)


@dataclass(frozen=True, slots=True)
class ArtifactsPolicy:
    """A: where the agent may write, and what evidence completion requires."""

    write_roots: tuple[str, ...] = ("artifacts",)
    require_evidence_for_done: bool = True


@dataclass(frozen=True, slots=True)
class ProcessPolicy:
    """P: the lifecycle and which task types/actions enter it."""

    lifecycle: tuple[str, ...] = (
        "intake", "normalize", "validate", "route", "execute", "backlog", "replay", "evidence",
    )  # fmt: skip
    allowed_task_types: frozenset[str] = frozenset(TASK_TYPES)
    human_approval_required: frozenset[str] = HUMAN_APPROVAL_ACTIONS
    min_top_level_authority: int = 100  # authority a top-level task's issuer must hold
    overseer_may_issue_top_level: bool = False  # overseer (overseer, 50) needs this to start work


@dataclass(frozen=True, slots=True)
class ConstraintsPolicy:
    """C: hard limits."""

    allowed_actions: frozenset[str] = frozenset({"plan"})
    max_recursion_depth: int = 3
    max_inputs_bytes: int = 65_536
    cost_ceiling_usd: float = 0.0
    governance_mutable: bool = False


@dataclass(frozen=True, slots=True)
class KnowledgePolicy:
    """K: which knowledge sources (cognition endpoints) may see which data."""

    allowed_endpoints: frozenset[str] = frozenset()
    allow_sensitive_to_third_party: bool = False
    cognition_is_advisory: bool = True


@dataclass(frozen=True, slots=True)
class GovernanceConfig:
    version: int
    artifacts: ArtifactsPolicy
    process: ProcessPolicy
    constraints: ConstraintsPolicy
    knowledge: KnowledgePolicy
    fingerprint: str = ""

    @classmethod
    def from_dict(cls, raw: object, fingerprint: str = "") -> GovernanceConfig:
        d = v.as_mapping(raw, "governance")
        v.reject_unknown(
            d, ("version", "artifacts", "process", "constraints", "knowledge"), "governance"
        )
        a = v.as_mapping(d.get("artifacts", {}), "governance.artifacts")
        p = v.as_mapping(d.get("process", {}), "governance.process")
        c = v.as_mapping(d.get("constraints", {}), "governance.constraints")
        k = v.as_mapping(d.get("knowledge", {}), "governance.knowledge")
        v.reject_unknown(a, ("write_roots", "require_evidence_for_done"), "governance.artifacts")
        v.reject_unknown(
            p,
            ("lifecycle", "allowed_task_types", "human_approval_required",
             "min_top_level_authority", "overseer_may_issue_top_level"),
            "governance.process",
        )  # fmt: skip
        v.reject_unknown(
            c,
            ("allowed_actions", "max_recursion_depth", "max_inputs_bytes", "cost_ceiling_usd",
             "governance_mutable"),
            "governance.constraints",
        )  # fmt: skip
        v.reject_unknown(
            k,
            ("allowed_endpoints", "allow_sensitive_to_third_party", "cognition_is_advisory"),
            "governance.knowledge",
        )
        if v.b(c.get("governance_mutable", False), "constraints.governance_mutable"):
            raise ValidationError("governance_mutable must be false (mutation is disallowed)")
        if not v.b(k.get("cognition_is_advisory", True), "knowledge.cognition_is_advisory"):
            raise ValidationError("cognition_is_advisory must be true")
        cost = v.f(c.get("cost_ceiling_usd", 0.0), "constraints.cost_ceiling_usd")
        if cost != 0.0:
            raise ValidationError("HARD RULE: cost_ceiling_usd must be 0.0")
        actions = frozenset(v.str_list(c.get("allowed_actions", ["plan"]), "allowed_actions"))
        # Config may ADD to the human-approval set, never remove from the hard floor.
        human = HUMAN_APPROVAL_ACTIONS | frozenset(
            v.str_list(p.get("human_approval_required", []), "human_approval_required")
        )
        types = frozenset(v.str_list(p.get("allowed_task_types", list(TASK_TYPES)), "types"))
        for t in types:
            v.choice(t, TASK_TYPES, "process.allowed_task_types")
        return cls(
            version=v.i(d.get("version", 1), "governance.version", ge=1),
            artifacts=ArtifactsPolicy(
                write_roots=v.str_list(a.get("write_roots", ["artifacts"]), "write_roots"),
                require_evidence_for_done=v.b(
                    a.get("require_evidence_for_done", True), "require_evidence_for_done"
                ),
            ),
            process=ProcessPolicy(
                lifecycle=v.str_list(p.get("lifecycle", list(ProcessPolicy().lifecycle)), "lc"),
                allowed_task_types=types,
                human_approval_required=human,
                # bounded to the human range: config cannot let executors/cognition start work
                min_top_level_authority=v.i(
                    p.get("min_top_level_authority", 100),
                    "min_top_level_authority",
                    ge=50,
                    le=100,
                ),
                overseer_may_issue_top_level=v.b(
                    p.get("overseer_may_issue_top_level", False), "overseer_may_issue_top_level"
                ),
            ),
            constraints=ConstraintsPolicy(
                allowed_actions=actions,
                max_recursion_depth=v.i(
                    c.get("max_recursion_depth", 3), "max_recursion_depth", ge=0, le=16
                ),
                max_inputs_bytes=v.i(c.get("max_inputs_bytes", 65_536), "max_inputs_bytes", ge=1),
                cost_ceiling_usd=0.0,
                governance_mutable=False,
            ),
            knowledge=KnowledgePolicy(
                allowed_endpoints=frozenset(
                    v.str_list(k.get("allowed_endpoints", []), "allowed_endpoints")
                ),
                allow_sensitive_to_third_party=v.b(
                    k.get("allow_sensitive_to_third_party", False), "allow_sensitive"
                ),
                cognition_is_advisory=True,
            ),
            fingerprint=fingerprint,
        )
