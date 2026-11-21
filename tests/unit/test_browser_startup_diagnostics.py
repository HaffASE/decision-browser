"""Startup diagnostics for no attempt versus lost target-creation acknowledgement."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from decision_browser.browser import BrowserBlocked, BrowserDriver, BrowserMode

FOREIGN = {"targetId": "FOREIGN-1", "type": "page", "url": "https://example.test/keep"}
CREATED = {"targetId": "CREATED-UNKNOWN", "type": "page", "url": "about:blank"}


class _Session:
    def __init__(self, browser: _Browser) -> None:
        self._browser = browser

    def on(self, event, callback):
        pass

    async def send(self, method: str, params: object | None = None) -> dict:
        if method == "Target.setDiscoverTargets":
            return {}
        if method == "Target.createTarget":
            await self._browser.contexts[0].new_page()
            return {"targetId": CREATED["targetId"]}
        assert method == "Target.getTargets"
        return {"targetInfos": [dict(item) for item in self._browser.targets]}

    async def detach(self) -> None:
        return None


class _Page:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _Context:
    def __init__(self, browser: _Browser, behavior: str) -> None:
        self.browser = browser
        self.behavior = behavior
        self.close_calls = 0
        self.new_page_calls = 0
        self.pages: list[_Page] = []

    async def new_page(self) -> _Page:
        self.new_page_calls += 1
        page = _Page()
        self.pages.append(page)
        self.browser.targets.append(dict(CREATED))
        if self.behavior == "raise":
            raise RuntimeError("new_page failed after create")
        if self.behavior == "cancel":
            raise asyncio.CancelledError()
        return page

    async def close(self) -> None:
        self.close_calls += 1


class _Browser:
    def __init__(self) -> None:
        self.contexts: list[_Context] = []
        self.targets = [dict(FOREIGN)]
        self.close_calls = 0

    async def new_browser_cdp_session(self) -> _Session:
        return _Session(self)

    async def close(self) -> None:
        self.close_calls += 1


class _Playwright:
    def __init__(self, browser: _Browser) -> None:
        self.browser = browser
        self.chromium = self

    async def connect_over_cdp(
        self, endpoint_url: str, timeout: object = None, no_defaults: object = None
    ) -> _Browser:
        assert endpoint_url == "ws://127.0.0.1/devtools/browser/test"
        return self.browser

    async def stop(self) -> None:
        return None


def _install(monkeypatch: pytest.MonkeyPatch, browser: _Browser) -> None:
    playwright = _Playwright(browser)

    def async_playwright() -> SimpleNamespace:
        async def start() -> _Playwright:
            return playwright

        return SimpleNamespace(start=start)

    monkeypatch.setattr("decision_browser.browser.async_playwright", async_playwright)
    monkeypatch.setattr(
        "decision_browser.browser.read_devtools_active_port",
        lambda path: SimpleNamespace(websocket_url="ws://127.0.0.1/devtools/browser/test"),
    )


def _driver(tmp_path: Path) -> BrowserDriver:
    port = tmp_path / "DevToolsActivePort"
    port.write_text("1\n")
    return BrowserDriver(BrowserMode.existing_chrome, devtools_active_port=port)


def _assert_nothing_closed(browser: _Browser) -> None:
    assert browser.close_calls == 0
    assert FOREIGN in browser.targets
    for context in browser.contexts:
        assert context.close_calls == 0
        for page in context.pages:
            assert page.close_calls == 0


async def test_context_count_before_page_attempt_leaves_diagnostics_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = _Browser()
    browser.contexts = [_Context(browser, "raise"), _Context(browser, "raise")]
    _install(monkeypatch, browser)
    driver = _driver(tmp_path)
    with pytest.raises(BrowserBlocked) as caught:
        await driver.start()
    assert caught.value.code == "context_count"
    assert driver.cleanup_errors == []
    assert driver.owned_target_id is None
    assert driver.residual_owned is None
    assert driver.foreign_mismatch is None
    assert all(context.new_page_calls == 0 for context in browser.contexts)
    _assert_nothing_closed(browser)


async def test_create_target_ack_lost_after_create_keeps_unknown_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = _Browser()
    context = _Context(browser, "raise")
    browser.contexts = [context]
    _install(monkeypatch, browser)
    driver = _driver(tmp_path)
    with pytest.raises(BrowserBlocked) as caught:
        await driver.start()
    assert caught.value.code == "owned_page_failed"
    assert driver.cleanup_errors == []
    assert driver.owned_target_id is None
    assert driver.residual_owned is True
    assert driver.foreign_mismatch is True
    assert context.new_page_calls == 1
    assert CREATED in browser.targets
    _assert_nothing_closed(browser)


async def test_create_target_cancel_before_ack_keeps_unknown_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = _Browser()
    context = _Context(browser, "cancel")
    browser.contexts = [context]
    _install(monkeypatch, browser)
    driver = _driver(tmp_path)
    with pytest.raises(asyncio.CancelledError):
        await driver.start()
    assert driver.cleanup_errors == []
    assert driver.owned_target_id is None
    assert driver.residual_owned is True
    assert driver.foreign_mismatch is True
    assert context.new_page_calls == 1
    assert CREATED in browser.targets
    _assert_nothing_closed(browser)

class _InventoryFailBrowser(_Browser):
    def __init__(self, succeed: int) -> None:
        super().__init__()
        self._succeed = succeed
        self.inventory_calls = 0

    async def new_browser_cdp_session(self) -> _Session:
        browser = self

        class InventorySession(_Session):
            async def send(self, method, params=None):
                if method == "Target.getTargets":
                    browser.inventory_calls += 1
                    if browser.inventory_calls > browser._succeed:
                        raise RuntimeError("Target.getTargets failed")
                return await super().send(method, params)

        return InventorySession(self)


def _install_connect_failure(monkeypatch: pytest.MonkeyPatch) -> _Browser:
    browser = _Browser()

    class _Connecting(_Playwright):
        async def connect_over_cdp(
            self, endpoint_url: str, timeout: object = None, no_defaults: object = None
        ) -> _Browser:
            raise RuntimeError("cdp connect failed")

    playwright = _Connecting(browser)

    def async_playwright() -> SimpleNamespace:
        async def start() -> _Playwright:
            return playwright

        return SimpleNamespace(start=start)

    monkeypatch.setattr("decision_browser.browser.async_playwright", async_playwright)
    monkeypatch.setattr(
        "decision_browser.browser.read_devtools_active_port",
        lambda path: SimpleNamespace(websocket_url="ws://127.0.0.1/devtools/browser/test"),
    )
    return browser


async def test_connect_failure_before_page_attempt_leaves_diagnostics_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = _install_connect_failure(monkeypatch)
    driver = _driver(tmp_path)
    with pytest.raises(BrowserBlocked) as caught:
        await driver.start()
    assert caught.value.code == "cdp_connect_failed"
    assert driver.cleanup_errors == []
    assert driver.owned_target_id is None
    assert driver.residual_owned is None
    assert driver.foreign_mismatch is None
    _assert_nothing_closed(browser)


async def test_context_count_inventory_after_failure_leaves_diagnostics_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = _InventoryFailBrowser(succeed=0)
    browser.contexts = [_Context(browser, "raise"), _Context(browser, "raise")]
    _install(monkeypatch, browser)
    driver = _driver(tmp_path)
    with pytest.raises(BrowserBlocked) as caught:
        await driver.start()
    assert caught.value.code == "context_count"
    assert driver.cleanup_errors == ["inventory_after"]
    assert driver.owned_target_id is None
    assert driver.residual_owned is None
    assert driver.foreign_mismatch is None
    assert all(context.new_page_calls == 0 for context in browser.contexts)
    assert browser.inventory_calls == 1
    _assert_nothing_closed(browser)


async def test_create_target_ack_lost_inventory_after_failure_keeps_uncertain_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = _InventoryFailBrowser(succeed=1)
    context = _Context(browser, "raise")
    browser.contexts = [context]
    _install(monkeypatch, browser)
    driver = _driver(tmp_path)
    with pytest.raises(BrowserBlocked) as caught:
        await driver.start()
    assert caught.value.code == "owned_page_failed"
    assert driver.cleanup_errors == ["inventory_after"]
    assert driver.owned_target_id is None
    assert driver.residual_owned is True
    assert driver.foreign_mismatch is None
    assert context.new_page_calls == 1
    assert CREATED in browser.targets
    assert browser.inventory_calls == 2
    _assert_nothing_closed(browser)


async def test_create_target_cancel_inventory_after_failure_keeps_uncertain_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = _InventoryFailBrowser(succeed=1)
    context = _Context(browser, "cancel")
    browser.contexts = [context]
    _install(monkeypatch, browser)
    driver = _driver(tmp_path)
    with pytest.raises(asyncio.CancelledError):
        await driver.start()
    assert driver.cleanup_errors == ["inventory_after"]
    assert driver.owned_target_id is None
    assert driver.residual_owned is True
    assert driver.foreign_mismatch is None
    assert context.new_page_calls == 1
    assert CREATED in browser.targets
    assert browser.inventory_calls == 2
    _assert_nothing_closed(browser)
