"""Shared deterministic check bounds, independent of browser or model state."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal, get_args

TARGET_BYTES = 512
EXPECTED_BYTES = 4096
EXPECTED_CHARS = 4000
MAX_CHECKS = 32
FIXTURE_KEYS = 64
CheckKind = Literal["route", "text", "value", "visible", "count", "fixture_state"]
CHECK_KINDS = frozenset(get_args(CheckKind))
FixtureScalar = str | bool | int
CheckProblem = Literal[
    "type_mismatch", "target_invalid", "target_oversized", "expected_oversized",
    "selector_rejected", "empty_target",
]


def _size(value: str) -> int | None:
    try:
        return len(value.encode("utf-8"))
    except UnicodeError:
        return None


def shown_target(value: object) -> str:
    if type(value) is str:
        size = _size(value)
        if size is not None and size <= TARGET_BYTES:
            return value
    return ""


def _selector_rejected(target: str) -> bool:
    if any(char in target for char in ("\n", "\r", "\x00")) or ">>" in target:
        return True
    if re.search(r"^[A-Za-z_-]+=", target):
        return True
    lowered = target.lower()
    return lowered.startswith("//") or any(
        marker in lowered for marker in ("xpath=", "text=", ":has-text(", ":text(", "internal:")
    )


def check_problem(kind: object, target: object, expected: object) -> CheckProblem | None:
    """Validate fields before a request starts and at the direct verifier seam."""
    if type(kind) is not str or kind not in CHECK_KINDS:
        return "type_mismatch"
    if type(target) is not str:
        return "target_invalid"
    target_size = _size(target)
    if target_size is None:
        return "target_invalid"
    if target_size > TARGET_BYTES:
        return "target_oversized"
    if kind == "route":
        if target:
            return "target_invalid"
    elif kind != "fixture_state":
        if not target:
            return "empty_target"
        if _selector_rejected(target):
            return "selector_rejected"
    if kind in {"route", "text", "value"} and type(expected) is not str:
        return "type_mismatch"
    if kind == "visible" and type(expected) is not bool:
        return "type_mismatch"
    if kind == "count" and (type(expected) is not int or expected < 0):
        return "type_mismatch"
    if kind == "fixture_state" and type(expected) not in {str, bool, int}:
        return "type_mismatch"
    if type(expected) is str:
        size = _size(expected)
        if size is None:
            return "type_mismatch"
        if len(expected) > EXPECTED_CHARS or size > EXPECTED_BYTES:
            return "expected_oversized"
    return None


def clean_fixture_state(value: object) -> dict[str, FixtureScalar] | None:
    """Snapshot a bounded direct-call mapping; it is never public admission."""
    if not isinstance(value, Mapping) or len(value) > FIXTURE_KEYS:
        return None
    cleaned: dict[str, FixtureScalar] = {}
    for index, (key, item) in enumerate(value.items()):
        if index >= FIXTURE_KEYS or type(key) is not str:
            return None
        size = _size(key)
        if size is None or size > EXPECTED_BYTES or type(item) not in {str, bool, int}:
            return None
        if type(item) is str:
            size = _size(item)
            if size is None or size > EXPECTED_BYTES:
                return None
        cleaned[key] = item
    return cleaned
