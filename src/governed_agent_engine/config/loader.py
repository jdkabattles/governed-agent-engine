"""Typed loader for config/{routing,governance,usage,endpoints}.yaml."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from governed_agent_engine.core import validate as v
from governed_agent_engine.core.charter import load_charter
from governed_agent_engine.core.doctrine import Doctrine, load_doctrine
from governed_agent_engine.core.errors import ConfigError, ValidationError
from governed_agent_engine.core.grok_binding import bind
from governed_agent_engine.core.models import (
    TASK_TYPES,
    EndpointConfig,
    Executor,
    GovernanceConfig,
)

CONFIG_FILES = ("routing.yaml", "governance.yaml", "usage.yaml", "endpoints.yaml")


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    default_executor: Executor
    default_chain: tuple[str, ...]
    task_types: dict[str, tuple[str, ...]] = field(default_factory=dict)
    cognition_only_types: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class UsageConfig:
    heartbeat_path: str = "artifacts/evidence/heartbeat.json"
    heartbeat_stale_after_s: int = 7200
    probe_interval_s: int = 900
    loop_interval_s: int = 300


@dataclass(frozen=True, slots=True)
class HealthConfig:
    failure_threshold: int = 3
    cooldown_s: int = 600


@dataclass(frozen=True, slots=True)
class AppConfig:
    root: Path
    routing: RoutingConfig
    governance: GovernanceConfig
    usage: UsageConfig
    endpoints: dict[str, EndpointConfig]
    health: HealthConfig
    doctrine: Doctrine = field(default_factory=Doctrine)
    charter_fingerprint: str = ""  # sha256 over identities/*.yaml + doctrine/doctrine.yaml

    def enabled_endpoints(self) -> dict[str, EndpointConfig]:
        return {n: e for n, e in self.endpoints.items() if e.enabled and e.kind != "judge0"}


def file_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_yaml(path: Path) -> Any:
    if not path.is_file():
        raise ConfigError(f"missing config file: {path}")
    try:
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc


def parse_routing(raw: object) -> RoutingConfig:
    d = v.as_mapping(raw, "routing")
    v.reject_unknown(
        d, ("default_executor", "default_chain", "task_types", "cognition_only_types"), "routing"
    )
    tt_raw = v.as_mapping(d.get("task_types", {}), "routing.task_types")
    task_types: dict[str, tuple[str, ...]] = {}
    for t, chain in tt_raw.items():
        v.choice(t, TASK_TYPES, "routing.task_types")
        task_types[t] = v.str_list(chain, f"routing.task_types.{t}")
    only = frozenset(v.str_list(d.get("cognition_only_types", []), "cognition_only_types"))
    for t in only:
        v.choice(t, TASK_TYPES, "routing.cognition_only_types")
    chain = v.str_list(v.req(d, "default_chain", "routing"), "routing.default_chain")
    if not chain:
        raise ValidationError("routing.default_chain must not be empty")
    return RoutingConfig(
        default_executor=v.enum(Executor, d.get("default_executor", "grok"), "default_executor"),
        default_chain=chain,
        task_types=task_types,
        cognition_only_types=only,
    )


def parse_usage(raw: object) -> UsageConfig:
    d = v.as_mapping(raw, "usage")
    v.reject_unknown(
        d,
        ("heartbeat_path", "heartbeat_stale_after_s", "probe_interval_s", "loop_interval_s"),
        "usage",
    )
    return UsageConfig(
        heartbeat_path=v.s(
            d.get("heartbeat_path", "artifacts/evidence/heartbeat.json"), "heartbeat_path"
        ),
        heartbeat_stale_after_s=v.i(d.get("heartbeat_stale_after_s", 7200), "stale", ge=1),
        probe_interval_s=v.i(d.get("probe_interval_s", 900), "probe_interval_s", ge=1),
        loop_interval_s=v.i(d.get("loop_interval_s", 300), "loop_interval_s", ge=1),
    )


def parse_endpoints(raw: object) -> tuple[dict[str, EndpointConfig], HealthConfig]:
    d = v.as_mapping(raw, "endpoints.yaml")
    v.reject_unknown(d, ("endpoints", "health"), "endpoints.yaml")
    eps = v.as_mapping(v.req(d, "endpoints", "endpoints.yaml"), "endpoints")
    h = v.as_mapping(d.get("health", {}), "health")
    v.reject_unknown(h, ("failure_threshold", "cooldown_s"), "health")
    return (
        {name: EndpointConfig.from_dict(name, cfg) for name, cfg in sorted(eps.items())},
        HealthConfig(
            failure_threshold=v.i(h.get("failure_threshold", 3), "failure_threshold", ge=1),
            cooldown_s=v.i(h.get("cooldown_s", 600), "cooldown_s", ge=1),
        ),
    )


def load_config(config_dir: Path, root: Path | None = None) -> AppConfig:
    gov_path = config_dir / "governance.yaml"
    routing = parse_routing(read_yaml(config_dir / "routing.yaml"))
    governance = GovernanceConfig.from_dict(read_yaml(gov_path), file_fingerprint(gov_path))
    usage = parse_usage(read_yaml(config_dir / "usage.yaml"))
    endpoints, health = parse_endpoints(read_yaml(config_dir / "endpoints.yaml"))

    enabled = {n for n, e in endpoints.items() if e.enabled and e.kind != "judge0"}
    chains = [routing.default_chain, *routing.task_types.values()]
    referenced = {n for chain in chains for n in chain}
    unknown = referenced - set(endpoints)
    if unknown:
        raise ConfigError(f"routing references unknown endpoints: {sorted(unknown)}")
    disabled = referenced - enabled
    if disabled:
        raise ConfigError(f"routing references disabled endpoints: {sorted(disabled)}")
    not_allowed = referenced - governance.knowledge.allowed_endpoints
    if not_allowed:
        raise ConfigError(f"routing uses endpoints not in knowledge.allowed_endpoints: "
                          f"{sorted(not_allowed)}")  # fmt: skip
    base = root if root is not None else config_dir.parent
    doctrine = load_doctrine(config_dir / "doctrine.yaml")
    charter = load_charter(base, doctrine.min_steps, doctrine.min_step_chars)
    bind(base, doctrine)  # .grok binding: named error codes, fail fast
    return AppConfig(
        root=base,
        routing=routing,
        governance=governance,
        usage=usage,
        endpoints=endpoints,
        health=health,
        doctrine=doctrine,
        charter_fingerprint=charter.fingerprint,
    )
