"""Minimal metrics: counters + durations, flushed to artifacts/metrics as JSON and CSV."""

from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Metrics:
    counters: Counter[str] = field(default_factory=Counter)
    durations_ms: dict[str, list[float]] = field(default_factory=dict)

    def incr(self, name: str, by: int = 1) -> None:
        self.counters[name] += by

    def observe(self, name: str, ms: float) -> None:
        self.durations_ms.setdefault(name, []).append(ms)

    def snapshot(self) -> dict[str, float]:
        out: dict[str, float] = {k: float(v) for k, v in sorted(self.counters.items())}
        for k, vals in sorted(self.durations_ms.items()):
            out[f"{k}.avg_ms"] = sum(vals) / len(vals)
            out[f"{k}.count"] = float(len(vals))
        return out

    def flush(self, metrics_dir: Path, stem: str = "metrics") -> tuple[Path, Path]:
        metrics_dir.mkdir(parents=True, exist_ok=True)
        snap = self.snapshot()
        jpath = metrics_dir / f"{stem}.json"
        jpath.write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        cpath = metrics_dir / f"{stem}.csv"
        with cpath.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["metric", "value"])
            writer.writerows(snap.items())
        return jpath, cpath
