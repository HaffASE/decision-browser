from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from decision_browser import experiment_budget as eb
from decision_browser.experiment_budget import ExperimentBudgetError, ExperimentLedger


def test_missing_and_explicit_creation_never_reset(tmp_path: Path) -> None:
    path = tmp_path / "experiment.jsonl"
    with pytest.raises(ExperimentBudgetError):
        ExperimentLedger(path)
    with ExperimentLedger(path, create=True) as ledger:
        ledger.reserve_call(128)
        ledger.reserve_step()
    with pytest.raises(ExperimentBudgetError):
        ExperimentLedger(path, create=True)
    with ExperimentLedger(path) as ledger:
        assert ledger.snapshot["calls"] == 1
        assert ledger.snapshot["steps"] == 1
        assert ledger.snapshot["input_bytes"] == 128
        assert ledger.snapshot["reported_usage"] == {}


def test_unknown_failed_calls_are_not_refunded(tmp_path: Path) -> None:
    path = tmp_path / "experiment.jsonl"
    with ExperimentLedger(path, create=True) as ledger:
        for _ in range(20):
            ledger.reserve_call(1)
    with ExperimentLedger(path) as ledger:
        for _ in range(20):
            ledger.reserve_call(1)
        with pytest.raises(ExperimentBudgetError, match="experiment_calls"):
            ledger.reserve_call(0)
        assert ledger.snapshot["calls"] == 40


def test_bytes_and_steps_have_independent_cumulative_limits(tmp_path: Path) -> None:
    with ExperimentLedger(tmp_path / "ledger", create=True) as ledger:
        ledger.reserve_call(eb.MAX_INPUT_BYTES)
        with pytest.raises(ExperimentBudgetError, match="experiment_input_bytes"):
            ledger.reserve_call(1)
        for _ in range(40):
            ledger.reserve_step()
        with pytest.raises(ExperimentBudgetError, match="experiment_steps"):
            ledger.reserve_step()
        assert ledger.snapshot["calls"] == 1


@pytest.mark.parametrize("contents", [b"", b"{}\n", b"not json\n", b"{}", b"[]\n"])
def test_corrupt_ledger_fails_closed(tmp_path: Path, contents: bytes) -> None:
    path = tmp_path / "ledger"
    path.write_bytes(contents)
    with pytest.raises(ExperimentBudgetError):
        ExperimentLedger(path)
    assert path.read_bytes() == contents


def test_partial_record_or_invalid_record_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    with ExperimentLedger(path, create=True) as ledger:
        ledger.reserve_call(12)
    original = path.read_bytes()
    for suffix in [b'{"kind":', b"{}\n", b"null\n"]:
        path.write_bytes(original + suffix)
        with pytest.raises(ExperimentBudgetError):
            ExperimentLedger(path)


def test_exclusive_lock_across_processes(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    with ExperimentLedger(path, create=True):
        with pytest.raises(ExperimentBudgetError):
            ExperimentLedger(path)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from decision_browser.experiment_budget import "
                    "ExperimentLedger; import sys; ExperimentLedger(sys.argv[1])"
                ),
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert "ExperimentBudgetError" in result.stderr
    with ExperimentLedger(path) as ledger:
        ledger.reserve_call(1)


