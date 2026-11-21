"""Viewport whitespace work must preserve visited-node and visibility contracts."""
import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from decision_browser.dom import DOMSession


@pytest_asyncio.fixture
async def viewport_page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, chromium_sandbox=True, channel="chromium")
        try:
            context = await browser.new_context(viewport={"width": 1280, "height": 720})
            yield await context.new_page()
        finally:
            await browser.close()


@pytest.mark.parametrize("empty_nodes,expected", [(19999, "Boundary"), (20000, "")])
async def test_empty_nodes_still_consume_bounded_visit_budget(viewport_page, empty_nodes, expected):
    await viewport_page.set_content("<!doctype html><body></body>")
    await viewport_page.evaluate("""count => {
        const nodes = Array.from({length: count}, () => document.createTextNode(' '));
        document.body.append(...nodes);
        const marker = document.createElement('span');
        marker.textContent = 'Boundary'; document.body.append(marker);
    }""", empty_nodes)
    session = DOMSession(viewport_page)
    try:
        assert (await session.observe()).text == expected
    finally:
        await session.close()


@pytest.mark.parametrize("hostile", [False, True])
async def test_whitespace_keeps_text_order_visibility_and_codepoints(viewport_page, hostile):
    await viewport_page.set_content("""<!doctype html><body><span>Visible</span>
      <div hidden>Hidden one</div><div style="opacity:0"><span>Hidden two</span></div>
      <span id="last"></span></body>""")
    text = "a" * (4096 - len("Visible") - 1 - 1) + "😀" + "excluded"
    await viewport_page.locator("#last").evaluate("(node, value) => node.textContent = value", text)
    if hostile:
        await viewport_page.evaluate("""() => {
          window.getComputedStyle = () => ({display:'none', opacity:'0', visibility:'hidden'});
          Document.prototype.createRange = () => ({getClientRects:() => []});
        }""")
    session = DOMSession(viewport_page)
    try:
        observed = (await session.observe()).text
        assert observed == "Visible\n" + text[:4096 - len("Visible") - 1]
        assert observed.endswith("😀")
        assert not any(0xD800 <= ord(point) <= 0xDFFF for point in observed)
    finally:
        await session.close()


async def test_large_excluded_script_and_textarea_keep_visible_output(viewport_page):
    await viewport_page.set_content(
        '<!doctype html><body><script type="application/json">' + ' X' * 500000
        + '</script><textarea style="display:none">' + ' Y' * 500000
        + '</textarea><span>Visible marker</span></body>'
    )
    session = DOMSession(viewport_page)
    try:
        assert (await session.observe()).text == "Visible marker"
    finally:
        await session.close()
