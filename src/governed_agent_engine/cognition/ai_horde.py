"""AI Horde (volunteer GPUs): async submit -> poll -> collect. Anonymous key 0000000000."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping

from governed_agent_engine.cognition.base import (
    ChatMessage,
    Completion,
    HttpEndpoint,
    resolve_key,
)
from governed_agent_engine.cognition.http import USER_AGENT, HttpClient
from governed_agent_engine.core.errors import ProviderError
from governed_agent_engine.core.models import EndpointConfig


def render_prompt(messages: list[ChatMessage]) -> str:
    parts = [f"### {m.role.capitalize()}:\n{m.content}" for m in messages]
    return "\n\n".join([*parts, "### Assistant:\n"])


class AIHordeEndpoint(HttpEndpoint):
    def __init__(
        self,
        cfg: EndpointConfig,
        http: HttpClient,
        env: Mapping[str, str] | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval_s: float = 5.0,
        max_new_tokens: int = 256,
    ) -> None:
        super().__init__(cfg, http, env)
        self._sleep, self._poll, self._max = sleep, poll_interval_s, max_new_tokens

    def complete(self, messages: list[ChatMessage]) -> Completion:
        headers = {"apikey": resolve_key(self.cfg, self.env) or "0000000000",
                   "Client-Agent": USER_AGENT}  # fmt: skip
        body = {"prompt": render_prompt(messages),
                "params": {"max_length": self._max, "max_context_length": 2048}}  # fmt: skip
        sub = self.call("POST", f"{self.cfg.base_url}/generate/text/async", headers=headers,
                        body=body, timeout=30.0)  # fmt: skip
        job = sub.get("id") if isinstance(sub, dict) else None
        if not job:
            raise ProviderError(self.name, "no job id returned")
        waited = 0.0
        while waited <= self.cfg.timeout_s:
            st = self.call("GET", f"{self.cfg.base_url}/generate/text/status/{job}",
                           headers=headers, timeout=30.0)  # fmt: skip
            if st.get("faulted") or st.get("is_possible") is False:
                raise ProviderError(self.name, "job faulted or impossible")
            if st.get("done"):
                gens = st.get("generations") or []
                if not gens or not str(gens[0].get("text", "")).strip():
                    raise ProviderError(self.name, "empty generation")
                return Completion(
                    self.name, str(gens[0].get("model", self.model)), str(gens[0]["text"])
                )
            self._sleep(self._poll)
            waited += self._poll
        raise ProviderError(self.name, f"timed out after {self.cfg.timeout_s}s")
