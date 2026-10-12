"""Binding of the owner's ``.grok`` spec to the native runtime (binding spec 2026-10-11).

* Native code is primary and executable (``core/identity.py``, ``config/doctrine.yaml``); the
  ``.grok`` files are the read-only constitutional mirror and the audit source of truth.
* Startup binds identity, doctrine and directive for every agent package
  (``config/agents/<bot>.grok``) and fails fast with a named ``ErrorCode``.
* ``audit`` (Overseer Agent) runs the same checks without stopping at the first finding and
  writes evidence; ``DriftMonitor`` re-checks the charter fingerprint while running and only
  accepts a change backed by a migration record signed off by Owner.
Kept identical (apart from imports) in example-executor-backend and governed-agent-engine.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from governed_agent_engine.core import agent_selection, identity, routing_penalty
from governed_agent_engine.core.acquisition import ACQUISITION_POLICY
from governed_agent_engine.core.charter import (
    CharterError,
    charter_fingerprint,
    load_charter,
    read_grok,
)
from governed_agent_engine.core.doctrine import DOCTRINE_STATEMENT, load_doctrine
from governed_agent_engine.core.grok_format import GrokValue
from governed_agent_engine.core.identity import AgentIdentity
from governed_agent_engine.core.scopes import (
    COGNITION_POLICY,
    COGNITION_REQUIREMENTS,
    GOVERNANCE_POLICY,
    OVERSEER_DOCTRINE_OVERRIDE,
    OVERSIGHT_REQUIREMENTS,
)

DOMAIN_BLOCKS: dict[str, tuple[str, dict[str, Any]]] = {
    "acquirer": ("ACQUISITION", ACQUISITION_POLICY),
    "overseer": ("GOVERNANCE", GOVERNANCE_POLICY),
    "analyst": ("COGNITION", COGNITION_POLICY),
}
PACKAGE_AGENTS: tuple[str, ...] = ("overseer", "grok", "acquirer", "analyst")
CLARIFY_WITH = ["Owner", "Overseer Agent"]
TRUTH_PRIORITIES = ("low", "medium", "high", "highest")
# LAZINESS_PENALTY level -> largest native utility multiplier that still honours it.
LAZINESS_MAX_FACTOR = {"low": 0.9, "medium": 0.75, "high": 0.5, "maximum": 0.25}
IMPROVEMENT_LOOPS = ("optional", "recommended", "mandatory")
# Strictness order per rule (later = stricter); booleans: True is stricter than False.
RULE_ORDER: dict[str, tuple[GrokValue, ...]] = {
    "TRUTH_PRIORITY": TRUTH_PRIORITIES,
    "EVIDENCE_REQUIRED": (False, True),
    "LAZINESS_PENALTY": tuple(LAZINESS_MAX_FACTOR),
    "FAILURE_ANALYSIS_REQUIRED": (False, True),
    "IMPROVEMENT_LOOP": IMPROVEMENT_LOOPS,
}
# Agent-level extras: DOCTRINE_OVERRIDE (any agent, stricter only); Overseer must carry both.
AGENT_EXTRA_KEYS: dict[str, set[str]] = {
    "overseer": {"DOCTRINE_OVERRIDE", "OVERSIGHT_REQUIREMENTS"},
    "analyst": {"COGNITION_REQUIREMENTS"},
}
PACKAGE_KEYS = {
    "FORMAT", "AGENT", "IDENTITY", "DOCTRINE", "DIRECTIVE", "DIRECTIVE_SOURCE", "RESPONSE_WINDOW",
}  # fmt: skip
# Agents whose DIRECTIVE is the owner's verbatim text; the others are derived placeholders.
OWNER_DIRECTIVES = frozenset({"grok", "acquirer", "overseer", "analyst"})
# Topic routing for clarification (required in packages that declare it, e.g. acquirer).
CLARIFY_TOPICS: dict[str, dict[str, list[str]]] = {
    "overseer": {
        "CLARIFY_OWNER_ON": [
            "governance_rule",
            "routing_decision",
            "doctrine_interpretation",
            "compliance_conflict",
        ],
    },
    "analyst": {
        "CLARIFY_OWNER_ON": ["reasoning_task", "data_interpretation", "refinement_requirement"],
        "CLARIFY_OVERSEER_ON": ["governance", "routing"],
    },
    "acquirer": {
        "CLARIFY_OWNER_ON": ["acquisition_task", "source", "schema", "apify_usage_pattern"],
        "CLARIFY_OVERSEER_ON": ["governance", "routing", "health"],
    },
}
MIGRATION_KEYS = {
    "FORMAT", "KIND", "FROM_FINGERPRINT", "TO_FINGERPRINT", "APPROVED_BY", "SIGNED_OFF",
    "REASON", "DATE",
}  # fmt: skip
_HEX64 = re.compile(r"[0-9a-f]{64}")


class ErrorCode(StrEnum):
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    DOCTRINE_MISMATCH = "DOCTRINE_MISMATCH"
    DIRECTIVE_MISMATCH = "DIRECTIVE_MISMATCH"
    ACQUISITION_MISMATCH = "ACQUISITION_MISMATCH"
    GOVERNANCE_MISMATCH = "GOVERNANCE_MISMATCH"
    COGNITION_MISMATCH = "COGNITION_MISMATCH"
    NON_COMPLIANT = "NON_COMPLIANT"
    DOCTRINE_DRIFT = "DOCTRINE_DRIFT"
    CLARIFICATION_RULE_MISSING = "CLARIFICATION_RULE_MISSING"
    UNAUTHORIZED_GROK_CHANGE = "UNAUTHORIZED_GROK_CHANGE"
    PENALTY_MISMATCH = "PENALTY_MISMATCH"
    UNAUTHORIZED_PENALTY_CHANGE = "UNAUTHORIZED_PENALTY_CHANGE"
    PENALTY_DRIFT_DETECTED = "PENALTY_DRIFT_DETECTED"


class NativeDoctrine(Protocol):
    @property
    def statement(self) -> str: ...
    @property
    def truth_priority(self) -> int: ...
    @property
    def evidence_required(self) -> bool: ...
    @property
    def laziness_penalty(self) -> float: ...
    @property
    def failure_analysis_required(self) -> bool: ...
    @property
    def improvement_loop(self) -> bool: ...


Finding = tuple[ErrorCode, str]


def _get(d: GrokValue, *path: str) -> GrokValue:
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def check_native_doctrine(
    doctrine_grok: dict[str, GrokValue], native: NativeDoctrine
) -> list[Finding]:
    """Every enforced native rule must be derivable from doctrine.grok RULES."""
    out: list[Finding] = []
    bad = ErrorCode.DOCTRINE_MISMATCH
    if doctrine_grok.get("STATEMENT") != native.statement:
        out.append((bad, "STATEMENT differs from the native doctrine"))
    rules = doctrine_grok.get("RULES")
    if not isinstance(rules, dict):
        return [*out, (bad, "RULES missing")]
    tp, lp, il = (
        rules.get("TRUTH_PRIORITY"),
        rules.get("LAZINESS_PENALTY"),
        rules.get("IMPROVEMENT_LOOP"),
    )
    if tp not in TRUTH_PRIORITIES or (native.truth_priority == 1) != (tp in ("high", "highest")):
        out.append((bad, f"TRUTH_PRIORITY {tp!r} does not match native truth_priority"))
    if rules.get("EVIDENCE_REQUIRED") is not native.evidence_required:
        out.append((bad, "EVIDENCE_REQUIRED does not match native evidence_required"))
    if (
        not isinstance(lp, str)
        or lp not in LAZINESS_MAX_FACTOR
        or native.laziness_penalty > LAZINESS_MAX_FACTOR[lp]
    ):
        out.append(
            (
                bad,
                f"LAZINESS_PENALTY {lp!r} not honoured by native factor {native.laziness_penalty}",
            )
        )
    if rules.get("FAILURE_ANALYSIS_REQUIRED") is not native.failure_analysis_required:
        out.append((bad, "FAILURE_ANALYSIS_REQUIRED does not match native"))
    if il not in IMPROVEMENT_LOOPS or (il == "mandatory") != native.improvement_loop:
        out.append((bad, f"IMPROVEMENT_LOOP {il!r} does not match native improvement_loop"))
    return out


def _directive_findings(text: str, agent: AgentIdentity) -> list[Finding]:
    """The freeform DIRECTIVE may not contradict native authority, type or role."""
    bad = ErrorCode.DIRECTIVE_MISMATCH
    out: list[Finding] = []
    flat = " ".join(text.replace('"', " ").split())
    if f"You are {agent.display_name}." not in text:
        out.append((bad, f"DIRECTIVE does not address {agent.display_name}"))
    for key, want in (("agent_name", agent.display_name), ("agent_type", agent.type.value),
                      ("authority_level", str(agent.authority_level))):  # fmt: skip
        m = re.search(rf"^\s*- {key}: (.+)$", text, re.M)
        if m is None or m.group(1).strip() != want:
            out.append((bad, f"DIRECTIVE {key} must be {want!r}"))
    m = re.search(r"Owner holds the highest authority level \((\d+)\)", text)
    if m is None or int(m.group(1)) != identity.HUMAN_OWNER.authority_level:
        out.append((bad, "DIRECTIVE must state the owner's authority level (100)"))
    for lvl in re.findall(r"Overseer Agent \(authority_level: (\d+)\)", text):
        if int(lvl) != identity.OVERSEER_AUTHORITY:
            out.append((bad, "DIRECTIVE states a wrong Overseer Agent authority level"))
    if DOCTRINE_STATEMENT not in flat:
        out.append((bad, "DIRECTIVE must quote the doctrine verbatim"))
    if "Never override Owner." not in text:
        out.append((bad, "DIRECTIVE must say 'Never override Owner.'"))
    return out


def check_override(doctrine_grok: dict[str, GrokValue], override: GrokValue) -> list[Finding]:
    """An agent-level DOCTRINE_OVERRIDE may only be STRICTER than the shared doctrine."""
    bad = ErrorCode.DOCTRINE_MISMATCH
    rules = _get(override, "RULES")
    shared = doctrine_grok.get("RULES")
    if not isinstance(override, dict) or set(override) != {"RULES"} or not isinstance(rules, dict):
        return [(bad, "DOCTRINE_OVERRIDE must be a mapping with RULES")]
    if not isinstance(shared, dict):
        return [(bad, "shared RULES missing")]
    out: list[Finding] = []
    for key, val in rules.items():
        order = RULE_ORDER.get(key)
        if order is None or val not in order or shared.get(key) not in order:
            out.append((bad, f"DOCTRINE_OVERRIDE {key}={val!r} is not a known rule value"))
        elif order.index(val) < order.index(shared[key]):
            out.append(
                (bad, f"DOCTRINE_OVERRIDE {key}={val!r} is looser than shared {shared[key]!r}")
            )
    return out


def effective_rules(
    doctrine_grok: dict[str, GrokValue], pkg: dict[str, GrokValue]
) -> dict[str, GrokValue]:
    """Shared RULES with the agent's (stricter) override applied."""
    shared = doctrine_grok.get("RULES")
    rules: dict[str, GrokValue] = dict(shared) if isinstance(shared, dict) else {}
    ovr = _get(pkg.get("DOCTRINE_OVERRIDE"), "RULES")
    if isinstance(ovr, dict):
        rules.update(ovr)
    return rules


