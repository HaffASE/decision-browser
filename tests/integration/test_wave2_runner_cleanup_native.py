"""F06 public RunManager regressions using owned Chromium and real task-scope policy."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from playwright.async_api import async_playwright

from decision_browser import runner
from decision_browser.browser import BrowserDriver
from decision_browser.contracts import Check, Limits, PrivacyPolicy, TaskRequest
from decision_browser.decision_codec import Decision
from decision_browser.provider import OpenAIProvider
from decision_browser.runner import RunManager, RunnerError

_URL = "https://example.test/"


class Provider:
    async def decide(self, *args, **kwargs):
        return Decision("DONE", None, 1.0)

    async def aclose(self):
        pass


class OwnedDriver(BrowserDriver):
    def __init__(self, mode, *, hold_close=False):
        super().__init__(mode)
        self.close_entered = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_job = None
        self.closes = 0
        self.hold_close = hold_close

    async def start(self):
        await super().start()
        await self.page.context.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html", body='<p id="ready">Owned cleanup fixture</p>',
        ))

    async def close(self):
        self.close_job = asyncio.current_task()
        self.closes += 1
        self.close_entered.set()
        try:
            if self.hold_close:
                await self.close_release.wait()
        finally:
            await super().close()


def request(*, visual):
    return TaskRequest(
        goal="Review the owned page", start_url=_URL, values={},
        checks=[Check(kind="route", target="", expected=_URL)],
        privacy=PrivacyPolicy(), limits=Limits(max_seconds=5.0), visual=visual,
    )


async def settle(host):
    task = host._run.task
    assert task is not None
    async with asyncio.timeout(10):
        await asyncio.gather(task, return_exceptions=True)


async def release_driver(driver):
    driver.close_release.set()
    if driver.close_job is not None:
        async with asyncio.timeout(10):
            await asyncio.gather(driver.close_job, return_exceptions=True)
    if driver._state not in {"closed", "idle"}:
        await driver.close()


@pytest.mark.parametrize("cancel", [True, False])
async def test_native_public_visual_cancel_preserves_functional_evidence(monkeypatch, cancel):
    entered = asyncio.Event()

    async def geometry(page, viewport):
        entered.set()
        if cancel:
            await asyncio.Future()
        return SimpleNamespace(model_dump=lambda: {"source": "owned-test-geometry"})

    monkeypatch.setattr(runner, "inspect_geometry", geometry)
    drivers = []
    host = RunManager(
        Provider, lambda mode: drivers.append(OwnedDriver(mode)) or drivers[-1],
        # This control requires completed native cleanup; bounded partial-close
        # behavior is covered separately with deterministic small-cap cases.
        admission_mode="task_scope", cleanup_grace=2.0,
    )
    run_id = host.start_task(request(visual=True))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        if cancel:
            assert host.get_task(run_id).terminal == "running"
            host.cancel_task(run_id)
        await settle(host)
        result = host.get_task(run_id)
        assert result.terminal == ("cancelled" if cancel else "verified")
        assert result.functional == "verified"
        assert result.usage.steps == 1 and result.usage.calls == 0
        assert drivers[0].closes == 1
        assert result.artifact_ids
        assert all(host.read_artifact(run_id, artifact_id).id == artifact_id for artifact_id in result.artifact_ids)
    finally:
        host.cancel_task(run_id)
        await settle(host)
        await release_driver(drivers[0])


async def test_native_direct_task_cancel_during_close_never_exposes_verified():
    drivers = []
    host = RunManager(
        Provider, lambda mode: drivers.append(OwnedDriver(mode, hold_close=True)) or drivers[-1],
        admission_mode="task_scope", cleanup_grace=0.1,
    )
    run_id = host.start_task(request(visual=False))
    try:
        driver = drivers[0]
        await asyncio.wait_for(driver.close_entered.wait(), timeout=10)
        assert host.get_task(run_id).terminal == "running"
        host._run.task.cancel()
        await settle(host)
        result = host.get_task(run_id)
        assert result.terminal == "outcome_unknown"
        assert result.reason == "cleanup_unknown"
        assert result.functional == "verified"
        assert result.usage.steps == 1 and result.usage.calls == 0
    finally:
        drivers[0].close_release.set()
        await settle(host)
        await release_driver(drivers[0])


@pytest.mark.parametrize("expiry_path", ["get", "start"])
async def test_native_provider_residual_gets_deferred_cleanup_before_expiry(expiry_path):
    offset = {"seconds": 0.0}
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

    provider = OpenAIProvider("synthetic-key", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=Stream()),
    ))
    drivers = []
    host = RunManager(
        lambda: provider, lambda mode: drivers.append(OwnedDriver(mode)) or drivers[-1],
        clock=lambda: time.monotonic() + offset["seconds"],
        # This is the native success/retention control. Small cleanup-cap
        # failures are deterministic unit cases; real browser shutdown has
        # several protocol phases and uses the normal production grace here.
        admission_mode="task_scope", cleanup_grace=2.0,
    )
    req = TaskRequest(
        goal="Review the owned page", start_url=_URL, values={}, checks=[],
        privacy=PrivacyPolicy(), limits=Limits(max_seconds=5.0, max_output_bytes=10),
    )
    run_id = host.start_task(req)
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        await settle(host)
        run = host._run
        driver = drivers[0]
        assert host.get_task(run_id).terminal == "outcome_unknown"
        assert driver.closes == 0 and driver._state == "open"
        assert not run.cleanup_attempted and host._owned_pending(run)
        deadline = run.budget.original_deadline
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=2)
        await asyncio.wait_for(driver.close_entered.wait(), timeout=2)
        async with asyncio.timeout(3):
            while host._owned_pending(run):
                await asyncio.sleep(0)
        assert driver.closes == 1 and driver._state == "closed"
        assert provider._client.is_closed
        assert run.cleanup_attempted and run.budget.original_deadline == deadline
        assert host.get_task(run_id).terminal == "outcome_unknown"
        assert run.budget.usage().steps == run.budget.usage().calls == 1
        offset["seconds"] = 61.0
        if expiry_path == "get":
            with pytest.raises(RunnerError, match="expired"):
                host.get_task(run_id)
            assert host._run is None
        else:
            new_id = host.start_task(req)
            assert new_id != run_id
            host.cancel_task(new_id)
            await settle(host)
        await host.aclose()
        assert driver.closes == 1
    finally:
        release.set()
        await host.aclose()
        for driver in drivers:
            if driver._state not in {"closed", "idle"}:
                await release_driver(driver)
        await provider.aclose()


@pytest.mark.parametrize("stage", ["page", "stop"])
async def test_native_cleanup_cancel_retries_same_owned_resources_only(tmp_path, monkeypatch, stage):
    async with async_playwright() as owner:
        profile = tmp_path / "owned-profile"
        context = await owner.chromium.launch_persistent_context(
            str(profile), headless=True, chromium_sandbox=True, channel="chromium",
            args=["--remote-debugging-port=0"],
        )
        driver = BrowserDriver("existing_chrome", devtools_active_port=profile / "DevToolsActivePort")
        close_task = None
        try:
            foreign = context.pages[0]
            await foreign.set_content('<p id="kept">Foreign owned fixture</p>')
            await driver.start()
            page = driver.page
            attached_browser, attached_context, transport = driver._browser, driver._context, driver._playwright
            original_close = page.close
            original_stop = transport.stop
            page_closes = 0
            successful_page_closes = 0
            stop_calls = 0
            entered = asyncio.Event()

            async def close_once():
                nonlocal page_closes, successful_page_closes
                page_closes += 1
                if stage == "page" and page_closes == 1:
                    entered.set()
                    await asyncio.Future()
                assert not page.is_closed()
                await original_close()
                successful_page_closes += 1

            async def stop_once_cancelled():
                nonlocal stop_calls
                stop_calls += 1
                if stage == "stop" and stop_calls == 1:
                    entered.set()
                    await asyncio.Future()
                await original_stop()

            monkeypatch.setattr(page, "close", close_once)
            monkeypatch.setattr(transport, "stop", stop_once_cancelled)
            no_browser_close = AsyncMock(side_effect=AssertionError("attached browser must stay open"))
            no_context_close = AsyncMock(side_effect=AssertionError("foreign context must stay open"))
            monkeypatch.setattr(attached_browser, "close", no_browser_close)
            monkeypatch.setattr(attached_context, "close", no_context_close)
            close_task = asyncio.create_task(driver.close())
            await asyncio.wait_for(entered.wait(), timeout=5)
            assert page.is_closed() is (stage == "stop") and page_closes == 1
            close_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await close_task
            assert driver.cleanup_pending and driver._playwright is transport
            assert driver._page is page and driver._browser is attached_browser
            if stage == "page":
                assert stop_calls == 0 and attached_browser.is_connected()
                assert not foreign.is_closed()
                assert await foreign.locator("#kept").inner_text() == "Foreign owned fixture"

            async def no_repeat(*args, **kwargs):
                raise AssertionError("stop-only retry must not probe closed page, inventory, or reconnect")

            if stage == "stop":
                monkeypatch.setattr(driver, "_ownership_matches", no_repeat)
                monkeypatch.setattr(driver, "_page_target_id", no_repeat)
                monkeypatch.setattr(driver, "_cdp_inventory", no_repeat)
            monkeypatch.setattr(transport.chromium, "connect_over_cdp", no_repeat)
            monkeypatch.setattr(attached_context, "new_page", no_repeat)
            await driver.close()
            assert stop_calls == (2 if stage == "stop" else 1)
            assert page_closes == (1 if stage == "stop" else 2)
            assert successful_page_closes == 1
            assert not driver.cleanup_pending and driver._state == "closed"
            assert driver._playwright is driver._page is driver._context is driver._browser is None
            assert no_browser_close.await_count == no_context_close.await_count == 0
            assert not foreign.is_closed()
            assert await foreign.locator("#kept").inner_text() == "Foreign owned fixture"
            assert driver.foreign_mismatch is False and driver.residual_owned is False
        finally:
            if close_task is not None and not close_task.done():
                close_task.cancel()
                await asyncio.gather(close_task, return_exceptions=True)
            await driver.close()
            await context.close()
