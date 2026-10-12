"""Error hierarchy. Every error raised by this package derives from HghaError."""

from __future__ import annotations

from typing import Any


class HghaError(Exception):
    """Base error."""


class ValidationError(HghaError, ValueError):
    """Input failed validation (models, config, inbox files)."""


class ConfigError(ValidationError):
    """Configuration missing or invalid."""


class GovernanceError(HghaError, PermissionError):
    """A task or action violates APCK governance."""


class GovernanceTamperError(GovernanceError):
    """Governance config changed while the agent was running (mutation is disallowed)."""


class BacklogError(HghaError):
    """Backlog immutability or transition violation."""


class TransportError(HghaError):
    """Grok transport failure (filesystem / git)."""


class ProviderError(HghaError):
    """Cognition provider failure (network, 5xx, malformed body)."""

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider


class RateLimitedError(ProviderError):
    """HTTP 429 from the provider."""

    def __init__(self, provider: str, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(provider, message)
        self.retry_after_s = retry_after_s


class AuthError(ProviderError):
    """Missing or rejected credential (401/403 or env var unset)."""


class AllEndpointsFailedError(HghaError):
    """Every candidate endpoint was skipped or failed."""

    def __init__(self, attempts: tuple[Any, ...]) -> None:
        super().__init__(f"all endpoints failed ({len(attempts)} attempts)")
        self.attempts = attempts


class DoctrineError(HghaError):
    """Every candidate model produced a plan that violates the doctrine."""

    def __init__(self, rejections: tuple[dict[str, object], ...]) -> None:
        self.rejections = rejections
        super().__init__(f"no doctrine-compliant plan after {len(rejections)} attempt(s)")
