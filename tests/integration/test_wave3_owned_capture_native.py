"""Private loopback + sandboxed pinned Chromium. No live provider or user profile."""
from __future__ import annotations

import asyncio
import contextlib
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from decision_browser.browser import BrowserDriver
from decision_browser.dom import Action, DOMSession
from decision_browser.owned_fixture import CONTENT_IDS
from decision_browser.owned_fixture_server import OwnedFixtureResource


@asynccontextmanager
async def _loopback(*, delay=0):
    tasks = set()

    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            if delay:
                await asyncio.sleep(delay)
            body = b"<html><body>owned capture ready</body></html>"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    def accept(reader, writer):
        task = asyncio.create_task(handle(reader, writer))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        server.close()
        await server.wait_closed()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_db072_native_literal_pattern_does_not_pause_foreign_url():
    async with _loopback() as origin, BrowserDriver("isolated") as driver:
        page, context = driver.page, driver.context
        session = await context.new_cdp_session(page)
        enabled = asyncio.Event()
        pauses = []
        session.on("Fetch.requestPaused", lambda event: pauses.append(event["request"]["url"]))

        class Session:
            def on(self, *args):
                session.on(*args)

            async def send(self, method, params):
                result = await session.send(method, params)
                if method == "Fetch.enable":
                    enabled.set()
                return result

            async def detach(self):
                await session.detach()

        class Context:
            async def new_cdp_session(self, owned_page):
                assert owned_page is page
                return Session()

        driver._context = Context()
        expected = origin + "/doc?x=*"
        capture = asyncio.create_task(driver.read_owned_document_body(expected, time.monotonic() + 5))
        try:
            await asyncio.wait_for(enabled.wait(), 1)
            foreign = origin + "/docAx=unrelated"
            await page.goto(foreign, timeout=1500)
            assert pauses == []
            await page.goto(expected, timeout=2000)
            body = await asyncio.wait_for(capture, 2)
            assert b"owned capture ready" in body.body
            assert pauses == [expected]
        finally:
            if not capture.done():
                capture.cancel()
            await asyncio.gather(capture, return_exceptions=True)
            driver._context = context


@pytest.mark.parametrize("remaining,succeeds", [(.5, True), (.02, False)])
async def test_db053_native_goto_uses_task_budget_instead_of_page_default(remaining, succeeds):
    async with _loopback(delay=.08) as origin, BrowserDriver("isolated") as driver:
        driver.page.set_default_navigation_timeout(5)
        dom = DOMSession(driver.page, navigation_remaining=lambda: remaining)
        action = Action("navigate", None, None, origin + "/doc", "obs", "{}")
        try:
            if succeeds:
                assert await dom._native(action) == origin + "/doc"
                observation = await dom.observe()
                assert observation.url == origin + "/doc"
            else:
                with pytest.raises(PlaywrightTimeoutError):
                    await dom._native(action)
        finally:
            await dom.close()


async def test_db058_db071_db164_native_fixture_clock_connections_and_postdeadline_cleanup():
    clock = SimpleNamespace(now=0.0)
    resource = OwnedFixtureResource(clock=lambda: clock.now)
    await resource.start(CONTENT_IDS["clean"], 5.0)
    parts = urlsplit(resource.url)
    try:
        for _ in range(30):
            reader, writer = await asyncio.open_connection("127.0.0.1", parts.port)
            writer.write(f"GET {parts.path} HTTP/1.1\r\nHost: 127.0.0.1:{parts.port}\r\n\r\n".encode())
            await writer.drain()
            reply = await asyncio.wait_for(reader.read(), 1)
            assert reply.startswith(b"HTTP/1.1 200")
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0)
            assert resource.owned_task_count <= 1  # Only the expiry watcher.
        reader, writer = await asyncio.open_connection("127.0.0.1", parts.port)
        writer.write(b"GET /")
        await writer.drain()
        await asyncio.sleep(0)
        clock.now = 6.0
        report = await resource.close()
        assert report.clean and report.listener_closed and report.unfinished_tasks == 0
        with contextlib.suppress(ConnectionResetError):
            assert await asyncio.wait_for(reader.read(), .5) == b""
        writer.close()
        with contextlib.suppress(ConnectionResetError):
            await writer.wait_closed()
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", parts.port)
    finally:
        await resource.close()