def laziness_factor(rules: dict[str, GrokValue]) -> float:
    lp = rules.get("LAZINESS_PENALTY")
    return LAZINESS_MAX_FACTOR[lp] if isinstance(lp, str) and lp in LAZINESS_MAX_FACTOR else 1.0


def check_package(
    pkg: dict[str, GrokValue],
    bot: str,
    doctrine_grok: dict[str, GrokValue],
    drift_code: ErrorCode = ErrorCode.DOCTRINE_MISMATCH,
    purposes: dict[str, str] | None = None,
) -> list[Finding]:
    """Bind one ``config/agents/<bot>.grok`` package to the native identity and doctrine."""
    agent = identity.KNOWN[bot]
    out: list[Finding] = []
    idm = ErrorCode.IDENTITY_MISMATCH
    domain = DOMAIN_BLOCKS.get(bot)
    want_keys = PACKAGE_KEYS | ({domain[0]} if domain else set()) | AGENT_EXTRA_KEYS.get(bot, set())
    extra_ok = set(pkg) == want_keys | {"DOCTRINE_OVERRIDE"}  # a stricter override is allowed
    if (set(pkg) != want_keys and not extra_ok) or pkg.get("AGENT") != bot:
        out.append((idm, f"package keys must be {sorted(want_keys)} with AGENT {bot!r}"))
    source = "owner" if bot in OWNER_DIRECTIVES else "derived"
    if pkg.get("DIRECTIVE_SOURCE") != source:
        out.append((ErrorCode.DIRECTIVE_MISMATCH, f"DIRECTIVE_SOURCE must be {source!r}"))
    if domain and pkg.get(domain[0]) != domain[1]:
        msg = f"{domain[0]} block differs from the native {domain[0].lower()} policy"
        out.append((ErrorCode(f"{domain[0]}_MISMATCH"), msg))
    want_id = {
        "NAME": agent.display_name, "TYPE": agent.type.value,
        "AUTHORITY_LEVEL": agent.authority_level,
        "HUMAN_AUTHORITY": {"NAME": "Owner",
                            "AUTHORITY_LEVEL": identity.HUMAN_OWNER.authority_level},
        "OVERSEER_AUTHORITY": {"NAME": "Overseer Agent",
                               "AUTHORITY_LEVEL": identity.OVERSEER_AUTHORITY},
    }  # fmt: skip
    ident = pkg.get("IDENTITY")
    desc = _get(ident, "DESCRIPTION")
    if not isinstance(ident, dict) or not isinstance(desc, str) or not desc.strip():
        out.append((idm, "IDENTITY.DESCRIPTION must be a non-empty string"))
    elif purposes is not None and " ".join(desc.split()) != " ".join(purposes.get(bot, "").split()):
        out.append((idm, f"IDENTITY.DESCRIPTION differs from identities/{bot}.yaml purpose"))
    if isinstance(ident, dict):
        got = {k: v for k, v in ident.items() if k != "DESCRIPTION"}
        if got != want_id:
            out.append((idm, f"IDENTITY does not match native {bot}: {got}"))
    if "DOCTRINE_OVERRIDE" in pkg:
        out += check_override(doctrine_grok, pkg["DOCTRINE_OVERRIDE"])
    if bot == "overseer":
        ovr = _get(pkg.get("DOCTRINE_OVERRIDE"), "RULES")
        if (
            ovr != OVERSEER_DOCTRINE_OVERRIDE
            or pkg.get("OVERSIGHT_REQUIREMENTS") != OVERSIGHT_REQUIREMENTS
        ):
            msg = (
                "Overseer DOCTRINE_OVERRIDE / OVERSIGHT_REQUIREMENTS differ from the native copies"
            )
            out.append((ErrorCode.GOVERNANCE_MISMATCH, msg))
    if bot == "analyst" and pkg.get("COGNITION_REQUIREMENTS") != COGNITION_REQUIREMENTS:
        msg = "ANALYST COGNITION_REQUIREMENTS differ from the native copy"
        out.append((ErrorCode.COGNITION_MISMATCH, msg))
    doc = pkg.get("DOCTRINE")
    want_doc = {k: doctrine_grok.get(k) for k in ("STATEMENT", "RULES", "OPERATIONAL_REQUIREMENTS")}
    if doc != want_doc:
        out.append((drift_code, "DOCTRINE differs from config/doctrine.grok"))
    human = identity.HUMAN_OWNER.authority_level
    ov = identity.OVERSEER_AUTHORITY
    if not (
        human > ov >= agent.authority_level and (agent.authority_level < ov or bot == "overseer")
    ):
        out.append((ErrorCode.DIRECTIVE_MISMATCH, "authority must be Owner > Overseer > agent"))
    directive = pkg.get("DIRECTIVE")
    if not isinstance(directive, str) or not directive.strip():
        out.append((ErrorCode.DIRECTIVE_MISMATCH, "DIRECTIVE must be a non-empty string"))
    else:
        out.extend(_directive_findings(directive, agent))
    rw = pkg.get("RESPONSE_WINDOW")
    clarify_with = ["Owner"] if bot == "overseer" else list(CLARIFY_WITH)
    want_rw: dict[str, GrokValue] = {
        "CLARIFICATION_REQUIRED": True,
        "CLARIFY_WITH": [*clarify_with],
    }
    want_rw.update({k: list(v) for k, v in CLARIFY_TOPICS.get(bot, {}).items()})
    if isinstance(rw, dict) and "TEXT" in rw:
        text = rw.get("TEXT")
        flat = " ".join(str(text).split())
        if not isinstance(text, str) or any(f"ask {w}" not in flat for w in clarify_with):
            rw = None
        else:
            rw = {k: v for k, v in rw.items() if k != "TEXT"}
    if rw != want_rw:
        msg = "RESPONSE_WINDOW must require clarification with Owner, then Overseer Agent"
        out.append((ErrorCode.CLARIFICATION_RULE_MISSING, msg))
    return out


