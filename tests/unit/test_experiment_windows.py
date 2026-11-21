from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from decision_browser import experiment_budget as eb
from decision_browser import experiment_operator as operator
from decision_browser.experiment_budget import (
    ExperimentBudgetError,
    ExperimentLedger,
    WindowIntent,
)


@pytest.fixture
def state(tmp_path: Path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(eb.time, "time", lambda: now[0])
    path = tmp_path / "synthetic-v1.jsonl"
    with ExperimentLedger(path, create=True) as ledger:
        ledger.reserve_call(100)
        ledger.record_usage({"input_tokens": 2})
        ledger.reserve_call(50)  # unknown outcome keeps its full reservation
        ledger.reserve_step()
    now[0] = 1600.0
    return path, now


def plan(path: Path, seconds: int = 600) -> dict[str, object]:
    with ExperimentLedger(path, read_only=True) as ledger:
        return asdict(ledger.plan_window(seconds=seconds))


def authorize(ledger: ExperimentLedger, intent: dict[str, object]) -> None:
    ledger.authorize_window(intent, operator_authorized=True)


def test_append_only_v1_migration_and_cumulative_unknown_cost(state) -> None:
    path, now = state
    original = path.read_bytes()
    proposal = plan(path)
    assert path.read_bytes() == original
    assert proposal["expected_ledger_sha256"] == hashlib.sha256(original).hexdigest()
    with ExperimentLedger(path) as ledger:
        before = ledger.snapshot
        authorize(ledger, proposal)
        migrated = ledger.snapshot
        assert migrated["format_version"] == 2
        assert migrated["started_at"] == 1000 and migrated["original_deadline"] == 1600
        assert migrated["deadline"] == 2200 and migrated["authorized_seconds"] == 1200
        assert migrated["windows"] == 2
        for key in (
            "calls",
            "steps",
            "input_bytes",
            "reported_usage",
            "estimated_cost_usd",
            "limits",
        ):
            assert migrated[key] == before[key]
        assert migrated["calls"] == 2 and migrated["input_bytes"] == 150
        assert migrated["estimated_cost_usd"] == (150 + 2 * 4096) * 0.0000001
        assert path.read_bytes().startswith(original)
        assert len(path.read_bytes().splitlines()) == len(original.splitlines()) + 1
        now[0] = 1700
        ledger.reserve_call(77)
        ledger.reserve_step()
    with ExperimentLedger(path) as ledger:
        assert ledger.remaining_seconds == 500
        assert ledger.snapshot["calls"] == 3 and ledger.snapshot["input_bytes"] == 227
        now[0] = 2200
        with pytest.raises(ExperimentBudgetError, match="expired"):
            ledger.reserve_call(1)


def test_expired_ordinary_run_stops_before_key_provider_or_browser(
    state, monkeypatch
) -> None:
    from decision_browser import flights_pilot

    path, _ = state
    original = path.read_bytes()

    class NoEnvironmentReads:
        def get(self, *args):
            raise AssertionError("expired run read environment")

    monkeypatch.setattr(
        flights_pilot, "os", SimpleNamespace(environ=NoEnvironmentReads())
    )
    with (
        ExperimentLedger(path) as ledger,
        pytest.raises(ExperimentBudgetError, match="expired"),
    ):
        asyncio.run(flights_pilot.run_pilot(ledger))
    assert path.read_bytes() == original


def test_proposal_never_grants_or_writes_and_gate_is_strict(state) -> None:
    path, _ = state
    original = path.read_bytes()
    intent = plan(path)
    with ExperimentLedger(path) as ledger:
        for flag in (False, None, 1, "yes"):
            with pytest.raises(
                ExperimentBudgetError, match="operator_authorization_required"
            ):
                ledger.authorize_window(intent, operator_authorized=flag)
        with pytest.raises(ExperimentBudgetError, match="expired"):
            ledger.reserve_step()
    assert path.read_bytes() == original
    with (
        ExperimentLedger(path, read_only=True) as ledger,
        pytest.raises(ExperimentBudgetError, match="read_only"),
    ):
        authorize(ledger, intent)
    assert path.read_bytes() == original


def test_duplicate_ids_changed_payload_and_two_proposals_conflict(state) -> None:
    path, now = state
    first, second = plan(path), plan(path)
    with ExperimentLedger(path) as ledger:
        authorize(ledger, first)
        granted = path.read_bytes()
        for retry in (
            first,
            {**first, "not_after": 2100},
            {**first, "expected_ledger_sha256": ledger.snapshot["ledger_sha256"]},
        ):
            with pytest.raises(ExperimentBudgetError, match="replayed"):
                authorize(ledger, retry)
        with pytest.raises(ExperimentBudgetError, match="state_changed"):
            authorize(ledger, second)
        with pytest.raises(ExperimentBudgetError, match="still_active"):
            ledger.plan_window()
        assert path.read_bytes() == granted
    now[0] = 2200
    with ExperimentLedger(path) as ledger:
        with pytest.raises(ExperimentBudgetError, match="replayed"):
            authorize(ledger, first)
        assert ledger.remaining_seconds == 0
        next_window = asdict(ledger.plan_window(seconds=30))
        authorize(ledger, next_window)
        assert ledger.snapshot["windows"] == 3 and ledger.snapshot["deadline"] == 2230


def test_lost_record_retry_never_moves_fixed_deadline(state, monkeypatch) -> None:
    path, now = state
    original = path.read_bytes()
    intent = plan(path)

    def lost_fsync(_fd):
        raise OSError("synthetic nondurable write")

    with ExperimentLedger(path) as ledger:
        with monkeypatch.context() as patch:
            # Simulate a write acknowledged by OS but lost before durability.
            patch.setattr(eb.os, "write", lambda fd, raw: len(raw))
            patch.setattr(eb.os, "fsync", lost_fsync)
            with pytest.raises(ExperimentBudgetError, match="write_uncertain"):
                authorize(ledger, intent)
        with pytest.raises(ExperimentBudgetError, match="uncertain"):
            authorize(ledger, intent)
    assert path.read_bytes() == original
    now[0] = 1700
    with ExperimentLedger(path) as ledger:
        authorize(ledger, intent)
        assert ledger.remaining_seconds == 500
        assert ledger.snapshot["deadline"] == 2200
        assert ledger.snapshot["calls"] == 2


def test_surviving_fsync_failure_is_counted_once_after_reopen(
    state, monkeypatch
) -> None:
    path, _ = state
    intent = plan(path)
    with ExperimentLedger(path) as ledger:
        with monkeypatch.context() as patch:

            def fail(_fd):
                raise OSError("synthetic disk failure")

            patch.setattr(eb.os, "fsync", fail)
            with pytest.raises(ExperimentBudgetError, match="write_uncertain"):
                authorize(ledger, intent)
        with pytest.raises(ExperimentBudgetError, match="uncertain"):
            ledger.reserve_step()
    with ExperimentLedger(path) as ledger:
        assert ledger.snapshot["windows"] == 2 and ledger.snapshot["deadline"] == 2200
        with pytest.raises(ExperimentBudgetError, match="replayed"):
            authorize(ledger, intent)


def test_partial_authorization_is_never_repaired(state, monkeypatch) -> None:
    path, _ = state
    intent = plan(path)
    real_write = os.write
    with ExperimentLedger(path) as ledger, monkeypatch.context() as patch:
        patch.setattr(eb.os, "write", lambda fd, raw: real_write(fd, raw[:17]))
        with pytest.raises(ExperimentBudgetError, match="write_uncertain"):
            authorize(ledger, intent)
    damaged = path.read_bytes()
    with pytest.raises(ExperimentBudgetError, match="corrupt"):
        ExperimentLedger(path)
    assert path.read_bytes() == damaged


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 2),
        ("authorization_id", "not-uuid"),
        ("expected_ledger_sha256", "A" * 64),
        ("ledger_identity", "-1:2"),
        ("previous_window_id", []),
        ("not_before", True),
        ("not_before", float("nan")),
        ("not_after", float("inf")),
        ("not_after", 10**500),
        ("not_after", 1600),
        ("not_after", 2201),
        ("unrecognized_secret", "CANARY_NEVER_RETAIN"),
    ],
)
def test_strict_closed_intent_schema_never_appends(state, field, value) -> None:
    path, _ = state
    original = path.read_bytes()
    intent = {**plan(path), field: value}
    with (
        ExperimentLedger(path) as ledger,
        pytest.raises(ExperimentBudgetError, match="intent_invalid"),
    ):
        authorize(ledger, intent)
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "mutation,now",
    [
        ({"not_before": 1599, "not_after": 2199}, 1600),
        ({"not_before": 1700, "not_after": 2200}, 1600),
        ({}, 2200),
        ({"previous_window_id": "00000000-0000-0000-0000-000000000000"}, 1600),
    ],
)
def test_fixed_window_cannot_overlap_start_early_or_revive_expired_intent(
    state, mutation, now
) -> None:
    path, clock = state
    intent = {**plan(path), **mutation}
    original = path.read_bytes()
    clock[0] = now
    with (
        ExperimentLedger(path) as ledger,
        pytest.raises(
            ExperimentBudgetError, match="intent_expired_or_invalid|intent_invalid"
        ),
    ):
        authorize(ledger, intent)
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "exhaustion,code",
    [
        ("calls", "experiment_calls"),
        ("steps", "experiment_steps"),
        ("bytes", "experiment_input_bytes"),
        ("cost", "experiment_estimated_cost"),
        ("finish", "finished"),
    ],
)
def test_terminal_or_exhausted_budget_cannot_get_new_window(
    state, exhaustion, code
) -> None:
    path, now = state
    now[0] = 1000
    with ExperimentLedger(path) as ledger:
        if exhaustion == "calls":
            for _ in range(38):
                ledger.reserve_call(0)
        elif exhaustion == "steps":
            for _ in range(39):
                ledger.reserve_step()
        elif exhaustion == "bytes":
            ledger.reserve_call(eb.MAX_INPUT_BYTES - 150)
        elif exhaustion == "cost":
            ledger.record_usage({"input_tokens": 2_000_000})
        else:
            ledger.finish()
    now[0] = 1600
    original = path.read_bytes()
    with (
        ExperimentLedger(path) as ledger,
        pytest.raises(ExperimentBudgetError, match=code),
    ):
        ledger.plan_window()
    assert path.read_bytes() == original


