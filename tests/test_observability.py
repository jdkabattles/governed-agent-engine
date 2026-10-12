from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from governed_agent_engine.observability.logging import JsonLogger
from governed_agent_engine.observability.metrics import Metrics

from .conftest import T0


def test_json_logger_fields_and_redaction(tmp_path: Path) -> None:
    buf = io.StringIO()
    log = JsonLogger(tmp_path, stream=buf, clock=lambda: T0)
    rec = log.event(task_id="t", phase="route", executor="grok", status="ok", api_key="s3cr3t")
    for k in ("ts", "task_id", "phase", "executor", "status"):
        assert k in rec
    assert rec["api_key"] == "[REDACTED]"
    line = (tmp_path / "2026-10-10.jsonl").read_text().strip()
    assert json.loads(line) == json.loads(buf.getvalue())
    assert "s3cr3t" not in line


def test_metrics_flush(tmp_path: Path) -> None:
    m = Metrics()
    m.incr("tasks.grok")
    m.incr("tasks.grok")
    m.observe("submit", 10)
    m.observe("submit", 20)
    snap = m.snapshot()
    assert snap == {"tasks.grok": 2.0, "submit.avg_ms": 15.0, "submit.count": 2.0}
    j, c = m.flush(tmp_path)
    assert json.loads(j.read_text()) == snap
    rows = list(csv.reader(c.open()))
    assert rows[0] == ["metric", "value"]
    assert len(rows) == 4
