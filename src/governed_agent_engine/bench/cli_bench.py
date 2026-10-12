"""`gae bench`: build targets from config, run, summarize, optionally write capabilities."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from governed_agent_engine.bench.capabilities import build_capabilities, write_capabilities
from governed_agent_engine.bench.graders import GRADER_VERSION, grade
from governed_agent_engine.bench.items import ALL_ITEMS, SEED, SUITE_VERSION
from governed_agent_engine.bench.plan import budget, items_for
from governed_agent_engine.bench.report import render_markdown
from governed_agent_engine.bench.runner import CallRecord, Target, run_benchmark, summarize
from governed_agent_engine.cognition.ai_horde import AIHordeEndpoint
from governed_agent_engine.cognition.http import HttpClient, UrllibHttpClient
from governed_agent_engine.cognition.registry import ADAPTERS
from governed_agent_engine.config.loader import load_config


def bench(
    root: Path,
    *,
    endpoints: list[str] | None = None,
    mode: str = "auto",
    out_dir: Path | None = None,
    write_caps: bool = False,
    max_per_group: dict[str, int] | None = None,
    http: HttpClient | None = None,
    dry_run: bool = False,
    resume: bool = False,
    only_items: list[str] | None = None,
    **run_kw: Any,
) -> dict[str, Any]:
    cfg = load_config(root / "config", root)
    eps = {n: e for n, e in cfg.enabled_endpoints().items() if endpoints is None or n in endpoints}
    client = http or UrllibHttpClient()
    plan = {
        n: tuple(i for i in items_for(n, e, mode) if not only_items or i.item_id in only_items)
        for n, e in eps.items()
    }
    calls = budget(plan, eps)
    for group, cap in (max_per_group or {}).items():
        if calls.get(group, 0) > cap:
            raise ValueError(f"planned {calls[group]} calls for {group} > budget {cap}")
    if dry_run:
        return {"plan": {n: [i.item_id for i in its] for n, its in plan.items()}, "calls": calls}
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    out = out_dir or root / "artifacts" / "benchmarks" / stamp
    out.mkdir(parents=True, exist_ok=True)
    targets = []
    for n, e in eps.items():
        c = (
            AIHordeEndpoint(e, client, max_new_tokens=512)
            if e.kind == "ai_horde"
            else ADAPTERS[e.kind](e, client)
        )
        targets.append(Target(n, c, plan[n], 60.0 / e.rpm + 0.5, group=e.limit_key))
    result = run_benchmark(targets, out, resume=resume, **run_kw)
    summary = summarize(result.records)
    meta = {"suite_version": SUITE_VERSION, "seed": SEED, "temperature": 0,
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "calls": calls}  # fmt: skip
    (out / "scores.json").write_text(
        json.dumps({"meta": meta, "summary": summary}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out / "summary.md").write_text(render_markdown(summary), encoding="utf-8")
    if write_caps:
        rel = out.relative_to(root).as_posix() if out.is_relative_to(root) else str(out)
        write_capabilities(
            build_capabilities(summary, SUITE_VERSION, rel), root / "config" / "capabilities.yaml"
        )
    return {"out": str(out), "summary": summary}


ITEMS_BY_ID = {i.item_id: i for i in ALL_ITEMS}


def load_raw(out: Path) -> tuple[list[CallRecord], list[CallRecord]]:
    """(latest record per target+item, every attempt) from raw/*.jsonl of this suite version."""
    every: list[CallRecord] = []
    for f in sorted((out / "raw").glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r.get("suite_version") == SUITE_VERSION:
                every.append(CallRecord(**r))
    latest: dict[tuple[str, str], CallRecord] = {}
    for r in every:
        prev = latest.get((r.target, r.item_id))
        # a successful answer is never replaced by a later skip; otherwise last attempt wins
        if prev is None or not (prev.ok and not r.ok):
            latest[(r.target, r.item_id)] = r
    return list(latest.values()), every


def rebuild(root: Path, out: Path, write_caps: bool = False) -> dict[str, Any]:
    """Re-grade nothing, call nothing: recompute scores/capabilities from recorded raw data.
    Scores use the latest attempt per item; reliability uses every attempt."""
    latest, every = load_raw(out)
    for r in latest:  # re-grade with the current graders (deterministic, no network)
        if r.ok:
            r.score, r.reason = grade(ITEMS_BY_ID[r.item_id], r.response)
    summary = summarize(latest)
    all_attempts = summarize(every)
    for name, s in summary.items():
        a = all_attempts[name]
        s["reliability_all_attempts"] = {
            k: a[k] for k in ("calls", "error_rate", "timeout_rate", "rate_limited", "overloaded")
        }
        s["error_rate"] = a["error_rate"]  # routing penalises flakiness seen across reruns
    meta = {"suite_version": SUITE_VERSION, "seed": SEED, "temperature": 0,
            "grader_version": GRADER_VERSION,
            "rebuilt_at": datetime.now(UTC).isoformat(timespec="seconds")}  # fmt: skip
    (out / "scores.json").write_text(
        json.dumps({"meta": meta, "summary": summary}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out / "summary.md").write_text(render_markdown(summary), encoding="utf-8")
    if write_caps:
        rel = out.relative_to(root).as_posix() if out.is_relative_to(root) else str(out)
        write_capabilities(
            build_capabilities(summary, SUITE_VERSION, rel), root / "config" / "capabilities.yaml"
        )
    return {"out": str(out), "summary": summary}