def test_quota_never_replenishes_across_windows_and_cap_includes_initial(state) -> None:
    path, now = state
    for i in range(39):
        with ExperimentLedger(path) as ledger:
            authorize(ledger, asdict(ledger.plan_window(seconds=1)))
        now[0] += 1
    with ExperimentLedger(path) as ledger:
        assert ledger.snapshot["windows"] == 40 and ledger.snapshot["calls"] == 2
        assert ledger.snapshot["authorized_seconds"] == 639
        with pytest.raises(ExperimentBudgetError, match="experiment_windows"):
            ledger.plan_window()


def test_cumulative_call_cap_still_blocks_after_renewal(state) -> None:
    path, now = state
    with ExperimentLedger(path) as ledger:
        authorize(ledger, asdict(ledger.plan_window()))
        for _ in range(38):
            ledger.reserve_call(1)
        with pytest.raises(ExperimentBudgetError, match="experiment_calls"):
            ledger.reserve_call(0)
    now[0] = 2200
    with ExperimentLedger(path) as ledger:
        assert ledger.snapshot["calls"] == 40
        with pytest.raises(ExperimentBudgetError, match="experiment_calls"):
            ledger.plan_window()


def test_copied_v2_inode_rejected_but_v1_synthetic_copy_plans_own_identity(
    state, tmp_path
) -> None:
    path, _ = state
    copy = tmp_path / "synthetic-copy"
    copy.write_bytes(path.read_bytes())
    own_intent = plan(copy)
    original_intent = plan(path)
    assert own_intent["ledger_identity"] != original_intent["ledger_identity"]
    with (
        ExperimentLedger(copy) as ledger,
        pytest.raises(ExperimentBudgetError, match="replaced"),
    ):
        authorize(ledger, original_intent)
    with ExperimentLedger(path) as ledger:
        authorize(ledger, original_intent)
    copy.write_bytes(path.read_bytes())
    with pytest.raises(ExperimentBudgetError, match="replaced"):
        ExperimentLedger(copy)


