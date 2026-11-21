"""Hostile main-world URL lie versus the browser's real navigation."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from decision_browser.dom import DOMSession

ADMITTED = "http://admitted.test/allowed"
FOREIGN = "http://foreign.test/effect"
_LIE = (
    "(() => { const orig = Element.prototype.getAttribute;"
    " Element.prototype.getAttribute = function (name) {"
    " const key = String(name).toLowerCase();"
    " if (key === 'href' || key === 'action' || key === 'formaction') return '"
    + ADMITTED
    + "'; return orig.apply(this, arguments); }; })()"
)
_PAGE_SPOOF = (
    "(() => {"
    " const RealURL = URL;"
    " function FakeURL() { return new RealURL('" + ADMITTED + "'); }"
    " FakeURL.prototype = RealURL.prototype;"
    " window.URL = FakeURL;"
    " const fakeBox = {x:0,y:0,width:12,height:12,top:0,left:0,right:12,bottom:12,"
    " toJSON(){return {x:0,y:0,width:12,height:12,top:0,left:0,right:12,bottom:12};}};"
    " Element.prototype.getBoundingClientRect = function () { return fakeBox; };"
    " Document.prototype.elementFromPoint = function () { return document.body; };"
    " const empty = document.createElement('div').querySelectorAll('*');"
    " Document.prototype.querySelectorAll = function () { return empty; };"
    " Element.prototype.querySelectorAll = function () { return empty; };"
    " Object.defineProperty(window, '__decisionRegistry', {configurable:true, value:"
    " {href:'" + ADMITTED + "', width:12, height:12, foreign:0}});"
    "})()"
)
_CASES = (
    ("href", "click", "Open", f'<a data-x href="{FOREIGN}">Open</a>'),
    (
        "action",
        "submit",
        "Send",
        f'<form aria-label="Send" action="{FOREIGN}"><button type="submit">Go</button></form>',
    ),
    (
        "formaction",
        "click",
        "Go",
        f'<form action="{ADMITTED}"><button type="submit" formaction="{FOREIGN}">Go</button></form>',
    ),
)
_BOX_W = 80
_BOX_H = 40


class customDenied(Exception):
    pass


def _policy(action):
    sem = json.loads(action.fingerprint)["semantic"]
    node, form = sem.get("node") or {}, sem.get("form") or {}
    urls = [u for u in (node.get("href"), form.get("action")) if isinstance(u, str)]
    if any("foreign.test" in u for u in urls):
        raise customDenied


def _pick(obs, op, name):
    for cand in obs.candidates.values():
        sem = cand.semantic_by_operation.get(op) or {}
        node = sem.get("node") or {}
        if op in cand.operations and node.get("name") == name:
            return cand
    raise AssertionError(name)


def _assert_real_geometry(cand):
    rect = cand.geometry["rect"]
    assert rect["width"] == _BOX_W and rect["height"] == _BOX_H


async def _wire(page, bag):
    async def admitted(route):
        await route.fulfill(status=200, content_type="text/html", body=bag["html"])

    async def foreign(route):
        bag["foreign"] += 1
        await route.fulfill(status=204, body="")

    await page.route("http://admitted.test/**", admitted)
    await page.route("http://foreign.test/**", foreign)
    await page.add_init_script(_LIE)


def _html(inner):
    return (
        "<!doctype html><meta charset=utf-8><title>t</title><body style=margin:8px>"
        + inner
    )


@pytest_asyncio.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, chromium_sandbox=True, channel="chromium")
        ctx = None
        try:
            ctx = await browser.new_context()
            tab = await ctx.new_page()
            tab.set_default_timeout(5000)
            yield tab
        finally:
            try:
                if ctx is not None:
                    await ctx.close()
            finally:
                await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,op,name,inner", _CASES)
async def test_admitted_lie_does_not_reach_foreign(page, kind, op, name, inner):
    bag = {"html": _html(inner), "foreign": 0}
    await _wire(page, bag)
    await page.goto("http://admitted.test/page")
    session = DOMSession(page)
    try:
        obs = await session.observe()
        action = await session.prepare(op, _pick(obs, op, name).token)
        raised = False
        try:
            await session.dispatch(action, before_dispatch=_policy)
        except customDenied:
            raised = True
        await page.wait_for_timeout(100)
        assert bag["foreign"] == 0
        assert raised is True
        semantic = json.loads(action.fingerprint)["semantic"]
        if kind == "href":
            assert semantic["node"]["href"] == FOREIGN
        else:
            assert semantic["form"]["action"] == FOREIGN
            routing = semantic["routing"]
            if kind == "formaction":
                assert semantic["form"]["formAction"] == FOREIGN
                assert routing["element"]["formaction"] == FOREIGN
                assert routing["form"]["action"] == ADMITTED
            else:
                assert routing["form"]["action"] == FOREIGN
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_hidden_attribute_mutation_is_stale(page):
    bag = {"html": _html(_CASES[0][3]), "foreign": 0}
    await _wire(page, bag)
    await page.goto("http://admitted.test/page")
    session = DOMSession(page)
    called = []
    try:
        obs = await session.observe()
        action = await session.prepare("click", _pick(obs, "click", "Open").token)
        await page.evaluate(
            "(url) => document.querySelector('[data-x]').setAttribute('href', url)",
            "http://foreign.test/mutated",
        )
        result = await session.dispatch(action, before_dispatch=called.append)
        await page.wait_for_timeout(100)
        assert bag["foreign"] == 0
        assert result.status == "stale"
        assert called == []
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_reobserve_after_navigation_keeps_old_action_stale(page):
    bag = {"html": _html(_CASES[0][3]), "foreign": 0}
    await _wire(page, bag)
    await page.goto("http://admitted.test/one")
    session = DOMSession(page)
    called = []
    try:
        obs = await session.observe()
        action = await session.prepare("click", _pick(obs, "click", "Open").token)
        bag["html"] = _html("<p>next</p>")
        await page.goto("http://admitted.test/two")
        await session.observe()
        result = await session.dispatch(action, before_dispatch=called.append)
        await page.wait_for_timeout(100)
        assert bag["foreign"] == 0
        assert result.status == "stale"
        assert called == []
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_page_world_spoof_still_exposes_foreign_geometry(page):
    inner = (
        f'<a data-x href="{FOREIGN}" aria-label="Open" '
        f'style="display:block;width:{_BOX_W}px;height:{_BOX_H}px">Open</a>'
    )
    bag = {"html": _html(inner), "foreign": 0}
    await _wire(page, bag)
    await page.add_init_script(_PAGE_SPOOF)
    await page.goto("http://admitted.test/page")
    lied = await page.evaluate(
        """() => {
            const node = document.querySelector('[data-x]');
            return {
                href: node.getAttribute('href'),
                aria: node.getAttribute('aria-label'),
                name: node.getAttribute('name'),
                url: String(new URL('http://foreign.test/effect')),
                width: node.getBoundingClientRect().width,
                hit: document.elementFromPoint(12, 12) === document.body,
                qsa: document.querySelectorAll('a').length,
                registry: window.__decisionRegistry.foreign,
                registryHref: window.__decisionRegistry.href,
            };
        }"""
    )
    assert lied["href"] == ADMITTED
    assert lied["aria"] == "Open"
    assert lied["name"] is None
    assert lied["url"] == ADMITTED
    assert lied["width"] == 12
    assert lied["hit"] is True
    assert lied["qsa"] == 0
    assert lied["registry"] == 0
    assert lied["registryHref"] == ADMITTED
    session = DOMSession(page)
    try:
        obs = await session.observe()
        cand = _pick(obs, "click", "Open")
        action = await session.prepare("click", cand.token)
        assert FOREIGN in action.fingerprint
        assert ADMITTED not in action.fingerprint
        _assert_real_geometry(cand)
        raised = False
        try:
            await session.dispatch(action, before_dispatch=_policy)
        except customDenied:
            raised = True
        await page.wait_for_timeout(100)
        assert raised is True
        assert bag["foreign"] == 0
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_observe_fail_closed_without_cdp_session(page, monkeypatch):
    bag = {"html": _html(_CASES[0][3]), "foreign": 0}
    await _wire(page, bag)
    await page.goto("http://admitted.test/page")
    session = DOMSession(page)

    async def _cdp_down(*args, **kwargs):
        raise PlaywrightError("new_cdp_session denied")

    monkeypatch.setattr(page.context, "new_cdp_session", _cdp_down)
    try:
        with pytest.raises(PlaywrightError):
            await session.observe()
        await page.wait_for_timeout(100)
        assert bag["foreign"] == 0
    finally:
        monkeypatch.undo()
        await session.close()


@pytest.mark.parametrize(
    "inner,blocked",
    [
        (
            f'<div role="button" aria-label="Container" style="box-sizing:border-box;display:block;width:80px;height:40px;margin:0;padding:0;border:0"><a href="{FOREIGN}" style="box-sizing:border-box;display:block;width:80px;height:40px;margin:0;padding:0;border:0">x</a></div>',
            True,
        ),
        (
            f'<a href="{FOREIGN}" aria-label="Away" style="box-sizing:border-box;display:block;width:80px;height:40px;margin:0;padding:0;border:0"><span role="button" aria-label="Container" style="box-sizing:border-box;display:block;width:80px;height:40px;margin:0;padding:0;border:0">x</span></a>',
            True,
        ),
        (
            f'<form action="{ADMITTED}"><button type="submit" formaction="{FOREIGN}" aria-label="Away" style="box-sizing:border-box;display:block;width:80px;height:40px;margin:0;padding:0;border:0"><span role="button" aria-label="Container" style="box-sizing:border-box;display:block;width:80px;height:40px;margin:0;padding:0;border:0">x</span></button></form>',
            True,
        ),
        (
            f'<form action="{ADMITTED}"><div role="button" aria-label="Container" style="box-sizing:border-box;display:block;width:80px;height:40px;margin:0;padding:0;border:0"><button type="submit" formaction="{FOREIGN}" style="box-sizing:border-box;display:block;width:80px;height:40px;margin:0;padding:0;border:0">go</button></div></form>',
            True,
        ),
        (
            '<button type="button" aria-label="Container" style="box-sizing:border-box;display:block;width:80px;height:40px;margin:0;padding:0;border:0" onclick="window.__benign=(window.__benign||0)+1"><span style="box-sizing:border-box;display:block;width:80px;height:40px;margin:0;padding:0;border:0">ok</span></button>',
            False,
        ),
    ],
    ids=[
        "anchor-descendant",
        "anchor-ancestor",
        "formaction-ancestor",
        "submit-descendant",
        "plain-child",
    ],
)
@pytest.mark.asyncio
async def test_native_activation_recipient_mismatch_blocks_before_callback(
    page, inner, blocked
):
    bag = {"html": _html(inner), "foreign": 0}
    await _wire(page, bag)
    session = DOMSession(page)
    try:
        await page.goto(ADMITTED)
        obs = await session.observe()
        token = _pick(obs, "click", "Container").token
        callback = []

        def spy(action):
            callback.append(action)
            return _policy(action)

        action = await session.prepare("click", token)
        result = await session.dispatch(action, before_dispatch=spy)
        await page.wait_for_timeout(100)
        assert bag["foreign"] == 0
        if blocked:
            assert result.status == "blocked"
            assert callback == []
        else:
            assert result.status == "pass"
            assert len(callback) == 1
            assert await page.evaluate("window.__benign") == 1
    finally:
        await session.close()


import pytest

_SEED = {
    "text": "seed",
    "email": "a@b.co",
    "number": "12",
    "search": "seed",
    "tel": "555",
    "url": "https://a.example",
    "date": "2020-01-02",
    "time": "01:02",
    "color": "#112233",
    "month": "2020-01",
    "week": "2020-W01",
    "datetime-local": "2020-01-02T01:02",
    "textarea": "seed",
}
_NEXT = {
    "text": "next",
    "email": "c@d.co",
    "number": "34",
    "search": "q",
    "tel": "+1555",
    "url": "https://b.example/x",
    "date": "2021-03-04",
    "time": "05:06:07",
    "color": "#ABCDEF",
    "month": "2021-07",
    "week": "2021-W02",
    "datetime-local": "2021-03-04T05:06",
    "textarea": "next",
}


def _markup(kind, seed, watch=False):
    ev = (
        ' onfocus="window.__f=(window.__f||0)+1" oninput="window.__i=(window.__i||0)+1" onchange="window.__c=(window.__c||0)+1"'
        if watch
        else ""
    )
    if kind == "textarea":
        return f"<label>Field<textarea aria-label='Field'{ev}>{seed}</textarea></label>"
    return f"<label>Field<input type='{kind}' value='{seed}'{ev}></label>"


async def _dispatch(page, kind, seed, value, watch=False):
    await page.set_content(_html(_markup(kind, seed, watch)))
    session = DOMSession(page)
    try:
        obs = await session.observe()
        cand = _pick(obs, "fill", "Field")
        action = await session.prepare("fill", cand.token, value=value)
        callback = []
        result = await session.dispatch(action, before_dispatch=callback.append)
        live = await page.locator("input,textarea").input_value()
        counts = await page.evaluate(
            "() => [window.__f||0, window.__i||0, window.__c||0]"
        )
        return result, callback, live, counts
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(_SEED))
async def test_fill_replace_and_empty(page, kind):
    result, callback, live, _counts = await _dispatch(
        page, kind, _SEED[kind], _NEXT[kind]
    )
    assert live == (_NEXT[kind].lower() if kind == "color" else _NEXT[kind])
    assert result.status == "pass" and len(callback) == 1
    if kind == "color":
        result, callback, live, counts = await _dispatch(
            page, kind, _SEED[kind], "", watch=True
        )
        assert result.status == "blocked" and result.reason == "value"
        assert callback == [] and live == _SEED[kind] and counts == [0, 0, 0]
    else:
        result, callback, live, _counts = await _dispatch(page, kind, _SEED[kind], "")
        assert result.status == "pass" and live == "" and len(callback) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,bad",
    [("number", "abc"), ("date", "2020-13-40"), ("time", "25:99"), ("color", "red")],
)
async def test_fill_invalid_untouched(page, kind, bad):
    result, callback, live, counts = await _dispatch(
        page, kind, _SEED[kind], bad, watch=True
    )
    assert result.status == "blocked" and result.reason == "value"
    assert callback == [] and live == _SEED[kind] and counts == [0, 0, 0]


@pytest.mark.parametrize(
    ("html", "op", "name", "fill", "script"),
    [
        (
            '<!doctype html><body><input type="email" aria-label="Email" style="width:80px;height:40px"></body>',
            "fill",
            "Email",
            True,
            "document.querySelector('input').setAttribute('type','text')",
        ),
        (
            '<!doctype html><body><input type="email" aria-label="Email" style="width:80px;height:40px"></body>',
            "fill",
            "Email",
            True,
            "document.querySelector('input').setAttribute('aria-label','Updated')",
        ),
        (
            '<!doctype html><body><a href="/one" aria-label="Open" style="display:block;width:80px;height:40px"></a></body>',
            "click",
            "Open",
            False,
            "document.querySelector('a').setAttribute('href','/two')",
        ),
    ],
)
async def test_dynamic_blocks_mutated_target_at_layout_metrics(
    page, monkeypatch, html, op, name, fill, script
):
    async def _dynamic_fulfill(route):
        await route.fulfill(body=html, content_type="text/html")

    await page.route("http://127.0.0.1:8123/**", _dynamic_fulfill)
    await page.goto("http://127.0.0.1:8123/")
    session = DOMSession(page)
    try:
        obs = await session.observe()
        action = await session.prepare(
            op, _pick(obs, op, name).token, value="42" if fill else None
        )
        sends = []
        called = []
        original = session._world._send
        armed = True

        async def _dynamic_hook(method, params=None, **kwargs):
            nonlocal armed
            if method == "Page.getLayoutMetrics" and armed:
                armed = False
                await session._world.evaluate("() => { " + script + "; }")
            if method.startswith("Input.") or method == "DOM.focus":
                sends.append(method)
            return await original(method, params, **kwargs)

        monkeypatch.setattr(session._world, "_send", _dynamic_hook)
        result = await session.dispatch(action, before_dispatch=called.append)
        assert result.status in {"stale", "blocked"}
        assert called == []
        assert sends == []
    finally:
        await session.close()


def _dynamic_forms(kind):
    box = "width:80px;height:40px;display:block;margin:0;padding:0;border:0"
    if kind == "association":
        html = (
            "<!doctype html><html><body style='margin:0'>"
            f"<form id='first' action='/one' style='{box}'></form>"
            f"<form id='second' action='/one' style='{box}'></form>"
            f"<input aria-label='Email' form='first' style='{box}'>"
            "</body></html>"
        )
        js = "document.querySelector('[aria-label=\"Email\"]').setAttribute('form','second')"
        return "fill", "Email", html, js
    if kind == "action":
        html = (
            "<!doctype html><html><body style='margin:0'>"
            f"<form aria-label='Send' action='/one' style='{box}'></form>"
            "</body></html>"
        )
        js = (
            "(()=>{const el=document.querySelector('[aria-label=\"Send\"]');"
            "el.setAttribute('action','/two');el.action='/two';})()"
        )
        return "submit", "Send", html, js
    html = (
        "<!doctype html><html><body style='margin:0'><form action='/one' style='margin:0'>"
        f"<button type='submit' aria-label='Go' formaction='/one' style='{box}'>Go</button>"
        "</form></body></html>"
    )
    js = (
        "(()=>{const el=document.querySelector('[aria-label=\"Go\"]');"
        "el.setAttribute('formaction','/two');el.formAction='/two';})()"
    )
    return "click", "Go", html, js


@pytest.mark.parametrize("kind", ["association", "action", "formaction"])
async def test_dynamic_forms(page, monkeypatch, kind):
    op, name, html, js = _dynamic_forms(kind)

    async def route(route):
        await route.fulfill(status=200, content_type="text/html", body=html)

    await page.route("http://127.0.0.1:8123/**", route)
    await page.goto("http://127.0.0.1:8123/one")
    session = DOMSession(page)
    try:
        obs = await session.observe()
        action = await session.prepare(
            op, _pick(obs, op, name).token, value="42" if op == "fill" else None
        )
        called, sends, armed = [], [], False
        original = session._world._send

        async def hook(method, params=None, **kwargs):
            nonlocal armed
            if method.startswith("Input.") or method == "DOM.focus":
                sends.append(method)
            if method == "Page.getLayoutMetrics" and not armed:
                armed = True
                await session._world.evaluate("() => { " + js + "; }")
            return await original(method, params, **kwargs)

        monkeypatch.setattr(session._world, "_send", hook)
        result = await session.dispatch(action, before_dispatch=called.append)
        assert armed
        assert result.status in ("stale", "blocked")
        assert called == []
        assert sends == []
    finally:
        await session.close()


def _dynamic_svg_html(inner):
    return "<!DOCTYPE html><html><body>" + inner + "</body></html>"


_DYNAMIC_SVG_ANCHOR = _dynamic_svg_html(
    '<div role="button" aria-label="Container" style="width:80px;height:40px">'
    '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40">'
    '<a href="http://127.0.0.1:8123/two"><rect width="80" height="40" fill="black"></rect></a>'
    "</svg></div>"
)

_DYNAMIC_SVG_CONTROL = _dynamic_svg_html(
    '<div role="button" aria-label="Container" onclick="window.benign=(window.benign||0)+1" '
    'style="width:80px;height:40px">'
    '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40">'
    '<rect width="80" height="40" fill="black"></rect></svg></div>'
    "<script>window.benign=0</script>"
)

_DYNAMIC_SVG_NUMBER = _dynamic_svg_html(
    '<input id="dom" type="number" aria-label="DOM">'
    '<input id="sdk" type="number" aria-label="SDK">'
    "<script>window._dynamic_svg_events={dom:[],sdk:[]};"
    "for (const id of ['dom','sdk']) { const el = document.getElementById(id);"
    "for (const type of ['input','change']) { el.addEventListener(type, (event) => {"
    "window._dynamic_svg_events[id].push([event.type, event.target.value]); }); } }</script>"
)


async def test_dynamic_svg_anchor(page):
    async def _route(route):
        await route.fulfill(
            status=200, content_type="text/html", body=_DYNAMIC_SVG_ANCHOR
        )

    await page.route("http://127.0.0.1:8123/**", _route)
    await page.goto("http://127.0.0.1:8123/one")
    session = DOMSession(page)
    called = []
    sends = []
    try:
        obs = await session.observe()
        action = await session.prepare(
            "click", _pick(obs, "click", "Container").token, value=None
        )
        world = session._world
        original = world._send

        async def _dynamic_svg_send(method, *args, **kwargs):
            if str(method).startswith("Input.") or method == "DOM.focus":
                sends.append(method)
            return await original(method, *args, **kwargs)

        world._send = _dynamic_svg_send
        try:
            result = await session.dispatch(action, before_dispatch=called.append)
        finally:
            world._send = original
        assert result.status in ("stale", "blocked")
        assert called == []
        assert sends == []
    finally:
        await session.close()


async def test_dynamic_svg_control(page):
    async def _route(route):
        await route.fulfill(
            status=200, content_type="text/html", body=_DYNAMIC_SVG_CONTROL
        )

    await page.route("http://127.0.0.1:8123/**", _route)
    await page.goto("http://127.0.0.1:8123/one")
    session = DOMSession(page)
    called = []
    try:
        obs = await session.observe()
        action = await session.prepare(
            "click", _pick(obs, "click", "Container").token, value=None
        )
        result = await session.dispatch(action, before_dispatch=called.append)
        assert result.status == "pass"
        assert len(called) == 1
        assert await page.evaluate("window.benign") == 1
    finally:
        await session.close()


async def test_dynamic_number_padding(page):
    async def _route(route):
        await route.fulfill(
            status=200, content_type="text/html", body=_DYNAMIC_SVG_NUMBER
        )

    await page.route("http://127.0.0.1:8123/**", _route)
    await page.goto("http://127.0.0.1:8123/one")
    session = DOMSession(page)
    called = []
    try:
        obs = await session.observe()
        action = await session.prepare(
            "fill", _pick(obs, "fill", "DOM").token, value=" 42 "
        )
        result = await session.dispatch(action, before_dispatch=called.append)
        assert result.status == "pass"
        assert len(called) == 1
        dom_value = await page.locator("#dom").input_value()
        dom_events = (await page.evaluate("window._dynamic_svg_events"))["dom"]
        await page.locator("#sdk").fill(" 42 ")
        assert result.status == "pass"
        sdk_value = await page.locator("#sdk").input_value()
        sdk_events = (await page.evaluate("window._dynamic_svg_events"))["sdk"]
        assert dom_value == sdk_value
        assert dom_events == sdk_events
    finally:
        await session.close()


@pytest.mark.parametrize("cover_over", [True, False])
async def test_dynamic_geometry(page, cover_over):
    body = (
        "<!doctype html><form id=f>"
        "<button id=selected type=button aria-label=Go "
        'style="position:fixed;left:40px;top:40px;width:80px;height:40px;z-index:1">Go</button>'
        "<button id=cover type=button "
        'style="position:fixed;left:200px;top:200px;width:80px;height:40px;z-index:10"></button>'
        "</form><script>window.selectedCount=0;window.coverCount=0;"
        "selected.addEventListener('click',()=>selectedCount++);"
        "cover.addEventListener('click',()=>coverCount++);</script>"
    )
    await page.route(
        "http://127.0.0.1:8123/owned",
        lambda route: route.fulfill(status=200, content_type="text/html", body=body),
    )
    await page.goto("http://127.0.0.1:8123/owned")
    session = DOMSession(page)
    calls = {"cb": 0, "input": 0}
    try:
        obs = await session.observe()
        cand = _pick(obs, "click", "Go")
        action = await session.prepare("click", cand.token)
        original_geometry = session._geometry
        hook_world = session._world
        real_send = hook_world._send

        async def _geometry(geo, el):
            blocked = await original_geometry(geo, el)
            if blocked is None:
                await page.evaluate(
                    """(over) => {
                      const c = document.getElementById('cover');
                      c.style.left = over ? '40px' : '400px';
                      c.style.top = over ? '40px' : '400px';
                    }""",
                    cover_over,
                )
            return blocked

        async def _send(method, params=None, **kwargs):
            if method == "DOM.focus" or method.startswith("Input."):
                calls["input"] += 1
            return await real_send(method, params, **kwargs)

        session._geometry = _geometry
        hook_world._send = _send

        def _spy(_action):
            calls["cb"] += 1

        result = await session.dispatch(action, before_dispatch=_spy)
        counts = await page.evaluate("() => [window.selectedCount, window.coverCount]")
        if cover_over:
            assert result.status == "blocked"
            assert result.reason == "covered"
            assert calls["cb"] == 0 and calls["input"] == 0 and counts == [0, 0]
        else:
            assert result.status == "pass"
            assert calls["cb"] == 1 and counts == [1, 0]
    finally:
        await session.close()


@pytest.mark.parametrize("type_", ["submit", "button", "reset"])
async def test_input_caption_change_is_stale_before_native_dispatch(page, type_):
    await page.set_content(
        f'<input id="caption" type="{type_}" value="Review" aria-label="Review">'
    )
    session = DOMSession(page)
    called = []
    try:
        obs = await session.observe()
        action = await session.prepare("click", _pick(obs, "click", "Review").token)
        await page.locator("#caption").evaluate("el => el.value = 'Delete account'")
        result = await session.dispatch(action, before_dispatch=called.append)
        assert result.status == "stale"
        assert called == []
    finally:
        await session.close()


@pytest.mark.parametrize("target", ["_blank", "existing"])
async def test_base_target_mutation_blocks_before_dispatch(page, target):
    await page.set_content('<base target="_self"><a href="https://admitted.test/safe">Review</a>')
    session = DOMSession(page)
    called = []
    try:
        obs = await session.observe()
        action = await session.prepare("click", _pick(obs, "click", "Review").token)
        await page.locator("base").evaluate("(el, target) => el.target = target", target)
        result = await session.dispatch(action, before_dispatch=called.append)
        assert result.status in {"stale", "blocked"}
        assert called == []
    finally:
        await session.close()


async def test_label_cannot_forward_to_unobserved_submitter(page):
    await page.set_content(
        '<form action="https://admitted.test/safe">'
        '<button id="danger" formaction="https://foreign.test/effect">Delete account</button>'
        '<label for="danger" role="button" style="display:block;width:200px;height:40px">Review</label>'
        '</form>'
    )
    foreign_requests = []

    async def abort_foreign(route):
        foreign_requests.append(route.request.url)
        await route.abort()

    await page.route("https://foreign.test/**", abort_foreign)
    await page.evaluate("""() => {
        window.__labelEffects = {clicks: 0, submits: 0};
        document.getElementById('danger').addEventListener('click', () => window.__labelEffects.clicks++);
        document.querySelector('form').addEventListener('submit', event => {
            window.__labelEffects.submits++;
            event.preventDefault();
        });
    }""")
    session = DOMSession(page)
    called = []
    try:
        obs = await session.observe()
        labels = [
            candidate for candidate in obs.candidates.values()
            if "click" in candidate.operations
            and candidate.semantic_by_operation["click"]["node"]["state"]["tag"] == "label"
        ]
        assert len(labels) == 1
        descriptor = labels[0].semantic_by_operation["click"]["node"]
        assert descriptor["state"]["tag"] == "label" and descriptor["name"] == "Review"
        action = await session.prepare("click", labels[0].token)
        result = await session.dispatch(action, before_dispatch=called.append)
        assert result.status == "blocked"
        assert called == []
        assert await page.evaluate("window.__labelEffects") == {"clicks": 0, "submits": 0}
        assert foreign_requests == []
    finally:
        await session.close()
