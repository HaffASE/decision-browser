from __future__ import annotations

import math
import time

import pytest
from pydantic import ValidationError

from decision_browser.budget import Budget, BudgetExceeded
from decision_browser.contracts import Limits


def lim(
    steps: int = 1,
    seconds: float = 5.0,
    calls: int = 1,
    inn: int = 4,
    out: int = 4,
    stored: int = 4,
) -> Limits:
    return Limits(
        max_steps=steps,
        max_seconds=seconds,
        max_provider_calls=calls,
        max_input_bytes=inn,
        max_output_bytes=out,
        max_stored_bytes=stored,
    )


def test_exact_limit_last_action_norefund_and_readonly() -> None:
    now = [0.0]
    b = Budget(lim(), lambda: now[0])
    assert b.deadline == 5.0
    assert b.original_deadline == b.deadline
    b.reserve_step()
    b.reserve_provider(4)
    b.consume_output(4)
    b.reserve_stored(4)
    snap = b.usage()
    assert (
        snap.steps,
        snap.calls,
        snap.input_bytes,
        snap.output_bytes,
        snap.stored_bytes,
    ) == (1, 1, 4, 4, 4)
    with pytest.raises(BudgetExceeded) as denied:
        b.reserve_provider(0)
    assert denied.value.code == "provider_calls" and b.usage() == snap
    assert b.remaining() == 5.0


def test_all_caps_fail_without_partial_commit() -> None:
    now = [1.0]
    b = Budget(lim(2, 9.0, 2, 10, 7, 3), lambda: now[0])
    assert b.deadline == 10.0
    assert b.original_deadline == b.deadline
    b.reserve_provider(6)
    with pytest.raises(BudgetExceeded) as ei:
        b.reserve_provider(5)
    assert (
        ei.value.code == "input_bytes"
        and b.usage().calls == 1
        and b.usage().input_bytes == 6
    )
    b.reserve_provider(4)
    with pytest.raises(BudgetExceeded) as ec:
        b.reserve_provider(0)
    assert ec.value.code == "provider_calls"
    b.consume_output(5)
    with pytest.raises(BudgetExceeded) as eo:
        b.consume_output(3)
    assert eo.value.code == "output_bytes" and b.usage().output_bytes == 5
    b.reserve_stored(3)
    with pytest.raises(BudgetExceeded) as es:
        b.reserve_stored(1)
    assert es.value.code == "stored_bytes" and b.usage().stored_bytes == 3
    b.reserve_step()
    b.reserve_step()
    with pytest.raises(BudgetExceeded) as et:
        b.reserve_step()
    assert et.value.code == "steps" and b.usage().steps == 2


def test_expired_nonfinite_default_clock_and_validation() -> None:
    now = [0.0]
    b = Budget(lim(), lambda: now[0])
    now[0] = 5.0
    with pytest.raises(BudgetExceeded) as ed:
        b.remaining()
    assert ed.value.code == "deadline_expired"
    with pytest.raises(BudgetExceeded) as ea:
        b.reserve_step()
    assert ea.value.code == "deadline_expired" and b.usage().steps == 0
    now[0] = math.nan
    with pytest.raises(BudgetExceeded) as en:
        b.consume_output(0)
    assert en.value.code == "deadline_expired"
    with pytest.raises(ValueError):
        Budget(lim(), lambda: math.inf)
    before = time.monotonic()
    live = Budget(lim(1, 30.0, 1, 1, 1, 1))
    after = time.monotonic()
    assert before + 30.0 <= live.deadline <= after + 30.0
    assert live.original_deadline == live.deadline
    assert math.isfinite(live.deadline) and live.remaining() > 0
    live.reserve_step()
    live.reserve_provider(1)
    live.consume_output(1)
    live.reserve_stored(1)
    snap = live.usage()
    assert (
        snap.steps,
        snap.calls,
        snap.input_bytes,
        snap.output_bytes,
        snap.stored_bytes,
    ) == (1, 1, 1, 1, 1)
    with pytest.raises(BudgetExceeded) as er:
        live.reserve_step()
    assert er.value.code == "steps" and live.usage() == snap
    with pytest.raises(BudgetExceeded) as ep:
        live.reserve_provider(0)
    assert ep.value.code == "provider_calls" and live.usage() == snap
    with pytest.raises(BudgetExceeded) as eo:
        live.consume_output(1)
    assert eo.value.code == "output_bytes" and live.usage().output_bytes == 1
    with pytest.raises(BudgetExceeded) as es:
        live.reserve_stored(1)
    assert es.value.code == "stored_bytes" and live.usage().stored_bytes == 1
    for bad in (True, -1, 0):
        with pytest.raises(ValidationError):
            Limits(
                max_steps=bad,
                max_seconds=1.0,
                max_provider_calls=1,
                max_input_bytes=1,
                max_output_bytes=1,
                max_stored_bytes=1,
            )
    for bad_seconds in (math.nan, math.inf, 0, -1, 601):
        with pytest.raises(ValidationError):
            Limits(
                max_steps=1,
                max_seconds=bad_seconds,
                max_provider_calls=1,
                max_input_bytes=1,
                max_output_bytes=1,
                max_stored_bytes=1,
            )
    for field in ("max_input_bytes", "max_output_bytes", "max_stored_bytes"):
        kwargs = {
            "max_steps": 1,
            "max_seconds": 1.0,
            "max_provider_calls": 1,
            "max_input_bytes": 1,
            "max_output_bytes": 1,
            "max_stored_bytes": 1,
        }
        kwargs[field] = 0
        with pytest.raises(ValidationError):
            Limits(**kwargs)
