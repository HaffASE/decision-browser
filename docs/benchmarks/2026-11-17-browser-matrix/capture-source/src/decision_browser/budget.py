"""Fixed action, byte, and provider budgets.

A successful reservation commits before the socket or any retention.
Failed work is not refunded. Nothing sleeps, retries, or moves the deadline.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from .contracts import Limits, UsageCounters

_Clock = Callable[[], float]
_AUDIT_GRACE = 1.0


@dataclass(frozen=True, slots=True)
class _LimitsSnapshot:
    max_steps: int
    max_seconds: float
    max_provider_calls: int
    max_input_bytes: int
    max_output_bytes: int
    max_stored_bytes: int


class StepReservation:
    """A held step; an unused pre-dispatch hold is released, not spent."""

    def __init__(self, budget: Budget) -> None:
        self._budget = budget
        self._active = True

    def commit(self) -> None:
        if not self._active:
            raise ValueError("released_step")
        self._budget._held_steps -= 1
        self._budget._steps += 1
        self._active = False

    def release(self) -> None:
        if self._active:
            self._budget._held_steps -= 1
            self._active = False


class StoredReservation:
    """Only pre-encoded audit records can spend this fixed finalization hold."""

    def __init__(self, budget: Budget, amount: int) -> None:
        self._budget = budget
        self._remaining = amount

    def commit(self, actual: int, reserved: int) -> None:
        actual = _count(actual, "bytes")
        reserved = _count(reserved, "reserved_bytes")
        if not actual <= reserved <= self._remaining:
            raise ValueError("audit_reservation")
        self._budget._ensure_audit_time()
        self._budget._held_stored -= reserved
        self._budget._stored += actual
        self._remaining -= reserved

    def release(self) -> None:
        self._budget._held_stored -= self._remaining
        self._remaining = 0


class BudgetExceeded(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(name)
    if value < 0:
        raise ValueError(name)
    return value


def _sample(clock: _Clock) -> float:
    value = clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("clock")
    now = float(value)
    if not math.isfinite(now):
        raise ValueError("clock")
    return now


class Budget:
    __slots__ = (
        "_calls",
        "_clock",
        "_deadline",
        "_held_steps",
        "_held_stored",
        "_input",
        "_limits",
        "_output",
        "_steps",
        "_stored",
    )

    def __init__(self, limits: Limits, clock: _Clock = time.monotonic) -> None:
        if not isinstance(limits, Limits):
            raise TypeError("limits")
        validated = Limits.model_validate(limits.model_dump())
        start = _sample(clock)
        self._limits = _LimitsSnapshot(**validated.model_dump())
        self._clock = clock
        self._deadline = start + self._limits.max_seconds
        self._calls = self._input = self._output = self._steps = self._stored = 0
        self._held_steps = self._held_stored = 0

    @property
    def deadline(self) -> float:
        return self._deadline

    @property
    def original_deadline(self) -> float:
        return self._deadline

    def _ensure_time(self) -> float:
        try:
            now = _sample(self._clock)
        except (TypeError, ValueError):
            raise BudgetExceeded("deadline_expired") from None
        if now >= self._deadline:
            raise BudgetExceeded("deadline_expired")
        return now

    def remaining(self) -> float:
        return self._deadline - self._ensure_time()

    @property
    def remaining_output_bytes(self) -> int:
        self._ensure_time()
        return self._limits.max_output_bytes - self._output

    def _ensure_audit_time(self) -> None:
        try:
            now = _sample(self._clock)
        except (TypeError, ValueError):
            raise BudgetExceeded("audit_deadline_expired") from None
        if now >= self._deadline + _AUDIT_GRACE:
            raise BudgetExceeded("audit_deadline_expired")

    def hold_step(self) -> StepReservation:
        self._ensure_time()
        if self._steps + self._held_steps >= self._limits.max_steps:
            raise BudgetExceeded("steps")
        self._held_steps += 1
        return StepReservation(self)

    def hold_stored(self, n_bytes: int) -> StoredReservation:
        n = _count(n_bytes, "bytes")
        self._ensure_time()
        if self._stored + self._held_stored + n > self._limits.max_stored_bytes:
            raise BudgetExceeded("stored_bytes")
        self._held_stored += n
        return StoredReservation(self, n)

    def reserve_step(self) -> None:
        self._ensure_time()
        if self._steps + self._held_steps >= self._limits.max_steps:
            raise BudgetExceeded("steps")
        self._steps += 1

    def reserve_provider(self, input_bytes: int) -> None:
        n = _count(input_bytes, "input_bytes")
        self._ensure_time()
        if self._calls >= self._limits.max_provider_calls:
            raise BudgetExceeded("provider_calls")
        if self._input + n > self._limits.max_input_bytes:
            raise BudgetExceeded("input_bytes")
        self._calls += 1
        self._input += n

    def consume_output(self, chunk_bytes: int) -> None:
        n = _count(chunk_bytes, "chunk_bytes")
        self._ensure_time()
        if self._output + n > self._limits.max_output_bytes:
            raise BudgetExceeded("output_bytes")
        self._output += n

    def reserve_stored(self, n_bytes: int) -> None:
        n = _count(n_bytes, "bytes")
        self._ensure_time()
        if self._stored + self._held_stored + n > self._limits.max_stored_bytes:
            raise BudgetExceeded("stored_bytes")
        self._stored += n

    def usage(self) -> UsageCounters:
        return UsageCounters(
            calls=self._calls,
            input_bytes=self._input,
            output_bytes=self._output,
            steps=self._steps,
            stored_bytes=self._stored,
        )
