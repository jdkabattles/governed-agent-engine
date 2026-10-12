"""`gae` CLI: submit, run [--once], status, blocks, example, replay, smoke, validate, audit."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from governed_agent_engine.bench.cli_bench import bench, rebuild
from governed_agent_engine.core.errors import (
    AllEndpointsFailedError,
    ConfigError,
    DoctrineError,
    GovernanceError,
    ValidationError,
)
from governed_agent_engine.core.grok_binding import audit_summary, run_audit
from governed_agent_engine.orchestrator import main as orch_main
from governed_agent_engine.smoke import run_smoke


def _default_root() -> Path:
    return Path(os.environ.get("GAE_ROOT", Path.cwd()))


def _emit(obj: object) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def _fail(code: int, kind: str, detail: str, **extra: object) -> int:
    """One clear line for humans (stderr) + one JSON line for scripts (stdout); no traceback."""
    detail = " ".join(str(detail).split())
    print(f"gae: error: {kind}: {detail}", file=sys.stderr)
    print(json.dumps({"error": kind, "detail": detail, **extra}, sort_keys=True, default=str))
    return code


EXAMPLE_GOALS = {
    "research": "Outline a blog post comparing free, no-cost AI API tiers",
    "market_watch": "Summarise today's public price moves for a watchlist (read-only, no trades)",
    "repo_maintain": "List outdated dependencies in this repo and propose upgrades",
    "governance_check": "Check config/governance.yaml against the APCK policy and report gaps",
    "custom": "Describe the task goal here",
}


def example_task(task_type: str) -> dict[str, object]:
    """A valid, doctrine-compliant top-level task issued by owner."""
    return {
        "task_id": f"example-{task_type.replace('_', '-')}-1",
        "type": task_type,
        "action": "plan",
        "issued_by": "owner",
        "priority": "normal",
        "inputs": {
            "goal": EXAMPLE_GOALS.get(task_type, EXAMPLE_GOALS["custom"]),
            "constraints": ["$0 budget", "cite sources", "facts only"],
        },
    }


def _read_source(source: str | None, inline: str | None) -> object:
    if inline is not None:
        raw, where = inline, "--json"
    elif source in (None, "-"):
        raw, where = sys.stdin.read(), "stdin"
    else:
        path = Path(str(source))
        if not path.is_file():
            hint = " (try: gae example --out task.json, or examples/task.json)"
            raise _UserError(f"file not found: {path}{hint if not path.exists() else ''}")
        try:
            raw, where = path.read_text(encoding="utf-8"), str(path)
        except (OSError, UnicodeDecodeError) as exc:
            raise _UserError(f"cannot read {path}: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _UserError(f"{where} is not valid JSON (line {exc.lineno}, col {exc.colno}: "
                         f"{exc.msg})") from exc  # fmt: skip


class _UserError(Exception):
    pass


def status_explanation(status: str, last_heartbeat: str | None, now: datetime,
                       stale_after_s: int) -> str:  # fmt: skip
    """Plain words for the Grok usage state."""
    limit = f"{stale_after_s / 3600:g} h"
    if status == "UNKNOWN" or not last_heartbeat:
        return ("UNKNOWN: Executor Agent has never checked in "
                "(no artifacts/evidence/heartbeat.json). Tasks are planned by the free "
                "models and queue in the backlog until the first check-in.")  # fmt: skip
    age_h = (now - datetime.fromisoformat(last_heartbeat)).total_seconds() / 3600
    ago = f"{age_h:.1f} hours ago"
    if status == "EXHAUSTED":
        return (
            f"EXHAUSTED: Executor Agent last checked in {ago} (limit {limit}). "
            "Tasks will queue until its next check-in; cognition still plans them."
        )
    return f"AVAILABLE: Executor Agent last checked in {ago} (within {limit}); tasks go to Grok."


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gae", description="Governed agent engine")
    ap.add_argument("--root", type=Path, default=None, help="repo root (default: $GAE_ROOT or cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("submit", help="submit a task (file path, '-' for stdin, or --json)")
    p.add_argument(
        "--as",
        dest="issuer",
        default="owner",
        help="issued_by stamped on tasks that do not name one (default: owner)",
    )
    p.add_argument("source", nargs="?", default=None)
    p.add_argument("--json", dest="inline", default=None)
    r = sub.add_parser("run", help="run the loop (inbox intake, probe, replay)")
    r.add_argument("--once", action="store_true", help="run exactly one cycle and exit")
    r.add_argument("--cycles", type=int, default=None)
    sub.add_parser("status", help="Grok usage state + backlog counts")
    ex = sub.add_parser("example", help="print (or --out write) a valid example task")
    ex.add_argument("--type", dest="task_type", default="research", choices=sorted(EXAMPLE_GOALS))
    ex.add_argument("--out", type=Path, default=None, help="write to this file instead")
    sub.add_parser("blocks", help="agent blocks: reports, scores, held tasks")
    apr = sub.add_parser("approve", help="human approval of an awaiting_approval entry")
    apr.add_argument("entry_id")
    apr.add_argument("--as", dest="actor", default="owner")
    bl = sub.add_parser("block", help="reject an entry (human or overseer) with a reason")
    bl.add_argument("entry_id")
    bl.add_argument("--reason", required=True)
    bl.add_argument("--as", dest="actor", default="owner")
    sub.add_parser("replay", help="reconcile evidence and re-dispatch outstanding Grok entries")
    s = sub.add_parser("smoke", help="LIVE: ping enabled endpoints (opt-in, uses network)")
    s.add_argument("--endpoints", default="")
    s.add_argument("--judge0", action="store_true")
    s.add_argument("--markdown", type=Path, default=None)
    sub.add_parser("validate", help="load and validate config/*.yaml")
    sub.add_parser("audit", help="Overseer Agent .grok audit -> artifacts/evidence/audits/")
    bp = sub.add_parser("bench", help="LIVE: run the capability benchmark (opt-in, uses quota)")
    bp.add_argument("--endpoints", default="", help="comma list (default: all enabled)")
    bp.add_argument("--mode", choices=["auto", "full", "subset", "mini"], default="auto")
    bp.add_argument("--out", type=Path, default=None)
    bp.add_argument("--write-capabilities", action="store_true")
    bp.add_argument(
        "--openrouter-budget",
        type=int,
        default=40,
        help="refuse to run if planned OpenRouter calls exceed this (50/day quota)",
    )
    bp.add_argument("--dry-run", action="store_true")
    bp.add_argument(
        "--rebuild-from",
        type=Path,
        default=None,
        help="no calls: recompute scores/capabilities from an existing run dir",
    )
    bp.add_argument("--items", default="", help="comma list of item ids to run (default all)")
    bp.add_argument("--resume", action="store_true", help="skip items already ok in raw/*.jsonl")
    args = ap.parse_args(argv)
    root = (args.root or _default_root()).resolve()

    try:
        if args.cmd == "example":
            task = example_task(args.task_type)
            text = json.dumps(task, indent=2) + "\n"
            if args.out is None:
                print(text, end="")
            else:
                args.out.write_text(text, encoding="utf-8")
                print(f"wrote {args.out} (submit with: gae submit {args.out})")
            return 0
        if args.cmd == "smoke":
            rows = run_smoke(root, args.endpoints, judge0=args.judge0, markdown=args.markdown)
            _emit(rows)
            return 0 if any(row["outcome"] == "ok" for row in rows) else 1
        if args.cmd == "bench" and args.rebuild_from is not None:
            _emit(rebuild(root, args.rebuild_from, args.write_capabilities))
            return 0
        if args.cmd == "bench":
            res = bench(
                root,
                endpoints=[e for e in args.endpoints.split(",") if e] or None,
                mode=args.mode,
                out_dir=args.out,
                write_caps=args.write_capabilities,
                max_per_group={"openrouter": args.openrouter_budget},
                dry_run=args.dry_run,
                resume=args.resume,
                only_items=[i for i in args.items.split(",") if i] or None,
            )
            _emit(res)
            return 0
        if args.cmd == "audit":
            report, path = run_audit(root)
            _emit(audit_summary(report, path))
            return 0 if report.compliant else 6
        orch = orch_main.build(root)
        if args.cmd == "submit":
            data = _read_source(args.source, args.inline)
            if isinstance(data, dict) and set(data) == {"task"}:
                data = data["task"]
            if isinstance(data, dict) and not data.get("issued_by"):
                # the person at this terminal is the issuer (default: the human principal)
                data = {**data, "issued_by": args.issuer}
            out = orch.submit(orch_main.normalize(data))
            orch.metrics.flush(orch.art / "metrics")
            _emit({"decision": out.decision.to_dict(), "entry_id": out.entry.entry_id,
                   "status": out.entry.status.value})  # fmt: skip
        elif args.cmd == "run":
            if args.once:
                _emit(orch.run_cycle().to_dict())
            else:
                _emit({"cycles": orch.run_forever(max_cycles=args.cycles)})
        elif args.cmd == "status":
            usage, _ = orch.refresh_usage(force=True)
            d = usage.to_dict()
            d["explanation"] = status_explanation(
                d["status"], d["last_heartbeat"], orch.clock(),
                orch.config.usage.heartbeat_stale_after_s,
            )  # fmt: skip
            print(d["explanation"], file=sys.stderr)
            _emit({"grok": d, "backlog": orch.backlog.counts(),
                   "endpoints": sorted(orch.config.enabled_endpoints()),
                   "blocks": orch.blocks_overview()})  # fmt: skip
        elif args.cmd == "blocks":
            _emit(orch.blocks_overview())
        elif args.cmd == "replay":
            _emit([e.entry_id for e in orch.replay()])
        elif args.cmd in ("approve", "block"):
            e = (orch.approve(args.entry_id, args.actor) if args.cmd == "approve"
                 else orch.block(args.entry_id, args.actor, args.reason))  # fmt: skip
            _emit({"entry_id": e.entry_id, "status": e.status.value})
        else:
            _emit({"ok": True, "endpoints": sorted(orch.config.enabled_endpoints()),
                   "governance_fingerprint": orch.config.governance.fingerprint[:12],
                   "doctrine_fingerprint": orch.config.doctrine.fingerprint[:12]})  # fmt: skip
    except _UserError as exc:
        return _fail(4, "input", str(exc))
    except GovernanceError as exc:
        return _fail(2, "governance", str(exc))
    except AllEndpointsFailedError as exc:
        return _fail(3, "cognition", f"all {len(exc.attempts)} endpoint attempts failed",
                     attempts=[a.to_dict() for a in exc.attempts])  # fmt: skip
    except DoctrineError as exc:
        return _fail(5, "doctrine", f"{len(exc.rejections)} plan(s) rejected",
                     rejections=list(exc.rejections))  # fmt: skip
    except (ConfigError, ValidationError, ValueError) as exc:
        return _fail(4, "invalid", str(exc))
    except OSError as exc:
        return _fail(4, "io", str(exc))
    except Exception as exc:  # last resort: never a traceback (GAE_DEBUG=1 to see one)
        if os.environ.get("GAE_DEBUG"):
            raise
        return _fail(1, type(exc).__name__, str(exc) or "unexpected error")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
