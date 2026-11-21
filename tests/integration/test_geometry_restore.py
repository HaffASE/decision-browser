from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import AsyncIterator

import pytest
from playwright.async_api import (
    Browser,
    Page,
    ViewportSize,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)

from decision_browser.contracts import Viewport
from decision_browser.verification import inspect_geometry

pytestmark = pytest.mark.asyncio

_PROBE = Viewport(width=390, height=844)
_ORIGINAL: ViewportSize = {"width": 1280, "height": 720}
_BUTTON = (
    "<button style=\"position:absolute;left:500px;top:10px;"
    "width:100px;height:30px\">Go</button>"
)


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(
            headless=True, chromium_sandbox=True, channel="chromium"
        )
        try:
            yield chromium
        finally:
            await chromium.close()


@pytest.fixture
async def tab(browser: Browser) -> AsyncIterator[Page]:
    page = await browser.new_page(viewport=_ORIGINAL)
    try:
        yield page
    finally:
        await page.close()


@pytest.mark.parametrize("failure_mode", ["raise", "noop"])
async def test_restore_failure_retains_responsive_finding(
    tab: Page, monkeypatch: pytest.MonkeyPatch, failure_mode: str
) -> None:
    await tab.set_content(_BUTTON)
    real_set = tab.set_viewport_size
    delegated = False

    async def set_viewport_size(size: ViewportSize) -> None:
        nonlocal delegated
        if (
            not delegated
            and size["width"] == _PROBE.width
            and size["height"] == _PROBE.height
        ):
            delegated = True
            await real_set(size)
            return
        if failure_mode == "raise":
            raise PlaywrightError("restore failed")
        return

    monkeypatch.setattr(tab, "set_viewport_size", set_viewport_size)
    report = await inspect_geometry(tab, _PROBE)
    assert delegated
    assert report.diagnostic == "failed"
    assert report.reason == "viewport_restore_failed"
    assert any(finding.kind == "responsive" for finding in report.findings)
    payload = report.model_dump()
    assert "functional" not in payload
    assert "terminal" not in payload
    assert tab.viewport_size == {"width": _PROBE.width, "height": _PROBE.height}


async def test_success_restores_original_viewport_and_window(tab: Page) -> None:
    await tab.set_content(_BUTTON)
    report = await inspect_geometry(tab, _PROBE)
    assert tab.viewport_size == _ORIGINAL
    window = await tab.evaluate(
        "() => ({width: window.innerWidth, height: window.innerHeight})"
    )
    assert window == _ORIGINAL
    assert report.diagnostic == "complete"
    assert any(finding.kind == "responsive" for finding in report.findings)


async def test_no_viewport_rejects_without_resize(browser: Browser) -> None:
    context = await browser.new_context(no_viewport=True)
    page = await context.new_page()
    try:
        assert page.viewport_size is None
        report = await inspect_geometry(page, _PROBE)
        assert report.diagnostic == "failed"
        assert report.reason == "viewport_original_invalid"
        assert report.findings == []
        assert page.viewport_size is None
    finally:
        await page.close()
        await context.close()


@pytest.mark.parametrize("opacity,visible", [("0", False), ("0.5", True), ("1", True)])
async def test_geometry_respects_transparent_ancestors(tab: Page, opacity: str, visible: bool) -> None:
    await tab.set_content(f'<div style="opacity:{opacity}"><div>{_BUTTON}</div></div>')
    report = await inspect_geometry(tab, _PROBE)
    assert report.diagnostic == "complete"
    assert any(finding.kind == "responsive" for finding in report.findings) is visible
    assert tab.viewport_size == _ORIGINAL


async def test_geometry_uses_isolated_world_despite_hostile_page_overrides(tab: Page) -> None:
    await tab.set_content(_BUTTON)
    baseline = await inspect_geometry(tab, _PROBE)
    assert any(finding.kind == "responsive" for finding in baseline.findings)
    await tab.evaluate("""() => {
        document.querySelectorAll = () => [];
        window.getComputedStyle = () => ({display:'none'});
        Element.prototype.getBoundingClientRect = () => ({x:0,y:0,width:0,height:0});
    }""")
    report = await inspect_geometry(tab, _PROBE)
    assert report.diagnostic == "complete"
    assert report.findings == baseline.findings
    assert tab.viewport_size == _ORIGINAL


@pytest.fixture
def commented_bundled_scripts() -> list[str]:
    probe = r'''
import json
from pathlib import Path
read_text = Path.read_text
def commented(path, *args, **kwargs):
    text = read_text(path, *args, **kwargs)
    return text.rstrip() + "\n// fixture trailing comment" if path.name in {"snapshot.js", "collector.js"} else text
Path.read_text = commented
from decision_browser import dom
print(json.dumps([dom._NULL, dom._COLLECT, dom._SHADOW]))
'''
    return json.loads(subprocess.check_output([sys.executable, "-c", probe], text=True))


async def test_bundled_sources_survive_trailing_line_comment(
    tab: Page, commented_bundled_scripts: list[str]
) -> None:
    compiled = await tab.evaluate(
        "scripts => scripts.map(script => { new Function('return (' + script + '\\n)'); return true; })",
        commented_bundled_scripts,
    )
    assert compiled == [True, True, True]