def _shared_directive_findings(
    root: Path, packages: dict[str, dict[str, GrokValue]]
) -> list[Finding]:
    """config/directive.grok (Executor Agent's directive) must equal the grok package and keep
    the clarification rule in its RESPONSE_WINDOW text."""
    d = read_grok(root / "config" / "directive.grok", ErrorCode.DIRECTIVE_MISMATCH)
    out: list[Finding] = []
    if d.get("STATUS") == "active":
        if packages.get("grok", {}).get("DIRECTIVE") != d.get("DIRECTIVE"):
            out.append(
                (
                    ErrorCode.DIRECTIVE_MISMATCH,
                    "agents/grok.grok DIRECTIVE != config/directive.grok",
                )
            )
        window = str(d.get("RESPONSE_WINDOW") or "")
        if "ask Owner" not in window or "ask Overseer Agent" not in window:
            msg = "RESPONSE_WINDOW must route clarification to Owner and Overseer Agent"
            out.append((ErrorCode.CLARIFICATION_RULE_MISSING, msg))
    return out


def _load_packages(root: Path) -> tuple[dict[str, dict[str, GrokValue]], list[Finding]]:
    pkgs: dict[str, dict[str, GrokValue]] = {}
    out: list[Finding] = []
    folder = root / "config" / "agents"
    present = {p.stem for p in folder.glob("*.grok")} if folder.is_dir() else set()
    for extra in sorted(present - set(PACKAGE_AGENTS)):
        out.append((ErrorCode.IDENTITY_MISMATCH, f"unknown agent package {extra}.grok"))
    for bot in PACKAGE_AGENTS:
        try:
            pkgs[bot] = read_grok(folder / f"{bot}.grok", ErrorCode.IDENTITY_MISMATCH)
        except CharterError as exc:
            out.append((ErrorCode.IDENTITY_MISMATCH, str(exc)))
    return pkgs, out


