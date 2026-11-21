import dataclasses
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from decision_browser.dom import Action, DOMSession

pytestmark = pytest.mark.asyncio

HTML = """<!doctype html><html><head><base href="https://fixture.test/"></head><body style="margin:0">
<button id="inc" type="button">Increment</button>
<label>Name <input id="name" value="seed" aria-label="Name"></label>
<label>Color <select id="color" aria-label="Color"><option value="red">red</option><option value="blue">blue</option></select></label>
<a id="link" href="/old">Docs</a>
<form id="search" action="/search" method="get" aria-label="Search">
<input id="query" name="q" aria-label="Query">
<button id="go" type="submit" formaction="/one" formmethod="post">Go</button>
</form>
<div style="height:5000px"></div>
<script>
window.__clicks=0;window.__saves=0;window.__changes=0;window.__submits=[];
document.getElementById("inc").addEventListener("click",()=>{window.__clicks++});
document.getElementById("name").addEventListener("input",()=>{window.__saves++});
document.getElementById("color").addEventListener("change",()=>{window.__changes++});
addEventListener("submit",e=>{e.preventDefault();
 const s=e.submitter;
 window.__submits.push((s&&s.getAttribute("formmethod"))||e.target.getAttribute("method")||"get");
},true);
</script></body></html>"""


def pick(obs, op, name):
    for token, cand in obs.candidates.items():
        if (
            op in cand.operations
            and cand.semantic_by_operation[op]["node"]["name"] == name
        ):
            return token
    raise AssertionError((op, name))


def tracker():
    box = {"n": 0}

    def cb(action):
        box["n"] += 1

    return box, cb


def st(result):
    return getattr(result.status, "value", result.status)


async def prep(dom, op, name=None, value=None):
    obs = await dom.observe()
    token = None if name is None else pick(obs, op, name)
    return await dom.prepare(op, token=token, value=value)


@asynccontextmanager
async def mounted(page):
    await page.set_content(HTML)
    dom = DOMSession(page)
    try:
        yield dom
    finally:
        await dom.close()


@pytest_asyncio.fixture
async def page():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True, chromium_sandbox=True, channel="chromium")
    try:
        yield await browser.new_page()
    finally:
        await browser.close()
        await pw.stop()


async def test_required_field_reports_missing_value_until_filled(page):
    async with mounted(page) as dom:
        await page.locator("#query").evaluate("(el) => { el.required = true }")
        obs = await dom.observe()
        token = pick(obs, "fill", "Query")
        assert obs.candidates[token].semantic_by_operation["fill"]["node"]["valueMissing"] is True
        assert st(await dom.dispatch(await dom.prepare("fill", token=token, value="Atlas"))) == "pass"
        obs = await dom.observe()
        token = pick(obs, "fill", "Query")
        assert "valueMissing" not in obs.candidates[token].semantic_by_operation["fill"]["node"]


async def test_finite_actions(page):
    async with mounted(page) as dom:
        assert st(await dom.dispatch(await prep(dom, "click", "Increment"))) == "pass"
        assert await page.evaluate("() => window.__clicks") == 1
        assert (
            st(await dom.dispatch(await prep(dom, "fill", "Name", value="Ada")))
            == "pass"
        )
        assert await page.evaluate(
            "() => [document.getElementById('name').value, window.__saves]"
        ) == ["Ada", 1]
        assert (
            st(await dom.dispatch(await prep(dom, "select", "Color", value="blue")))
            == "pass"
        )
        assert await page.evaluate(
            "() => [document.getElementById('color').value, window.__changes]"
        ) == ["blue", 1]
        assert st(await dom.dispatch(await prep(dom, "scroll", value="down"))) == "pass"
        assert await page.evaluate("() => window.scrollY") > 0
        assert st(await dom.dispatch(await prep(dom, "wait", value=1))) == "pass"
        shot = await dom.dispatch(await prep(dom, "screenshot"))
        assert st(shot) == "pass" and shot.data.startswith(b"\x89PNG\r\n\x1a\n")
    assert await page.evaluate("() => window.__clicks") == 1


@pytest.mark.parametrize(
    "op,name,method,expect",
    [
        ("enter", "Query", None, "post"),
        ("submit", "Search", "get", "get"),
        ("submit", "Search", "post", "post"),
        ("submit", "Search", "delete", "delete"),
    ],
)
async def test_targeted_enter_and_submit(page, op, name, method, expect):
    async with mounted(page) as dom:
        if method:
            await page.evaluate(
                """(m)=>{document.getElementById('search').setAttribute('method',m);document.getElementById('go').setAttribute('formmethod',m)}""",
                method,
            )
        assert st(await dom.dispatch(await prep(dom, op, name))) == "pass"
        assert await page.evaluate("() => window.__submits") == [expect]


