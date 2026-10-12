"""Google Gemini free tier (AI Studio key in GEMINI_API_KEY, sent as a header)."""

from __future__ import annotations

from typing import Any

from governed_agent_engine.cognition.base import (
    ChatMessage,
    Completion,
    HttpEndpoint,
    resolve_key,
)
from governed_agent_engine.core.errors import ProviderError


class GeminiEndpoint(HttpEndpoint):
    def complete(self, messages: list[ChatMessage]) -> Completion:
        key = resolve_key(self.cfg, self.env) or ""
        system = "\n".join(m.content for m in messages if m.role == "system")
        body: dict[str, Any] = {
            "contents": [
                {
                    "role": "model" if m.role == "assistant" else "user",
                    "parts": [{"text": m.content}],
                }
                for m in messages
                if m.role != "system"
            ],
            "generationConfig": {"temperature": 0},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        data = self.call(
            "POST",
            f"{self.cfg.base_url}/models/{self.model}:generateContent",
            headers={"x-goog-api-key": key},
            body=body,
        )
        try:
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ProviderError(self.name, "unexpected response shape") from exc
        if not text.strip():
            raise ProviderError(self.name, "empty completion")
        return Completion(self.name, self.model, text)
