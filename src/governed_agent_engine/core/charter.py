"""Multi-agent charter: ``identities/*.yaml`` + ``doctrine/doctrine.yaml``, verified against code.

The identities in ``core/identity.py`` are the source of truth. Each YAML must restate its bot
exactly (name, id, type, authority, display name), deny every ``approve:<action>`` for
non-humans, escalate upward to Owner, and no file may be missing or extra. The doctrine file
must carry the verbatim statement and every operational rule. Any mismatch raises
``CharterError``; the SHA-256 over all files lets the orchestrator halt if they change while it
runs. Kept identical (apart from imports) in example-executor-backend and governed-agent-engine.
"""

from __future__ import annotations

import hashlib
import itertools
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from governed_agent_engine.core import identity
from governed_agent_engine.core.doctrine import DOCTRINE_STATEMENT
from governed_agent_engine.core.grok_format import GrokFormatError, GrokValue, parse_grok
from governed_agent_engine.core.identity import AgentIdentity

APPROVAL_ACTIONS: tuple[str, ...] = (
    "send_message", "delete", "purchase", "publish", "trade", "modify_governance",
)  # fmt: skip
IDENTITY_KEYS = frozenset({
    "version", "name", "display_name", "former_name", "id", "type", "authority", "role",
    "purpose", "allowed_actions", "forbidden_actions", "integrations", "escalation_path",
})  # fmt: skip
DOCTRINE_RULES: dict[str, tuple[str, ...]] = {
    "anti_laziness": ("min_plan_steps", "min_step_chars", "require_gather_before_execute",
                      "no_filler_or_duplicate_steps"),
    "truth_and_evidence": ("facts_over_guesses", "cite_evidence",
                           "evidence_required_for_execute_and_verify",
                           "done_without_evidence_is_failed"),
    "failure_modes": ("list_failure_modes_for_execute_steps",
                      "record_every_rejection_as_evidence", "analyse_failures_before_retry"),
    "improvement": ("mandatory_improve_step_after_verify", "feed_lessons_into_next_plan"),
    "wisdom": ("principle", "stages"),
}  # fmt: skip
REPO_STATUSES = frozenset({"existing", "planned"})
EXISTING_REPOS = frozenset({"governed-agent-engine", "example-executor-backend"})
WISDOM_PRINCIPLE = "knowledge -> understanding -> improved action"
WISDOM_STAGES = ("knowledge", "understanding", "improved_action")


