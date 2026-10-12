"""The only boundary through which work reaches Grok Bot."""

from __future__ import annotations

from typing import Protocol

from governed_agent_engine.core.models import BacklogEntry, GrokResponse, GrokResult
from governed_agent_engine.grok.client import GrokClient


class Executor(Protocol):
    def dispatch(self, entry: BacklogEntry) -> GrokResponse: ...

    def collect(self, entry_id: str) -> GrokResult | None: ...


class GrokExecutor:
    def __init__(self, client: GrokClient) -> None:
        self.client = client

    def dispatch(self, entry: BacklogEntry) -> GrokResponse:
        return self.client.transport.submit(entry)

    def collect(self, entry_id: str) -> GrokResult | None:
        return self.client.transport.fetch_result(entry_id)
