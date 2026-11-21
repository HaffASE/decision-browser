"""Offline operator commands. No provider, browser, environment key or Keychain."""

from __future__ import annotations

import argparse
import json
import os
import stat
from dataclasses import asdict
from pathlib import Path

from .experiment_budget import (
    MAX_SECONDS,
    ExperimentBudgetError,
    ExperimentLedger,
    WindowIntent,
    decode_json,
)

_MAX_INTENT_BYTES = 4096


def read_intent(path: Path) -> dict[str, object]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("not regular")
            raw = os.read(fd, _MAX_INTENT_BYTES + 1)
            if len(raw) > _MAX_INTENT_BYTES:
                raise ValueError("too large")
            return asdict(WindowIntent.parse(decode_json(raw)))
        finally:
            os.close(fd)
    except (OSError, ValueError, TypeError, ExperimentBudgetError):
        raise ExperimentBudgetError("window_intent_invalid") from None


def write_intent(path: Path, intent: WindowIntent) -> None:
    """Exclusive new file; never overwrite a fixed intent during a retry."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            raw = (json.dumps(asdict(intent), allow_nan=False) + "\n").encode()
            if os.write(fd, raw) != len(raw):
                raise OSError("short write")
            os.fsync(fd)
        finally:
            os.close(fd)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        raise ExperimentBudgetError("window_intent_write_uncertain") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser(
        "status", help="Read cumulative state without changing it"
    )
    plan = commands.add_parser(
        "plan-window", help="Write a proposal; grants no authority"
    )
    authorize = commands.add_parser(
        "authorize-window", help="Append separately approved fixed window"
    )
    for command in (status, plan, authorize):
        command.add_argument("--ledger", type=Path, required=True)
    plan.add_argument("--seconds", type=int, default=MAX_SECONDS)
    plan.add_argument("--output", type=Path, required=True)
    authorize.add_argument("--intent", type=Path, required=True)
    authorize.add_argument("--operator-authorized", action="store_true")
    args = parser.parse_args(argv)
    result: dict[str, object]
    try:
        # Missing explicit operator gate cannot even open the ledger for writing.
        if args.command == "authorize-window" and not args.operator_authorized:
            raise ExperimentBudgetError("operator_authorization_required")
        with ExperimentLedger(
            args.ledger, read_only=args.command != "authorize-window"
        ) as ledger:
            if args.command == "plan-window":
                intent = ledger.plan_window(seconds=args.seconds)
                write_intent(args.output, intent)
                result = {
                    "status": "PLANNED_NOT_AUTHORIZED",
                    "intent": asdict(intent),
                    "experiment": ledger.snapshot,
                }
            elif args.command == "authorize-window":
                ledger.authorize_window(
                    read_intent(args.intent), operator_authorized=True
                )
                result = {"status": "WINDOW_AUTHORIZED", "experiment": ledger.snapshot}
            else:
                result = {
                    "status": "READ_ONLY",
                    "experiment": ledger.snapshot,
                    "remaining_seconds": ledger.remaining_seconds,
                }
    except ExperimentBudgetError as exc:
        # Codes are fixed library literals; never serialize exception text/paths.
        print(json.dumps({"status": "BLOCKED", "reason": exc.code}))
        return 1
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
