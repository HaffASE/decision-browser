"""F12 boundary regressions; synthetic CDP proves state, not Chromium races."""
from __future__ import annotations

import asyncio
import hashlib
import time
from types import SimpleNamespace

import pytest
from test_browser_body_reader import _chunk, _open, _pause, _Session

from decision_browser.browser import (
    BrowserBlocked,
    BrowserDriver,
    _OwnedInitialArm,
    _OwnedInitialObservation,
)
from decision_browser.dom import Action, DOMSession
from decision_browser.owned_fixture_server import OwnedFixtureResource
from decision_browser.privacy import CaptureEvidence


def _arm(driver, *, deadline=None):
    arm = _OwnedInitialArm(
        expected_url="https://owned.example/doc", deadline=deadline or time.monotonic() + 2,
        budget=object(), owner=object(), resource=object(), page=object(),
        context=object(), target_id="owned",
    )
    arm.note.set()
    arm.paused_ids.append("req")
    arm.gate.update(request_id="req", fulfilled=True, enabled=True, disabled=True)
    driver._owned_arm = arm
    return arm


def _observation():
    return _OwnedInitialObservation("req", "stream", (), b"ok", False, (), "main")


async def test_db073_finish_rereads_successful_late_waiter():
    driver = BrowserDriver("isolated")
    arm = _arm(driver)

    async def observe():
        await asyncio.sleep(0)
        arm.observation = _observation()

    arm.waiter = asyncio.create_task(observe())
    observation = await driver.finish_owned_initial_document(arm, arm.budget)
    assert observation.body == b"ok" and observation.release_ok


@pytest.mark.parametrize("disarm", [False, True])
async def test_db074_detach_late_pause_cannot_be_release_ok(disarm):
    driver = BrowserDriver("isolated")
    arm = _arm(driver)
    arm.observation = _observation()
    arm.waiter = asyncio.create_task(asyncio.sleep(0))
    await arm.waiter

    class Session:
        async def send(self, method, params):
            return {}

        async def detach(self):
            driver._owned_on_paused(arm, {"requestId": "late"})

    arm.session = Session()
    if disarm:
        await driver.disarm_owned_initial_document()
        assert "cleanup_uncertain" in driver.cleanup_errors
    else:
        result = await driver.finish_owned_initial_document(arm, arm.budget)
        assert not result.release_ok and result.body == b""
    assert not arm.release_ok


async def test_db071_expired_execution_still_closes_owned_stream_and_detaches():
    driver = BrowserDriver("isolated")
    arm = _arm(driver, deadline=time.monotonic() - 1)
    arm.gate.update(handle="stream", stream_closed=False, disabled=False)
    calls = []

    class Session:
        async def send(self, method, params):
            calls.append(method)
            return {}

        async def detach(self):
            calls.append("detach")

    arm.session = Session()
    await driver.disarm_owned_initial_document()
    assert calls == ["IO.close", "Fetch.disable", "detach"]
    assert arm.session is None


