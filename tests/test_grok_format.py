"""The .grok spec format (strict YAML subset) and its consistency with identities/doctrine."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from governed_agent_engine.core.charter import CharterError, load_charter
from governed_agent_engine.core.grok_format import GrokFormatError, parse_grok

REPO = Path(__file__).resolve().parent.parent
GROK = ("identity.grok", "doctrine.grok", "directive.grok")


def _root(tmp_path: Path) -> Path:
    for d in ("identities", "doctrine"):
        shutil.copytree(REPO / d, tmp_path / d)
    (tmp_path / "config").mkdir()
    for f in GROK:
        shutil.copy(REPO / "config" / f, tmp_path / "config" / f)
    return tmp_path


@pytest.mark.parametrize("name", GROK)
def test_files_are_yaml_subset(name: str) -> None:
    text = (REPO / "config" / name).read_text(encoding="utf-8")
    assert parse_grok(text) == yaml.safe_load(text)


def test_parser_values() -> None:
    text = 'A: 1\nB: -7\nC: 0\nL:\n  - "x"\n  - true\n  - null\nM:  # c\n  S: "q\\"\\\\\\n\\t"\n'
    v = parse_grok(text + "\n# end\n")
    assert v == {"A": 1, "B": -7, "C": 0, "L": ["x", True, None], "M": {"S": 'q"\\\n\t'}}
    assert v == yaml.safe_load(text)
    assert parse_grok('K: "a # not a comment" # real\n') == {"K": "a # not a comment"}


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("\ufeffA: 1\n", "BOM"),
        ("A: 1\r\n", "CR"),
        ("A:\n\t- 1\n", "tabs"),
        ("A:\n   B: 1\n", "multiple of two"),
        ("A: plain\n", "double-quoted"),
        ("A: 01\n", "double-quoted"),
        ('A: "open\n', "unterminated"),
        ('A: "\\x"\n', "invalid escape"),
        ("A: 1 2\n", "unexpected text"),
        ("A:1\n", "space must follow"),
        ("A:#c\n  B: 1\n", "comment must follow"),
        ("A: 1\nA: 2\n", "duplicate"),
        ("A:\n", "non-empty nested block"),
        ("A:\nB: 1\n", "non-empty nested block"),
        ("A:\n  - 1\n  B: 2\n", "cannot mix"),
        ("A: 1\n  B: 2\n", "unexpected indentation"),
        ("- 1\n", "top level must be a mapping"),
        ("1x: 2\n", "expected KEY"),
        ("A: {b: 1}\n", "double-quoted"),
        ("", "non-empty nested block"),
    ],
)
def test_parser_rejects(text: str, needle: str) -> None:
    with pytest.raises(GrokFormatError, match=needle):
        parse_grok(text)


def _swap(path: Path, old: str, new: str) -> None:
    s = path.read_text()
    assert old in s
    path.write_text(s.replace(old, new, 1))


@pytest.mark.parametrize(
    ("name", "old", "new", "needle"),
    [
        ("identity.grok", "AUTHORITY: 50", "AUTHORITY: 49", "does not match identities"),
        ("identity.grok", 'NAME: "analyst"', 'NAME: "zed"', "unknown or duplicate"),
        ("identity.grok", 'NAME: "analyst"', 'NAME: "grok"', "unknown or duplicate"),
        ("identity.grok", '    NAME: "analyst"\n', "", "needs NAME"),
        ("identity.grok", 'FINAL_AUTHORITY: "owner"', 'FINAL_AUTHORITY: "overseer"', "bounds"),
        ("identity.grok", 'APPROVER: "owner"', 'APPROVER: "overseer"', "bounds"),
        ("identity.grok", '    - "trade"\n', "", "bounds"),
        ("identity.grok", 'FORMAT: "grok/1"', 'FORMAT: "grok/2"', "FORMAT"),
        ("identity.grok", "IDENTITIES:\n", "IDENTITIES: null\nX:\n", "IDENTITIES"),
        ("doctrine.grok", "Example doctrine:", "Sample doctrine:", "verbatim"),
        ("doctrine.grok", "at least 3 steps", "at least 2 steps", "at least N steps"),
        ("doctrine.grok", "EVIDENCE_REQUIRED: true", "EVIDENCE_REQUIRED: false", "RULES"),
        ("doctrine.grok", "OPERATIONAL_REQUIREMENTS:\n", "OPERATIONAL_REQUIREMENTS: null\nX:\n", "must list"),  # noqa: E501
        ("directive.grok", "Example doctrine:", "Sample doctrine:", "quote the doctrine"),
        ("directive.grok", "RESPONSE_WINDOW: \"", "RESPONSE_WINDOW: \"  \"\nX: \"", "needs RESPONSE_WINDOW"),  # noqa: E501
        ("doctrine.grok", "RULES:\n", "RULES: null\nX:\n", "RULES"),
        ("directive.grok", 'ISSUED_BY: "owner"', 'ISSUED_BY: "overseer"', "issued by"),
        ("directive.grok", "MAY_CHANGE_AUTHORITY: false", "MAY_CHANGE_AUTHORITY: true", "BOUNDS"),
        ("directive.grok", 'STATUS: "active"', 'STATUS: "maybe"', "STATUS"),
        ("directive.grok", "DIRECTIVE: \"", "DIRECTIVE: null\nX: \"", "needs DIRECTIVE"),
        ("directive.grok", 'STATUS: "active"', "STATUS: active", "line 9"),
    ],
)  # fmt: skip
def test_grok_mismatch_blocks(tmp_path: Path, name: str, old: str, new: str,
                              needle: str) -> None:  # fmt: skip
    root = _root(tmp_path)
    _swap(root / "config" / name, old, new)
    with pytest.raises(CharterError, match=needle):
        load_charter(root)


def test_identity_block_must_be_complete(tmp_path: Path) -> None:
    root = _root(tmp_path)
    p = root / "config" / "identity.grok"
    s = p.read_text()
    start = s.index("  ANALYST_COGNITION_NODE:")
    end = s.index("BOUNDS:")
    p.write_text(s[:start] + s[end:])
    with pytest.raises(CharterError, match="missing identities"):
        load_charter(root)