class CharterError(ValueError):
    """identities/ or doctrine/ disagree with the code-fixed charter (tamper or drift).

    ``code`` is a named error code (see core/grok_binding.py ``ErrorCode``)."""

    def __init__(self, message: str, code: str = "CHARTER_MISMATCH") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class BotCharter:
    identity: AgentIdentity
    former_name: str | None
    role: str
    purpose: str
    allowed_actions: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    integrations: tuple[tuple[str, str], ...]
    escalation_path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Charter:
    bots: dict[str, BotCharter]
    rules: dict[str, Any]
    fingerprint: str

    def bot(self, name: str) -> BotCharter:
        return self.bots[name]


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CharterError(f"{path.name}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise CharterError(f"{path.name}: expected a mapping")
    return raw


def _strs(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        raise CharterError(f"{where}: expected a list of non-empty strings")
    return tuple(value)


def _check_bot(raw: dict[str, Any], fixed: AgentIdentity, fname: str) -> BotCharter:
    keys = set(raw)
    if keys != IDENTITY_KEYS:
        raise CharterError(
            f"{fname}: keys differ (missing {sorted(IDENTITY_KEYS - keys)}, "
            f"unknown {sorted(keys - IDENTITY_KEYS)})"
        )
    actual = (raw["name"], raw["id"] or "", raw["type"], raw["authority"], raw["display_name"])
    want = (fixed.name, fixed.agent_id, fixed.type.value, fixed.authority_level,
            fixed.display_name)  # fmt: skip
    if actual != want:
        raise CharterError(f"{fname}: identity does not match code: {actual} != {want}")
    allowed = _strs(raw["allowed_actions"], f"{fname}.allowed_actions")
    forbidden = _strs(raw["forbidden_actions"], f"{fname}.forbidden_actions")
    approvals = {f"approve:{a}" for a in APPROVAL_ACTIONS}
    if not fixed.is_human:
        if set(allowed) & approvals:
            raise CharterError(f"{fname}: only a human may approve human-approval actions")
        if not approvals <= set(forbidden):
            raise CharterError(f"{fname}: must forbid {sorted(approvals - set(forbidden))}")
        if fixed.is_overseer and "override_owner" not in forbidden:
            raise CharterError(f"{fname}: overseer must forbid override_owner")
    if set(allowed) & set(forbidden):
        raise CharterError(f"{fname}: actions both allowed and forbidden")
    path = _strs(raw["escalation_path"], f"{fname}.escalation_path") if raw[
        "escalation_path"] else ()  # fmt: skip
    levels = [fixed.authority_level]
    for step in path:
        nxt = identity.KNOWN.get(step)
        if nxt is None:
            raise CharterError(f"{fname}: unknown escalation target {step!r}")
        levels.append(nxt.authority_level)
    if any(b <= a for a, b in itertools.pairwise(levels)):
        raise CharterError(f"{fname}: escalation must go strictly upward in authority")
    if not fixed.is_human and (not path or path[-1] != identity.HUMAN_OWNER.name):
        raise CharterError(f"{fname}: escalation must end with owner")
    ints = raw["integrations"]
    if (
        not isinstance(ints, list)
        or not ints
        or not all(isinstance(i, dict) and set(i) == {"repo", "status", "use"} for i in ints)
    ):
        raise CharterError(
            f"{fname}: integrations must be a non-empty list of {{repo, status, use}}"
        )
    for i in ints:
        expect = "existing" if i["repo"] in EXISTING_REPOS else "planned"
        if i["status"] not in REPO_STATUSES or (
            i["repo"] in EXISTING_REPOS and i["status"] != expect
        ):
            raise CharterError(f"{fname}: {i['repo']}: status must be {expect}")
    for k in ("role", "purpose"):
        if not isinstance(raw[k], str) or not raw[k].strip():
            raise CharterError(f"{fname}: {k} must be a non-empty string")
    former = raw["former_name"]
    if former is not None and not isinstance(former, str):
        raise CharterError(f"{fname}: former_name must be a string or null")
    return BotCharter(fixed, former, raw["role"].strip(), " ".join(raw["purpose"].split()),
                      allowed, forbidden,
                      tuple((str(i["repo"]), str(i["use"])) for i in ints), path)  # fmt: skip


def _check_rules(raw: dict[str, Any], min_steps: int | None, min_chars: int | None) -> Any:
    if " ".join(str(raw.get("statement", "")).split()) != DOCTRINE_STATEMENT:
        raise CharterError("doctrine.yaml: statement must match the user's statement verbatim")
    rules = raw.get("rules")
    if not isinstance(rules, dict) or set(rules) != set(DOCTRINE_RULES):
        raise CharterError(f"doctrine.yaml: rules must have exactly {sorted(DOCTRINE_RULES)}")
    for section, keys in DOCTRINE_RULES.items():
        sec = rules[section]
        if not isinstance(sec, dict) or set(sec) != set(keys):
            raise CharterError(f"doctrine.yaml: rules.{section} must have exactly {list(keys)}")
        for k in keys:
            if section not in ("wisdom", "anti_laziness") and sec[k] is not True:
                raise CharterError(f"doctrine.yaml: rules.{section}.{k} must be true")
    al = rules["anti_laziness"]
    if al["require_gather_before_execute"] is not True or (
        al["no_filler_or_duplicate_steps"] is not True
    ):
        raise CharterError("doctrine.yaml: anti_laziness flags must be true")
    if min_steps is not None and al["min_plan_steps"] != min_steps:
        raise CharterError("doctrine.yaml: min_plan_steps must equal config/doctrine.yaml")
    if min_chars is not None and al["min_step_chars"] != min_chars:
        raise CharterError("doctrine.yaml: min_step_chars must equal config/doctrine.yaml")
    w = rules["wisdom"]
    stages = w["stages"]
    if (
        w["principle"] != WISDOM_PRINCIPLE
        or not isinstance(stages, list)
        or tuple(s.get("stage") if isinstance(s, dict) else None for s in stages) != WISDOM_STAGES
    ):
        raise CharterError(f"doctrine.yaml: wisdom must be '{WISDOM_PRINCIPLE}' in that order")
    return rules


_MIN_STEPS = re.compile(r"at least (\d+) steps")
GROK_FILES = ("identity.grok", "doctrine.grok", "directive.grok")


def charter_files(root: Path) -> list[Path]:
    return [
        *sorted((root / "identities").glob("*.yaml")),
        root / "doctrine" / "doctrine.yaml",
        *(root / "config" / f for f in GROK_FILES),
        *sorted((root / "config" / "agents").glob("*.grok")),
    ]


_GROK_CODES = {
    "identity.grok": "IDENTITY_MISMATCH", "doctrine.grok": "DOCTRINE_MISMATCH",
    "directive.grok": "DIRECTIVE_MISMATCH",
}  # fmt: skip


def read_grok(path: Path, code: str) -> dict[str, GrokValue]:
    """Parse a ``.grok`` file (FORMAT "grok/1"); errors raise ``CharterError(code)``."""
    if not path.is_file():
        raise CharterError(f"missing {path.parent.name}/{path.name}", code)
    try:
        data = parse_grok(path.read_text(encoding="utf-8"))
    except GrokFormatError as exc:
        raise CharterError(f"{path.name}: {exc}", code) from exc
    if data.get("FORMAT") != "grok/1":
        raise CharterError(f'{path.name}: FORMAT must be "grok/1"', code)
    return data


def _grok(root: Path, name: str) -> dict[str, GrokValue]:
    return read_grok(root / "config" / name, _GROK_CODES[name])


def _check_grok(root: Path, bots: dict[str, BotCharter], rules: dict[str, Any]) -> None:
    """config/*.grok must agree with identities/*.yaml and doctrine/doctrine.yaml."""
    ident = _grok(root, "identity.grok")
    blocks = ident.get("IDENTITIES")
    if not isinstance(blocks, dict):
        raise CharterError("identity.grok: IDENTITIES must be a mapping", code="IDENTITY_MISMATCH")
    seen: set[str] = set()
    for key, b in blocks.items():
        name = b.get("NAME") if isinstance(b, dict) else None
        if not isinstance(b, dict) or not isinstance(name, str):
            raise CharterError(f"identity.grok: {key} needs NAME", code="IDENTITY_MISMATCH")
        bot = bots.get(name)
        if bot is None or name in seen:
            raise CharterError(
                f"identity.grok: unknown or duplicate identity {name!r}", code="IDENTITY_MISMATCH"
            )
        seen.add(name)
        i = bot.identity
        got = (b.get("DISPLAY_NAME"), b.get("FORMER_NAME"), b.get("ID"), b.get("TYPE"),
               b.get("AUTHORITY"))  # fmt: skip
        want = (i.display_name, bot.former_name, i.agent_id or None, i.type.value,
                i.authority_level)  # fmt: skip
        if got != want:
            raise CharterError(
                f"identity.grok: {name} does not match identities/: {got}", code="IDENTITY_MISMATCH"
            )
    if seen != set(bots):
        raise CharterError(
            f"identity.grok: missing identities {sorted(set(bots) - seen)}",
            code="IDENTITY_MISMATCH",
        )
    bounds = ident.get("BOUNDS")
    if (
        ident.get("FINAL_AUTHORITY") != identity.HUMAN_OWNER.name
        or ident.get("COGNITION_ENDPOINT_AUTHORITY") != identity.COGNITION_AUTHORITY
        or not isinstance(bounds, dict)
        or bounds.get("OVERSEER_CANNOT_OVERRIDE") != identity.HUMAN_OWNER.name
        or bounds.get("APPROVER") != identity.HUMAN_OWNER.name
        or bounds.get("HUMAN_APPROVAL_ACTIONS") != list(APPROVAL_ACTIONS)
    ):
        raise CharterError(
            "identity.grok: authority bounds do not match the code", code="IDENTITY_MISMATCH"
        )
    doc = _grok(root, "doctrine.grok")
    if doc.get("STATEMENT") != DOCTRINE_STATEMENT:
        raise CharterError(
            "doctrine.grok: STATEMENT must match the doctrine verbatim", code="DOCTRINE_MISMATCH"
        )
    if doc.get("RULES") != {
        "TRUTH_PRIORITY": "high", "EVIDENCE_REQUIRED": True, "LAZINESS_PENALTY": "high",
        "FAILURE_ANALYSIS_REQUIRED": True, "IMPROVEMENT_LOOP": "mandatory",
    }:  # fmt: skip
        raise CharterError(
            "doctrine.grok: RULES do not match the doctrine", code="DOCTRINE_MISMATCH"
        )
    req = doc.get("OPERATIONAL_REQUIREMENTS")
    if not isinstance(req, list) or not req or not all(isinstance(x, str) and x for x in req):
        raise CharterError(
            "doctrine.grok: OPERATIONAL_REQUIREMENTS must list strings", code="DOCTRINE_MISMATCH"
        )
    steps = [int(m.group(1)) for x in req if (m := _MIN_STEPS.search(str(x)))]
    if steps != [rules["anti_laziness"]["min_plan_steps"]]:
        raise CharterError(
            "doctrine.grok: 'at least N steps' must match doctrine.yaml", code="DOCTRINE_MISMATCH"
        )
    d = _grok(root, "directive.grok")
    db = d.get("BOUNDS")
    if d.get("ISSUED_BY") != identity.HUMAN_OWNER.name or db != {
        "MAY_CHANGE_AUTHORITY": False, "MAY_APPROVE_HUMAN_ACTIONS": False,
    }:  # fmt: skip
        raise CharterError(
            "directive.grok: must be issued by owner with fixed BOUNDS",
            code="DIRECTIVE_MISMATCH",
        )
    status = d.get("STATUS")
    if status == "active":
        for k in ("DIRECTIVE", "RESPONSE_WINDOW"):
            v = d.get(k)
            if not isinstance(v, str) or not v.strip():
                raise CharterError(
                    f"directive.grok: active directive needs {k}", code="DIRECTIVE_MISMATCH"
                )
        text = " ".join(str(d["DIRECTIVE"]).replace('"', " ").split())
        if DOCTRINE_STATEMENT not in text:
            raise CharterError(
                "directive.grok: DIRECTIVE must quote the doctrine verbatim",
                code="DIRECTIVE_MISMATCH",
            )
    elif status != "pending":
        raise CharterError(
            'directive.grok: STATUS must be "pending" or "active"', code="DIRECTIVE_MISMATCH"
        )


def charter_fingerprint(root: Path) -> str:
    h = hashlib.sha256()
    for p in charter_files(root):
        h.update(p.relative_to(root).as_posix().encode() + b"\0")
        h.update(p.read_bytes() if p.is_file() else b"<missing>")
    return h.hexdigest()


def load_charter(
    root: Path, min_steps: int | None = None, min_step_chars: int | None = None
) -> Charter:
    ident_dir, doc = root / "identities", root / "doctrine" / "doctrine.yaml"
    if not ident_dir.is_dir() or not doc.is_file():
        raise CharterError(f"missing identities/ or doctrine/doctrine.yaml under {root}")
    files = {p.stem: p for p in ident_dir.glob("*.yaml")}
    if set(files) != set(identity.KNOWN):
        raise CharterError(
            f"identities/: expected exactly {sorted(identity.KNOWN)}.yaml, got {sorted(files)}"
        )
    bots = {n: _check_bot(_read(p), identity.KNOWN[n], p.name) for n, p in sorted(files.items())}
    rules = _check_rules(_read(doc), min_steps, min_step_chars)
    _check_grok(root, bots, rules)
    return Charter(bots, rules, charter_fingerprint(root))
