"""Doctrine: the user's operating principles, loaded from config/doctrine.yaml and enforced.

The statement is pinned in code: the YAML must reproduce it verbatim, so the doctrine cannot be
silently reworded by a config edit (same spirit as governance immutability). The numeric/
boolean knobs are validated and used by ``orchestrator/doctrine_gate.py``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

from governed_agent_engine.core import validate as v
from governed_agent_engine.core.errors import ConfigError

# EXAMPLE statement. Deployments replace it (and every copy in config/ + doctrine/) with their
# own; the binding checks keep all copies identical.
DOCTRINE_STATEMENT = (
    "Example doctrine: do the work completely, ground every claim in evidence, analyse every "
    "failure, and improve after each run."
)
FIELDS = (
    "version", "statement", "truth_priority", "evidence_required", "laziness_penalty",
    "failure_analysis_required", "improvement_loop", "min_steps", "min_step_chars",
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class Doctrine:
    statement: str = DOCTRINE_STATEMENT
    truth_priority: int = 1  # 1 = truth outranks every other objective (speed, brevity, cost)
    evidence_required: bool = True
    laziness_penalty: float = 0.5  # utility multiplier per doctrine rejection (per session)
    failure_analysis_required: bool = True
    improvement_loop: bool = True
    min_steps: int = 3
    min_step_chars: int = 12
    version: int = 1
    fingerprint: str = ""

    def system_prompt(self) -> str:
        """Text injected into every cognition system prompt."""
        return (
            f"DOCTRINE: {self.statement}\n"
            "Truth first: state only facts you can support; say 'unknown' instead of guessing."
        )


def parse_doctrine(raw: object, fingerprint: str = "") -> Doctrine:
    d = v.as_mapping(raw, "doctrine")
    v.reject_unknown(d, FIELDS, "doctrine")
    statement = " ".join(v.s(v.req(d, "statement", "doctrine"), "doctrine.statement").split())
    if statement != DOCTRINE_STATEMENT:
        raise ConfigError("doctrine.statement must match the user's statement verbatim")
    return Doctrine(
        statement=statement,
        truth_priority=v.i(d.get("truth_priority", 1), "doctrine.truth_priority", ge=1, le=1),
        evidence_required=v.b(d.get("evidence_required", True), "doctrine.evidence_required"),
        laziness_penalty=v.f(
            d.get("laziness_penalty", 0.5), "doctrine.laziness_penalty", ge=0.0, le=1.0
        ),
        failure_analysis_required=v.b(
            d.get("failure_analysis_required", True), "doctrine.failure_analysis_required"
        ),
        improvement_loop=v.b(d.get("improvement_loop", True), "doctrine.improvement_loop"),
        min_steps=v.i(d.get("min_steps", 3), "doctrine.min_steps", ge=1, le=20),
        min_step_chars=v.i(d.get("min_step_chars", 12), "doctrine.min_step_chars", ge=1, le=200),
        version=v.i(d.get("version", 1), "doctrine.version", ge=1),
        fingerprint=fingerprint,
    )


def load_doctrine(path: Path) -> Doctrine:
    if not path.is_file():
        raise ConfigError(f"missing doctrine file: {path}")
    data = path.read_bytes()
    try:
        raw = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name}: invalid YAML: {exc}") from exc
    try:
        return parse_doctrine(raw, hashlib.sha256(data).hexdigest())
    except ValueError as exc:
        raise ConfigError(f"{path.name}: {exc}") from exc
