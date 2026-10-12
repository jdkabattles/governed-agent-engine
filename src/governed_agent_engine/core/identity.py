"""Agent identities and authority for the multi-agent system (+ free model endpoints).

Identities are hard-coded (not configuration) for the same reason the human-approval floor is:
a config edit must not be able to mint a new high-authority principal. ``identities/*.yaml``
documents each bot and is verified against these constants at load (``core/charter.py``);
a mismatch is a tamper and stops the orchestrator. This module is kept identical (apart from
this import line) in example-executor-backend and governed-agent-engine.

Rules enforced here:
* authority is 0..100; only humans may hold >= 50;
* the single exception is the overseer type, which must hold exactly 50 (never more, never less);
* free model endpoints act as ``cognition:<endpoint>`` with authority 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from governed_agent_engine.core.errors import ValidationError


class IdentityError(ValidationError):
    """Invalid identity definition."""


class AgentType(StrEnum):
    HUMAN = "human"
    OVERSEER = "overseer"
    EXECUTOR = "executor"
    ACQUISITION = "acquisition"
    COGNITION = "cognition"


MAX_AUTHORITY = 100
HUMAN_MIN_AUTHORITY = 50  # anything below this is never treated as a human principal
OVERSEER_AUTHORITY = 50  # hard-coded: the overseer sits exactly at the human floor, never above


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    name: str
    type: AgentType
    authority_level: int
    agent_id: str = ""  # Grok Bot agent id where one exists
    display_name: str = ""

    def __post_init__(self) -> None:
        if not 0 <= self.authority_level <= MAX_AUTHORITY:
            raise IdentityError(f"authority_level out of range: {self.authority_level}")
        if self.type is AgentType.OVERSEER:
            if self.authority_level != OVERSEER_AUTHORITY:
                raise IdentityError(f"overseer authority must be exactly {OVERSEER_AUTHORITY}")
        elif self.type is not AgentType.HUMAN and self.authority_level >= HUMAN_MIN_AUTHORITY:
            raise IdentityError(f"non-human identity {self.name!r} cannot hold human authority")

    @property
    def is_human(self) -> bool:
        return self.type is AgentType.HUMAN

    @property
    def is_overseer(self) -> bool:
        return self.type is AgentType.OVERSEER


HUMAN_OWNER = AgentIdentity("owner", AgentType.HUMAN, 100, "", "Owner")
OVERSEER_OVERSEER = AgentIdentity(
    "overseer", AgentType.OVERSEER, 50, "00000000-0000-4000-8000-000000000050", "Overseer Agent"
)
EXECUTOR_GROK = AgentIdentity(
    "grok", AgentType.EXECUTOR, 10, "00000000-0000-4000-8000-000000000010", "Executor Agent"
)
ACQUISITION_ACQUIRER = AgentIdentity(
    "acquirer", AgentType.ACQUISITION, 5, "00000000-0000-4000-8000-000000000005",
    "Acquisition Agent",
)  # fmt: skip
COGNITION_ANALYST = AgentIdentity(
    "analyst", AgentType.COGNITION, 3, "00000000-0000-4000-8000-000000000003", "Analyst Agent"
)
COGNITION_AUTHORITY = 1
UNKNOWN = AgentIdentity("unknown", AgentType.COGNITION, 0)

BOTS: tuple[AgentIdentity, ...] = (
    HUMAN_OWNER,
    OVERSEER_OVERSEER,
    EXECUTOR_GROK,
    ACQUISITION_ACQUIRER,
    COGNITION_ANALYST,
)
KNOWN: dict[str, AgentIdentity] = {i.name: i for i in BOTS}
BY_ID: dict[str, AgentIdentity] = {i.agent_id: i for i in BOTS if i.agent_id}


def cognition_identity(endpoint: str) -> AgentIdentity:
    """Every free model endpoint acts as ``cognition:<endpoint>`` with authority 1."""
    return AgentIdentity(f"cognition:{endpoint}", AgentType.COGNITION, COGNITION_AUTHORITY)


def resolve(name: str | None) -> AgentIdentity:
    """Map ``Task.issued_by`` (name or agent id) to an identity. Unknown issuers get 0."""
    if not name:
        return UNKNOWN
    if name in KNOWN:
        return KNOWN[name]
    if name in BY_ID:
        return BY_ID[name]
    if name.startswith("cognition:") and len(name) > len("cognition:"):
        return cognition_identity(name.split(":", 1)[1])
    return UNKNOWN


def can_approve(who: AgentIdentity, min_authority: int = 100) -> bool:
    """Only a human with enough authority may approve human-approval actions. Never the
    overseer, whatever configuration says."""
    return who.is_human and who.authority_level >= min_authority


def can_block(who: AgentIdentity) -> bool:
    """Review/flag/block (never approve): the human and the overseer."""
    return who.is_human or who.is_overseer
