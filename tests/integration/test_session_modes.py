"""Isolated and mocked driver checks. Default runs do not attach to user Chrome."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from decision_browser.browser import (
    _INVENTORY_TARGET_CAP,
    MAX_CDP_TIMEOUT_MS,
    BrowserBlocked,
    BrowserDriver,
    BrowserMode,
    _snapshot_from_target_infos,
    parse_devtools_active_port,
)

_PATH = "/devtools/browser/Ab-12"


def test_driver_constructor_signature() -> None:
    signature = inspect.signature(BrowserDriver)
    assert list(signature.parameters) == ["mode", "devtools_active_port", "cdp_timeout_ms"]
    assert signature.parameters["devtools_active_port"].kind is inspect.Parameter.KEYWORD_ONLY


def test_parse_accepts_optional_single_trailing_newline() -> None:
    bare = f"9333\n{_PATH}".encode()
    terminated = bare + b"\n"
    bare_endpoint = parse_devtools_active_port(bare)
    terminated_endpoint = parse_devtools_active_port(terminated)
    assert bare_endpoint == terminated_endpoint
    assert bare_endpoint.websocket_url == f"ws://127.0.0.1:9333{_PATH}"
    assert bare_endpoint.browser_path == _PATH


@pytest.mark.parametrize(
    "blob",
    [
        b"",
        b"9333",
        b"9333\n",
        f"9333\n{_PATH}\n\n".encode(),
        f"9333\r\n{_PATH}\r\n".encode(),
        f"9333 \n{_PATH}\n".encode(),
        f"9333\n{_PATH} \n".encode(),
        f"\n{_PATH}\n".encode(),
        f"9333\n\n{_PATH}".encode(),
        b"65536\n/devtools/browser/Ab-12\n",
        "9333\n/devtools/browser/Ab-12\n".encode("utf-16"),
    ],
)
def test_parse_rejects_malformed_port_file(blob: bytes) -> None:
    with pytest.raises(BrowserBlocked) as caught:
        parse_devtools_active_port(blob)
    assert caught.value.code in {"devtools_port_malformed", "endpoint_rejected"}


@pytest.mark.parametrize(
    "timeout_ms",
    [0, -1, MAX_CDP_TIMEOUT_MS + 0.1, float("inf"), float("nan"), True, "10"],
)
def test_timeout_must_be_finite_and_inside_bound(timeout_ms: object) -> None:
    with pytest.raises(BrowserBlocked) as caught:
        BrowserDriver(BrowserMode.isolated, cdp_timeout_ms=timeout_ms)  # type: ignore[arg-type]
    assert caught.value.code == "invalid_timeout"


def test_cdp_inventory_keeps_pages_and_caps_count() -> None:
    snapshot = _snapshot_from_target_infos(
        {
            "targetInfos": [
                {"targetId": "page-b", "type": "page", "url": "http://127.0.0.1/b"},
                {"targetId": "worker", "type": "service_worker", "url": "http://127.0.0.1/sw.js"},
                {"targetId": "page-a", "type": "page", "url": "http://127.0.0.1/a"},
            ]
        }
    )
    assert [item.target_id for item in snapshot.targets] == ["page-a", "page-b"]
    oversized = {
        "targetInfos": [
            {"targetId": f"p{index}", "type": "page", "url": "http://127.0.0.1/"}
            for index in range(_INVENTORY_TARGET_CAP + 1)
        ]
    }
    with pytest.raises(BrowserBlocked) as caught:
        _snapshot_from_target_infos(oversized)
    assert caught.value.code == "inventory_failed"


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
    def __init__(self, browser: _Browser) -> None:
        self.browser = browser
        self.page: _Page | None = None
        self.sessions: list[_Session] = []

    async def close(self) -> None:
        return None

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
    def __init__(self) -> None:
        self.contexts = [_Context(self)]
        self.close_calls = 0
        self.sessions: list[_Session] = []

    async def new_context(self) -> _Context:
        return self.contexts[0]

    async def close(self) -> None:
        self.close_calls += 1

    async def new_browser_cdp_session(self) -> _Session:
        context = self.contexts[0]
        infos = [{"targetId": "foreign", "type": "page", "url": "http://127.0.0.1/kept"}]
        if context.page is not None and not context.page.closed:
            infos.append({"targetId": "owned-1", "type": "page", "url": "about:blank"})
        infos.append({"targetId": "worker", "type": "service_worker", "url": "http://127.0.0.1/sw.js"})
        session = _Session(infos)
        session.browser = self
        self.sessions.append(session)
        return session


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


async def test_existing_fake_never_closes_browser_and_drops_only_owned_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_bytes(f"9222\n{_PATH}".encode())
    seen: dict[str, object] = {}

    class _Chromium:
        async def connect_over_cdp(self, endpoint_url: str, **kwargs: object) -> _Browser:
            seen["endpoint"] = endpoint_url
            seen["no_defaults"] = kwargs.get("no_defaults")
            seen["browser"] = _Browser()
            return seen["browser"]  # type: ignore[return-value]

    _install_playwright(monkeypatch, _Chromium())
    driver = BrowserDriver(BrowserMode.existing_chrome, devtools_active_port=port_file)
    await driver.start()
    browser = seen["browser"]
    assert isinstance(browser, _Browser)
    assert seen["no_defaults"] is True
    assert seen["endpoint"] == f"ws://127.0.0.1:9222{_PATH}"
    assert driver.owned_target_id == "owned-1"
    assert [item.target_id for item in driver.foreign_before.targets] == ["foreign"]  # type: ignore[union-attr]
    assert all(session.detached for session in browser.sessions if session is not driver._ownership_session)
    await driver.close()
    assert browser.close_calls == 0
    assert browser.contexts[0].page is not None
    assert browser.contexts[0].page.closed is True
    assert driver.foreign_mismatch is False
    assert driver.residual_owned is False
    assert driver.signed_in_provenance == "UNKNOWN"


async def test_acknowledged_created_id_cleans_page_when_binding_disagrees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_bytes(f"9222\n{_PATH}\n".encode())
    browsers: list[_Browser] = []

    class _ContextShifted(_Context):
        async def new_cdp_session(self, page: _Page) -> _Session:
            session = await super().new_cdp_session(page)

            async def _send_other(
                method: str,
                params: dict[str, object] | None = None,
            ) -> dict[str, object]:
                if method == "Target.getTargetInfo":
                    return {"targetInfo": {"targetId": "someone-else"}}
                raise AssertionError(method)

            session.send = _send_other  # type: ignore[method-assign]
            return session

    class _Chromium:
        async def connect_over_cdp(self, endpoint_url: str, **kwargs: object) -> _Browser:
            browser = _Browser()
            browser.contexts = [_ContextShifted(browser)]
            browsers.append(browser)
            return browser

    _install_playwright(monkeypatch, _Chromium())
    driver = BrowserDriver("existing_chrome", devtools_active_port=port_file, cdp_timeout_ms=50)
    with pytest.raises(BrowserBlocked) as caught:
        await driver.start()
    assert caught.value.code == "ownership_unverified"
    assert driver._state == "closed"
    assert browsers[0].close_calls == 0
    assert browsers[0].contexts[0].page is not None
    assert browsers[0].contexts[0].page.closed is True


async def test_startup_failure_cleans_owned_process(monkeypatch: pytest.MonkeyPatch) -> None:
    launched: dict[str, _Browser] = {}

    class _Chromium:
        async def launch(self, *, chromium_sandbox: bool, channel: str) -> _Browser:
            assert chromium_sandbox is True
            assert channel == "chromium"
            browser = _Browser()

            async def _fail() -> object:
                raise RuntimeError("context failed")

            browser.contexts[0].new_page = _fail  # type: ignore[assignment]
            launched["browser"] = browser
            return browser

    box = _install_playwright(monkeypatch, _Chromium())
    driver = BrowserDriver(BrowserMode.isolated)
    with pytest.raises(BrowserBlocked) as caught:
        await driver.start()
    assert caught.value.code == "launch_failed"
    assert driver._state == "closed"
    assert launched["browser"].close_calls == 1
    assert box["playwright"].stopped is True
    with pytest.raises(BrowserBlocked) as again:
        await driver.start()
    assert again.value.code == "already_closed"


async def test_startup_cancellation_cleans_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    entered = asyncio.Event()
    box = _install_playwright(monkeypatch, object())

    class _Chromium:
        async def launch(self, *, chromium_sandbox: bool, channel: str) -> _Browser:
            assert chromium_sandbox is True
            assert channel == "chromium"
            entered.set()
            await asyncio.Future()
            raise AssertionError("launch should be cancelled")

    class _Manager:
        async def start(self) -> _Playwright:
            playwright = _Playwright(_Chromium())
            box["playwright"] = playwright
            return playwright

    monkeypatch.setattr("decision_browser.browser.async_playwright", lambda: _Manager())
    driver = BrowserDriver(BrowserMode.isolated)
    task = asyncio.create_task(driver.start())
    try:
        async with asyncio.timeout(2):
            await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(2):
                await task
    finally:
        if not task.done():
            task.cancel()
        async with asyncio.timeout(2):
            await asyncio.gather(task, return_exceptions=True)
    assert driver._state == "closed"
    assert box["playwright"].stopped is True


async def test_existing_cancellation_does_not_close_attached_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_bytes(f"9222\n{_PATH}".encode())
    entered = asyncio.Event()
    browsers: list[_Browser] = []

    class _HangingContext(_Context):
        async def new_page(self) -> _Page:
            entered.set()
            await asyncio.Future()
            raise AssertionError("new_page should be cancelled")

    class _Chromium:
        async def connect_over_cdp(self, endpoint_url: str, **kwargs: object) -> _Browser:
            assert kwargs.get("no_defaults") is True
            browser = _Browser()
            browser.contexts = [_HangingContext(browser)]
            browsers.append(browser)
            return browser

    box = _install_playwright(monkeypatch, _Chromium())
    driver = BrowserDriver(BrowserMode.existing_chrome, devtools_active_port=port_file)
    task = asyncio.create_task(driver.start())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert driver._state == "closed"
    assert browsers[0].close_calls == 0
    assert box["playwright"].stopped is True


async def test_two_isolated_contexts_keep_fixture_state_independent(counter_page_url: str) -> None:
    first = BrowserDriver(BrowserMode.isolated)
    second = BrowserDriver(BrowserMode.isolated)
    await first.start()
    try:
        await second.start()
        try:
            await first.context.add_cookies(
                [{"name": "db_fixture_a", "value": "alpha", "url": counter_page_url}]
            )
            await second.context.add_cookies(
                [{"name": "db_fixture_b", "value": "beta", "url": counter_page_url}]
            )
            await first.page.goto(counter_page_url)
            await second.page.goto(counter_page_url)
            await first.page.evaluate("localStorage.setItem('db_fixture', 'one')")
            await second.page.evaluate("localStorage.setItem('db_fixture', 'two')")
            assert await first.page.evaluate("localStorage.getItem('db_fixture')") == "one"
            assert await second.page.evaluate("localStorage.getItem('db_fixture')") == "two"
            assert await first.page.evaluate("document.cookie") == "db_fixture_a=alpha"
            assert await second.page.evaluate("document.cookie") == "db_fixture_b=beta"
            assert await first.page.title() == "decision-browser-counter"
            await first.page.click("#inc")
            assert (await first.page.text_content("#count")) == "1"
            assert (await second.page.text_content("#count")) == "0"
            png = await first.screenshot()
            assert png.startswith(b"\x89PNG\r\n\x1a\n")
            await first.page.click("#reset")
            assert (await first.page.text_content("#count")) == "0"
        finally:
            await second.close()
    finally:
        await first.close()
    with pytest.raises(BrowserBlocked) as caught:
        _ = first.page
    assert caught.value.code == "not_started"
