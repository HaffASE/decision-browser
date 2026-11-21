"""CDP hit testing uses document CSS coordinates; native input uses the viewport."""

from __future__ import annotations

import pytest
from playwright.async_api import async_playwright

from decision_browser.dom import DOMSession


@pytest.mark.parametrize("scroll_x,scroll_y,device_scale", [
    (0, 850, 1), (650, 0, 1), (650, 850, 1), (650, 850, 2),
])
@pytest.mark.parametrize("covered", [False, True], ids=["reachable", "covered"])
async def test_scrolled_native_click_reaches_only_the_current_visible_target(
    scroll_x, scroll_y, device_scale, covered,
):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, chromium_sandbox=True, channel="chromium")
        try:
            page = await browser.new_page(viewport={"width": 640, "height": 480},
                                          device_scale_factor=device_scale)
            await page.set_content("""<!doctype html><style>
              body { margin:0; width:3000px; height:3000px }
              button { position:absolute; width:120px; height:40px }
            </style><button id="target">Continue</button><script>
              window.counts = {target:0, cover:0};
              target.onclick = () => counts.target++;
            </script>""")
            await page.locator("#target").evaluate(
                "(el, offset) => {el.style.left=(offset.x+80)+'px';el.style.top=(offset.y+90)+'px'}",
                {"x": scroll_x, "y": scroll_y},
            )
            await page.evaluate("offset => scrollTo(offset.x, offset.y)", {"x": scroll_x, "y": scroll_y})
            await page.wait_for_function(
                "offset => scrollX === offset.x && scrollY === offset.y",
                arg={"x": scroll_x, "y": scroll_y}, timeout=2000,
            )
            dom = DOMSession(page)
            try:
                observation = await dom.observe()
                token = next(token for token, candidate in observation.candidates.items()
                             if "click" in candidate.operations
                             and candidate.semantic_by_operation["click"]["node"]["name"] == "Continue")
                action = await dom.prepare("click", token=token)
                if covered:
                    await page.evaluate("""() => {
                      const cover = document.createElement('button');
                      cover.style='position:fixed;left:80px;top:90px;width:120px;height:40px;z-index:10';
                      cover.onclick=()=>counts.cover++; document.body.appendChild(cover);
                    }""")
                callbacks = []
                result = await dom.dispatch(action, before_dispatch=callbacks.append)
                counts = await page.evaluate("counts")
                if covered:
                    assert (result.status, result.reason) == ("blocked", "covered")
                    assert callbacks == [] and counts == {"target": 0, "cover": 0}
                else:
                    assert result.status == "pass", result
                    assert callbacks == [action] and counts == {"target": 1, "cover": 0}
                assert await page.evaluate("[scrollX,scrollY]") == [scroll_x, scroll_y]
            finally:
                await dom.close()
        finally:
            await browser.close()