def test_deadline_includes_idle_restart_and_clock_rollback(
    tmp_path: Path, monkeypatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(eb.time, "time", lambda: now[0])
    path = tmp_path / "ledger"
    with ExperimentLedger(path, create=True) as ledger:
        assert ledger.remaining_seconds == 600
        now[0] = 1100
        ledger.reserve_step()
    now[0] = 1599
    with ExperimentLedger(path) as ledger:
        assert ledger.remaining_seconds == 1
        ledger.reserve_call(1)
        now[0] = 1598
        with pytest.raises(ExperimentBudgetError, match="clock_rollback"):
            ledger.reserve_call(1)
        now[0] = 1600
        assert ledger.remaining_seconds == 0
        with pytest.raises(ExperimentBudgetError, match="expired"):
            ledger.reserve_call(1)


def test_finish_is_terminal_close_is_resumable(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    ledger = ExperimentLedger(path, create=True)
    ledger.finish()
    with ExperimentLedger(path) as resumed:
        assert resumed.remaining_seconds == 0
        with pytest.raises(ExperimentBudgetError, match="finished"):
            resumed.reserve_step()


def test_usage_is_safe_audit_and_cannot_refund_or_expand(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    with ExperimentLedger(path, create=True) as ledger:
        ledger.reserve_call(100)
        estimate = ledger.snapshot["estimated_cost_usd"]
        ledger.record_usage({"input_tokens": 2, "output_tokens": 1})
        assert ledger.snapshot["estimated_cost_usd"] == estimate
        with pytest.raises(ExperimentBudgetError, match="duplicate"):
            ledger.record_usage({"input_tokens": 2})
        ledger.reserve_call(10)
        with pytest.raises(ExperimentBudgetError):
            ledger.record_usage({"secret": "not retained"})
        ledger.record_usage({"input_tokens": 2_000_000})
        with pytest.raises(ExperimentBudgetError, match="estimated_cost"):
            ledger.reserve_call(1)
    with ExperimentLedger(path) as ledger:
        assert ledger.snapshot["calls"] == 2
        assert ledger.snapshot["estimated_cost_usd"] > 0.20
        assert ledger.snapshot["billing_status"] == "estimated_not_provider_billing"
        assert "secret" not in path.read_text()
        with pytest.raises(ExperimentBudgetError, match="estimated_cost"):
            ledger.reserve_call(1)


def test_estimate_uses_approved_input_price_constant(tmp_path: Path, monkeypatch) -> None:
    import decision_browser.experiment_budget as budget

    assert budget._MAX_ESTIMATED_TOKENS == int(
        (budget.APPROVED_USD - budget.UNCERTAINTY_RESERVE_USD)
        * 1_000_000 / budget.INPUT_USD_PER_MILLION
    ) == 2_000_000
    with ExperimentLedger(tmp_path / "price-ledger", create=True) as ledger:
        ledger.reserve_call(100)
        before = ledger.snapshot["estimated_cost_usd"]
        monkeypatch.setattr(budget, "INPUT_USD_PER_MILLION", budget.INPUT_USD_PER_MILLION * 2)
        assert ledger.snapshot["estimated_cost_usd"] == before * 2
        assert ledger.snapshot["billing_status"] == "estimated_not_provider_billing"


def test_reservation_is_on_disk_before_return(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    with ExperimentLedger(path, create=True) as ledger:
        ledger.reserve_call(77)
        assert json.loads(path.read_text().splitlines()[-1])["value"] == 77


def test_fsync_failure_poisoned_and_no_retry(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "ledger"
    with ExperimentLedger(path, create=True) as ledger:

        def fail(fd):
            raise OSError("simulated disk failure")

        with monkeypatch.context() as patch:
            patch.setattr(eb.os, "fsync", fail)
            with pytest.raises(ExperimentBudgetError, match="write_uncertain"):
                ledger.reserve_call(77)
        with pytest.raises(ExperimentBudgetError, match="uncertain"):
            ledger.reserve_call(77)
    with ExperimentLedger(path) as ledger:
        assert ledger.snapshot["calls"] == 1


def test_removed_or_replaced_ledger_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    with ExperimentLedger(path, create=True) as ledger:
        path.unlink()
        with pytest.raises(ExperimentBudgetError, match="missing_or_replaced"):
            ledger.reserve_call(1)


@pytest.mark.parametrize("value", [-1, True, 1.2, "1"])
def test_invalid_counts_never_reserved(tmp_path: Path, value) -> None:
    with ExperimentLedger(tmp_path / "ledger", create=True) as ledger:
        with pytest.raises(ExperimentBudgetError, match="invalid_count"):
            ledger.reserve_call(value)
        assert ledger.snapshot["calls"] == 0


def test_symlink_resume_rejected(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    with ExperimentLedger(path, create=True):
        pass
    link = tmp_path / "link"
    os.symlink(path, link)
    with pytest.raises(ExperimentBudgetError):
        ExperimentLedger(link)


def test_changed_limits_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    with ExperimentLedger(path, create=True):
        pass
    header = json.loads(path.read_text())
    header["limits"]["calls"] = 41
    path.write_text(json.dumps(header) + "\n")
    with pytest.raises(ExperimentBudgetError, match="ledger_corrupt"):
        ExperimentLedger(path)


def test_short_write_poisoned_and_resume_rejects_partial_record(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "ledger"
    original_write = os.write
    with ExperimentLedger(path, create=True) as ledger:

        def short_write(fd, data):
            return original_write(fd, data[:5])

        with monkeypatch.context() as patch:
            patch.setattr(eb.os, "write", short_write)
            with pytest.raises(ExperimentBudgetError, match="write_uncertain"):
                ledger.reserve_call(77)
        with pytest.raises(ExperimentBudgetError, match="uncertain"):
            ledger.reserve_step()
    with pytest.raises(ExperimentBudgetError, match="ledger_corrupt"):
        ExperimentLedger(path)