def _purposes(root: Path) -> dict[str, str] | None:
    try:
        return {n: b.purpose for n, b in load_charter(root).bots.items()}
    except CharterError:
        return None


def _penalty_findings(
    doctrine_grok: dict[str, GrokValue], pkgs: dict[str, dict[str, GrokValue]]
) -> list[Finding]:
    try:
        routing_penalty.resolve(doctrine_grok, pkgs)
        agent_selection.window_size(doctrine_grok)
    except routing_penalty.PenaltyError as exc:
        return [(ErrorCode.PENALTY_MISMATCH, str(exc))]
    return []


def penalty_table(root: Path) -> dict[str, routing_penalty.Penalty]:
    """Routing penalties derived from the .grok files (raises CharterError PENALTY_MISMATCH)."""
    doctrine_grok = read_grok(root / "config" / "doctrine.grok", ErrorCode.PENALTY_MISMATCH)
    pkgs, _ = _load_packages(root)
    try:
        return routing_penalty.resolve(doctrine_grok, pkgs)
    except routing_penalty.PenaltyError as exc:
        raise CharterError(str(exc), exc.code) from exc


@dataclass(frozen=True)
class SelectionContext:
    table: dict[str, routing_penalty.Penalty]
    window: int
    unblocks: list[agent_selection.Unblock]


