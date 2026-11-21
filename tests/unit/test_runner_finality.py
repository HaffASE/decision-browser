"""Public polling must not publish terminal results before cleanup finishes."""
from __future__ import annotations

import asyncio

import pytest

from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.runner import RunManager


class DelayedDriver:
    cleanup_errors = ()
    residual_owned = ()
    foreign_mismatch = False

    def __init__(self, fail=False):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = fail

    async def close(self):
        self.entered.set()
        await self.release.wait()
        if self.fail:
            raise RuntimeError("close failed")


class VerifiedManager(RunManager):
    async def _body(self, run):
        self._set(run, terminal="verified", functional="verified", reason="verified")


def request():
    return TaskRequest(
        goal="Review", start_url="https://example.test/", values={}, checks=[],
        privacy=PrivacyPolicy(), limits=Limits(max_seconds=10),
    )


@pytest.mark.parametrize("fail", [False, True])
async def test_public_terminal_waits_for_cleanup(fail):
    driver = DelayedDriver(fail)
    host = VerifiedManager(lambda: None, lambda mode: driver)
    run_id = host.start_task(request())
    await driver.entered.wait()
    try:
        interim = host.get_task(run_id)
        assert interim.terminal == "running"
        assert interim.reason != "verified"
    finally:
        driver.release.set()
        await host._run.task
    result = host.get_task(run_id)
    assert result.terminal == ("outcome_unknown" if fail else "verified")
    assert result.reason == ("cleanup_unknown" if fail else "verified")
    assert result.functional == "verified"
    await asyncio.sleep(0)
    assert host.get_task(run_id) == result


async def test_final_evidence_storage_failure_cannot_publish_success():
    from types import SimpleNamespace

    from decision_browser.contracts import Check

    class VerifyingManager(RunManager):
        async def _body(self, run):
            self._set(run, terminal="running")
            await self._verify(run, finish=True)

    driver = DelayedDriver()
    driver.page = SimpleNamespace(url="https://example.test/")
    driver.release.set()
    host = VerifyingManager(lambda: None, lambda mode: driver)
    task = request().model_copy(update={
        "checks": [Check(kind="route", target="", expected=driver.page.url)],
        "limits": Limits(max_seconds=10, max_stored_bytes=1),
    })
    run_id = host.start_task(task)
    await host._run.task
    result = host.get_task(run_id)
    assert result.functional == "verified"
    assert result.terminal == "limit_exceeded"
    assert result.reason == "limit_exceeded"
    assert result.artifact_ids == []


@pytest.mark.parametrize("trigger", ["cancel", "deadline"])
@pytest.mark.parametrize("fail", [False, True])
async def test_cancellation_and_watchdog_wait_for_cleanup(trigger, fail):
    started = asyncio.Event()

    class WaitingManager(RunManager):
        async def _body(self, run):
            self._set(run, terminal="running")
            started.set()
            await asyncio.Future()

    driver = DelayedDriver(fail)
    host = WaitingManager(lambda: None, lambda mode: driver)
    req = request().model_copy(update={"limits": Limits(max_seconds=0.05)})
    run_id = host.start_task(req)
    await started.wait()
    if trigger == "cancel":
        assert host.cancel_task(run_id).terminal == "running"
    await driver.entered.wait()
    assert host.get_task(run_id).terminal == "running"
    driver.release.set()
    async with asyncio.timeout(2):
        while host.get_task(run_id).terminal == "running":
            await asyncio.sleep(0)
    result = host.get_task(run_id)
    expected = "cancelled" if trigger == "cancel" else "limit_exceeded"
    assert result.terminal == ("outcome_unknown" if fail else expected)
    assert host._run.task.done()


async def test_watchdog_and_cleanup_timeout_leave_public_final_result_readable():
    class HangingManager(RunManager):
        async def _body(self, run):
            self._set(run, terminal="running")
            await asyncio.Future()

    driver = DelayedDriver()
    host = HangingManager(lambda: None, lambda mode: driver, cleanup_grace=0.03)
    req = request().model_copy(update={"limits": Limits(max_seconds=0.02)})
    run_id = host.start_task(req)
    async with asyncio.timeout(2):
        while host.get_task(run_id).terminal in {"queued", "awaiting_human_admission", "running"}:
            await asyncio.sleep(0.001)
    result = host.get_task(run_id)
    assert result.terminal == "outcome_unknown"
    assert result.reason == "cleanup_unknown"
