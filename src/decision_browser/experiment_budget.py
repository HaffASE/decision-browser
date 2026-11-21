"""Durable authorization for one explicitly initialized Decisions experiment.

Reserve before dispatch; unknown/failed outcomes are never refunded. The 600-second
wall-clock deadline starts at initialization, so idle time and restarts consume it.
Later windows require a separate explicit operator authorization with fixed
absolute bounds. They never replenish cumulative quotas. Migration appends to the
original v1 journal; it never rewrites history. Never replace or copy a ledger.

Pricing is the approved standard input price ($0.10/million tokens). One token per
serialized request byte plus 4096 tokens/call and a $0.05 uncertainty reserve is a
conservative *estimate*, not provider billing or a server-enforced dollar cap.
Provider integration must reject other models, images, text generation and retries.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import stat
import time
import uuid
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

MAX_CALLS = 40
MAX_STEPS = 40
MAX_INPUT_BYTES = 512 * 1024
MAX_SECONDS = 600
MAX_WINDOWS = 40
_MAX_LEDGER_BYTES = 128 * 1024
APPROVED_USD = 0.25
INPUT_USD_PER_MILLION = 0.10
OVERHEAD_TOKENS_PER_CALL = 4096
UNCERTAINTY_RESERVE_USD = 0.05
_MAX_ESTIMATED_TOKENS = int(
    (Decimal(str(APPROVED_USD)) - Decimal(str(UNCERTAINTY_RESERVE_USD)))
    * 1_000_000 / Decimal(str(INPUT_USD_PER_MILLION))
)
_LIMITS = {
    "calls": MAX_CALLS,
    "steps": MAX_STEPS,
    "input_bytes": MAX_INPUT_BYTES,
    "seconds": MAX_SECONDS,
    "approved_usd": APPROVED_USD,
    "input_usd_per_million": INPUT_USD_PER_MILLION,
    "overhead_tokens_per_call": OVERHEAD_TOKENS_PER_CALL,
    "uncertainty_reserve_usd": UNCERTAINTY_RESERVE_USD,
}
_USAGE_KEYS = {"input_tokens", "output_tokens", "total_tokens", "cached_tokens"}


class ExperimentBudgetError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ExperimentBudgetError("invalid_count")
    return value


def decode_json(raw: bytes) -> Any:
    """Strict decoding shared with the operator; reject duplicate fields."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=pairs)


def _uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _timestamp(value: object) -> bool:
    if not isinstance(value, (int, float)) or type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


@dataclass(frozen=True)
class WindowIntent:
    """Nonsecret, fixed operator intent. A flag is not human attestation."""

    version: int
    authorization_id: str
    expected_ledger_sha256: str
    ledger_identity: str
    previous_window_id: str
    not_before: float
    not_after: float

    @classmethod
    def parse(cls, value: object) -> WindowIntent:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ExperimentBudgetError("window_intent_invalid")
        digest = value["expected_ledger_sha256"]
        identity = value["ledger_identity"]
        before, after = value["not_before"], value["not_after"]
        if (
            type(value["version"]) is not int
            or value["version"] != 1
            or not _uuid(value["authorization_id"])
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
            or not isinstance(identity, str)
            or len(identity) > 64
            or len(identity.split(":")) != 2
            or any(not n.isascii() or not n.isdecimal() for n in identity.split(":"))
            or not (
                value["previous_window_id"] == "initial"
                or _uuid(value["previous_window_id"])
            )
            or not _timestamp(before)
            or not _timestamp(after)
            or not 0 < after - before <= MAX_SECONDS
        ):
            raise ExperimentBudgetError("window_intent_invalid")
        return cls(**value)