@pytest.mark.parametrize(
    "op,name,expr,value",
    [
        (
            "click",
            "Increment",
            "document.getElementById('inc').setAttribute('role','switch')",
            None,
        ),
        (
            "click",
            "Increment",
            "document.getElementById('inc').setAttribute('aria-label','X')",
            None,
        ),
        ("click", "Increment", "document.getElementById('inc').disabled=true", None),
        (
            "click",
            "Docs",
            "document.getElementById('link').setAttribute('href','/x')",
            None,
        ),
        ("fill", "Name", "document.getElementById('name').value='z'", "Ada"),
        (
            "select",
            "Color",
            "document.getElementById('color').options[1].label='green'",
            "blue",
        ),
        ("select", "Color", "document.getElementById('color').selectedIndex=1", "blue"),
        (
            "select",
            "Color",
            "document.getElementById('color').innerHTML='<option>green</option>'",
            "blue",
        ),
    ],
)
async def test_attached_mutation_stale(page, op, name, expr, value):
    async with mounted(page) as dom:
        obs = await dom.observe()
        action = await dom.prepare(op, token=pick(obs, op, name), value=value)
        await page.evaluate(f"()=>{{ {expr} }}")
        box, cb = tracker()
        result = await dom.dispatch(action, before_dispatch=cb)
        effects = await page.evaluate(
            "() => window.__clicks+window.__saves+window.__changes+window.__submits.length"
        )
        assert st(result) == "stale" and box["n"] == 0 and effects == 0


@pytest.mark.parametrize("kind", ["reobserve", "detach", "document"])
async def test_old_action_stale(page, kind):
    async with mounted(page) as dom:
        obs = await dom.observe()
        action = await dom.prepare("click", token=pick(obs, "click", "Increment"))
        if kind == "reobserve":
            await dom.observe()
        elif kind == "detach":
            await page.evaluate(
                "()=>{const n=document.getElementById('inc');n.replaceWith(n.cloneNode(true))}"
            )
        else:
            await page.set_content("<html><body>replaced</body></html>")
        box, cb = tracker()
        assert (
            st(await dom.dispatch(action, before_dispatch=cb)) == "stale"
            and box["n"] == 0
        )


@pytest.mark.parametrize(
    "field,new",
    [
        ("value", "nope"),
        ("operation", "hover"),
        ("token", "forged"),
        ("fingerprint", "forged"),
    ],
)
async def test_replaced_action_stale(page, field, new):
    async with mounted(page) as dom:
        action = await prep(dom, "click", "Increment")
        box, cb = tracker()
        result = await dom.dispatch(
            dataclasses.replace(action, **{field: new}), before_dispatch=cb
        )
        assert (
            st(result) == "stale"
            and box["n"] == 0
            and await page.evaluate("() => window.__clicks") == 0
        )


async def test_forged_action_rejected(page):
    async with mounted(page) as dom:
        action = await prep(dom, "click", "Increment")
        forged = Action(
            operation=action.operation,
            token=action.token,
            value=action.value,
            destination=action.destination,
            observation_id=action.observation_id,
            fingerprint=action.fingerprint,
        )
        box, cb = tracker()
        result = await dom.dispatch(forged, before_dispatch=cb)
        assert (
            st(result) == "stale"
            and box["n"] == 0
            and await page.evaluate("() => window.__clicks") == 0
        )


async def test_cross_session_rejected(page):
    await page.set_content(HTML)
    first, second = DOMSession(page), DOMSession(page)
    try:
        action = await prep(first, "click", "Increment")
        box, cb = tracker()
        result = await second.dispatch(action, before_dispatch=cb)
        assert (
            st(result) == "stale"
            and box["n"] == 0
            and await page.evaluate("() => window.__clicks") == 0
        )
    finally:
        await first.close()
        await second.close()
    assert await page.evaluate("() => 1") == 1


STRIP = "()=>{const b=document.getElementById('go');b.removeAttribute('formaction');b.removeAttribute('formmethod')}"
MOVE = "()=>{const f=document.getElementById('search');const q=document.getElementById('query');const c=f.cloneNode(false);f.replaceWith(c);c.appendChild(q)}"


@pytest.mark.parametrize(
    "expr,base",
    [
        ("document.getElementById('search').setAttribute('action','/two')", True),
        ("document.getElementById('search').setAttribute('method','post')", True),
        ("document.getElementById('go').setAttribute('formaction','/two')", False),
        ("document.getElementById('go').setAttribute('formmethod','delete')", False),
    ],
)
async def test_enter_form_changed_stale(page, expr, base):
    async with mounted(page) as dom:
        if base:
            await page.evaluate(STRIP)
        obs = await dom.observe()
        action = await dom.prepare("enter", token=pick(obs, "enter", "Query"))
        await page.evaluate(f"()=>{{ {expr} }}")
        box, cb = tracker()
        result = await dom.dispatch(action, before_dispatch=cb)
        assert (
            st(result) == "stale"
            and box["n"] == 0
            and await page.evaluate("() => window.__submits.length") == 0
        )


async def test_form_owner_replaced_before_prepare(page):
    async with mounted(page) as dom:
        obs = await dom.observe()
        await page.evaluate(MOVE)
        action = await dom.prepare("enter", token=pick(obs, "enter", "Query"))
        box, cb = tracker()
        result = await dom.dispatch(action, before_dispatch=cb)
        assert (
            st(result) == "stale"
            and box["n"] == 0
            and await page.evaluate("() => window.__submits.length") == 0
        )


