from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from governed_agent_engine.cognition.http import HttpResponse
from governed_agent_engine.config.loader import AppConfig, load_config
from governed_agent_engine.core.models import Task

REPO = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


class Mono:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


Handler = Callable[[str, str, dict[str, str], Any], HttpResponse]


class FakeHttp:
    """Records requests; routes by (method, url-prefix) to queued responses or a handler."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, list[HttpResponse | Exception] | Handler]] = []
        self.calls: list[tuple[str, str, dict[str, str], Any]] = []

    def add(self, method: str, prefix: str, *responses: HttpResponse | Exception) -> None:
        self.routes.append((method, prefix, list(responses)))

    def handle(self, method: str, prefix: str, fn: Handler) -> None:
        self.routes.append((method, prefix, fn))

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: object | None = None,
        timeout: float = 60.0,
    ) -> HttpResponse:
        self.calls.append((method, url, headers, json_body))
        for m, prefix, target in self.routes:
            if m == method and url.startswith(prefix):
                if callable(target):
                    return target(method, url, headers, json_body)
                item = target.pop(0) if len(target) > 1 else target[0]
                if isinstance(item, Exception):
                    raise item
                return item
        raise AssertionError(f"unexpected request {method} {url}")


def jr(status: int, payload: object, headers: dict[str, str] | None = None) -> HttpResponse:
    return HttpResponse(status, json.dumps(payload).encode(), headers or {})


GOOD_PLAN = json.dumps({
    "summary": "Plan the task using facts first.",
    "steps": [
        {"index": 0, "kind": "gather", "description": "Collect the relevant facts and inputs",
         "evidence_required": False, "failure_modes": []},
        {"index": 1, "kind": "execute", "description": "Draft the requested artifact carefully",
         "evidence_required": True, "failure_modes": ["inputs incomplete"]},
        {"index": 2, "kind": "verify", "description": "Check the draft against the facts",
         "evidence_required": True, "failure_modes": []},
        {"index": 3, "kind": "improve", "description": "Apply what verification revealed",
         "evidence_required": False, "failure_modes": []},
    ],
})  # fmt: skip


def oai(text: str = GOOD_PLAN, model: str = "m1") -> HttpResponse:
    return jr(200, {"model": model, "choices": [{"message": {"content": text}}]})


@pytest.fixture
def root(tmp_path: Path) -> Path:
    for d in ("config", "identities", "doctrine"):
        shutil.copytree(REPO / d, tmp_path / d)
    for d in ("plans", "backlog", "evidence", "logs", "metrics", "inbox"):
        (tmp_path / "artifacts" / d).mkdir(parents=True)
    return tmp_path


@pytest.fixture
def config(root: Path) -> AppConfig:
    return load_config(root / "config", root)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_task(**kw: object) -> Task:
    base: dict[str, object] = {
        "task_id": "t1",
        "type": "custom",
        "action": "plan",
        "issued_by": "owner",
    }
    base.update(kw)
    return Task.from_dict(base)
