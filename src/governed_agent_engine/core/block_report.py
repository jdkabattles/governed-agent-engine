"""Block report: every agent block produces
``artifacts/evidence/blocks/<agent>-<ts>.json`` + ``.md`` covering WHO / WHAT / WHERE / WHEN /
HOW / WHY. It is hash-linked into the routing chain through ``block_records.jsonl`` and the
selection records. The audit treats a block without a report as NON_COMPLIANT."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import agent_selection as sel
from . import routing_penalty as rp
from .grok_format import GrokValue

try:  # Termux and minimal images may lack tzdata
    from zoneinfo import ZoneInfo

    CT: Any = ZoneInfo("America/Chicago")
except Exception:  # pragma: no cover - environment dependent
    CT = timezone(timedelta(hours=-5), "CT")

ISSUER = "overseer/routing-kernel"
APPROVER = "owner"
DOCTRINE_QUOTES = (
    "All actions must produce artifacts: logs, evidence, structured outputs.",
    "Every step must require evidence unless exempt.",
)


def _times(ts: str) -> dict[str, str]:
    try:
        t = datetime.fromisoformat(ts)
    except ValueError:
        return {"ct": ts, "utc": ts}
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return {"ct": t.astimezone(CT).isoformat(), "utc": t.astimezone(UTC).isoformat()}


def _commit(root: Path) -> str:
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True, text=True, timeout=5, check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def _check_for(violation: str) -> str:
    head = violation.split(":", 1)[0].strip()
    if head in ("ACQUISITION_NON_COMPLIANT", "COGNITION_NON_COMPLIANT"):
        return head
    return "EVIDENCE_OF_TRUTH"


def _violations(artifacts: Path, run: Mapping[str, Any]) -> list[str]:
    rel = str(run.get("evidence_path") or "")
    p = artifacts.parent / rel if rel else None
    if p is None or not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return []
    v = data.get("violations", [])
    return [str(x) for x in v] if isinstance(v, list) else []


def build(
    root: Path, artifacts: Path, st: sel.AgentState, penalty: rp.Penalty,
    held: Sequence[str], now: datetime, doctrine_grok: Mapping[str, GrokValue],
) -> dict[str, str]:  # fmt: skip
    """Write the report pair; returns the reference stored in the routing chain."""
    lazy = [r for r in st.window_runs if r.get("laziness_detected") is True]
    runs_rel = "/".join(rp.LEDGER)
    how: list[dict[str, Any]] = []
    for r in lazy:
        v = _violations(artifacts, r)
        checks = sorted({_check_for(x) for x in v}) or ["EVIDENCE_OF_TRUTH"]
        how.append({"task_id": r.get("task_id"), "run_record_hash": r.get("hash"),
                    "checks_fired": checks, "missing_or_failed": v,
                    "evidence_path": r.get("evidence_path") or None})  # fmt: skip
    rules = doctrine_grok.get("RULES")
    penalty_rule = rules.get("LAZINESS_PENALTY") if isinstance(rules, dict) else None
    reasons: list[str] = [x for h in how for x in h["missing_or_failed"]]
    ops = doctrine_grok.get("OPERATIONAL_REQUIREMENTS")
    quotes = [q for q in DOCTRINE_QUOTES if isinstance(ops, list) and q in ops]
    report: dict[str, Any] = {
        "kind": "agent_block_report",
        "who": {"agent_blocked": st.agent, "issuer": ISSUER, "approver_needed": APPROVER},
        "what": {"routing_score": st.score, "threshold": sel.THRESHOLD,
                 "lazy_count": st.lazy_count, "window_size": st.window_size,
                 "window_run_ids": st.run_ids, "tasks_held": sorted(set(held))},
        "where": {"repo": root.resolve().name, "commit": _commit(root),
                  "routing_records": f"artifacts/{runs_rel}",
                  "evidence_paths": [h["evidence_path"] for h in how if h["evidence_path"]]},
        "when": {"lazy_runs": [{"task_id": r.get("task_id"), **_times(str(r.get("timestamp")))}
                               for r in lazy],
                 "blocked_at": _times(now.isoformat())},
        "how": {"trigger_hash": st.block_trigger, "lazy_runs": how},
        "why": {"doctrine_statement": doctrine_grok.get("STATEMENT"),
                "laziness_penalty": penalty_rule,
                "rules_violated": quotes,
                "failure_reasons": reasons},
        "unblock": "Owner-signed record in migrations/unblocks/*.grok "
                   "(KIND agent_unblock, RESET_AFTER_HASH = a run record hash).",
    }  # fmt: skip
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = artifacts / "evidence" / "blocks"
    out.mkdir(parents=True, exist_ok=True)
    jpath, mpath = out / f"{st.agent}-{stamp}.json", out / f"{st.agent}-{stamp}.md"
    jpath.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    mpath.write_text(markdown(report), encoding="utf-8")
    return {"json": jpath.relative_to(artifacts).as_posix(),
            "md": mpath.relative_to(artifacts).as_posix(),
            "sha256_json": rp.sha256_file(jpath), "sha256_md": rp.sha256_file(mpath)}  # fmt: skip


def summary(report: Mapping[str, Any]) -> str:
    w, wh = report["what"], report["who"]
    return (f"BLOCKED {wh['agent_blocked']}: score {w['routing_score']} < {w['threshold']} "
            f"({w['lazy_count']} lazy in last {w['window_size']}); held {len(w['tasks_held'])} "
            f"task(s); unblock needs {wh['approver_needed']}.")  # fmt: skip


def markdown(r: Mapping[str, Any]) -> str:
    lines = [f"# Block report: {r['who']['agent_blocked']}", "", summary(r), "",
             "## Who", f"- Agent blocked: {r['who']['agent_blocked']}",
             f"- Issuer: {r['who']['issuer']}", f"- Approver needed: {r['who']['approver_needed']}",
             "", "## What"]  # fmt: skip
    lines += [f"- {k}: {v}" for k, v in r["what"].items()]
    lines += ["", "## Where"] + [f"- {k}: {v}" for k, v in r["where"].items()]
    lines += ["", "## When", f"- Blocked at: {r['when']['blocked_at']['ct']} "
              f"({r['when']['blocked_at']['utc']})"]  # fmt: skip
    lines += [f"- Lazy run {x['task_id']}: {x['ct']} ({x['utc']})" for x in r["when"]["lazy_runs"]]
    lines += ["", "## How", f"- Trigger run record hash: {r['how']['trigger_hash']}"]
    for h in r["how"]["lazy_runs"]:
        lines.append(f"- {h['task_id']} (record {h['run_record_hash']}): checks "
                     f"{', '.join(h['checks_fired'])}")  # fmt: skip
        lines += [f"  - {m}" for m in h["missing_or_failed"]]
    lines += ["", "## Why", f"> {r['why']['doctrine_statement']}", ""]
    lines += [f"> {q}" for q in r["why"]["rules_violated"]]
    lines += ["", f"LAZINESS_PENALTY: {r['why']['laziness_penalty']}", "", "Failure reasons:"]
    lines += [f"- {x}" for x in r["why"]["failure_reasons"]] or ["- (none recorded)"]
    lines += ["", "## Unblock", r["unblock"], ""]
    return "\n".join(lines)
