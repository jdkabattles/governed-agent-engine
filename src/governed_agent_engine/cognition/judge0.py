"""Judge0 CE public instance — sandboxed, offline code validation. LIGHT USE ONLY.

Shared community resource (no network inside the sandbox, ~5 s CPU). Never send secrets.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from governed_agent_engine.cognition.base import HttpEndpoint
from governed_agent_engine.cognition.http import HttpClient
from governed_agent_engine.cognition.rate_limit import TokenBucket
from governed_agent_engine.core.models import EndpointConfig

LANGUAGE_IDS = {"python3": 71, "bash": 46, "javascript": 63, "c": 50, "cpp": 54}


@dataclass(frozen=True, slots=True)
class CodeRun:
    ok: bool
    status: str
    stdout: str = ""
    stderr: str = ""


class Judge0Client(HttpEndpoint):
    def __init__(
        self,
        cfg: EndpointConfig,
        http: HttpClient,
        bucket: TokenBucket,
        env: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(cfg, http, env)
        self.bucket = bucket

    def run(self, source: str, language: str = "python3", stdin: str = "") -> CodeRun:
        if language not in LANGUAGE_IDS:
            raise ValueError(f"unsupported language: {language}")
        if not self.bucket.try_acquire():
            return CodeRun(ok=False, status="local_rate_limited")
        data = self.call(
            "POST",
            f"{self.cfg.base_url}/submissions?base64_encoded=false&wait=true",
            headers={},
            body={"source_code": source, "language_id": LANGUAGE_IDS[language], "stdin": stdin},
            timeout=30.0,
        )
        status = str((data.get("status") or {}).get("description", "unknown"))
        return CodeRun(
            ok=status == "Accepted",
            status=status,
            stdout=data.get("stdout") or "",
            stderr=(data.get("stderr") or "") + (data.get("compile_output") or ""),
        )
