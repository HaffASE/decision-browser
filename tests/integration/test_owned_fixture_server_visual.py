"""Served owned-fixture geometry under the pinned style CSP.

Computed style on that served document shows the seeded clip, overlap, and responsive boxes at 390, 768, and 1440.
"""

from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import math
import time
from urllib.parse import urlparse

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from decision_browser.contracts import Viewport
from decision_browser.owned_fixture import CONTENT_IDS, DOCUMENTS, PINS
from decision_browser.owned_fixture_server import OwnedFixtureResource
from decision_browser.verification import inspect_geometry

_VIEWPORTS = [(390, 844), (768, 1024), (1440, 900)]
_STAGE = "Computed style on that served document shows the seeded clip, overlap, and responsive boxes at 390, 768, and 1440."
_KIND = {
    "clean": set(),
    "clipping": {"clipping"},
    "overlap": {"overlap"},
    "responsive": {"responsive"},
}
_PROBE = """
(arg) => {
  const name = arg.name;
  const css = (sel) => getComputedStyle(document.querySelector(sel));
  const box = (sel) => document.querySelector(sel).getBoundingClientRect();
  const hit = (a, b) => {
    const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
    const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    return w > 0 && h > 0;
  };
  const inside = (r) => r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0
    && r.right <= window.innerWidth && r.bottom <= window.innerHeight;
  if (name === "clean") {
    const body = css("body");
    const form = css("form");
    return {
      backgroundColor: body.backgroundColor,
      color: body.color,
      display: form.display,
      flexDirection: form.flexDirection,
      gap: form.gap,
      query: document.querySelector("#query").value,
      label: document.querySelector("label").textContent.trim(),
      button: document.querySelector("#submit").textContent.trim(),
      queryInside: inside(box("#query")),
      buttonInside: inside(box("#submit")),
    };
  }
  if (name === "clipping") {
    const parent = box("#clip-box");
    const inner = box("#clipped");
    const clip = css("#clip-box");
    return {
      clipWidth: clip.width,
      childWidth: css("#clipped").width,
      overflowX: clip.overflowX,
      overflowY: clip.overflowY,
      extends: inner.left < parent.left || inner.right > parent.right,
    };
  }
  if (name === "overlap") {
    const a = css("#real-a");
    const b = css("#real-b");
    return {
      aLeft: a.left, bLeft: b.left, aTop: a.top, bTop: b.top,
      aWidth: a.width, bWidth: b.width,
      realHit: hit(box("#real-a"), box("#real-b")),
      ignoredHit: hit(box("#ignored .one"), box("#ignored .two")),
      ignore: document.querySelector("#ignored").getAttribute("data-visual-ignore"),
    };
  }
  return {width: css("#overflow-button").width, actual: box("#overflow-button").width};
}
"""


def _expect_style(name: str, probed: dict[str, object], width: int) -> None:
    if name == "clean":
        assert probed["backgroundColor"] == "rgb(255, 255, 255)"
        assert probed["color"] == "rgb(68, 68, 68)"
        assert probed["display"] == "flex" and probed["flexDirection"] == "column"
        assert probed["gap"] not in {"0px", "0", "normal"}
        assert probed["label"] == "Search" and probed["button"] == "Search"
        assert probed["query"] == "atlas"
        assert probed["queryInside"] is True and probed["buttonInside"] is True
        return
    if name == "clipping":
        assert probed["clipWidth"] == "80px" and probed["childWidth"] == "160px"
        assert probed["overflowX"] == "hidden" and probed["overflowY"] == "hidden"
        assert probed["extends"] is True
        return
    if name == "overlap":
        assert probed["realHit"] is True and probed["ignoredHit"] is True
        assert probed["ignore"] == "true"
        assert probed["aLeft"] == "20px" and probed["bLeft"] == "90px"
        assert probed["aTop"] == "16px" and probed["bTop"] == "16px"
        assert probed["aWidth"] == "100px" and probed["bWidth"] == "100px"
        return
    assert probed["width"] == "1700px"
    assert float(probed["actual"]) > width


def _style_source_b64(name: str) -> str:
    document = DOCUMENTS[name]
    opener = "<style>"
    closer = "</style>"
    start = document.find(opener)
    end = document.find(closer)
    assert start != -1 and end > start
    assert document.find(opener, start + len(opener)) == -1
    assert document.find(closer, end + len(closer)) == -1
    inner = document[start + len(opener) : end].encode("utf-8")
    style = base64.b64encode(hashlib.sha256(inner).digest()).decode("ascii")
    whole = base64.b64encode(hashlib.sha256(document.encode("utf-8")).digest()).decode("ascii")
    assert style != whole
    return style


