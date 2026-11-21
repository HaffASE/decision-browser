"""Owned isolated Chromium. AC03 existing Chrome unresolved."""

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from decision_browser.dom import DOMSession

pytestmark = pytest.mark.asyncio
_CASES = [
    (
        "iframe",
        "<iframe srcdoc='<p>x</p>' style='width:40px;height:40px'></iframe>",
        "iframe",
    ),
    ("canvas", "<canvas width='20' height='20'></canvas>", "canvas"),
    ("file", "<input type='file'>", "file"),
    ("password", "<input type='password' name='pw'>", "password"),
    ("download", "<a href='/a.txt' download='a.txt'>dl</a>", "download"),
    ("newwindow", "<a href='https://example.com/' target='_blank'>go</a>", "newwindow"),
    (
        "namedtarget",
        "<a href='https://example.com/' target='other'>go</a>",
        "namedtarget",
    ),
    (
        "shadow",
        "<div id='host' tabindex='0' style='width:20px;height:20px'></div>",
        "shadow",
    ),
]


@pytest_asyncio.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, chromium_sandbox=True, channel="chromium")
        try:
            context = await browser.new_context()
            yield await context.new_page()
        finally:
            await browser.close()


async def _open(page, html, shadow=False):
    await page.set_content(html)
    if shadow:
        await page.evaluate(
            "() => document.querySelector('#host').attachShadow({mode:'open'})"
        )
    session = DOMSession(page)
    return session, await session.observe()


def _inert(obs, reason, n):
    assert isinstance(obs.unsupported, tuple) and obs.unsupported == (reason,) * n
    assert len(obs.candidates) == n
    assert all(
        isinstance(c.operations, tuple) and c.operations == ()
        for c in obs.candidates.values()
    )


async def _no_click(session, obs):
    for token in obs.candidates:
        with pytest.raises(ValueError):
            await session.prepare("click", token)


@pytest.mark.parametrize("_name,html,reason", _CASES, ids=[c[0] for c in _CASES])
async def test_markup_unsupported(page, _name, html, reason):
    session, obs = await _open(page, html, reason == "shadow")
    try:
        _inert(obs, reason, 1)
        await _no_click(session, obs)
    finally:
        await session.close()


@pytest.mark.parametrize(
    "target,reason", [("_blank", "newwindow"), ("other", "namedtarget")]
)
async def test_form_target_blocks_input_button_form(page, target, reason):
    html = (
        f"<form action='https://example.com/go' target='{target}'>"
        "<input name='q'><button type='submit'>Go</button></form>"
    )
    session, obs = await _open(page, html)
    try:
        _inert(obs, reason, 3)
        await _no_click(session, obs)
    finally:
        await session.close()


async def test_popup_after_observe_blocks_dispatch(page):
    await page.set_content(
        '<button onclick="window.hits = (window.hits || 0) + 1">OK</button>'
    )
    await page.evaluate("() => { window.hits = 0 }")
    session = DOMSession(page)
    popup = None
    hits = 0
    try:
        obs = await session.observe()
        token, candidate = next(
            (t, c) for t, c in obs.candidates.items() if "click" in c.operations
        )
        click = candidate.semantic_by_operation["click"]
        assert (
            click["node"]["name"],
            click["node"]["role"],
            click["node"]["state"]["tag"],
        ) == (
            "OK",
            "button",
            "button",
        )
        action = await session.prepare("click", token)
        popup = await page.context.new_page()

        def before_dispatch(action):
            nonlocal hits
            hits += 1

        result = await session.dispatch(action, before_dispatch=before_dispatch)
        assert (result.status, result.reason, hits) == ("blocked", "popup", 0)
        assert await page.evaluate("() => window.hits") == 0
    finally:
        await session.close()
    assert popup.url == "about:blank" and not page.is_closed() and not popup.is_closed()
    await page.evaluate("() => { globalThis.sentinel = 'page' }")
    await popup.evaluate("() => { globalThis.sentinel = 'popup' }")
    assert await page.evaluate("() => globalThis.sentinel") == "page"
    assert await popup.evaluate("() => globalThis.sentinel") == "popup"
    await popup.close()


async def test_preexisting_context_page_is_foreign_baseline(page):
    foreign = await page.context.new_page()
    try:
        await foreign.set_content('<p id="sentinel">foreign-sentinel</p>')
        foreign_url = foreign.url
        foreign_content = await foreign.content()
        await page.set_content(
            '<button onclick="globalThis.clickCount=(globalThis.clickCount||0)+1">OK</button>'
        )
        session = DOMSession(page)
        try:
            obs = await session.observe()
            assert not any(
                isinstance(reason, str) and reason.startswith("popup:")
                for reason in obs.unsupported
            )
            token = next(
                candidate.token
                for candidate in obs.candidates.values()
                for op in candidate.operations
                if candidate.semantic_by_operation[op]["node"]["name"] == "OK"
            )
            action = await session.prepare("click", token)

            def before_dispatch(action):
                return action

            result = await session.dispatch(action, before_dispatch=before_dispatch)
            assert result.status == "pass"
            assert await page.evaluate("() => globalThis.clickCount") == 1
        finally:
            await session.close()
        assert not page.is_closed()
        assert not foreign.is_closed()
        assert foreign.url == foreign_url
        assert await foreign.content() == foreign_content
    finally:
        await foreign.close()
