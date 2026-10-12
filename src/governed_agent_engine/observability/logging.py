"""Structured JSON-lines logging: task_id, phase, executor, status, ts (+ extra fields)."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO

Phase = Literal[
    "intake",
    "normalize",
    "validate",
    "route",
    "execute",
    "cognition",
    "backlog",
    "replay",
    "evidence",
    "governance",
    "loop",
]

_REDACT_KEYS = ("key", "token", "secret", "password", "authorization")


def _redact(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        k: ("[REDACTED]" if any(s in k.lower() for s in _REDACT_KEYS) else v)
        for k, v in fields.items()
    }


class JsonLogger:
    def __init__(
        self,
        log_dir: Path | None,
        stream: TextIO | None = sys.stderr,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._log_dir = log_dir
        self._stream = stream
        self._clock = clock

    def event(
        self,
        *,
        task_id: str,
        phase: Phase,
        executor: str,
        status: str,
        **extra: Any,
    ) -> dict[str, Any]:
        ts = self._clock()
        record: dict[str, Any] = {
            "ts": ts.isoformat(),
            "task_id": task_id,
            "phase": phase,
            "executor": executor,
            "status": status,
            **_redact(extra),
        }
        line = json.dumps(record, sort_keys=True, default=str)
        if self._log_dir is not None:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            with (self._log_dir / f"{ts.date().isoformat()}.jsonl").open(
                "a", encoding="utf-8"
            ) as fh:
                fh.write(line + "\n")
        if self._stream is not None:
            self._stream.write(line + "\n")
        return record