def selection_context(root: Path) -> SelectionContext:
    """Penalties, window and unblock records, all derived from files Owner controls."""
    doctrine_grok = read_grok(root / "config" / "doctrine.grok", ErrorCode.PENALTY_MISMATCH)
    try:
        return SelectionContext(
            penalty_table(root), agent_selection.window_size(doctrine_grok),
            agent_selection.read_unblocks(root),
        )  # fmt: skip
    except routing_penalty.PenaltyError as exc:
        raise CharterError(str(exc), exc.code) from exc


def ledger_findings(root: Path, artifacts: Path, ctx: SelectionContext) -> list[Finding]:
    """Run ledger + selection ledger: complete, untampered, consistent (stop routing if any)."""
    runs_path = artifacts.joinpath(*routing_penalty.LEDGER)
    out = routing_penalty.verify(runs_path, ctx.table)
    try:
        runs = routing_penalty.read_rows(runs_path)
    except (ValueError, OSError):
        runs = []
    out += agent_selection.verify(
        artifacts.joinpath(*agent_selection.SELECTION_LEDGER), runs, ctx.table, ctx.window,
        ctx.unblocks, artifacts,
    )  # fmt: skip
    out += agent_selection.block_problems(artifacts, runs, ctx.table, ctx.window, ctx.unblocks)
    return [(ErrorCode(c), m) for c, m in out]


