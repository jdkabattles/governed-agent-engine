"""LIVE smoke test (opt-in). Never part of unit tests. Prints no secrets."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

from governed_agent_engine.cognition.base import ChatMessage
from governed_agent_engine.cognition.http import HttpClient, UrllibHttpClient
from governed_agent_engine.cognition.judge0 import Judge0Client
from governed_agent_engine.cognition.rate_limit import TokenBucket
from governed_agent_engine.cognition.registry import build_clients
from governed_agent_engine.config.loader import load_config
from governed_agent_engine.core.errors import AuthError, ProviderError, RateLimitedError

PROMPT = [ChatMessage("user", "Reply with exactly the word: PONG")]


def run_smoke(
    root: Path,
    only: str = "",
    *,
    judge0: bool = False,
    markdown: Path | None = None,
    http: HttpClient | None = None,
) -> list[dict[str, str]]:
    cfg = load_config(root / "config", root)
    client = http or UrllibHttpClient()
    clients = build_clients(cfg.enabled_endpoints(), client)
    names = [n for n in only.split(",") if n] or sorted(clients)
    rows: list[dict[str, str]] = []
    for name in names:
        ep = clients[name]
        t0 = time.perf_counter()
        try:
            c = ep.complete(PROMPT)
            outcome, model, excerpt = "ok", c.model, c.text.strip().replace("\n", " ")[:60]
        except RateLimitedError as e:
            outcome, model, excerpt = "rate_limited", ep.model, str(e)[:100]
        except AuthError as e:
            outcome, model, excerpt = "auth_error", ep.model, str(e)[:100]
        except ProviderError as e:
            outcome, model, excerpt = "error", ep.model, str(e)[:100]
        rows.append({"endpoint": name, "outcome": outcome, "model": model,
                     "latency_ms": f"{(time.perf_counter() - t0) * 1000:.0f}",
                     "excerpt": excerpt.replace("|", "/")})  # fmt: skip
    if judge0 and "judge0" in cfg.endpoints:
        jcfg = cfg.endpoints["judge0"]
        j = Judge0Client(jcfg, client, TokenBucket.per_minute(jcfg.rpm))
        t0 = time.perf_counter()
        try:
            r = j.run("print(6*7)")
            outcome, excerpt = ("ok" if r.ok else r.status), r.stdout.strip()
        except ProviderError as e:
            outcome, excerpt = "error", str(e)[:100]
        rows.append({"endpoint": "judge0", "outcome": outcome, "model": "python3",
                     "latency_ms": f"{(time.perf_counter() - t0) * 1000:.0f}",
                     "excerpt": excerpt})  # fmt: skip
    if markdown is not None:
        head = "| endpoint | outcome | model | latency ms | excerpt |\n|---|---|---|---|---|\n"
        body = "".join(
            f"| {r['endpoint']} | {r['outcome']} | {r['model']} | {r['latency_ms']} | "
            f"{r['excerpt']} |\n"
            for r in rows
        )
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        markdown.write_text(f"Run at {stamp}\n\n{head}{body}", encoding="utf-8")
    return rows
