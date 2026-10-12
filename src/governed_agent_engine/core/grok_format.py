"""Parser for the ``.grok`` spec format: a strict, line-oriented subset of YAML 1.2.

Any ``.grok`` file is valid YAML and parses to the same value with any YAML loader, but the
grammar is small enough to implement in a few dozen lines in any language (docs/grok_format.md):

    file     := line*            (UTF-8, LF line endings, no tabs, no BOM)
    line     := blank | comment | entry | item
    comment  := ws* "#" any*
    entry    := indent KEY ":" ( " " scalar )? ws* comment?   (no scalar => nested block)
    item     := indent "- " scalar ws* comment?
    KEY      := [A-Za-z][A-Za-z0-9_]*
    scalar   := "null" | "true" | "false" | int | dqstring
    int      := "-"? ( "0" | [1-9][0-9]* )
    dqstring := '"' ( any char except '"' or '\\' | '\\' ( '"' | '\\' | "n" | "t" ) )* '"'
    indent   := two spaces per nesting level

A key without a scalar opens a nested block: either a mapping (entries) or a sequence (items)
at exactly one more indent level. Duplicate keys, empty blocks, mixed blocks, and anything else
(flow style, anchors, tags, plain strings, multi-line scalars) are errors. Stdlib only; this
module is byte-identical in example-executor-backend and governed-agent-engine.
"""

from __future__ import annotations

import re

_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_INT = re.compile(r"-?(?:0|[1-9][0-9]*)")
_ESC = {'"': '"', "\\": "\\", "n": "\n", "t": "\t"}

GrokValue = None | bool | int | str | list["GrokValue"] | dict[str, "GrokValue"]


class GrokFormatError(ValueError):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")
        self.line = line


def _scalar(text: str, ln: int) -> tuple[GrokValue, str]:
    """Parse one scalar at the start of ``text``; return (value, rest)."""
    for word, val in (("null", None), ("true", True), ("false", False)):
        if text.startswith(word) and (len(text) == len(word) or text[len(word)] in " #"):
            return val, text[len(word) :]
    if text.startswith('"'):
        out: list[str] = []
        i = 1
        while i < len(text):
            c = text[i]
            if c == '"':
                return "".join(out), text[i + 1 :]
            if c == "\\":
                nxt = text[i + 1 : i + 2]
                if nxt not in _ESC:
                    raise GrokFormatError(ln, f"invalid escape \\{nxt}")
                out.append(_ESC[nxt])
                i += 2
                continue
            out.append(c)
            i += 1
        raise GrokFormatError(ln, "unterminated string")
    m = _INT.match(text)
    if m and (m.end() == len(text) or text[m.end()] in " #"):
        return int(m.group()), text[m.end() :]
    raise GrokFormatError(ln, f"invalid scalar: {text[:30]!r} (strings must be double-quoted)")


def _tail(rest: str, ln: int) -> None:
    rest = rest.lstrip(" ")
    if rest and not rest.startswith("#"):
        raise GrokFormatError(ln, f"unexpected text after value: {rest[:30]!r}")


def parse_grok(text: str) -> dict[str, GrokValue]:
    if text.startswith("\ufeff"):
        raise GrokFormatError(1, "BOM not allowed")
    if "\r" in text:
        raise GrokFormatError(1, "CR line endings not allowed (LF only)")
    lines: list[tuple[int, int, str]] = []  # (line no, level, content)
    for n, raw in enumerate(text.split("\n"), start=1):
        if "\t" in raw:
            raise GrokFormatError(n, "tabs not allowed")
        body = raw.lstrip(" ")
        if not body or body.startswith("#"):
            continue
        spaces = len(raw) - len(body)
        if spaces % 2:
            raise GrokFormatError(n, "indent must be a multiple of two spaces")
        lines.append((n, spaces // 2, body.rstrip(" ")))
    root, pos = _block(lines, 0, 0)
    if pos != len(lines):  # pragma: no cover - _block consumes or raises
        raise GrokFormatError(lines[pos][0], "unexpected indentation")
    if not isinstance(root, dict):
        raise GrokFormatError(lines[0][0] if lines else 1, "top level must be a mapping")
    return root


def _block(lines: list[tuple[int, int, str]], pos: int, level: int) -> tuple[GrokValue, int]:
    if pos >= len(lines) or lines[pos][1] != level:
        ln = lines[pos][0] if pos < len(lines) else (lines[-1][0] if lines else 1)
        raise GrokFormatError(ln, "expected a non-empty nested block")
    is_seq = lines[pos][2].startswith("- ")
    mapping: dict[str, GrokValue] = {}
    seq: list[GrokValue] = []
    while pos < len(lines):
        ln, lvl, body = lines[pos]
        if lvl < level:
            break
        if lvl > level:
            raise GrokFormatError(ln, "unexpected indentation")
        if body.startswith("- ") != is_seq:
            raise GrokFormatError(ln, "cannot mix mapping entries and sequence items")
        if is_seq:
            val, rest = _scalar(body[2:], ln)
            _tail(rest, ln)
            seq.append(val)
            pos += 1
            continue
        m = _KEY.match(body)
        if not m or body[m.end() : m.end() + 1] != ":":
            raise GrokFormatError(ln, f"expected KEY: (got {body[:30]!r})")
        key, rest = m.group(), body[m.end() + 1 :]
        if key in mapping:
            raise GrokFormatError(ln, f"duplicate key {key}")
        if rest == "" or rest.lstrip(" ").startswith("#"):
            if rest and not rest.startswith(" "):
                raise GrokFormatError(ln, "comment must follow a space")
            mapping[key], pos = _block(lines, pos + 1, level + 1)
            continue
        if not rest.startswith(" "):
            raise GrokFormatError(ln, "a space must follow ':'")
        val, rest = _scalar(rest[1:], ln)
        _tail(rest, ln)
        mapping[key] = val
        pos += 1
    return (seq if is_seq else mapping), pos