def bind(root: Path, native: NativeDoctrine) -> dict[str, AgentIdentity]:
    """Startup binding; raises ``CharterError`` with the first named code. Returns the one
    runtime ``AgentIdentity`` per agent (always the native object)."""
    doctrine_grok = read_grok(root / "config" / "doctrine.grok", ErrorCode.DOCTRINE_MISMATCH)
    findings = check_native_doctrine(doctrine_grok, native)
    pkgs, load_findings = _load_packages(root)
    findings += load_findings
    for bot, pkg in pkgs.items():
        findings += check_package(pkg, bot, doctrine_grok, purposes=_purposes(root))
    findings += _shared_directive_findings(root, pkgs)
    findings += _penalty_findings(doctrine_grok, pkgs)
    if findings:
        code, msg = findings[0]
        raise CharterError(msg, code.value)
    return {bot: identity.KNOWN[bot] for bot in PACKAGE_AGENTS}


@dataclass
class AuditReport:
    at: str
    auditor: str
    charter_fingerprint: str
    agents: dict[str, dict[str, object]] = field(default_factory=dict)
    global_findings: list[dict[str, str]] = field(default_factory=list)
    evaluator: dict[str, object] = field(default_factory=dict)
    routing: dict[str, object] = field(default_factory=dict)

    @property
    def compliant(self) -> bool:
        return not self.global_findings and all(
            a["status"] == "COMPLIANT" for a in self.agents.values()
        )

    def to_dict(self) -> dict[str, object]:
        return {"kind": "grok_audit", "at": self.at, "auditor": self.auditor,
                "charter_fingerprint": self.charter_fingerprint, "compliant": self.compliant,
                "agents": self.agents, "global_findings": self.global_findings,
                "evaluator": self.evaluator, "routing_penalties": self.routing}  # fmt: skip


