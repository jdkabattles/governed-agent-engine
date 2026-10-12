"""Agent selection.

Selects which governed agent may take a task in its scope: execution -> grok, acquisition ->
acquirer, cognition -> analyst. Overseer is never selectable. Separate from the model-selection
router (cognition fallback chain).

* Window: the last N completed runs of that agent in the run ledger (``routing_penalty``), N
  from ``config/doctrine.grok`` ``AGENT_SELECTION.WINDOW`` (default 10). Changing it is a .grok
  change and needs a migration.
* Score: ``penalty_value ** lazy_in_window``. The penalty comes from .grok, which is the shared
  ``high`` = 0.5, so this equals the owner's ``0.5 ** lazy``. Honest failures are not runs in the
  ledger.
* Blocked: once the score drops below 0.50 (2+ lazy in the window) the agent stays blocked, even
  after lazy runs age out, until a Owner-signed unblock record
  (``migrations/unblocks/*.grok``) resets its window start.
* Every decision is a hash-chained record in ``artifacts/evidence/routing/selection_records.jsonl``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import routing_penalty as rp
from .grok_format import GrokFormatError, GrokValue, parse_grok

SCOPES: dict[str, str] = {"execution": "grok", "acquisition": "acquirer", "cognition": "analyst"}
DEFAULT_WINDOW = 10
THRESHOLD = 0.5
SELECTION_LEDGER = ("evidence", "routing", "selection_records.jsonl")
BLOCK_LEDGER = ("evidence", "routing", "block_records.jsonl")
UNBLOCK_DIR = ("migrations", "unblocks")
UNBLOCK_KEYS = frozenset(
    {"FORMAT", "KIND", "AGENT", "RESET_AFTER_HASH", "APPROVED_BY", "SIGNED_OFF", "REASON", "DATE"}
)
SELECTION_KEYS = (
    "task_id", "scope", "agent_selected", "base_score", "laziness_detected", "penalty_level",
    "penalty_value", "routing_score", "eligibility", "window", "timestamp", "run_log_head",
    "unblocks_applied", "block_report",
)  # fmt: skip


def scope_for(is_acquisition: bool, is_cognition: bool) -> str:
    return "acquisition" if is_acquisition else "cognition" if is_cognition else "execution"


def window_size(doctrine_grok: Mapping[str, GrokValue]) -> int:
    block = doctrine_grok.get("AGENT_SELECTION")
    if block is None:
        return DEFAULT_WINDOW
    n = block.get("WINDOW") if isinstance(block, dict) else None
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise rp.PenaltyError(f"AGENT_SELECTION.WINDOW {n!r} must be a positive integer")
    return n


@dataclass(frozen=True)
class Unblock:
    agent: str
    reset_after_hash: str
    source: str


def read_unblocks(root: Path) -> list[Unblock]:
    """Owner-signed unblock records (attested records, not cryptographic signatures)."""
    folder = root.joinpath(*UNBLOCK_DIR)
    out: list[Unblock] = []
    for path in sorted(folder.glob("*.grok")) if folder.is_dir() else []:
        bad = f"{path.name}: invalid unblock record"
        try:
            m = parse_grok(path.read_text(encoding="utf-8"))
        except GrokFormatError as exc:
            raise rp.PenaltyError(f"{bad}: {exc}", rp.UNAUTHORIZED_PENALTY_CHANGE) from exc
        ok = (
            set(m) == UNBLOCK_KEYS and m.get("KIND") == "agent_unblock"
            and m.get("APPROVED_BY") == "owner" and m.get("SIGNED_OFF") is True
            and m.get("AGENT") in SCOPES.values() and isinstance(m.get("RESET_AFTER_HASH"), str)
        )  # fmt: skip
        if not ok:
            raise rp.PenaltyError(bad, rp.UNAUTHORIZED_PENALTY_CHANGE)
        out.append(Unblock(str(m["AGENT"]), str(m["RESET_AFTER_HASH"]), path.name))
    return out


@dataclass
class AgentState:
    agent: str
    window_size: int
    score: float = 1.0
    lazy_count: int = 0
    run_ids: list[str] = field(default_factory=list)
    blocked: bool = False
    blocked_by: list[str] = field(default_factory=list)
    block_trigger: str | None = None  # hash of the run record that caused the current block
    triggers: list[str] = field(default_factory=list)  # every block ever (run record hashes)
    window_runs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def eligibility(self) -> str:
        return "blocked" if self.blocked else "eligible"

    def summary(self) -> dict[str, Any]:
        return {"agent": self.agent, "window_size": self.window_size, "score": self.score,
                "lazy_count": self.lazy_count, "run_ids": self.run_ids, "blocked": self.blocked,
                "blocked_by": self.blocked_by, "block_trigger": self.block_trigger}  # fmt: skip


def agent_state(
    agent: str, runs: Sequence[Mapping[str, Any]], penalty: rp.Penalty, window: int,
    unblocks: Sequence[Unblock], strict: bool = True,
) -> AgentState:  # fmt: skip
    """Deterministic replay of the run ledger for one agent. An unblock record resets the
    window (and the block) right after the run record it names. ``strict=False`` ignores
    unblock records that point past ``runs`` (replaying a historical prefix)."""
    if agent not in SCOPES.values():
        raise rp.PenaltyError(f"{agent} is never selectable")
    mine = [r for r in runs if r.get("agent") == agent]
    hashes = {str(r.get("hash")) for r in mine}
    resets: set[str] = set()
    for u in (u for u in unblocks if u.agent == agent):
        if u.reset_after_hash in hashes:
            resets.add(u.reset_after_hash)
        elif u.reset_after_hash != rp.GENESIS and strict:
            raise rp.PenaltyError(
                f"{u.source}: RESET_AFTER_HASH is not a {agent} run record",
                rp.UNAUTHORIZED_PENALTY_CHANGE,
            )
    st = AgentState(agent, window)
    seen: list[Mapping[str, Any]] = []
    for r in mine:
        seen = [*seen, r][-window:]
        lazy = sum(1 for x in seen if x.get("laziness_detected") is True)
        if penalty.value**lazy < THRESHOLD and not st.blocked:
            st.blocked = True
            st.block_trigger = str(r.get("hash"))
            st.triggers.append(st.block_trigger)
            st.blocked_by = [str(x.get("task_id")) for x in seen if x.get("laziness_detected")]
        if str(r.get("hash")) in resets:
            seen, st.blocked, st.block_trigger, st.blocked_by = [], False, None, []
    st.lazy_count = sum(1 for x in seen if x.get("laziness_detected") is True)
    st.score = round(penalty.value**st.lazy_count, 6)
    st.run_ids = [str(x.get("task_id")) for x in seen]
    st.window_runs = [dict(x) for x in seen]
    return st


def select(
    scope: str, table: Mapping[str, rp.Penalty], runs: Sequence[Mapping[str, Any]],
    window: int, unblocks: Sequence[Unblock], strict: bool = True,
) -> AgentState:  # fmt: skip
    """One candidate per scope today; highest-score eligible agent wins."""
    if scope not in SCOPES:
        raise rp.PenaltyError(f"unknown scope {scope!r}")
    agent = SCOPES[scope]
    return agent_state(agent, runs, table[agent], window, unblocks, strict)


def record(path: Path, task_id: str, scope: str, st: AgentState, penalty: rp.Penalty,
           runs: Sequence[Mapping[str, Any]], timestamp: str, unblocks: Sequence[Unblock] = (),
           block_report: Mapping[str, str] | None = None) -> dict[str, Any]:  # fmt: skip
    rows = rp.read_rows(path)
    rec: dict[str, Any] = {
        "task_id": task_id, "scope": scope, "agent_selected": st.agent,
        "base_score": rp.BASE_SCORE, "laziness_detected": st.lazy_count > 0,
        "penalty_level": penalty.level, "penalty_value": penalty.value,
        "routing_score": st.score, "eligibility": st.eligibility,
        "window": {"size": st.window_size, "lazy_count": st.lazy_count, "run_ids": st.run_ids},
        "timestamp": timestamp, "run_log_head": runs[-1]["hash"] if runs else rp.GENESIS,
        "unblocks_applied": sorted(u.source for u in unblocks if u.agent == st.agent),
        "block_report": dict(block_report) if block_report else None,
        "prev_hash": rows[-1]["hash"] if rows else rp.GENESIS,
    }  # fmt: skip
    rec["hash"] = rp.digest(rec)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
    return rec


def verify(
    path: Path, runs: Sequence[Mapping[str, Any]], table: Mapping[str, rp.Penalty],
    window: int, unblocks: Sequence[Unblock], artifacts: Path | None = None,
) -> list[tuple[str, str]]:  # fmt: skip
    """Overseer audit: chain intact; each decision recomputes from the runs that preceded it
    (score, window, threshold) and no agent was routed while blocked."""
    try:
        rows = rp.read_rows(path)
    except (ValueError, OSError) as exc:
        return [(rp.UNAUTHORIZED_PENALTY_CHANGE, f"selection ledger unreadable: {exc}")]
    out: list[tuple[str, str]] = []
    for scope in SCOPES:  # every unblock must point at a real run record of its agent
        try:
            select(scope, table, runs, window, unblocks)
        except rp.PenaltyError as exc:
            out.append((exc.code, str(exc)))
    heads = [rp.GENESIS, *(str(r.get("hash")) for r in runs)]
    prev = rp.GENESIS
    for i, r in enumerate(rows, 1):
        where = f"selection record {i}"
        if set(r) != {*SELECTION_KEYS, "prev_hash", "hash"}:
            out.append((rp.PENALTY_MISMATCH, f"{where}: incomplete or unknown fields"))
            continue
        if r["prev_hash"] != prev or r["hash"] != rp.digest(r):
            out.append((rp.UNAUTHORIZED_PENALTY_CHANGE, f"{where}: hash chain broken (tampered)"))
        prev = r["hash"]
        if SCOPES.get(r["scope"]) != r["agent_selected"]:
            bad = f"{where}: {r['agent_selected']!r} not selectable for {r['scope']!r}"
            out.append((rp.PENALTY_MISMATCH, bad))
            continue
        if r["run_log_head"] not in heads:
            out.append((rp.UNAUTHORIZED_PENALTY_CHANGE, f"{where}: run_log_head not in run ledger"))
            continue
        before = runs[: heads.index(r["run_log_head"])]
        try:
            known = {u.source: u for u in unblocks}
            missing = [n for n in r["unblocks_applied"] if n not in known]
            if missing:
                raise rp.PenaltyError(
                    f"unblock records {missing} missing", rp.UNAUTHORIZED_PENALTY_CHANGE
                )
            applied = [known[n] for n in r["unblocks_applied"]]
            st = select(r["scope"], table, before, window, applied, strict=False)
        except rp.PenaltyError as exc:
            out.append((exc.code, f"{where}: {exc}"))
            continue
        if st.blocked and artifacts is not None:
            probs = report_problems(artifacts, r["block_report"], st.agent, st.block_trigger)
            out += [(rp.PENALTY_MISMATCH, f"{where}: {m}") for m in probs]
        if r["eligibility"] == "eligible" and st.blocked:
            out.append((rp.PENALTY_MISMATCH, f"{where}: {st.agent} routed while blocked"))
        else:
            w = r["window"]
            got = (r["routing_score"], r["eligibility"], w["lazy_count"], w["size"])
            if got != (st.score, st.eligibility, st.lazy_count, window):
                bad = f"{where}: score/window/threshold differ from ledger"
                out.append((rp.PENALTY_MISMATCH, bad))
    return out


def report_problems(artifacts: Path, ref: Any, agent: str, trigger: str | None) -> list[str]:
    """A block must have its block report: json + md present, hashes match, same trigger."""
    if not isinstance(ref, dict) or not {"json", "md", "sha256_json", "sha256_md"} <= set(ref):
        return [f"block of {agent} without block report"]
    out: list[str] = []
    for kind in ("json", "md"):
        p = artifacts / str(ref[kind])
        if not p.is_file():
            out.append(f"block report {ref[kind]} missing")
        elif rp.sha256_file(p) != ref[f"sha256_{kind}"]:
            out.append(f"block report {ref[kind]} altered")
    if not out:
        data = json.loads((artifacts / str(ref["json"])).read_text(encoding="utf-8"))
        if (
            data.get("how", {}).get("trigger_hash") != trigger
            or data.get("who", {}).get("agent_blocked") != agent
        ):
            out.append(f"block report {ref['json']} does not describe this block")
    return out


def block_problems(
    artifacts: Path, runs: Sequence[Mapping[str, Any]], table: Mapping[str, rp.Penalty],
    window: int, unblocks: Sequence[Unblock],
) -> list[tuple[str, str]]:  # fmt: skip
    """Every block that ever happened needs a block report in the chained block ledger."""
    ledger = artifacts.joinpath(*BLOCK_LEDGER)
    try:
        rows = rp.read_rows(ledger)
    except (ValueError, OSError) as exc:
        return [(rp.UNAUTHORIZED_PENALTY_CHANGE, f"block ledger unreadable: {exc}")]
    out: list[tuple[str, str]] = []
    prev = rp.GENESIS
    by_trigger: dict[str, dict[str, Any]] = {}
    for i, r in enumerate(rows, 1):
        if r.get("prev_hash") != prev or r.get("hash") != rp.digest(r):
            out.append((rp.UNAUTHORIZED_PENALTY_CHANGE, f"block record {i}: hash chain broken"))
        prev = str(r.get("hash"))
        by_trigger[str(r.get("trigger_hash"))] = r
    for agent in SCOPES.values():
        try:
            st = agent_state(agent, runs, table[agent], window, unblocks)
        except rp.PenaltyError as exc:
            out.append((exc.code, str(exc)))
            continue
        for trig in st.triggers:
            rec = by_trigger.get(trig)
            ref = rec.get("report") if rec else None
            out += [(rp.PENALTY_MISMATCH, m) for m in report_problems(artifacts, ref, agent, trig)]
    return out


def append_block(artifacts: Path, agent: str, trigger: str, report: Mapping[str, str],
                 timestamp: str) -> dict[str, Any]:  # fmt: skip
    path = artifacts.joinpath(*BLOCK_LEDGER)
    rows = rp.read_rows(path)
    rec: dict[str, Any] = {"agent": agent, "trigger_hash": trigger, "report": dict(report),
                           "timestamp": timestamp,
                           "prev_hash": rows[-1]["hash"] if rows else rp.GENESIS}  # fmt: skip
    rec["hash"] = rp.digest(rec)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
    return rec


def find_report(artifacts: Path, trigger: str) -> dict[str, str] | None:
    for r in rp.read_rows(artifacts.joinpath(*BLOCK_LEDGER)):
        if r.get("trigger_hash") == trigger:
            return dict(r["report"])
    return None
