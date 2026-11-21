import asyncio
from pathlib import Path

import pytest

from decision_browser.browser import BrowserBlocked, BrowserDriver, BrowserMode


class _Session:
    def __init__(self, infos: list[dict[str, str]]) -> None:
        self._infos = infos
        self.detached = False
        self.browser = None

    def on(self, event, callback):
        pass

    async def send(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        if method == "Target.setDiscoverTargets":
            return {}
        if method == "Target.createTarget":
            await self.browser.contexts[0].new_page()
            return {"targetId": "owned-1"}
        if method == "Target.closeTarget":
            await self.browser.contexts[0].page.close()
            return {"success": True}
        if method == "Target.getTargets":
            return {"targetInfos": list(self._infos)}
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": "owned-1"}}
        raise AssertionError(method)

    async def detach(self) -> None:
        self.detached = True


class _Page:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _Context:
    def __init__(self, browser: "_Browser") -> None:
        self.browser = browser
        self.page: _Page | None = None
        self.sessions: list[_Session] = []
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1

    @property
    def pages(self):
        return [] if self.page is None else [self.page]

    async def new_page(self) -> _Page:
        self.page = _Page()
        return self.page

    async def new_cdp_session(self, page: _Page) -> _Session:
        session = _Session([])
        self.sessions.append(session)
        return session


class _Browser:
    def __init__(self, *, extra_foreign: bool = False) -> None:
        self.contexts = [_Context(self)]
        self.close_calls = 0
        self.sessions: list[_Session] = []
        self.extra_foreign = extra_foreign

    async def new_context(self) -> _Context:
        return self.contexts[0]

    async def close(self) -> None:
        self.close_calls += 1

    async def new_browser_cdp_session(self) -> _Session:
        context = self.contexts[0]
        infos = [{"targetId": "foreign", "type": "page", "url": "http://127.0.0.1/kept"}]
        if context.page is not None and not context.page.closed:
            infos.append({"targetId": "owned-1", "type": "page", "url": "about:blank"})
        if self.extra_foreign and context.page is not None:
            infos.append(
                {"targetId": "extra-foreign", "type": "page", "url": "http://127.0.0.1/extra"}
            )
        infos.append({"targetId": "worker", "type": "service_worker", "url": "http://127.0.0.1/sw.js"})
        session = _Session(infos)
        session.browser = self
        self.sessions.append(session)
        return session


class _Chromium:
    def __init__(self, browser: _Browser) -> None:
        self._browser = browser

    async def connect_over_cdp(
        self,
        endpoint_url: str,
        timeout: int | None = None,
        no_defaults: bool = False,
    ) -> _Browser:
        return self._browser


class _Playwright:
    def __init__(self, chromium: object) -> None:
        self.chromium = chromium
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


def _install_playwright(monkeypatch: pytest.MonkeyPatch, chromium: object) -> dict[str, _Playwright]:
    box: dict[str, _Playwright] = {}

    class _Manager:
        async def start(self) -> _Playwright:
            box["playwright"] = _Playwright(chromium)
            return box["playwright"]

    monkeypatch.setattr("decision_browser.browser.async_playwright", lambda: _Manager())
    return box


def _devtools_port(tmp_path: Path) -> Path:
    path = tmp_path / "DevToolsActivePort"
    path.write_text("9222\n/devtools/browser/owned-cleanup\n", encoding="utf-8")
    return path


def _ids(snapshot: object) -> set[str]:
    return {item.target_id for item in snapshot.targets}


def _assert_sessions_detached(browser: _Browser) -> None:
    sessions = [*browser.sessions, *browser.contexts[0].sessions]
    assert sessions
    assert all(session.detached for session in sessions)


async def test_start_extra_foreign_retains_owned_and_closes_only_owned_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    browser = _Browser(extra_foreign=True)
    box = _install_playwright(monkeypatch, _Chromium(browser))
    driver = BrowserDriver(BrowserMode.existing_chrome, devtools_active_port=_devtools_port(tmp_path))

    with pytest.raises(BrowserBlocked) as caught:
        await driver.start()

    page = browser.contexts[0].page
    assert caught.value.code == "ownership_unverified"
    assert page is not None and page.closed is True
    assert browser.close_calls == 0
    assert browser.contexts[0].close_calls == 0
    assert driver.owned_target_id == "owned-1"
    assert driver.residual_owned is False
    assert driver.foreign_mismatch is True
    assert "foreign" in _ids(driver.foreign_before)
    assert "extra-foreign" not in _ids(driver.foreign_before)
    assert "owned-1" not in _ids(driver.foreign_before)
    assert _ids(driver.foreign_after) >= {"foreign", "extra-foreign"}
    assert "owned-1" not in _ids(driver.foreign_after)
    assert box["playwright"].stopped is True
    assert driver._browser is None
    assert driver._context is None
    assert driver._page is None
    assert driver._state == "closed"
    _assert_sessions_detached(browser)


async def test_close_cancelled_then_retry_closes_owned_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    browser = _Browser()
    box = _install_playwright(monkeypatch, _Chromium(browser))
    driver = BrowserDriver(BrowserMode.existing_chrome, devtools_active_port=_devtools_port(tmp_path))
    await driver.start()

    page = browser.contexts[0].page
    assert page is not None
    assert driver.owned_target_id == "owned-1"
    started = asyncio.Event()
    release = asyncio.Event()
    close_attempts = 0
    successful_closes = 0

    async def waiting_close() -> None:
        nonlocal close_attempts
        close_attempts += 1
        started.set()
        await release.wait()
        page.closed = True

    page.close = waiting_close

    task = asyncio.create_task(driver.close())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert page.closed is False
    assert driver._state == "cleanup_pending"
    assert driver.cleanup_pending is True
    assert driver._page is page
    assert driver._context is browser.contexts[0]
    assert driver._browser is browser
    assert driver.owned_target_id == "owned-1"
    assert box["playwright"].stopped is False
    assert browser.close_calls == 0
    assert browser.contexts[0].close_calls == 0
    assert close_attempts == 1 and successful_closes == 0
    with pytest.raises(BrowserBlocked, match="not_started"):
        await driver.screenshot()

    async def finish_close() -> None:
        nonlocal close_attempts, successful_closes
        close_attempts += 1
        successful_closes += 1
        page.closed = True

    page.close = finish_close
    await driver.close()

    assert page.closed is True
    assert browser.close_calls == 0
    assert browser.contexts[0].close_calls == 0
    assert box["playwright"].stopped is True
    assert driver.owned_target_id == "owned-1"
    assert driver.residual_owned is False
    assert driver._page is None
    assert driver._context is None
    assert driver._browser is None
    assert driver._state == "closed"
    assert driver.cleanup_pending is False
    await driver.close()
    assert close_attempts == 2 and successful_closes == 1
    _assert_sessions_detached(browser)


def _foreign_pairs(snapshot: object) -> tuple[tuple[str, str], ...]:
    return tuple((item.target_id, item.url) for item in snapshot.targets)


async def test_close_cancelled_during_stop_after_page_closed_retries_disconnect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    browser = _Browser()
    chromium = _Chromium(browser)
    box = _install_playwright(monkeypatch, chromium)
    driver = BrowserDriver(BrowserMode.existing_chrome, devtools_active_port=_devtools_port(tmp_path))
    await driver.start()

    page = browser.contexts[0].page
    playwright = box["playwright"]
    assert page is not None and page.closed is False
    assert driver.owned_target_id == "owned-1"
    assert driver._playwright is playwright
    pairs = _foreign_pairs(driver.foreign_before)
    assert ("foreign", "http://127.0.0.1/kept") in pairs

    connect_calls = 0

    async def connect_over_cdp(
        endpoint_url: str,
        timeout: int | None = None,
        no_defaults: bool = False,
    ) -> _Browser:
        nonlocal connect_calls
        connect_calls += 1
        return browser

    chromium.connect_over_cdp = connect_over_cdp

    entered = asyncio.Event()
    stop_calls = 0

    async def stop_first_hangs() -> None:
        nonlocal stop_calls
        stop_calls += 1
        if stop_calls == 1:
            entered.set()
            await asyncio.Event().wait()
        playwright.stopped = True

    playwright.stop = stop_first_hangs
    page_close_calls = 0
    original_close = page.close

    async def close_once() -> None:
        nonlocal page_close_calls
        page_close_calls += 1
        assert not page.closed, "completed owned-page closure must not be replayed"
        await original_close()

    page.close = close_once

    task = asyncio.create_task(driver.close())
    await entered.wait()
    assert page.closed is True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stop_calls == 1
    assert playwright.stopped is False
    assert driver._state == "cleanup_pending"
    assert driver.cleanup_pending is True
    assert driver._page is page
    assert driver._context is browser.contexts[0]
    assert driver._browser is browser
    assert driver._playwright is playwright
    assert driver.owned_target_id == "owned-1"
    assert browser.close_calls == 0
    assert browser.contexts[0].close_calls == 0
    assert connect_calls == 0
    assert driver.cleanup_errors == ["cleanup_cancelled"]
    assert _foreign_pairs(driver.foreign_before) == pairs
    assert _foreign_pairs(driver.foreign_after) == pairs
    assert driver.foreign_mismatch is False
    assert any(
        info["targetId"] == "foreign" and info["url"] == "http://127.0.0.1/kept"
        for session in browser.sessions
        for info in session._infos
    )

    async def reject_closed_page_probe(*args, **kwargs):
        raise AssertionError("closed page/finished inventory must not be probed on stop-only retry")

    driver._ownership_matches = reject_closed_page_probe
    driver._page_target_id = reject_closed_page_probe
    driver._cdp_inventory = reject_closed_page_probe
    await driver.close()

    assert stop_calls == 2
    assert playwright.stopped is True
    assert connect_calls == 0
    assert browser.close_calls == 0
    assert browser.contexts[0].close_calls == 0
    assert driver.owned_target_id == "owned-1"
    assert driver.residual_owned is False
    assert driver.foreign_mismatch is False
    assert _foreign_pairs(driver.foreign_before) == pairs
    assert _foreign_pairs(driver.foreign_after) == pairs
    assert driver._page is None
    assert driver._context is None
    assert driver._browser is None
    assert driver._playwright is None
    assert driver._state == "closed"
    assert driver.cleanup_pending is False
    assert page_close_calls == 1
    assert driver.cleanup_errors == ["cleanup_cancelled"]
    _assert_sessions_detached(browser)


async def test_start_failure_cancelled_owned_close_retains_exact_cleanup_for_retry(monkeypatch, tmp_path):
    class Page(_Page):
        def __init__(self):
            super().__init__()
            self.closes = 0

        async def close(self):
            self.closes += 1
            if self.closes == 1:
                raise asyncio.CancelledError
            self.closed = True

    class Context(_Context):
        async def new_page(self):
            self.page = Page()
            return self.page

    browser = _Browser(extra_foreign=True)
    browser.contexts = [Context(browser)]
    box = _install_playwright(monkeypatch, _Chromium(browser))
    driver = BrowserDriver("existing_chrome", devtools_active_port=_devtools_port(tmp_path))
    with pytest.raises(asyncio.CancelledError):
        await driver.start()
    page = browser.contexts[0].page
    assert driver.cleanup_pending and driver._state == "cleanup_pending"
    assert driver._page is page and not page.closed and page.closes == 1
    assert driver._browser is browser and driver._context is browser.contexts[0]
    assert not box["playwright"].stopped
    with pytest.raises(BrowserBlocked, match="already_closed"):
        await driver.start()
    await driver.close()
    assert page.closed and page.closes == 2
    assert not driver.cleanup_pending and driver._state == "closed"
    assert browser.close_calls == browser.contexts[0].close_calls == 0
    assert box["playwright"].stopped
    assert driver.residual_owned is False and driver.foreign_mismatch is True
