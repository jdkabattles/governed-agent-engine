from __future__ import annotations

from pathlib import Path

from governed_agent_engine.cognition.http import HttpResponse
from governed_agent_engine.core.errors import ProviderError
from governed_agent_engine.smoke import run_smoke

from .conftest import FakeHttp, jr, oai


def test_smoke_with_fake_http(root: Path, tmp_path: Path, monkeypatch: object) -> None:
    import os

    os.environ.pop("OPENROUTER_API_KEY", None)
    os.environ.pop("GEMINI_API_KEY", None)
    h = FakeHttp()
    h.add("POST", "https://text.pollinations.ai", oai("PONG"))
    h.add("POST", "https://api.llm7.io", HttpResponse(429, b""))
    h.add("POST", "https://oai.endpoints", jr(500, {}))
    h.add("POST", "https://ce.judge0.com", jr(200, {"status": {"description": "Accepted"},
                                                    "stdout": "42"}))  # fmt: skip
    md = tmp_path / "s.md"
    rows = run_smoke(root, "pollinations,llm7,ovh,openrouter", judge0=True, markdown=md, http=h)
    assert [r["outcome"] for r in rows] == ["ok", "rate_limited", "error", "auth_error", "ok"]
    assert "| pollinations | ok |" in md.read_text()


def test_smoke_judge0_error(root: Path) -> None:
    h = FakeHttp()
    h.add("POST", "https://text.pollinations.ai", oai())
    h.add("POST", "https://ce.judge0.com", ProviderError("judge0", "x"))
    rows = run_smoke(root, "pollinations", judge0=True, http=h)
    assert rows[-1]["outcome"] == "error"