async def test_db070_public_reader_preserves_nested_cancellation_depth(monkeypatch):
    driver, _, _ = _open(_Session([]))
    started = asyncio.Event()

    async def stalled(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(driver, "_read_owned_document_body_task", stalled)
    task = asyncio.create_task(driver.read_owned_document_body("https://owned.example/doc", time.monotonic() + 1))
    await started.wait()
    task.cancel()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelling() == 2
    assert not driver._owned_tasks


async def test_db111_reader_uses_injected_execution_clock():
    session = _Session([_chunk("ok", eof=True)])
    driver, _, _ = _open(session)
    driver._clock = lambda: 0.0
    result = await driver.read_owned_document_body("https://owned.example/doc", 5.0)
    assert result.body == b"ok" and session.detached


async def test_db072_url_is_literal_cdp_pattern():
    url = "https://owned.example/doc?x=*"
    pause = _pause()
    pause["request"]["url"] = url
    session = _Session([_chunk("ok", eof=True)], pause=pause)
    driver, _, _ = _open(session)
    result = await driver.read_owned_document_body(url, time.monotonic() + 2)
    assert result.body == b"ok"
    enabled = next(params for method, params in session.calls if method == "Fetch.enable")
    assert enabled["patterns"][0]["urlPattern"] == r"https://owned.example/doc\?x=\*"


@pytest.mark.parametrize("field,value", [
    ("sanitized", "false"), ("sanitized", 1),
    ("sensitive_content_detected", "false"), ("sensitive_content_detected", 0),
    ("capture_sha256", b"digest"),
])
def test_db151_capture_evidence_rejects_inexact_types(field, value):
    fields = {"capture_sha256": hashlib.sha256(b"ok").hexdigest(), "sanitized": True, "sensitive_content_detected": False}
    fields[field] = value
    with pytest.raises(TypeError):
        CaptureEvidence(**fields)


def test_db151_valid_evidence_keeps_hash_binding():
    digest = hashlib.sha256(b"ok").hexdigest()
    assert CaptureEvidence(digest, True, False).capture_sha256 == digest


async def test_db053_navigation_uses_remaining_budget_and_document_readiness():
    calls = []

    class Page:
        url = "about:blank"
        context = SimpleNamespace(pages=[])

        async def goto(self, url, **kwargs):
            calls.append(kwargs)
            self.url = url

    dom = DOMSession(Page())
    dom._navigation_remaining = lambda: 45.0
    action = Action("navigate", None, None, "https://owned.example", "obs", "{}")
    await dom._native(action)
    assert calls == [{"timeout": 45000.0, "wait_until": "load"}]


async def test_db058_completed_connections_are_discarded_and_failures_counted(monkeypatch):
    resource = OwnedFixtureResource()

    async def done(*args):
        return None

    monkeypatch.setattr(resource, "_handle", done)
    for _ in range(30):
        resource._client(object(), object())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert resource.owned_task_count == 0
    assert (await resource.close()).clean


async def test_db071_fixture_close_joins_cancelled_task_after_execution_deadline():
    resource = OwnedFixtureResource()
    resource._deadline = time.monotonic() - 1
    started = asyncio.Event()
    stopped = []

    async def pending():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(True)

    task = asyncio.create_task(pending())
    resource._tasks.add(task)
    await started.wait()
    report = await resource.close()
    await asyncio.gather(task, return_exceptions=True)
    assert report.clean and stopped == [True]


async def test_db070_committed_capture_keeps_depth_and_uncertain_priority():
    from test_committed_navigation_capture import _World

    world = _World(io_gate=asyncio.Event())
    task = asyncio.create_task(world.run(time.monotonic() + 2))
    await asyncio.wait_for(world.io_started.wait(), .5)
    task.cancel()
    task.cancel()
    with pytest.raises(BrowserBlocked, match="cleanup_uncertain"):
        await task
    assert task.cancelling() == 2
    assert "detach" in world.order
    assert [name for name, _ in world.sent if name in {"IO.close", "Fetch.disable"}] == ["IO.close", "Fetch.disable"]


async def test_db071_resistant_cdp_is_bounded_retained_and_explicitly_retried(monkeypatch):
    from decision_browser import browser

    monkeypatch.setattr(browser, "_OWNED_CLEANUP_SECONDS", .02)
    driver = BrowserDriver("isolated")
    arm = _arm(driver, deadline=time.monotonic() - 1)
    arm.gate.update(handle="stream", stream_closed=False)
    release = asyncio.Event()
    began = asyncio.Event()

    class Session:
        async def send(self, method, params):
            if method == "IO.close" and not release.is_set():
                began.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
            return {}

        async def detach(self):
            return None

    arm.session = Session()
    try:
        async with asyncio.timeout(.2):
            await driver.disarm_owned_initial_document()
        assert began.is_set()
        assert driver._owned_arm is arm and arm.session is not None
        residual = tuple(driver._owned_tasks)
        assert len(residual) == 1 and not residual[0].done()
    finally:
        release.set()
        await asyncio.gather(*driver._owned_tasks, return_exceptions=True)
    await driver.disarm_owned_initial_document()
    assert arm.session is None and driver._owned_arm is None
    assert not driver._owned_tasks


async def test_db071_fixture_unknown_report_refreshes_after_owned_task_ends(monkeypatch):
    import decision_browser.owned_fixture_server as fixture

    monkeypatch.setattr(fixture, "_CLEANUP_CAP", .02)
    resource = OwnedFixtureResource()
    release, began = asyncio.Event(), asyncio.Event()

    async def resistant():
        began.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    task = asyncio.create_task(resistant())
    resource._track(task)
    await began.wait()
    try:
        report = await resource.close()
        assert report.unknown and resource.owned_task_count == 1
    finally:
        release.set()
        await task
    report = await resource.close()
    assert report.clean and report.unfinished_tasks == 0


async def test_db058_completed_connection_exception_is_consumed_and_reported(monkeypatch):
    resource = OwnedFixtureResource()

    async def fail(*args):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(resource, "_handle", fail)
    resource._client(object(), object())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert resource.owned_task_count == 0
    assert (await resource.close()).unknown


async def test_db164_server_close_does_not_reclose_socket_and_abort_is_bounded():
    resource = OwnedFixtureResource()
    calls = []

    class Socket:
        def close(self):
            raise AssertionError("server owns socket closure")

        def fileno(self):
            return -1

    class Server:
        sockets = (Socket(),)

        def close(self):
            calls.append("server.close")

    class Writer:
        transport = SimpleNamespace(abort=lambda: calls.append("abort"))

        async def wait_closed(self):
            raise AssertionError("unbounded graceful close")

    resource._server = Server()
    resource._writers.add(Writer())
    report = await resource.close()
    assert report.clean and calls == ["server.close", "abort"]


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf")])
async def test_db053_invalid_or_expired_navigation_timeout_never_disables_timeout(seconds):
    calls = []

    class Page:
        context = SimpleNamespace(pages=[])

        async def goto(self, *args, **kwargs):
            calls.append(kwargs)

    dom = DOMSession(Page(), navigation_remaining=lambda: seconds)
    with pytest.raises(TimeoutError):
        await dom._native(Action("navigate", None, None, "https://owned.example", "obs", "{}"))
    assert calls == []


async def test_db111_fixture_deadline_uses_same_fake_clock():
    resource = OwnedFixtureResource(clock=lambda: 0.0)
    resource._validate_deadline(5.0)
    with pytest.raises(ValueError, match="deadline"):
        resource._validate_deadline(-1)


async def test_db071_runner_keeps_exact_resistant_close_and_starts_after_execution_expiry():
    from decision_browser.runner import RunManager

    manager = RunManager(lambda: None, clock=lambda: 0, cleanup_grace=.02)
    run = SimpleNamespace(shutdown_deadline=None, owned_resource_close_job=None, closing=set(), budget=SimpleNamespace(original_deadline=-1))
    release, began = asyncio.Event(), asyncio.Event()
    calls = []

    class Resource:
        async def close(self):
            calls.append(True)
            began.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return SimpleNamespace(unknown=False)

    resource = Resource()
    try:
        async with asyncio.timeout(.2):
            assert await manager._owned_close_for_deadline(run, resource) is None
        assert began.is_set() and calls == [True]
        job = run.owned_resource_close_job
        assert job in run.closing and not job.done()
        # The same exact close operation is used; it is not started twice.
        assert await manager._owned_close_for_deadline(run, resource) is None
        assert calls == [True]
    finally:
        release.set()
        await run.owned_resource_close_job
    report = await manager._owned_close_for_deadline(run, resource)
    assert report.unknown is False and not run.closing


async def test_db071_committed_residual_handoff_preserves_reference_identity(monkeypatch):
    from test_committed_navigation_capture import _HELPER, _identity, _World

    monkeypatch.setattr(_HELPER, "_CLEANUP_SECONDS", .02)
    world = _World(io_gate=asyncio.Event())
    release, cleanup_started = asyncio.Event(), asyncio.Event()
    original_send = world.session.send
    before = _identity(world.driver)

    async def send(method, params):
        if method == "IO.close":
            cleanup_started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
        return await original_send(method, params)

    world.session.send = send
    task = asyncio.create_task(world.run(time.monotonic() + 2))
    residuals = ()
    try:
        await asyncio.wait_for(world.io_started.wait(), .5)
        task.cancel()
        task.cancel()
        async with asyncio.timeout(.2):
            with pytest.raises(BrowserBlocked) as caught:
                await task
        assert cleanup_started.is_set() and task.cancelling() == 2
        assert caught.value.code == "cleanup_uncertain"
        residuals = caught.value.residual_tasks
        assert len([child for child in residuals if not child.done()]) == 1
        assert task._decision_browser_residual_tasks == residuals
        assert _identity(world.driver) == before
        assert str(caught.value) == "cleanup_uncertain: Browser cleanup could not be confirmed."
    finally:
        release.set()
        await asyncio.gather(*residuals, return_exceptions=True)


async def test_db111_runner_browser_fixture_and_existing_dom_share_fake_clock():
    from test_owned_initial_document import _bind

    from decision_browser.budget import Budget

    manager, run, driver, page, session, dom = await _bind(seconds=5)
    manager._clock = lambda: 0.0
    run.budget = Budget(run.request.limits, clock=manager._clock)

    async def wait_for_fulfill():
        await session.fulfilled.wait()

    page._until_fulfill_or_waiter = wait_for_fulfill
    try:
        await manager.bind_owned_initial_document(run)
        assert run.owned_initial_seal is not None
        assert run.owned_resource.original_deadline == 5.0
        assert driver._clock() == 0.0
        assert dom._navigation_remaining() == 5.0
        assert run.budget.usage().steps == 1
    finally:
        await driver.disarm_owned_initial_document()
        if run.owned_resource is not None:
            assert (await run.owned_resource.close()).clean


async def test_db070_new_cancellation_during_reader_cleanup_keeps_original_depth():
    io_started, detach_started = asyncio.Event(), asyncio.Event()

    class Session(_Session):
        async def send(self, method, params=None):
            if method == "IO.read":
                io_started.set()
                await asyncio.Event().wait()
            return await super().send(method, params)

        async def detach(self):
            detach_started.set()
            await asyncio.Event().wait()

    session = Session([])
    driver, _, _ = _open(session)
    task = asyncio.create_task(driver.read_owned_document_body("https://owned.example/doc", time.monotonic() + 2))
    await io_started.wait()
    task.cancel()
    await detach_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelling() == 2
    assert not driver._owned_tasks
    assert [method for method, _ in session.calls if method in {"IO.close", "Fetch.disable"}] == ["IO.close", "Fetch.disable"]


async def test_db070_finish_attempts_detach_after_cancellation_of_release(monkeypatch):
    driver = BrowserDriver("isolated")
    arm = _arm(driver)
    arm.observation = _observation()
    arm.waiter = asyncio.create_task(asyncio.sleep(0))
    await arm.waiter
    began = asyncio.Event()
    detached = []

    async def release(_arm):
        began.set()
        await asyncio.Event().wait()

    class Session:
        async def detach(self):
            detached.append(True)

    arm.session = Session()
    monkeypatch.setattr(driver, "_owned_release", release)
    task = asyncio.create_task(driver.finish_owned_initial_document(arm, arm.budget))
    await began.wait()
    task.cancel()
    result = await task
    assert task.cancelling() == 1
    assert not result.release_ok and not result.body
    assert detached == [True] and arm.session is None


async def test_review_driver_close_joins_same_arm_with_one_cleanup_deadline(monkeypatch):
    from decision_browser import browser

    monkeypatch.setattr(browser, "_OWNED_CLEANUP_SECONDS", .04)
    driver = BrowserDriver("isolated")
    driver._state = "open"
    arm = _arm(driver, deadline=time.monotonic() - 1)
    arm.gate.update(handle="stream", stream_closed=False)
    release = asyncio.Event()
    began = asyncio.Event()
    deadlines = []
    depths = []
    cancel_tasks = driver._cancel_owned_tasks

    async def tracked_join(deadline=None):
        deadlines.append(deadline)
        await cancel_tasks(deadline)

    monkeypatch.setattr(driver, "_cancel_owned_tasks", tracked_join)

    class Session:
        async def send(self, method, params):
            if method == "IO.close" and not release.is_set():
                began.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    depths.append(asyncio.current_task().cancelling())
                    await release.wait()
            return {}

        async def detach(self):
            return None

    arm.session = Session()
    opened = time.monotonic()
    try:
        await driver.close()
        elapsed = time.monotonic() - opened
        assert began.is_set() and driver.cleanup_pending
        assert driver._owned_arm is arm and len(driver._owned_tasks) == 1
        assert len(deadlines) == 2 and deadlines[0] is not None
        assert deadlines[1] == deadlines[0] == arm.cleanup_deadline
        assert depths == [1]  # the second join must not add cancellation depth
        assert elapsed < .3  # broad watchdog only; deadline identity proves one window
        prior = arm.cleanup_deadline
    finally:
        release.set()
        await asyncio.gather(*driver._owned_tasks, return_exceptions=True)
    await driver.close()
    assert arm.cleanup_deadline > prior  # explicit retry after quiescence gets a fresh window
    assert arm.session is None and driver._owned_arm is None
    assert driver._state == "closed" and not driver.cleanup_pending
