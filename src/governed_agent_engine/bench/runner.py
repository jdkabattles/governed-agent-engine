"""Benchmark runner: paced per provider, quota-aware, records raw responses (no secrets)."""

from __future__ import annotations

import json
import statistics
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from governed_agent_engine.bench.graders import grade
from governed_agent_engine.bench.items import DIMENSIONS, SUITE_VERSION, Item
from governed_agent_engine.cognition.base import ChatMessage, CognitionEndpoint
from governed_agent_engine.core.errors import AuthError, ProviderError, RateLimitedError


@dataclass(frozen=True, slots=True)
class Target:
    name: str  # endpoint name in config/endpoints.yaml
    client: CognitionEndpoint
    items: tuple[Item, ...]
    min_interval_s: float  # pacing derived from rpm (+ margin)
    group: str = ""  # targets in the same group run sequentially in one thread


@dataclass
class CallRecord:
    target: str
    model_requested: str
    model_served: str
    item_id: str
    dimension: str
    ok: bool
    outcome: str  # ok|rate_limited|auth_error|error|timeout|skipped_quota
    latency_ms: float
    score: float
    reason: str
    response: str
    ts: str
    suite_version: str = SUITE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class RunResult:
    records: list[CallRecord] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, rec: CallRecord) -> None:
        with self.lock:
            self.records.append(rec)


def messages_for(item: Item) -> list[ChatMessage]:
    msgs: list[ChatMessage] = []
    if item.system:
        msgs.append(ChatMessage("system", item.system))
    msgs.append(ChatMessage("user", item.prompt))
    return msgs


def estimate_tokens(item: Item) -> int:
    """Rough prompt+answer token estimate (4 chars/token + 1024 for the answer/reasoning)."""
    return (len(item.prompt) + len(item.system)) // 4 + 1024


def _classify(exc: ProviderError) -> str:
    if isinstance(exc, RateLimitedError):
        return "rate_limited"
    if isinstance(exc, AuthError):
        return "auth_error"
    low = str(exc).lower()
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if "http 503" in low or "http 502" in low or "http 500" in low:
        return "overloaded"
    return "error"


def _attempt(
    t: Target, item: Item, sleep: Callable[[float], None], backoff: tuple[float, ...]
) -> tuple[str, str, str, float]:
    """One graded call; transient 429/5xx get bounded backoff retries (each recorded latency
    is the final attempt's). Returns (served_model, text, outcome, latency_ms)."""
    for attempt in range(len(backoff) + 1):
        t0 = time.perf_counter()
        try:
            c = t.client.complete(messages_for(item))
            return c.model, c.text, "ok", (time.perf_counter() - t0) * 1000
        except ProviderError as exc:
            ms = (time.perf_counter() - t0) * 1000
            outcome = _classify(exc)
            if outcome not in ("rate_limited", "overloaded") or attempt == len(backoff):
                return "", str(exc), outcome, ms
            hinted = exc.retry_after_s if isinstance(exc, RateLimitedError) else None
            sleep(max(backoff[attempt], min(hinted or 0.0, 120.0)))
    raise AssertionError("unreachable")  # pragma: no cover


def run_target(
    t: Target,
    result: RunResult,
    out_dir: Path,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    max_consecutive_429: int = 2,
    retry_backoff_s: tuple[float, ...] = (20.0, 60.0),
    resume: bool = False,
) -> None:
    raw = out_dir / "raw" / f"{t.name}.jsonl"
    raw.parent.mkdir(parents=True, exist_ok=True)
    last = -1e9
    consecutive_429 = 0
    done: set[str] = set()
    if resume and raw.is_file():
        for line in raw.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r.get("ok") and r.get("suite_version") == SUITE_VERSION:
                done.add(r["item_id"])
                result.add(CallRecord(**r))
    for item in t.items:
        if item.item_id in done:
            continue
        if consecutive_429 >= max_consecutive_429:
            rec = CallRecord(t.name, t.client.model, "", item.item_id, item.dimension, False,
                             "skipped_quota", 0.0, 0.0, "quota", "",
                             datetime.now(UTC).isoformat())  # fmt: skip
        else:
            wait = t.min_interval_s - (clock() - last)
            snap = getattr(t.client, "last_ratelimit", None)
            if snap is not None:  # honor x-ratelimit-* (e.g. Groq 8000 tokens/min)
                wait = max(wait, snap.wait_needed(estimate_tokens(item)))
            if wait > 0:
                sleep(wait)
            last = clock()
            served, text, outcome, ms = _attempt(t, item, sleep, retry_backoff_s)
            consecutive_429 = consecutive_429 + 1 if outcome == "rate_limited" else 0
            last = clock()
            score, reason = grade(item, text) if outcome == "ok" else (0.0, outcome)
            rec = CallRecord(t.name, t.client.model, served, item.item_id, item.dimension,
                             outcome == "ok", outcome, round(ms, 1), score, reason, text,
                             datetime.now(UTC).isoformat())  # fmt: skip
        result.add(rec)
        with raw.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")


def run_benchmark(targets: Sequence[Target], out_dir: Path, **kw: Any) -> RunResult:
    """Different groups run in parallel threads; targets within a group run sequentially."""
    result = RunResult()
    groups: dict[str, list[Target]] = {}
    for t in targets:
        groups.setdefault(t.group or t.name, []).append(t)

    def work(ts: list[Target]) -> None:
        for t in ts:
            run_target(t, result, out_dir, **kw)

    threads = [threading.Thread(target=work, args=(ts,), daemon=True) for ts in groups.values()]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return result


def percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def summarize(records: Sequence[CallRecord]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name in sorted({r.target for r in records}):
        rs = [r for r in records if r.target == name]
        attempted = [r for r in rs if r.outcome != "skipped_quota"]
        oks = [r for r in attempted if r.ok]
        dims: dict[str, dict[str, float]] = {}
        for d in DIMENSIONS:
            # capability = quality of answers actually returned; failures count in error_rate
            graded = [r for r in oks if r.dimension == d and d != "latency"]
            if graded:
                dims[d] = {"score": round(statistics.fmean(r.score for r in graded), 3),
                           "n": len(graded)}  # fmt: skip
        lat = [r.latency_ms for r in oks]
        served = sorted({r.model_served for r in oks if r.model_served})
        out[name] = {
            "model_requested": rs[0].model_requested,
            "models_served": served,
            "calls": len(attempted),
            "skipped_quota": len(rs) - len(attempted),
            "error_rate": round(1 - len(oks) / len(attempted), 3) if attempted else 1.0,
            "timeout_rate": round(
                sum(r.outcome == "timeout" for r in attempted) / len(attempted), 3
            )
            if attempted
            else 0.0,
            "rate_limited": sum(r.outcome == "rate_limited" for r in attempted),
            "overloaded": sum(r.outcome == "overloaded" for r in attempted),
            "p50_ms": round(percentile(lat, 0.5)),
            "p95_ms": round(percentile(lat, 0.95)),
            "dimensions": dims,
        }
    return out
