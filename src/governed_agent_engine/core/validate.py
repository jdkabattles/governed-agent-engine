"""Tiny dependency-free validators used by the dataclass models.

Why not Pydantic? pydantic-core is a Rust extension with no Android/Termux wheels on PyPI
(verified 2026-10-10), so ``pip install pydantic`` on a phone means compiling Rust. The core
of this package therefore uses stdlib dataclasses plus these helpers.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from enum import Enum
from typing import Any, TypeVar

from governed_agent_engine.core.errors import ValidationError

E = TypeVar("E", bound=Enum)


def as_mapping(value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{where}: expected an object, got {type(value).__name__}")
    return value


def reject_unknown(data: Mapping[str, Any], allowed: Iterable[str], where: str) -> None:
    extra = sorted(set(data) - set(allowed))
    if extra:
        raise ValidationError(f"{where}: unknown field(s) {extra}")


def req(data: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in data:
        raise ValidationError(f"{where}: missing required field '{key}'")
    return data[key]


def s(
    value: object,
    where: str,
    *,
    min_len: int = 0,
    max_len: int = 10_000,
    pattern: str | None = None,
) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{where}: expected string")
    if not min_len <= len(value) <= max_len:
        raise ValidationError(f"{where}: length must be {min_len}..{max_len}")
    if pattern is not None and not re.fullmatch(pattern, value):
        raise ValidationError(f"{where}: does not match {pattern}")
    return value


def opt_s(value: object, where: str) -> str | None:
    return None if value is None else s(value, where)


def b(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{where}: expected boolean")
    return value


def i(value: object, where: str, *, ge: int | None = None, le: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{where}: expected integer")
    if (ge is not None and value < ge) or (le is not None and value > le):
        raise ValidationError(f"{where}: out of range")
    return value


def opt_i(value: object, where: str, *, ge: int | None = None) -> int | None:
    return None if value is None else i(value, where, ge=ge)


def f(value: object, where: str, *, ge: float | None = None, le: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{where}: expected number")
    out = float(value)
    if (ge is not None and out < ge) or (le is not None and out > le):
        raise ValidationError(f"{where}: out of range")
    return out


def enum(cls: type[E], value: object, where: str) -> E:
    try:
        return cls(value)
    except ValueError as exc:
        allowed = [m.value for m in cls]
        raise ValidationError(f"{where}: {value!r} not in {allowed}") from exc


def choice(value: object, allowed: Iterable[str], where: str) -> str:
    options = tuple(allowed)
    if value not in options:
        raise ValidationError(f"{where}: {value!r} not in {list(options)}")
    return str(value)


def dt(value: object, where: str) -> datetime:
    if isinstance(value, datetime):
        out = value
    elif isinstance(value, str):
        try:
            out = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{where}: invalid ISO-8601 datetime") from exc
    else:
        raise ValidationError(f"{where}: expected ISO-8601 string")
    if out.tzinfo is None:
        raise ValidationError(f"{where}: datetime must be timezone-aware")
    return out.astimezone(UTC)


def opt_dt(value: object, where: str) -> datetime | None:
    return None if value is None else dt(value, where)


def str_list(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{where}: expected list")
    return tuple(s(v, f"{where}[{n}]", min_len=1) for n, v in enumerate(value))


def json_obj(value: object, where: str) -> dict[str, Any]:
    return dict(as_mapping(value, where))