def audit(
    root: Path, native: NativeDoctrine, now: datetime, artifacts: Path | None = None
) -> AuditReport:
    """Overseer Agent audit: identity per agent (NON_COMPLIANT blocks execution), doctrine
    (DOCTRINE_DRIFT), directive hierarchy and clarification rule. Collects every finding."""
    report = AuditReport(
        now.isoformat(), identity.OVERSEER_OVERSEER.name, charter_fingerprint(root)
    )
    glob: list[Finding] = []
    purposes = _purposes(root)
    try:
        load_charter(root)
    except CharterError as exc:
        known = exc.code in ErrorCode.__members__
        glob.append((ErrorCode(exc.code) if known else ErrorCode.NON_COMPLIANT, str(exc)))
    try:
        doctrine_grok = read_grok(root / "config" / "doctrine.grok", ErrorCode.DOCTRINE_DRIFT)
    except CharterError as exc:
        doctrine_grok = {}
        glob.append((ErrorCode.DOCTRINE_DRIFT, str(exc)))
    glob += [(ErrorCode.DOCTRINE_DRIFT, m) for _, m in check_native_doctrine(doctrine_grok, native)]
    pkgs, load_findings = _load_packages(root)
    glob += [f for f in load_findings if "unknown agent package" in f[1]]
    try:
        glob += _shared_directive_findings(root, pkgs)
    except CharterError as exc:
        glob.append((ErrorCode.DIRECTIVE_MISMATCH, str(exc)))
    for bot in PACKAGE_AGENTS:
        if bot in pkgs:
            f = check_package(pkgs[bot], bot, doctrine_grok, ErrorCode.DOCTRINE_DRIFT, purposes)
        else:
            f = [x for x in load_findings if f"{bot}.grok" in x[1]]
        report.agents[bot] = {
            "status": "NON_COMPLIANT" if f else "COMPLIANT",
            "findings": [{"code": c.value, "detail": m} for c, m in f],
        }
    # routing penalties: match .grok, never looser, ledger complete + non-tampered
    pf = _penalty_findings(doctrine_grok, pkgs)
    glob += pf
    if not pf:
        art = artifacts or root / "artifacts"
        try:
            ctx = selection_context(root)
        except CharterError as exc:
            glob.append((ErrorCode(exc.code), str(exc)))
        else:
            glob += ledger_findings(root, art, ctx)
            report.routing = {a: vars(p) for a, p in ctx.table.items()}
            states: dict[str, object]
            try:
                runs = routing_penalty.read_rows(art.joinpath(*routing_penalty.LEDGER))
                states = {
                    s: agent_selection.select(
                        s, ctx.table, runs, ctx.window, ctx.unblocks
                    ).summary()
                    for s in agent_selection.SCOPES
                }
            except (ValueError, OSError, routing_penalty.PenaltyError) as exc:
                states = {"error": str(exc)}
            report.routing["agent_selection"] = {"window": ctx.window, "states": states}
    report.global_findings = [{"code": c.value, "detail": m} for c, m in glob]
    # Overseer evaluates with its own (stricter) rules: laziness multiplier 0.25 per audit.
    rules = effective_rules(doctrine_grok, pkgs.get("overseer", {}))
    factor = laziness_factor(rules)
    report.evaluator = {"agent": "overseer", "rules": rules, "laziness_factor": factor,
                        "enters_routing": False}  # fmt: skip
    for a in report.agents.values():
        a["penalty_factor"] = factor if a["status"] == "NON_COMPLIANT" else 1.0
    return report


def write_audit(report: AuditReport, artifacts: Path) -> Path:
    out = artifacts / "evidence" / "audits"
    out.mkdir(parents=True, exist_ok=True)
    stamp = report.at.replace(":", "").replace("-", "").replace("+", "_")
    path = out / f"audit-{stamp}.json"
    path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