@pytest.mark.parametrize(
    "op,name,value",
    [
        ("fill", "Name", "Ada"),
        ("select", "Color", "blue"),
        ("click", "Increment", None),
        ("enter", "Query", None),
        ("submit", "Search", None),
    ],
)
@pytest.mark.parametrize("method", ["get", "post", "delete"])
@pytest.mark.parametrize("kind", ["action", "method", "owner", "override"])
async def test_route_changed_after_prepare_stale(page, op, name, value, method, kind):
    async with mounted(page) as dom:
        await page.evaluate(
            """(m)=>{const f=document.getElementById('search');const b=document.getElementById('go');b.removeAttribute('formaction');b.removeAttribute('formmethod');f.setAttribute('method',m);for(const id of ['inc','name','color'])f.appendChild(document.getElementById(id))}""",
            method,
        )
        obs = await dom.observe()
        action = await dom.prepare(op, token=pick(obs, op, name), value=value)
        await page.evaluate(
            """({k,id})=>{const f=document.getElementById('search');if(k==='owner'){const c=f.cloneNode(false);while(f.firstChild)c.appendChild(f.firstChild);f.replaceWith(c);return}const el=document.getElementById(id);const onForm=id==='search';const a=onForm?'action':'formaction';const mv=onForm?'method':'formmethod';if(k==='action'||k==='override')el.setAttribute(a,'/two');if(k==='method')el.setAttribute(mv,f.getAttribute('method')==='get'?'post':'get');else if(k==='override')el.setAttribute(mv,'delete')}""",
            {
                "k": kind,
                "id": {
                    "click": "inc",
                    "fill": "name",
                    "select": "color",
                    "enter": "go",
                    "submit": "search",
                }[op],
            },
        )
        box, cb = tracker()
        result = await dom.dispatch(action, before_dispatch=cb)
        effects = await page.evaluate(
            "() => window.__clicks+window.__saves+window.__changes+window.__submits.length"
        )
        assert st(result) == "stale" and box["n"] == 0 and effects == 0


async def test_callback_raises_effects_zero(page):
    async with mounted(page) as dom:
        action = await prep(dom, "click", "Increment")

        def boom(action):
            raise RuntimeError("stop")

        with pytest.raises(RuntimeError, match="stop"):
            await dom.dispatch(action, before_dispatch=boom)
        assert await page.evaluate("() => window.__clicks") == 0


@pytest.fixture
def local_http():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            b = b'<button onclick="window.hits=(window.hits||0)+1">Go</button>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=5)


async def test_navigate_canonical_and_forged_stale(page, local_http):
    url, canonical = local_http, local_http + "/next?x=1#f"
    dom = DOMSession(page)
    try:
        await page.goto(url)
        await dom.observe()
        action = await dom.prepare("navigate", destination=url + "/a/../next?x=1#f")
        assert action.destination == canonical
        seen = []

        def before(action):
            seen.append(action)

        result = await dom.dispatch(action, before_dispatch=before)
        assert result.status == "pass" and page.url == canonical and len(seen) == 1
        forged = dataclasses.replace(action, destination=url + "/forged")
        seen.clear()
        stale = await dom.dispatch(forged, before_dispatch=before)
        assert stale.status == "stale" and seen == []
    finally:
        await dom.close()


async def test_click_reload_stale_then_fresh_hit(page, local_http):
    url, dom, seen = local_http, DOMSession(page), []

    def before(action):
        seen.append(action)

    def go_token(obs):
        for token, cand in obs.candidates.items():
            if "click" not in cand.operations:
                continue
            if cand.semantic_by_operation["click"]["node"]["name"] == "Go":
                return token
        raise AssertionError("Go")

    try:
        await page.goto(url)
        action = await dom.prepare("click", token=go_token(await dom.observe()))
        await page.goto(url)
        stale = await dom.dispatch(action, before_dispatch=before)
        assert stale.status == "stale" and seen == []
        fresh = await dom.prepare("click", token=go_token(await dom.observe()))
        result = await dom.dispatch(fresh, before_dispatch=before)
        assert result.status == "pass" and len(seen) == 1
        assert await page.evaluate("window.hits") == 1
    finally:
        await dom.close()


@pytest.mark.asyncio
async def test_custom_button_aria_pressed_stale(page):
    session = DOMSession(page)
    try:
        await page.set_content(
            "<div id='b' role='button' aria-pressed='false' "
            "onclick='window.count=(window.count||0)+1'>Go</div>"
            "<script>window.count=0</script>"
        )
        obs = await session.observe()
        token = pick(obs, "click", "Go")
        action = await session.prepare("click", token)
        await page.eval_on_selector("#b", "n => n.setAttribute('aria-pressed','true')")
        assert (await session.dispatch(action)).status == "stale"
        assert await page.evaluate("window.count") == 0
    finally:
        await session.close()
