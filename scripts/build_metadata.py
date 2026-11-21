"""Code-owned, stdlib-only contract shared by distribution builders."""
from __future__ import annotations

import re

VERSION = re.compile(
    r"((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))"
    r"(?:(a|b|rc)(0|[1-9]\d*))?\Z", re.ASCII
)

BOOTSTRAP_FILES = (
    "pyproject.toml", "hatch_build.py", "uv.lock", "README.md", "LICENSE",
    "THIRD_PARTY_NOTICES.md", "scripts/install.py", "scripts/prepare_tool_runtime.py",
    "scripts/build-constraints.txt", "scripts/build_metadata.py",
)
BOOTSTRAP_DIRECTORIES = ("src/decision_browser", "skills/decision-browser")
BOOTSTRAP_INPUTS = BOOTSTRAP_FILES + BOOTSTRAP_DIRECTORIES
REQUIRED_BOOTSTRAP_FILES = BOOTSTRAP_FILES + (
    "skills/decision-browser/SKILL.md", "src/decision_browser/cli.py",
)


def npm_version(version: str) -> str:
    """Map only the project's documented canonical Python release vocabulary."""
    match = VERSION.fullmatch(version) if type(version) is str else None
    if match is None:
        raise ValueError("Use X.Y.Z or X.Y.ZrcN/aN/bN (canonical Python prerelease).")
    base, prerelease, number = match.groups()
    if prerelease is None:
        return base
    label = {"a": "alpha", "b": "beta", "rc": "rc"}[prerelease]
    return f"{base}-{label}.{number}"