@pytest.mark.parametrize("change", ["append", "truncate", "replace"])
def test_in_place_tamper_or_replacement_before_authorization_fails_closed(
    state, change
) -> None:
    path, _ = state
    intent = plan(path)
    with ExperimentLedger(path) as ledger:
        if change == "replace":
            old = path.with_suffix(".old")
            path.rename(old)
            path.write_bytes(old.read_bytes())
        elif change == "append":
            with path.open("ab") as file:
                file.write(b" \n")
        else:
            path.write_bytes(path.read_bytes()[:-1])
        changed = path.read_bytes()
        with pytest.raises(ExperimentBudgetError, match="state_changed|replaced"):
            authorize(ledger, intent)
        assert path.read_bytes() == changed


def test_raw_prefix_hash_not_normalized_and_historical_call_window(state) -> None:
    path, _ = state
    with ExperimentLedger(path) as ledger:
        authorize(ledger, asdict(ledger.plan_window()))
    raw = path.read_bytes()
    first, *tail = raw.splitlines(keepends=True)
    path.write_bytes(b" " + first + b"".join(tail))
    with pytest.raises(ExperimentBudgetError, match="state_changed"):
        ExperimentLedger(path)
    path.write_bytes(raw)
    # Invalid historical call lies after the current window's fixed deadline.
    with path.open("ab") as file:
        file.write(b'{"kind":"call","at":2200,"value":1}\n')
    with pytest.raises(ExperimentBudgetError, match="corrupt"):
        ExperimentLedger(path)


