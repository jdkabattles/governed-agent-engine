from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from governed_agent_engine.config.loader import load_config, parse_routing, parse_usage
from governed_agent_engine.core.errors import ConfigError, ValidationError


def _edit(root: Path, name: str, fn: object) -> None:
    p = root / "config" / name
    data = yaml.safe_load(p.read_text())
    fn(data)  # type: ignore[operator]
    p.write_text(yaml.safe_dump(data))


def test_repo_config(root: Path) -> None:
    cfg = load_config(root / "config")
    assert cfg.root == root
    en = cfg.enabled_endpoints()
    assert {
        "pollinations",
        "llm7",
        "ovh",
        "openrouter",
        "gemini",
        "ai_horde",
        "openrouter_nemotron_super",
    } <= set(en)
    assert "openrouter_gemma4_31b" not in en
    dead = {"blackbox", "puter", "deepinfra_anon", "huggingface_serverless", "koboldai_net"}
    assert dead <= set(cfg.endpoints)
    assert all(not cfg.endpoints[d].enabled for d in dead)
    assert all("2026-10-10" in cfg.endpoints[d].status for d in dead)
    assert all(e.cost_usd == 0.0 for e in cfg.endpoints.values())


def test_missing_file(root: Path) -> None:
    (root / "config" / "usage.yaml").unlink()
    with pytest.raises(ConfigError, match="missing"):
        load_config(root / "config", root)


def test_bad_yaml(root: Path) -> None:
    (root / "config" / "usage.yaml").write_text("a: [")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(root / "config", root)


@pytest.mark.parametrize(
    ("fn", "msg"),
    [
        (lambda d: d["default_chain"].append("nope"), "unknown endpoints"),
        (lambda d: d["default_chain"].append("blackbox"), "disabled endpoints"),
    ],
)
def test_routing_refs(root: Path, fn: object, msg: str) -> None:
    _edit(root, "routing.yaml", fn)
    with pytest.raises(ConfigError, match=msg):
        load_config(root / "config", root)


def test_routing_needs_knowledge_permission(root: Path) -> None:
    _edit(root, "governance.yaml", lambda d: d["knowledge"]["allowed_endpoints"].remove("ovh"))
    with pytest.raises(ConfigError, match="allowed_endpoints"):
        load_config(root / "config", root)


def test_parse_errors() -> None:
    with pytest.raises(ValidationError):
        parse_routing({"default_chain": []})
    with pytest.raises(ValidationError):
        parse_routing({"default_chain": ["a"], "task_types": {"bogus": ["a"]}})
    with pytest.raises(ValidationError):
        parse_routing({"default_chain": ["a"], "cognition_only_types": ["bogus"]})
    with pytest.raises(ValidationError):
        parse_usage({"probe_interval_s": 0})
