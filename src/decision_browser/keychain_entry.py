"""Opt-in macOS Keychain entry for decision-browser.

Retrieves only Keychain item OPENAI_API_KEY via
``/usr/bin/security find-generic-password -s OPENAI_API_KEY -w``.
No key value is stored in this file. The value is placed only in the
replacement process environment.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

_SECURITY_ARGV: tuple[str, ...] = (
    "/usr/bin/security",
    "find-generic-password",
    "-s",
    "OPENAI_API_KEY",
    "-w",
)
_TIMEOUT_SECONDS = 20
_SERVE_ARGS = ("-I", "-m", "decision_browser.cli", "serve")


class _EntryFailure(Exception):
    """Generic keychain failure. Instances carry no key material."""


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def _key_from_keychain() -> str:
    try:
        completed = subprocess.run(
            list(_SECURITY_ARGV),
            check=False,
            shell=False,
            capture_output=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise _EntryFailure from None
    except OSError:
        raise _EntryFailure from None
    stdout = completed.stdout
    if completed.returncode != 0 or stdout is None or stdout == b"":
        raise _EntryFailure
    try:
        key = stdout.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError:
        raise _EntryFailure from None
    if key == "":
        raise _EntryFailure
    return key


def _replace_with_serve(key: str, admission_mode: str | None = None) -> None:
    # Child mapping only. This process environment is not mutated.
    # The Keychain value replaces any inherited OPENAI_API_KEY.
    child_env = os.environ.copy()
    child_env["OPENAI_API_KEY"] = key
    argv = [sys.executable, *_SERVE_ARGS]
    if admission_mode in {"task_scope", "standard_form"}:
        argv.extend(["--admission", admission_mode])
    elif admission_mode is not None:
        raise _EntryFailure
    os.execve(sys.executable, argv, child_env)


def main(argv: list[str] | None = None) -> int:
    """Replace this process with ``decision_browser.cli serve`` on macOS."""
    if sys.platform != "darwin":
        return _fail("error: unsupported platform")
    parser = argparse.ArgumentParser(prog="decision-browser-keychain")
    parser.add_argument("--admission", choices=("standard_form", "task_scope"), default=None)
    args = parser.parse_args([] if argv is None else argv)
    try:
        key = _key_from_keychain()
    except _EntryFailure:
        return _fail("error: keychain lookup failed")
    except Exception as exc:  # noqa: BLE001 - sanitized keychain boundary
        print(f"error: {type(exc).__name__}", file=sys.stderr)
        return 1
    try:
        _replace_with_serve(key, args.admission)
    except OSError:
        return _fail("error: exec failed")
    except Exception as exc:  # noqa: BLE001 - sanitized exec boundary
        print(f"error: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
