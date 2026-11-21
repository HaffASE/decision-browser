"""F06 lifecycle regressions; deterministic owned resources and public polling."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from decision_browser import runner
from decision_browser.browser import BrowserDriver
from decision_browser.contracts import Check, Limits, PrivacyPolicy, TaskRequest
from decision_browser.dom import Action
from decision_browser.provider_http import ProviderError, post_json
from decision_browser.runner import RunManager, RunnerError


def request(*, seconds=10.0, visual=False, checks=()):
    return TaskRequest(
        goal="Review the owned page", start_url="https://example.test/", values={},
        checks=list(checks), privacy=PrivacyPolicy(), limits=Limits(max_seconds=seconds),
        visual=visual,
    )


class Driver:
    cleanup_errors = ()
    residual_owned = False
    foreign_mismatch = False

    def __init__(self, close=None):
        self.close_hook = close
        self.entered = asyncio.Event()
        self.closed = 0
        self.page = SimpleNamespace(url="https://example.test/")

    async def close(self):
        self.entered.set()
        self.closed += 1
        if self.close_hook is not None:
            await self.close_hook()


class VerifiedManager(RunManager):
    async def _body(self, run):
        self._set(run, terminal="verified", functional="verified", reason="verified")


async def finish_task(host):
    task = host._run.task
    assert task is not None
    async with asyncio.timeout(2):
        return await asyncio.gather(task, return_exceptions=True)


async def test_wait_joins_slow_cancel_finally_before_returning():
    host = RunManager(lambda: None, lambda mode: Driver(), cleanup_grace=0.2)
    run = host._open(request())
    entered, finished = asyncio.Event(), asyncio.Event()

    async def operation():
        entered.set()
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0.01)
            finished.set()

    waiter = asyncio.create_task(host._wait(run, operation()))
    await entered.wait()
    run.cancel_event.set()
    try:
        with pytest.raises(RunnerError, match="cancelled"):
            await waiter
        assert finished.is_set(), "_wait returned while its operation finally was still live"
    finally:
        await asyncio.sleep(0.02)


async def test_successful_auxiliary_wait_does_not_cancel_running_body():
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingManager(VerifiedManager):
        async def _body(self, run):
            self._set(run, terminal="running")
            entered.set()
            await release.wait()
            await super()._body(run)

    host = WaitingManager(lambda: None, lambda mode: Driver())
    run_id = host.start_task(request())
    await entered.wait()
    try:
        result = await host._wait(host._run, asyncio.sleep(0, result="auxiliary consent result"))
        assert result == "auxiliary consent result"
        assert not host._run.task.done()
        assert host.get_task(run_id).terminal == "running"
    finally:
        release.set()
        await finish_task(host)
    assert host.get_task(run_id).terminal == "verified"


def test_unknown_audit_is_not_repeated_after_reservation_release_and_deadline():
    clock = {"now": 0.0}
    host = RunManager(lambda: None, lambda mode: Driver(), clock=lambda: clock["now"])
    run = host._open(request(seconds=0.01))
    action = Action("click", "target", None, None, "obs", "{}")
    host._prepare_audit(run, action, "fixture_mapped")
    run.queued = True
    host._audit_unknown(run)
    host._release_audit(run)
    before = list(run.result.artifact_ids)
    clock["now"] = 1.0
    host._audit_unknown(run)
    host._halt(run, "unknown")
    assert run.result.artifact_ids == before
    assert "journal_unavailable" not in run.result.evidence
    outcomes = [run.store.read(key).content for key in before]
    assert sum(b'"event":"outcome"' in body for body in outcomes) == 1


async def test_immediate_operation_error_keeps_specific_post_observe_audit_available():
    host = RunManager(lambda: None, lambda mode: Driver())
    run = host._open(request())
    action = Action("click", "target", None, None, "obs", "{}")
    host._prepare_audit(run, action, "fixture_mapped")
    run.queued = True
    host._stamp(run, run.journal.outcome(run.result.run_id, run.attempt_id, "acknowledged"))

    async def failed_read():
        raise RuntimeError("synthetic post-observe failure")

    with pytest.raises(RuntimeError, match="synthetic post-observe failure"):
        await host._wait(run, failed_read())
    assert run.unknown_attempt_id is None
    host._audit_unknown(run, post_observe=True)
    records = [run.store.read(key).content for key in run.result.artifact_ids]
    assert sum(b'"event":"post_observe_failure"' in record for record in records) == 1
    assert sum(b'"event":"outcome"' in record for record in records) == 1
    host._release_audit(run)


async def test_pending_cancel_audits_before_join_can_expire_reserved_grace():
    clock = {"now": 0.0}
    entered = asyncio.Event()
    host = RunManager(lambda: None, lambda mode: Driver(),
                      clock=lambda: clock["now"], cleanup_grace=0.1)
    run = host._open(request(seconds=0.01))
    host._prepare_audit(run, Action("click", "target", None, None, "obs", "{}"), "fixture_mapped")
    run.queued = True

    async def pending_operation():
        entered.set()
        try:
            await asyncio.Future()
        finally:
            clock["now"] = 2.0
            await asyncio.sleep(0.01)

    operation = asyncio.create_task(host._wait(run, pending_operation()))
    await entered.wait()
    run.cancel_event.set()
    with pytest.raises(RunnerError, match="cancelled"):
        await operation
    assert run.unknown_attempt_id == run.attempt_id
    assert "journal_unavailable" not in run.result.evidence
    records = [run.store.read(key).content for key in run.result.artifact_ids]
    assert sum(b'"status":"outcome_unknown"' in record for record in records) == 1
    host._release_audit(run)


@pytest.mark.parametrize("queued", [False, True])
async def test_public_cancellation_joins_body_before_conflicting_driver_close(queued):
    entered, finalizing, release, finished = (asyncio.Event() for _ in range(4))
    closed_before_quiescence = []

    async def record_close():
        closed_before_quiescence.append(not finished.is_set())

    class WaitingManager(RunManager):
        async def _body(self, run):
            self._set(run, terminal="running")
            run.queued = queued
            entered.set()
            try:
                await asyncio.Future()
            finally:
                finalizing.set()
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue
                finished.set()

    driver = Driver(record_close)
    host = WaitingManager(lambda: None, lambda mode: driver, cleanup_grace=0.2)
    run_id = host.start_task(request())
    await entered.wait()
    host.cancel_task(run_id)
    await finalizing.wait()
    try:
        await asyncio.sleep(0.01)
        assert not driver.entered.is_set(), "teardown overlapped a still-running body finally"
    finally:
        release.set()
        await finish_task(host)
    assert finished.is_set()
    assert closed_before_quiescence == [False]
    assert host.get_task(run_id).terminal == ("outcome_unknown" if queued else "cancelled")


async def test_hanging_dom_does_not_starve_driver_or_provider_close():
    provider_closed = []
    dom_entered = asyncio.Event()

    async def dom_close():
        dom_entered.set()
        await asyncio.Future()

    class ResourcesManager(VerifiedManager):
        async def _body(self, run):
            run.dom = SimpleNamespace(close=dom_close)
            run.provider = SimpleNamespace(aclose=AsyncMock(side_effect=lambda: provider_closed.append(True)))
            await super()._body(run)

    driver = Driver()
    host = ResourcesManager(lambda: None, lambda mode: driver, cleanup_grace=0.03)
    run_id = host.start_task(request())
    await finish_task(host)
    assert dom_entered.is_set()
    assert driver.closed == 1
    assert provider_closed == [True]
    result = host.get_task(run_id)
    assert result.terminal == "outcome_unknown" and result.reason == "cleanup_unknown"
    assert result.functional == "verified"


@pytest.mark.parametrize("provider_hangs", [False, True])
async def test_browser_close_uses_time_left_after_independent_provider_close(provider_hangs):
    finished = asyncio.Event()

    async def provider_close():
        if provider_hangs:
            await asyncio.Future()

    async def driver_close():
        if not provider_hangs:
            # This fits the two-second cleanup budget, but not the old
            # driver slot that reserved half of the remaining budget.
            await asyncio.sleep(1.0)
        finished.set()

    class ResourcesManager(VerifiedManager):
        async def _body(self, run):
            run.dom = SimpleNamespace(close=AsyncMock())
            run.provider = SimpleNamespace(aclose=provider_close)
            await super()._body(run)

    driver = Driver(driver_close)
    host = ResourcesManager(lambda: None, lambda mode: driver, cleanup_grace=0.2 if provider_hangs else 2.0)
    run_id = host.start_task(request())
    await finish_task(host)
    assert finished.is_set()
    assert driver.closed == 1
    result = host.get_task(run_id)
    assert result.functional == "verified"
    assert result.terminal == ("outcome_unknown" if provider_hangs else "verified")


async def test_residual_body_is_retained_without_conflicting_close_or_late_success():
    entered, finalizing, release, finished = (asyncio.Event() for _ in range(4))

    class ResistantManager(RunManager):
        async def _body(self, run):
            self._set(run, terminal="verified", functional="verified", reason="verified")
            entered.set()
            try:
                await asyncio.Future()
            finally:
                finalizing.set()
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue
                self._set(run, terminal="verified", reason="verified")
                finished.set()

    driver = Driver()
    host = ResistantManager(lambda: None, lambda mode: driver,
                            clock=lambda: 0.0, cleanup_grace=0.02)
    run_id = host.start_task(request())
    await entered.wait()
    host.cancel_task(run_id)
    await finalizing.wait()
    try:
        await finish_task(host)
        result = host.get_task(run_id)
        assert result.terminal == "outcome_unknown"
        assert "cleanup:residual_operation" in result.evidence
        assert driver.closed == 0, "a live residual operation still owns the driver"
        host._run.expires_at = 0.0
        with pytest.raises(RunnerError, match="busy"):
            host.start_task(request())
        host._run.expires_at = 60.0
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=1)
    assert host.get_task(run_id).terminal == "outcome_unknown"
    await host.aclose()
    assert driver.closed == 1


async def test_expired_execution_window_still_attempts_each_owned_close():
    clock = {"now": 0.0}
    provider_closed = []

    class ExpiredManager(VerifiedManager):
        async def _body(self, run):
            run.provider = SimpleNamespace(aclose=AsyncMock(side_effect=lambda: provider_closed.append(True)))
            await super()._body(run)
            clock["now"] = 1.0

    driver = Driver()
    host = ExpiredManager(lambda: None, lambda mode: driver,
                          clock=lambda: clock["now"], cleanup_grace=0.03)
    run_id = host.start_task(request(seconds=0.1))
    await finish_task(host)
    assert driver.closed == 1
    assert provider_closed == [True]
    assert host.get_task(run_id).terminal != "running"


@pytest.mark.parametrize("cancel", [False, True])
async def test_exact_transport_residual_task_handoff_retains_busy_ownership(cancel):
    entered, release, child_finished = (asyncio.Event() for _ in range(3))
    children = []

    async def close_child():
        entered.set()
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
        finally:
            child_finished.set()

    class TransportManager(RunManager):
        async def _body(self, run):
            self._set(run, terminal="running")

            async def operation():
                child = asyncio.create_task(close_child())
                children.append(child)
                await entered.wait()
                try:
                    if cancel:
                        await asyncio.Future()
                    raise ProviderError("malformed_response")
                finally:
                    task = asyncio.current_task()
                    task._decision_browser_residual_tasks = (child,)

            await self._wait(run, operation())

    driver = Driver()
    host = TransportManager(lambda: None, lambda mode: driver,
                            clock=lambda: 0.0, cleanup_grace=0.03)
    run_id = host.start_task(request())
    await entered.wait()
    if cancel:
        host.cancel_task(run_id)
    try:
        await finish_task(host)
        result = host.get_task(run_id)
        assert result.terminal == "outcome_unknown"
        assert "cleanup:residual_operation" in result.evidence
        assert driver.closed == 0
        host._run.expires_at = 0.0
        assert host.get_task(run_id).terminal == "outcome_unknown"
        with pytest.raises(RunnerError, match="busy"):
            host.start_task(request())
        host._run.expires_at = 60.0
    finally:
        release.set()
        await asyncio.wait_for(child_finished.wait(), timeout=1)
        await asyncio.gather(*children, return_exceptions=True)
    await host.aclose()
    assert driver.closed == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_actual_provider_response_close_residual_is_adopted_by_runner(cancel):
    started, release, finished = (asyncio.Event() for _ in range(3))

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 50

        async def aclose(self):
            started.set()
            try:
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue
            finally:
                finished.set()

    stream = Stream()
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream),
    ))

    class TransportManager(RunManager):
        async def _body(self, run):
            run.provider = client
            self._set(run, terminal="running")
            await self._wait(run, post_json(
                client, "https://example.test/provider", None, {},
                budget=run.budget, privacy=run.privacy,
            ))

    driver = Driver()
    host = TransportManager(lambda: None, lambda mode: driver,
                            clock=lambda: 0.0, cleanup_grace=0.03)
    req = request().model_copy(update={"limits": Limits(max_seconds=10, max_output_bytes=10)})
    run_id = host.start_task(req)
    await asyncio.wait_for(started.wait(), timeout=1)
    if cancel:
        host.cancel_task(run_id)
    try:
        await finish_task(host)
        result = host.get_task(run_id)
        assert result.terminal == "outcome_unknown"
        assert "cleanup:residual_operation" in result.evidence
        assert driver.closed == 0
        assert host._run.operations
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=1)
        await host.aclose()
        await client.aclose()
    assert driver.closed == 1


async def test_direct_task_cancel_during_cleanup_cannot_publish_provisional_verified():
    release = asyncio.Event()
    cleanup_job = []

    async def delayed_close():
        cleanup_job.append(asyncio.current_task())
        await release.wait()

    driver = Driver(delayed_close)
    host = VerifiedManager(lambda: None, lambda mode: driver, cleanup_grace=0.1)
    run_id = host.start_task(request())
    await driver.entered.wait()
    assert host.get_task(run_id).terminal == "running"
    host._run.task.cancel()
    try:
        await finish_task(host)
        result = host.get_task(run_id)
        assert result.terminal == "outcome_unknown"
        assert result.reason == "cleanup_unknown"
        assert result.functional == "verified"
    finally:
        release.set()
        if cleanup_job:
            await asyncio.gather(*cleanup_job, return_exceptions=True)


async def test_clock_failure_in_finalizer_cannot_publish_provisional_verified():
    state = {"bad": False}

    def clock():
        if state["bad"]:
            raise ValueError("synthetic clock failure")
        return 0.0

    async def fail_clock():
        state["bad"] = True

    driver = Driver(fail_clock)
    host = VerifiedManager(lambda: None, lambda mode: driver, clock=clock)
    run_id = host.start_task(request())
    await finish_task(host)
    state["bad"] = False
    result = host.get_task(run_id)
    assert result.terminal == "outcome_unknown"
    assert result.reason == "cleanup_unknown"
    assert result.functional == "verified"


async def test_public_cancel_during_visual_is_not_swallowed(monkeypatch):
    entered = asyncio.Event()

    async def inspect_geometry(page, viewport):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(runner, "inspect_geometry", inspect_geometry)

    class VerifyingManager(RunManager):
        async def _body(self, run):
            self._set(run, terminal="running")
            await self._verify(run, finish=True)

    driver = Driver()
    host = VerifyingManager(lambda: None, lambda mode: driver)
    run_id = host.start_task(request(visual=True, checks=[
        Check(kind="route", target="", expected=driver.page.url),
    ]))
    await entered.wait()
    assert host.get_task(run_id).terminal == "running"
    host.cancel_task(run_id)
    await finish_task(host)
    result = host.get_task(run_id)
    assert result.terminal == "cancelled"
    assert result.reason == "cancelled"
    assert result.functional == "verified"
    assert driver.closed == 1


@pytest.mark.parametrize("stage", ["context", "isolated", "owned_tasks"])
async def test_browser_cancelled_cleanup_attempts_remaining_owned_closures(stage):
    driver = BrowserDriver("isolated")
    driver._state = "open"
    context = SimpleNamespace(close=AsyncMock())
    browser = SimpleNamespace(close=AsyncMock())
    playwright = SimpleNamespace(stop=AsyncMock())
    driver._context, driver._browser, driver._playwright = context, browser, playwright
    driver.disarm_owned_initial_document = AsyncMock()
    driver._cancel_owned_tasks = AsyncMock()
    original_cleanup = driver._cleanup_isolated
    if stage == "context":
        context.close.side_effect = asyncio.CancelledError
    elif stage == "isolated":
        driver._cleanup_isolated = AsyncMock(side_effect=asyncio.CancelledError)
    else:
        driver._cancel_owned_tasks.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await driver.close()
    if stage == "isolated":
        # With no acknowledged resource closure, stopping the prerequisite
        # transport would destroy the capability needed for exact retry.
        assert driver.cleanup_pending and playwright.stop.await_count == 0
        assert driver._browser is browser and driver._context is context
        driver._cleanup_isolated = original_cleanup
        await driver.close()
    else:
        assert browser.close.await_count == 1
    assert playwright.stop.await_count == 1
    assert browser.close.await_count == 1
    assert driver._state == "closed"
    assert not driver.cleanup_pending


async def test_successful_cleanup_preserves_verified_and_closes_once():
    driver = Driver()
    host = VerifiedManager(lambda: None, lambda mode: driver)
    run_id = host.start_task(request())
    await finish_task(host)
    result = host.get_task(run_id)
    assert result.terminal == result.functional == "verified"
    assert result.reason == "verified"
    assert driver.closed == 1


@pytest.mark.parametrize("expiry_path", ["get", "start"])
async def test_completed_residual_cannot_expire_before_deferred_cleanup(expiry_path):
    host = RunManager(lambda: None, lambda mode: Driver(), clock=lambda: 0.0, cleanup_grace=0.1)
    run = host._open(request())
    host._run = run
    host._set(run, terminal="outcome_unknown", reason="cleanup_unknown")
    run.committed = run.result.model_copy(deep=True)
    run.expires_at = -1.0
    operation = asyncio.get_running_loop().create_future()
    host._track_operation(run, operation)
    # set_result schedules its done callback for a later loop turn. The sync
    # public methods must retain ownership during this exact interval.
    operation.set_result(None)
    try:
        if expiry_path == "get":
            assert host.get_task(run.result.run_id).terminal == "outcome_unknown"
        else:
            with pytest.raises(RunnerError, match="busy"):
                host.start_task(request())
        assert host._run is run and not run.cleanup_attempted
        await asyncio.wait_for(run.driver.entered.wait(), timeout=1)
        async with asyncio.timeout(1):
            while host._owned_pending(run):
                await asyncio.sleep(0)
        assert run.driver.closed == 1
        assert run.cleanup_attempted
        assert run.committed.terminal == "outcome_unknown"
        with pytest.raises(RunnerError, match="expired"):
            host.get_task(run.result.run_id)
    finally:
        if host._run is None:
            host._run = run
        await host.aclose()


async def test_aclose_joins_deferred_cleanup_without_cancelling_or_double_close():
    close_release = asyncio.Event()
    close_cancelled = []

    async def close():
        try:
            await close_release.wait()
        except asyncio.CancelledError:
            close_cancelled.append(True)
            raise

    driver = Driver(close)
    host = RunManager(lambda: None, lambda mode: driver, cleanup_grace=0.2)
    run = host._open(request())
    host._run = run
    host._set(run, terminal="outcome_unknown", reason="cleanup_unknown")
    run.committed = run.result.model_copy(deep=True)
    operation = asyncio.get_running_loop().create_future()
    host._track_operation(run, operation)
    operation.set_result(None)
    shutdown = None
    try:
        await asyncio.wait_for(driver.entered.wait(), timeout=0.2)
        shutdown = asyncio.create_task(host.aclose())
        await asyncio.sleep(0.01)
        assert driver.closed == 1 and not close_cancelled
        close_release.set()
        await asyncio.wait_for(shutdown, timeout=1)
        assert driver.closed == 1 and not close_cancelled
        assert not host._owned_pending(run)
        assert run.committed.terminal == "outcome_unknown"
    finally:
        close_release.set()
        if shutdown is not None:
            await asyncio.gather(shutdown, return_exceptions=True)
        await host.aclose()


async def test_pending_driver_cleanup_holds_expired_run_and_concurrent_aclose_retries_only_driver():
    retry_entered, retry_release = asyncio.Event(), asyncio.Event()
    counts = {"dom": 0, "provider": 0}
    clock = {"now": 0.0}

    class PartialDriver(Driver):
        def __init__(self):
            super().__init__()
            self.cleanup_errors = ["cleanup_cancelled"]
            self.cleanup_pending = False

        async def close(self):
            self.closed += 1
            if self.closed == 1:
                self.cleanup_pending = True
                raise asyncio.CancelledError
            # Like a real driver, an active retry is "closing" rather than
            # "cleanup_pending"; the exact shared job still owns the work.
            self.cleanup_pending = False
            retry_entered.set()
            await retry_release.wait()
            self.cleanup_pending = False

    class ResourcesManager(VerifiedManager):
        async def _body(self, run):
            async def dom_close():
                counts["dom"] += 1

            async def provider_close():
                counts["provider"] += 1

            run.dom = SimpleNamespace(close=dom_close)
            run.provider = SimpleNamespace(aclose=provider_close)
            await super()._body(run)

    driver = PartialDriver()
    host = ResourcesManager(lambda: None, lambda mode: driver,
                            clock=lambda: clock["now"], cleanup_grace=0.2)
    run_id = host.start_task(request())
    await finish_task(host)
    run = host._run
    assert driver.closed == 1 and driver.cleanup_pending
    assert counts == {"dom": 1, "provider": 1}
    assert host.get_task(run_id).terminal == "outcome_unknown"
    clock["now"] = 100.0
    shutdowns = []
    try:
        assert host.get_task(run_id).terminal == "outcome_unknown"
        with pytest.raises(RunnerError, match="busy"):
            host.start_task(request())
        shutdowns.append(asyncio.create_task(host.aclose()))
        await asyncio.wait_for(retry_entered.wait(), timeout=1)
        shutdowns.append(asyncio.create_task(host.aclose()))
        await asyncio.sleep(0.01)
        assert driver.closed == 2
        assert not any(shutdown.done() for shutdown in shutdowns)
        retry_release.set()
        await asyncio.wait_for(asyncio.gather(*shutdowns), timeout=1)
        assert driver.closed == 2 and not driver.cleanup_pending
        assert counts == {"dom": 1, "provider": 1}
        assert not host._owned_pending(run)
        assert run.committed.terminal == "outcome_unknown"
        assert driver.cleanup_errors == ["cleanup_cancelled"]
    finally:
        retry_release.set()
        if shutdowns:
            await asyncio.gather(*shutdowns, return_exceptions=True)
        if host._run is None:
            host._run = run
        await host.aclose()


async def test_driver_pending_predicate_tracks_owned_work_not_historical_errors():
    driver = BrowserDriver("isolated")
    driver._state = "cleanup_pending"
    driver._cleanup_mode_done = True
    driver.cleanup_errors.append("cleanup_cancelled")
    release = asyncio.Event()
    job = asyncio.create_task(release.wait())
    driver._owned_tasks.add(job)
    assert driver.cleanup_pending
    release.set()
    await job
    assert not driver.cleanup_pending
    await driver.close()
    assert driver._state == "closed"
    assert driver.cleanup_errors == ["cleanup_cancelled"]
