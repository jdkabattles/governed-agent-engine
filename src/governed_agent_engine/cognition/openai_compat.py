"""OpenAI-compatible chat endpoints: Pollinations, LLM7, OVH, OpenRouter."""

from __future__ import annotations

from governed_agent_engine.cognition.base import (
    ChatMessage,
    Completion,
    HttpEndpoint,
    resolve_key,
)
from governed_agent_engine.core.errors import ProviderError


class OpenAICompatEndpoint(HttpEndpoint):
    def complete(self, messages: list[ChatMessage]) -> Completion:
        headers: dict[str, str] = {}
        key = resolve_key(self.cfg, self.env)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = {"model": self.model, "messages": [m.to_dict() for m in messages], "temperature": 0}
        data = self.call("POST", self.cfg.base_url + self.cfg.chat_path, headers=headers, body=body)
        try:
            text = data["choices"][0]["message"]["content"]
            model = str(data.get("model") or self.model)
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(self.name, "unexpected response shape") from exc
        if not isinstance(text, str) or not text.strip():
            raise ProviderError(self.name, "empty completion")
        return Completion(self.name, model, text)
