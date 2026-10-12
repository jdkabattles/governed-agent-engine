"""Agent-level routing penalty.

Distinct from the per-cognition-MODEL doctrine penalty (``Doctrine.laziness_penalty`` applied
by the fallback chain when a model's PLAN fails the doctrine gate). This module scores the
governed AGENTS (grok, acquirer, analyst) on run completion, from the existing evidence /
artifact checks. A plan rejected before the backlog is the model's fault and never reaches
this module, so one failure is never counted twice.

Deterministic, derived only from ``config/doctrine.grok`` + ``config/agents/<bot>.grok``.
Overseer (``maximum`` = 0.25) is governance-only: recorded in audits, never routed, never
propagated. Records are a hash-chained JSONL ledger so Overseer can verify non-tampering.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .grok_format import GrokValue

PENALTY_MISMATCH = "PENALTY_MISMATCH"
UNAUTHORIZED_PENALTY_CHANGE = "UNAUTHORIZED_PENALTY_CHANGE"
PENALTY_DRIFT_DETECTED = "PENALTY_DRIFT_DETECTED"

ROUTING_LEVELS: dict[str, float] = {"low": 0.9, "medium": 0.75, "high": 0.5}
OVERSEER_LEVEL = ("maximum", 0.25)  # audit-only; never enters routing
ROUTED_AGENTS = ("grok", "acquirer", "analyst")
BASE_SCORE = 1.0
GENESIS = "0" * 64
RECORD_KEYS = (
    "agent", "laziness_detected", "penalty_level", "penalty_value", "base_score",
    "final_score", "doctrinal_source", "timestamp", "task_id", "evidence_path",
)  # fmt: skip
LEDGER = ("evidence", "routing", "routing_records.jsonl")


class PenaltyError(Exception):
    def __init__(self, message: str, code: str = PENALTY_MISMATCH) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Penalty:
    agent: str
    level: str
    value: float
    source: str  # "shared" | "agent-specific"


def _level(rules: GrokValue) -> GrokValue:
    return rules.get("LAZINESS_PENALTY") if isinstance(rules, dict) else None


def resolve(
    doctrine_grok: Mapping[str, GrokValue], pkgs: Mapping[str, Mapping[str, GrokValue]]
) -> dict[str, Penalty]:
    """Step 1: LAZINESS_PENALTY per routed agent -> numeric. Looser than shared, unknown, or
    the Overseer-only ``maximum`` on a routed agent -> PENALTY_MISMATCH (stops startup)."""
    shared = _level(doctrine_grok.get("RULES"))
    if not isinstance(shared, str) or shared not in ROUTING_LEVELS:
        raise PenaltyError(f"shared LAZINESS_PENALTY {shared!r} is not a routing level")
    out: dict[str, Penalty] = {}
    for agent in ROUTED_AGENTS:
        ovr = pkgs.get(agent, {}).get("DOCTRINE_OVERRIDE")
        lvl = _level(ovr.get("RULES") if isinstance(ovr, dict) else None)
        if lvl is None:
            out[agent] = Penalty(agent, shared, ROUTING_LEVELS[shared], "shared")
            continue
        if not isinstance(lvl, str) or lvl not in ROUTING_LEVELS:
            raise PenaltyError(f"{agent}: LAZINESS_PENALTY {lvl!r} not allowed for routing")
        if ROUTING_LEVELS[lvl] > ROUTING_LEVELS[shared]:
            raise PenaltyError(f"{agent}: LAZINESS_PENALTY {lvl!r} looser than shared {shared!r}")
        src = "shared" if lvl == shared else "agent-specific"
        out[agent] = Penalty(agent, lvl, ROUTING_LEVELS[lvl], src)
    return out


def agent_for(is_acquisition: bool, is_cognition: bool) -> str:
    return "acquirer" if is_acquisition else "analyst" if is_cognition else "grok"


def score(penalty: Penalty, base_score: float, laziness_detected: bool) -> float:
    """Step 3: lazy -> base * penalty, else base. Rounded for determinism."""
    if penalty.agent not in ROUTED_AGENTS:
        raise PenaltyError(f"{penalty.agent} never enters routing")
    return round(base_score * penalty.value, 6) if laziness_detected else base_score


def _digest(rec: Mapping[str, Any]) -> str:
    body = {k: v for k, v in rec.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


digest = _digest


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


read_rows = _read


def record(
    path: Path, penalty: Penalty, laziness_detected: bool, timestamp: str,
    task_id: str, base_score: float = BASE_SCORE, evidence_path: str = "",
) -> dict[str, Any]:  # fmt: skip
    """Step 4: append a ROUTING_RECORD to the hash-chained ledger."""
    rows = _read(path)
    rec: dict[str, Any] = {
        "agent": penalty.agent, "laziness_detected": laziness_detected,
        "penalty_level": penalty.level, "penalty_value": penalty.value,
        "base_score": base_score, "final_score": score(penalty, base_score, laziness_detected),
        "doctrinal_source": penalty.source, "timestamp": timestamp, "task_id": task_id,
        "evidence_path": evidence_path,
        "prev_hash": rows[-1]["hash"] if rows else GENESIS,
    }  # fmt: skip
    rec["hash"] = _digest(rec)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
    return rec


def verify(path: Path, table: Mapping[str, Penalty]) -> list[tuple[str, str]]:
    """Step 5: ledger complete + non-tampered + consistent with the approved .grok table."""
    out: list[tuple[str, str]] = []
    try:
        rows = _read(path)
    except (ValueError, OSError) as exc:
        return [(UNAUTHORIZED_PENALTY_CHANGE, f"routing ledger unreadable: {exc}")]
    prev = GENESIS
    for i, r in enumerate(rows, 1):
        where = f"routing record {i}"
        if set(r) != {*RECORD_KEYS, "prev_hash", "hash"}:
            out.append((PENALTY_MISMATCH, f"{where}: incomplete or unknown fields"))
            continue
        if r["prev_hash"] != prev or r["hash"] != _digest(r):
            out.append((UNAUTHORIZED_PENALTY_CHANGE, f"{where}: hash chain broken (tampered)"))
        prev = r["hash"]
        p = table.get(r["agent"])
        if p is None:
            out.append((PENALTY_MISMATCH, f"{where}: agent {r['agent']!r} never enters routing"))
            continue
        if (r["penalty_level"], r["penalty_value"], r["doctrinal_source"]) != (
            p.level, p.value, p.source,
        ):  # fmt: skip
            out.append((UNAUTHORIZED_PENALTY_CHANGE, f"{where}: penalty differs from .grok"))
        elif r["final_score"] != score(p, r["base_score"], bool(r["laziness_detected"])):
            out.append((PENALTY_MISMATCH, f"{where}: final_score inconsistent with detection"))
    return out


def current_scores(path: Path) -> dict[str, float]:
    """Latest routing score per routed agent (BASE_SCORE when no record yet)."""
    scores = dict.fromkeys(ROUTED_AGENTS, BASE_SCORE)
    for r in _read(path):
        if r.get("agent") in scores:
            scores[r["agent"]] = float(r["final_score"])
    return scores
