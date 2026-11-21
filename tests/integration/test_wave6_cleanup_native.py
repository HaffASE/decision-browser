"""Real document/lifecycle behavior retained by the narrow F23 read seams."""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from playwright.async_api import Page, async_playwright

from decision_browser.contracts import Viewport
from decision_browser.dom import DOMSession
from decision_browser.verification import inspect_geometry


@pytest_asyncio.fixture
async def page() -> AsyncIterator[Page]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True, chromium_sandbox=True, channel="chromium",
        )
        try:
            tab = await browser.new_page(viewport={"width": 800, "height": 600})
            yield tab
        finally:
            await browser.close()


async def test_readonly_observation_tracks_real_observe_and_close(page):
    await page.set_content('<button type="button">Search</button>')
    session = DOMSession(page)
    try:
        assert session.current_observation is None
        first = await session.observe()
        assert session.current_observation is first
        with pytest.raises(AttributeError):
            session.current_observation = None
        second = await session.observe()
        assert second.id != first.id and second.document_id == first.document_id
        assert session.current_observation is second
    finally:
        await session.close()
    assert session.current_observation is None


async def test_labels_remain_noninteractive_and_control_overlap_stays_detected(page):
    style = 'position:absolute;left:40px;top:40px;width:120px;height:32px'
    await page.set_content(f'<label for="query" style="{style}">Query</label><input id="query" style="{style}">')
    result = await inspect_geometry(page, Viewport(width=800, height=600))
    assert result.diagnostic == "complete"
    assert not any(finding.kind == "overlap" for finding in result.findings)
    await page.set_content(f'<button style="{style}">A</button><button style="{style}">B</button>')
    result = await inspect_geometry(page, Viewport(width=800, height=600))
    assert result.diagnostic == "complete"
    assert any(finding.kind == "overlap" for finding in result.findings)