async def _assert_owned_port_refused(host: str, port: int, deadline: float) -> None:
    writer = None
    try:
        async with asyncio.timeout_at(deadline):
            try:
                _reader, writer = await asyncio.open_connection(host, port)
            except OSError as exc:
                if exc.errno == errno.ECONNREFUSED:
                    return
                raise AssertionError("port refusal unknown") from exc
    except TimeoutError as exc:
        raise AssertionError("port refusal unknown") from exc
    writer.close()
    try:
        async with asyncio.timeout_at(deadline):
            await writer.wait_closed()
    except (TimeoutError, OSError, RuntimeError):
        writer.close()
    raise AssertionError("owned port accepted a connection")


class _Collect:
    def __init__(self, failures: list[BaseException]) -> None:
        self.failures = failures

    def __enter__(self) -> None:
        pass

    def __exit__(self, _exc_type: object, exc: BaseException | None, _traceback: object) -> bool:
        if not isinstance(exc, Exception):
            return False
        self.failures.append(exc)
        return True


@pytest.mark.parametrize("name", ["clean", "clipping", "overlap", "responsive"])
@pytest.mark.parametrize("width,height", _VIEWPORTS)
async def test_served_fixture_style_and_geometry(name: str, width: int, height: int) -> None:
    budget = 30
    grace = 2
    assert budget + grace <= 120
    deadline = time.monotonic() + budget
    cleanup_deadline = deadline + grace
    resource = OwnedFixtureResource()
    playwright = browser = context = page = None
    host: str | None = None
    port: int | None = None
    caught: BaseException | None = None
    try:
        async with asyncio.timeout_at(deadline):
            await resource.start(CONTENT_IDS[name], deadline)
            parsed = urlparse(resource.url)
            host, port = parsed.hostname, parsed.port
            assert parsed.scheme == "http"
            assert host == "127.0.0.1"
            assert isinstance(port, int)
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(headless=True, chromium_sandbox=True, channel="chromium")
            context = await browser.new_context(viewport={"width": width, "height": height})
            page = await context.new_page()
            response = await page.goto(resource.url, wait_until="load")
            assert response is not None and response.status == 200
            assert response.url == resource.url
            arrived = urlparse(response.url)
            assert arrived.scheme == "http"
            assert arrived.hostname == "127.0.0.1"
            assert arrived.port == port
            body = await response.body()
            assert body == DOCUMENTS[name].encode("utf-8")
            assert hashlib.sha256(body).hexdigest() == PINS[name]
            assert response.headers["x-content-type-options"] == "nosniff"
            csp = response.headers["content-security-policy"]
            quoted = "'sha256-" + _style_source_b64(name) + "'"
            assert "script-src 'none'" in csp
            assert f"style-src {quoted}" in csp
            assert f"style-src-elem {quoted}" in csp
            _expect_style(name, await page.evaluate(_PROBE, {"name": name}), width)
            report = await inspect_geometry(page, Viewport(width=width, height=height))
            assert report.diagnostic == "complete", _STAGE
            assert {item.kind for item in report.findings} == _KIND[name]
            if name == "overlap":
                described = " ".join(f"{item.element} {item.evidence}" for item in report.findings)
                assert "real-a" in described and "real-b" in described
                assert "ignored" not in described
            for item in report.findings:
                assert item.origin == "geometry" and item.evidence
                assert item.viewport.width == width and item.viewport.height == height
                assert math.isfinite(item.region.x) and math.isfinite(item.region.y)
                assert item.region.width > 0 and item.region.height > 0
    except BaseException as exc:
        caught = exc
        raise
    finally:
        failures: list[BaseException] = []

        async def _step(action):
            try:
                async with asyncio.timeout_at(cleanup_deadline):
                    await action()
            except (PlaywrightError, TimeoutError, OSError, RuntimeError) as exc:
                failures.append(exc)

        if page is not None:
            await _step(page.close)
        if context is not None:
            await _step(context.close)
        if browser is not None:
            await _step(browser.close)
        if playwright is not None:
            await _step(playwright.stop)
        cleanup = None
        try:
            async with asyncio.timeout_at(cleanup_deadline):
                cleanup = await resource.close()
        except (TimeoutError, OSError, RuntimeError) as exc:
            failures.append(exc)
        if page is not None:
            with _Collect(failures):
                assert page.is_closed()
        if browser is not None:
            with _Collect(failures):
                assert browser.is_connected() is False
        if host is not None and port is not None:
            try:
                await _assert_owned_port_refused(host, port, cleanup_deadline)
            except (AssertionError, TimeoutError, OSError, RuntimeError) as exc:
                failures.append(exc)
        if cleanup is None:
            failures.append(AssertionError("resource close did not report"))
        else:
            try:
                assert cleanup.unfinished_tasks == 0
                assert cleanup.unknown is False
                assert cleanup.clean is True
                assert cleanup.listener_closed is True
            except AssertionError as exc:
                failures.append(exc)
        if caught is not None:
            for item in failures:
                caught.add_note(f"cleanup also failed: {item!r}")
        elif len(failures) == 1:
            raise failures[0]
        elif failures:
            raise ExceptionGroup("owned cleanup failed", failures)