@dataclass(frozen=True)
class Migration:
    path: Path
    from_fingerprint: str
    to_fingerprint: str
    reason: str
    date: str


def read_migration(path: Path) -> Migration:
    """A migration record (``migrations/*.grok``) signed off by Owner. It is an attested
    record committed by Owner, not a cryptographic signature."""
    m = read_grok(path, ErrorCode.UNAUTHORIZED_GROK_CHANGE)
    code = ErrorCode.UNAUTHORIZED_GROK_CHANGE.value
    if set(m) != MIGRATION_KEYS or m.get("KIND") != "grok_migration":
        raise CharterError(f"{path.name}: migration keys must be {sorted(MIGRATION_KEYS)}", code)
    if m.get("APPROVED_BY") != identity.HUMAN_OWNER.name or m.get("SIGNED_OFF") is not True:
        raise CharterError(f"{path.name}: migration must be signed off by owner", code)
    fr, to, reason, date = (
        m.get(k) for k in ("FROM_FINGERPRINT", "TO_FINGERPRINT", "REASON", "DATE")
    )
    if not (
        isinstance(fr, str)
        and _HEX64.fullmatch(fr)
        and isinstance(to, str)
        and _HEX64.fullmatch(to)
    ):
        raise CharterError(f"{path.name}: fingerprints must be sha256 hex", code)
    if not (isinstance(reason, str) and reason.strip() and isinstance(date, str) and date.strip()):
        raise CharterError(f"{path.name}: REASON and DATE are required", code)
    return Migration(path, fr, to, reason, date)


class DriftMonitor:
    """Periodic reload check. A changed charter (identities/, doctrine/, config/*.grok,
    config/agents/*.grok) halts with UNAUTHORIZED_GROK_CHANGE unless ``migrations/`` holds a
    record signed off by Owner for exactly this FROM -> TO change and the new files bind."""

    def __init__(self, root: Path, fingerprint: str, validate: Callable[[], None]) -> None:
        self.root = root
        self.fingerprint = fingerprint
        self.validate = validate
        self.applied: list[Migration] = []
        self.penalties = self._penalties()

    def _penalties(self) -> dict[str, routing_penalty.Penalty] | None:
        try:
            return penalty_table(self.root)
        except CharterError:
            return None

    def check(self) -> Migration | None:
        current = charter_fingerprint(self.root)
        if current == self.fingerprint:
            return None
        code = ErrorCode.UNAUTHORIZED_GROK_CHANGE.value
        folder = self.root / "migrations"
        for path in sorted(folder.glob("*.grok")) if folder.is_dir() else []:
            mig = read_migration(path)
            if mig.from_fingerprint == self.fingerprint and mig.to_fingerprint == current:
                self.validate()
                self.fingerprint = current
                self.penalties = self._penalties()
                self.applied.append(mig)
                return mig
        if self._penalties() != self.penalties:  # routing stops; reported to Overseer
            raise CharterError(
                "routing penalties changed while running without a migration signed off by "
                "owner. Routing stopped.", ErrorCode.PENALTY_DRIFT_DETECTED.value,
            )  # fmt: skip
        raise CharterError(
            f"charter changed while running ({self.fingerprint[:12]} -> {current[:12]}) "
            "without a migration signed off by owner. Restart required.", code,
        )  # fmt: skip


def run_audit(root: Path, now: datetime | None = None) -> tuple[AuditReport, Path]:
    """``audit`` CLI: Overseer Agent audit written to artifacts/evidence/audits/."""
    native = load_doctrine(root / "config" / "doctrine.yaml")
    report = audit(root, native, now or datetime.now(UTC))
    return report, write_audit(report, root / "artifacts")


def audit_summary(report: AuditReport, path: Path) -> dict[str, object]:
    return {
        "compliant": report.compliant, "evidence": str(path),
        "agents": {b: a["status"] for b, a in report.agents.items()},
        "findings": [*report.global_findings,
                     *(f for a in report.agents.values() for f in a["findings"])],  # type: ignore[attr-defined]
    }  # fmt: skip
