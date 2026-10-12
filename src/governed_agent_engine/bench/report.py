"""Render a benchmark summary as a Markdown table (written next to scores.json)."""

from __future__ import annotations

from typing import Any

COLS = ("coding", "reasoning", "structured", "instruction", "honesty", "long_context")


def _cell(dims: dict[str, dict[str, float]], d: str) -> str:
    if d not in dims:
        return "-"
    x = dims[d]
    return f"{x['score']:.2f} (n={int(x['n'])})"


def render_markdown(summary: dict[str, dict[str, Any]]) -> str:
    head = "| endpoint | served model(s) | " + " | ".join(COLS) + " | p50 ms | p95 ms | err% |"
    sep = "|" + "---|" * (len(COLS) + 5)
    rows = [head, sep]
    for name, s in sorted(summary.items()):
        served = ", ".join(s["models_served"]) or s["model_requested"]
        cells = " | ".join(_cell(s["dimensions"], d) for d in COLS)
        rows.append(
            f"| {name} | {served} | {cells} | {s['p50_ms']} | {s['p95_ms']} | "
            f"{100 * s['error_rate']:.0f} |"
        )
    return "\n".join(rows) + "\n"