class ExperimentLedger:
    """Exclusive process lock, bounded append-only records, fsync before return.

    ``create=True`` is a separate, explicit operator initialization, never resume.
    ``close()`` releases the lock for resume; ``finish()`` permanently ends approval.
    ``record_usage`` accepts sanitized integer counters for the most recent call.
    No request/response content or secrets are retained.
    """

    def __init__(
        self, path: str | Path, *, create: bool = False, read_only: bool = False
    ) -> None:
        self.path = Path(path)
        self._fd: int | None = None
        self._poisoned = False
        self._read_only = read_only
        self._hash = hashlib.sha256()
        self._size = 0
        self._identity = ""
        self._calls = self._steps = self._input_bytes = 0
        self._usage: dict[int, dict[str, int]] = {}
        self._reservations: list[int] = []
        self._finished = False
        self._started = self._last_at = 0.0
        self._observed_at = 0.0
        self._window_start = self._window_end = 0.0
        self._window_ids = ["initial"]
        self._authorized_seconds = float(MAX_SECONDS)
        if create and read_only:
            raise ExperimentBudgetError("ledger_read_only")
        flags = os.O_RDONLY if read_only else os.O_RDWR | os.O_APPEND
        flags |= os.O_NOFOLLOW | os.O_NONBLOCK
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        try:
            self._fd = os.open(self.path, flags, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not stat.S_ISREG(os.fstat(self._fd).st_mode):
                raise ExperimentBudgetError("ledger_not_regular")
            info = os.fstat(self._fd)
            self._identity = f"{info.st_dev}:{info.st_ino}"
            if create:
                now = time.time()
                if not math.isfinite(now) or now <= 0:
                    raise ExperimentBudgetError("clock_rollback")
                self._started = self._last_at = now
                self._window_start, self._window_end = now, now + MAX_SECONDS
                self._append({"version": 1, "started_at": now, "limits": _LIMITS})
                directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            else:
                self._load()
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            OverflowError,
            ExperimentBudgetError,
        ) as exc:
            self.close()
            if isinstance(exc, ExperimentBudgetError):
                raise
            raise ExperimentBudgetError("ledger_unavailable_or_corrupt") from None

    def _load(self) -> None:
        assert self._fd is not None
        raw = os.pread(self._fd, _MAX_LEDGER_BYTES + 1, 0)
        if not raw or len(raw) > _MAX_LEDGER_BYTES or not raw.endswith(b"\n"):
            raise ExperimentBudgetError("ledger_corrupt")
        lines = raw.splitlines(keepends=True)
        header = decode_json(lines[0])
        if (
            not isinstance(header, dict)
            or set(header) != {"version", "started_at", "limits"}
            or type(header["version"]) is not int
            or header["version"] != 1
            or header["limits"] != _LIMITS
            or any(
                type(header["limits"][key]) is not type(expected)
                for key, expected in _LIMITS.items()
            )
        ):
            raise ExperimentBudgetError("ledger_corrupt")
        started = header["started_at"]
        if (
            type(started) not in (int, float)
            or not math.isfinite(started)
            or started <= 0
        ):
            raise ExperimentBudgetError("ledger_corrupt")
        self._started = self._last_at = started
        self._window_start, self._window_end = started, started + MAX_SECONDS
        for i, line in enumerate(lines):
            if i:
                self._apply(decode_json(line), prior_sha256=self._hash.hexdigest())
            self._hash.update(line)
        self._size = len(raw)

    def _apply(self, record: dict[str, Any], *, prior_sha256: str) -> None:
        if not isinstance(record, dict) or set(record) != {"kind", "at", "value"}:
            raise ExperimentBudgetError("ledger_corrupt")
        kind, at, value = record["kind"], record["at"], record["value"]
        if (
            type(at) not in (int, float)
            or not math.isfinite(at)
            or at < self._last_at
            or self._finished
        ):
            raise ExperimentBudgetError("ledger_corrupt")
        if kind in ("call", "step") and not self._window_start <= at < self._window_end:
            raise ExperimentBudgetError("ledger_corrupt")
        if kind == "call":
            n = _count(value)
            self._check_call(n)
            self._calls += 1
            self._input_bytes += n
            self._reservations.append(n + OVERHEAD_TOKENS_PER_CALL)
        elif kind == "step":
            if value is not None or self._steps >= MAX_STEPS:
                raise ExperimentBudgetError("ledger_corrupt")
            self._steps += 1
        elif kind == "usage":
            usage = self._validate_usage(value)
            self._usage[self._calls] = usage
        elif kind == "finish" and value is None:
            self._finished = True
        elif kind == "window_authorized":
            intent = self._check_window(value, at, prior_sha256)
            self._window_ids.append(intent.authorization_id)
            self._window_start, self._window_end = intent.not_before, intent.not_after
            self._authorized_seconds += intent.not_after - intent.not_before
        else:
            raise ExperimentBudgetError("ledger_corrupt")
        self._last_at = at

    def _append(self, record: dict[str, Any]) -> None:
        assert self._fd is not None
        if self._read_only:
            raise ExperimentBudgetError("ledger_read_only")
        try:
            raw = (
                json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n"
            ).encode()
            if self._size + len(raw) > _MAX_LEDGER_BYTES:
                raise ExperimentBudgetError("ledger_size_limit")
            if os.write(self._fd, raw) != len(raw):
                raise OSError("short write")
            os.fsync(self._fd)
        except (OSError, ValueError):
            self._poisoned = True
            raise ExperimentBudgetError("ledger_write_uncertain") from None
        self._hash.update(raw)
        self._size += len(raw)

    def _now(self) -> float:
        if self._fd is None or self._poisoned:
            raise ExperimentBudgetError("ledger_closed_or_uncertain")
        try:
            if not os.path.samestat(
                os.fstat(self._fd), os.stat(self.path, follow_symlinks=False)
            ):
                raise OSError("ledger replaced")
        except OSError:
            self._poisoned = True
            raise ExperimentBudgetError("ledger_missing_or_replaced") from None
        try:
            raw = os.pread(self._fd, _MAX_LEDGER_BYTES + 1, 0)
            if (
                len(raw) != self._size
                or hashlib.sha256(raw).digest() != self._hash.digest()
            ):
                raise OSError("ledger changed")
        except OSError:
            self._poisoned = True
            raise ExperimentBudgetError("ledger_state_changed") from None
        now = time.time()
        if not math.isfinite(now) or now < max(self._last_at, self._observed_at):
            raise ExperimentBudgetError("clock_rollback")
        self._observed_at = now
        return now

    @property
    def remaining_seconds(self) -> float:
        if self._finished:
            return 0.0
        now = self._now()
        return max(0.0, self._window_end - now) if now >= self._window_start else 0.0

    def _active(self) -> float:
        now = self._now()
        if self._finished or not self._window_start <= now < self._window_end:
            raise ExperimentBudgetError("experiment_expired_or_finished")
        return now

    def assert_active(self) -> None:
        """Preflight before reading a key or constructing provider/browser objects."""
        self._active()

    def _renewable(self, at: float) -> None:
        if self._finished:
            raise ExperimentBudgetError("experiment_expired_or_finished")
        if at < self._window_end:
            raise ExperimentBudgetError("window_still_active")
        if len(self._window_ids) >= MAX_WINDOWS:
            raise ExperimentBudgetError("experiment_windows")
        if self._steps >= MAX_STEPS:
            raise ExperimentBudgetError("experiment_steps")
        if self._input_bytes >= MAX_INPUT_BYTES:
            raise ExperimentBudgetError("experiment_input_bytes")
        self._check_call(0)

    def plan_window(self, *, seconds: int = MAX_SECONDS) -> WindowIntent:
        """Read-only proposal. Creating it grants no runtime authority."""
        if type(seconds) is not int or not 0 < seconds <= MAX_SECONDS:
            raise ExperimentBudgetError("window_intent_invalid")
        now = self._now()
        self._renewable(now)
        return WindowIntent(
            1,
            str(uuid.uuid4()),
            self._hash.hexdigest(),
            self._identity,
            self._window_ids[-1],
            now,
            now + seconds,
        )

    def _check_window(
        self, value: object, at: float, prior_sha256: str
    ) -> WindowIntent:
        intent = WindowIntent.parse(value)
        if intent.authorization_id in self._window_ids:
            raise ExperimentBudgetError("window_authorization_replayed")
        if intent.ledger_identity != self._identity:
            raise ExperimentBudgetError("ledger_missing_or_replaced")
        if intent.expected_ledger_sha256 != prior_sha256:
            raise ExperimentBudgetError("ledger_state_changed")
        if intent.previous_window_id != self._window_ids[-1]:
            raise ExperimentBudgetError("window_intent_invalid")
        self._renewable(at)
        if (
            intent.not_before < self._window_end
            or not intent.not_before <= at < intent.not_after
        ):
            raise ExperimentBudgetError("window_intent_expired_or_invalid")
        return intent

    def authorize_window(
        self, intent: object, *, operator_authorized: bool = False
    ) -> None:
        """Explicit operator action only; ordinary run/resume never calls this."""
        if operator_authorized is not True:
            raise ExperimentBudgetError("operator_authorization_required")
        now = self._now()
        validated = self._check_window(intent, now, self._hash.hexdigest())
        self._record("window_authorized", asdict(validated), now)

    def _estimated_tokens(
        self, additional_bytes: int = 0, additional_calls: int = 0
    ) -> int:
        # Each unknown call retains its full reservation, even when another call
        # reports higher usage. Reports never refund a particular call.
        return sum(
            max(reserved, self._usage.get(i, {}).get("input_tokens", 0))
            for i, reserved in enumerate(self._reservations, 1)
        ) + (additional_bytes + additional_calls * OVERHEAD_TOKENS_PER_CALL)

    def _check_call(self, n: int) -> None:
        if self._calls >= MAX_CALLS:
            raise ExperimentBudgetError("experiment_calls")
        if self._input_bytes + n > MAX_INPUT_BYTES:
            raise ExperimentBudgetError("experiment_input_bytes")
        if self._estimated_tokens(n, 1) > _MAX_ESTIMATED_TOKENS:
            raise ExperimentBudgetError("experiment_estimated_cost")

    def _record(self, kind: str, value: object, at: float) -> None:
        record = {"kind": kind, "at": at, "value": value}
        prior_sha256 = self._hash.hexdigest()
        self._append(record)
        self._apply(record, prior_sha256=prior_sha256)

    def reserve_call(self, input_bytes: int) -> None:
        now = self._active()
        n = _count(input_bytes)
        self._check_call(n)
        self._record("call", n, now)

    def reserve_step(self) -> None:
        now = self._active()
        if self._steps >= MAX_STEPS:
            raise ExperimentBudgetError("experiment_steps")
        self._record("step", None, now)

    def _validate_usage(self, usage: object) -> dict[str, int]:
        if (
            not isinstance(usage, dict)
            or "input_tokens" not in usage
            or not set(usage) <= _USAGE_KEYS
            or not self._calls
            or self._calls in self._usage
        ):
            raise ExperimentBudgetError("invalid_or_duplicate_usage")
        return {key: _count(value) for key, value in usage.items()}

    def record_usage(self, usage: dict[str, int]) -> None:
        now = self._now()
        if self._finished:
            raise ExperimentBudgetError("experiment_expired_or_finished")
        self._record("usage", self._validate_usage(usage), now)

    @property
    def snapshot(self) -> dict[str, Any]:
        return {
            "started_at": self._started,
            "original_deadline": self._started + MAX_SECONDS,
            "deadline": self._window_end,
            "format_version": 2 if len(self._window_ids) > 1 else 1,
            "window_id": self._window_ids[-1],
            "window_started_at": self._window_start,
            "windows": len(self._window_ids),
            "authorized_seconds": self._authorized_seconds,
            "ledger_sha256": self._hash.hexdigest(),
            "ledger_identity": self._identity,
            "limits": dict(_LIMITS),
            "calls": self._calls,
            "steps": self._steps,
            "input_bytes": self._input_bytes,
            "remaining_calls": MAX_CALLS - self._calls,
            "remaining_steps": MAX_STEPS - self._steps,
            "remaining_input_bytes": MAX_INPUT_BYTES - self._input_bytes,
            "finished": self._finished,
            "estimated_cost_usd": self._estimated_tokens() * INPUT_USD_PER_MILLION / 1_000_000,
            "uncertainty_reserve_usd": UNCERTAINTY_RESERVE_USD,
            "billing_status": "estimated_not_provider_billing",
            "reported_usage": {str(k): dict(v) for k, v in self._usage.items()},
        }

    def finish(self) -> None:
        if not self._finished:
            self._record("finish", None, self._now())
        self.close()

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