def test_clock_rollback_between_observations_cannot_revive_window(state) -> None:
    path, now = state
    with ExperimentLedger(path) as ledger:
        assert ledger.remaining_seconds == 0
        now[0] = 1599
        with pytest.raises(ExperimentBudgetError, match="clock_rollback"):
            ledger.reserve_call(1)


@pytest.mark.parametrize("code", ["ledger_state_changed", "ledger_size_limit"])
def test_runtime_failure_reports_only_fixed_code(code) -> None:
    from decision_browser.flights_pilot import safe_failure

    assert safe_failure(ExperimentBudgetError(code)) == code
    assert safe_failure(ExperimentBudgetError("CANARY_NEVER_RETAIN")) == "unclassified_failure"


def test_size_limit_checked_before_authorization_append(state) -> None:
    path, _ = state
    raw = path.read_bytes()
    path.write_bytes(b" " * (eb._MAX_LEDGER_BYTES - len(raw)) + raw)
    original = path.read_bytes()
    with ExperimentLedger(path) as ledger:
        intent = asdict(ledger.plan_window())
        with pytest.raises(ExperimentBudgetError, match="size_limit"):
            authorize(ledger, intent)
        assert ledger.snapshot["windows"] == 1
    assert path.read_bytes() == original


def test_operator_status_plan_authorization_and_exclusive_intent_file(
    state, tmp_path, capsys
) -> None:
    path, _ = state
    output = tmp_path / "intent.json"
    original = path.read_bytes()
    assert operator.main(["status", "--ledger", str(path)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["remaining_seconds"] == 0
    assert (
        operator.main(["plan-window", "--ledger", str(path), "--output", str(output)])
        == 0
    )
    proposal = json.loads(capsys.readouterr().out)
    assert proposal["status"] == "PLANNED_NOT_AUTHORIZED"
    assert path.read_bytes() == original
    original_intent = output.read_bytes()
    assert (
        operator.main(["plan-window", "--ledger", str(path), "--output", str(output)])
        == 1
    )
    capsys.readouterr()
    assert output.read_bytes() == original_intent and path.read_bytes() == original
    command = ["authorize-window", "--ledger", str(path), "--intent", str(output)]
    assert operator.main(command) == 1
    assert (
        json.loads(capsys.readouterr().out)["reason"]
        == "operator_authorization_required"
    )
    assert path.read_bytes() == original
    assert operator.main(command + ["--operator-authorized"]) == 0
    assert json.loads(capsys.readouterr().out)["experiment"]["calls"] == 2
    granted = path.read_bytes()
    assert operator.main(command + ["--operator-authorized"]) == 1
    assert (
        json.loads(capsys.readouterr().out)["reason"] == "window_authorization_replayed"
    )
    assert path.read_bytes() == granted


def test_operator_and_runner_share_cross_process_lock(state, tmp_path) -> None:
    path, _ = state
    intent_file = tmp_path / "intent.json"
    operator.write_intent(intent_file, WindowIntent.parse(plan(path)))
    original = path.read_bytes()
    with ExperimentLedger(path):
        for command in (
            ["status"],
            ["authorize-window", "--intent", str(intent_file), "--operator-authorized"],
        ):
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "decision_browser.experiment_operator",
                    *command,
                    "--ledger",
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 1
            assert (
                json.loads(result.stdout)["reason"] == "ledger_unavailable_or_corrupt"
            )
            assert not result.stderr
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "contents",
    [
        b"{}",
        b'{"version":1,"version":1}',
        b"x" * 4097,
        b'{"secret":"CANARY_NEVER_RETAIN"}',
    ],
)
def test_operator_bad_intent_is_safe_fixed_error(tmp_path, contents) -> None:
    path = tmp_path / "intent.json"
    path.write_bytes(contents)
    with pytest.raises(ExperimentBudgetError) as error:
        operator.read_intent(path)
    assert str(error.value) == "window_intent_invalid"
    assert path.read_bytes() == contents
