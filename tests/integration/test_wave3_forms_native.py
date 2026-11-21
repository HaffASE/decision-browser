"""F09 pinned Chromium, owned routes/pages; synthetic provider, production policy/budget/journal."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import pytest_asyncio
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from test_runner import RecordingDriver, ScriptedProvider, finish, request

from decision_browser.decision_codec import Decision
from decision_browser.dom import DOMSession
from decision_browser.runner import RunManager


@pytest_asyncio.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, chromium_sandbox=True, channel="chromium")
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


def pick(obs, operation, name):
    return next(token for token, item in obs.candidates.items() if operation in item.operations and item.semantic_by_operation[operation]["node"]["name"] == name)


async def test_names_roles_and_wrapped_select_label(page):
    await page.set_content('''<span id="ref">Referenced</span><input aria-label="Other" aria-labelledby="ref">
    <label>Color <select><option>Red</option><option>Blue</option></select></label>
    <input type="number" aria-label="Amount"><input type="range" aria-label="Level">''')
    dom = DOMSession(page)
    try:
        obs = await dom.observe()
        nodes = [item.semantic_by_operation["click"]["node"] for item in obs.candidates.values()]
        names = {node["name"]: node["role"] for node in nodes}
        assert names == {"Referenced": "textbox", "Color": "combobox", "Amount": "spinbutton", "Level": "slider"}
    finally:
        await dom.close()


async def test_effective_disabled_fieldset_legend_and_optgroup(page):
    await page.set_content('''<fieldset disabled><legend><input aria-label="Legend"></legend>
    <input aria-label="Blocked"><select aria-label="Disabled"><option>A</option><option>B</option></select></fieldset>
    <select aria-label="Menu"><option value="a">A</option><optgroup disabled label="Locked"><option value="b">B</option></optgroup></select>''')
    dom = DOMSession(page)
    try:
        obs = await dom.observe()
        assert pick(obs, "fill", "Legend")
        names = {item.semantic_by_operation["click"]["node"]["name"] for item in obs.candidates.values()}
        assert "Blocked" not in names and "Disabled" not in names
        token = pick(obs, "select", "Menu")
        with pytest.raises(ValueError, match="select"):
            await dom.prepare("select", token, value="b")
        action = await dom.prepare("select", token, value="a")
        assert (await dom.dispatch(action)).status == "pass"
    finally:
        await dom.close()


async def test_duplicate_options_are_refused_without_event(page):
    await page.set_content('''<select aria-label="Menu" onchange="window.changed=1"><option value="x">Allowed</option><option value="x">Requested</option></select>''')
    dom = DOMSession(page)
    try:
        obs = await dom.observe()
        with pytest.raises(ValueError, match="select_ambiguous"):
            await dom.prepare("select", pick(obs, "select", "Menu"), value="x")
        assert await page.evaluate("() => [document.querySelector('select').selectedIndex, window.changed || 0]") == [0, 0]
    finally:
        await dom.close()


@pytest.mark.parametrize("hostile", [False, True])
async def test_focus_open_combobox_preserves_routing_guard(page, hostile):
    handler = "this.setAttribute('aria-expanded','true');" + ("this.setAttribute('formaction','/purchase')" if hostile else "")
    await page.set_content(f'''<form action="/search"><input aria-label="Query" role="combobox" aria-expanded="false" onfocus="{handler}"></form>''')
    dom = DOMSession(page)
    try:
        obs = await dom.observe()
        action = await dom.prepare("fill", pick(obs, "fill", "Query"), value="fixture")
        if hostile:
            with pytest.raises(RuntimeError, match="recipient"):
                await dom.dispatch(action)
            assert await page.locator("input").input_value() == ""
        else:
            assert (await dom.dispatch(action)).status == "pass"
            assert await page.locator("input").input_value() == "fixture"
    finally:
        await dom.close()


@pytest.mark.parametrize("forwarding", [False, True])
async def test_descendant_label_allows_only_safe_recipient(page, forwarding):
    target = 'for="other"' if forwarding else ''
    await page.set_content(f'''<button type="button" aria-label="Safe" style="padding:0"><label {target}>Label</label></button><input id="other" type="checkbox">''')
    dom = DOMSession(page)
    try:
        obs = await dom.observe()
        action = await dom.prepare("click", pick(obs, "click", "Safe"))
        result = await dom.dispatch(action)
        assert result.status == ("blocked" if forwarding else "pass")
        assert await page.locator("#other").is_checked() is False
    finally:
        await dom.close()


class FormDriver(RecordingDriver):
    def __init__(self, mode, html, *, drift=False):
        super().__init__(mode)
        self.html, self.drift, self.hits = html, drift, []
        self.final_value = None

    async def start(self):
        await super().start()

        async def owned(route):
            self.hits.append(route.request.url)
            await route.fulfill(status=200, content_type="text/html", body=self.html)
        if not self.drift:
            await self.page.context.route("**/*", owned)

    async def close(self):
        if self._browser is not None:
            self.final_selection = await self.page.evaluate("() => document.querySelector('select')?.selectedIndex")
            self.final_value = await self.page.evaluate("() => document.querySelector('input')?.value")
        await super().close()


class FormProvider(ScriptedProvider):
    async def decide(self, questions, input_text, **kwargs):
        decision = await super().decide(questions, input_text, **kwargs)
        if decision.operation == "click":
            choices = json.loads(input_text)["candidates"]["click"]
            key = next(key for key, row in choices.items() if row["name"] in {"Go", "Details"})
            return Decision("click", key, 1.0)
        return decision


async def drive(html, operations, *, start="https://example.test/same", values=None, drift=False):
    provider = FormProvider(operations)
    drivers = []
    host = RunManager(lambda: provider, lambda mode: drivers.append(FormDriver(mode, html, drift=drift)) or drivers[-1], admission_mode="task_scope")
    req = request(start)
    req.values = values or {}
    run_id = host.start_task(req)
    result = await finish(host, run_id)
    run = host._run
    events = [json.loads(run.store.read(key).content) for key in result.artifact_ids]
    await host.aclose()
    if destination := os.environ.get("F09_EVIDENCE_DIR"):
        evidence = Path(destination)
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / (run_id + ".json")).write_text(json.dumps({
            "result": result.model_dump(mode="json"), "hits": drivers[0].hits,
            "events": events, "request": req.model_dump(mode="json"),
        }, indent=2), encoding="utf-8")
    return result, drivers[0], events


@pytest.mark.parametrize("operation", ["enter", "submit", "click"])
async def test_get_serialization_through_real_run_policy_budget_and_journal(operation):
    html = '''<form action="/search?discard=1" method="get" aria-label="Search"><input name="q" value="fixture + value" aria-label="Query"><input disabled name="skip" value="bad"><input type="checkbox" name="on" value="yes" checked><button type="submit" name="go" value="ok">Go</button></form>'''
    result, driver, events = await drive(html, [operation, "DONE"])
    assert result.terminal == "completed_unverified", result.evidence
    expected = "https://example.test/search?q=fixture+%2B+value&on=yes" + ("&go=ok" if operation != "submit" else "")
    assert driver.hits == ["https://example.test/same", expected]
    assert result.usage.steps == 2 and result.usage.calls == 2
    assert sum(event.get("event") == "before_dispatch" for event in events) == 2
    assert sum(event.get("event") == "outcome" and event.get("status") == "acknowledged" for event in events) == 2


@pytest.fixture
def redirect_server():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(302 if self.path == "/other" else 200)
            if self.path == "/other":
                self.send_header("Location", "/same")
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if self.path != "/other":
                self.wfile.write(b'<a href="/self">Details</a>' if self.path == "/self" else b'<a href="/other">Details</a>')

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_same_url_new_document_checks_expected_destination(redirect_server):
    base, hits = redirect_server
    result, _driver, events = await drive("", ["click", "DONE"], start=base + "/same", drift=True)
    assert result.terminal == "outcome_unknown", result.evidence
    assert [path for path in hits if path != "/favicon.ico"] == ["/same", "/other", "/same"]
    assert result.usage.steps == 2 and result.usage.calls == 1
    assert any(event.get("event") == "outcome" and event.get("status") == "outcome_unknown" for event in events)


@pytest.mark.parametrize("route,terminal", [("https://other.test/search", "completed_unverified"), ("https://other.test/%70urchase", "blocked")])
async def test_fill_form_cross_origin_does_not_require_submit_admission_and_keeps_autosave_signals(route, terminal):
    result, driver, events = await drive(f'<form action="{route}"><input aria-label="Query"></form>', ["fill", "DONE"], values={"Query": "fixture"})
    assert result.terminal == terminal, result.evidence
    assert driver.final_value == ("fixture" if terminal == "completed_unverified" else "")
    assert driver.hits == ["https://example.test/same"]
    assert result.usage.steps == (2 if terminal == "completed_unverified" else 1)
    assert sum(event.get("event") == "before_dispatch" for event in events) == result.usage.steps


async def test_enabled_legend_control_executes_but_late_optgroup_disabling_does_not(page):
    await page.set_content('''<fieldset disabled><legend><input aria-label="Legend"></legend></fieldset>
    <select aria-label="Menu" onchange="window.changed=1"><option value="a">A</option><optgroup label="Group"><option value="b">B</option></optgroup></select>''')
    dom = DOMSession(page)
    try:
        obs = await dom.observe()
        assert (await dom.dispatch(await dom.prepare("fill", pick(obs, "fill", "Legend"), value="safe"))).status == "pass"
        obs = await dom.observe()
        action = await dom.prepare("select", pick(obs, "select", "Menu"), value="b")
        original_native = dom._native

        async def late_disable(action):
            # The admission callback has already committed the attempt. Recheck
            # the actual option immediately inside its single mutation call.
            await page.locator("optgroup").evaluate("el => el.disabled = true")
            return await original_native(action)

        dom._native = late_disable
        with pytest.raises(PlaywrightError, match="select changed"):
            await dom.dispatch(action)
        assert await page.evaluate("() => [document.querySelector('select').value, window.changed || 0]") == ["a", 0]
    finally:
        await dom.close()


@pytest.mark.parametrize("extra", [
    '<input name="attachment" type="file">',
    '<input name="secret" type="password" value="synthetic">',
    '<textarea name="q" dirname="direction">fixture</textarea>',
    '<input name="_charset_" value="ignored">',
])
async def test_unsupported_get_serialization_is_inert_and_observation_has_no_formdata_event(page, extra):
    await page.set_content('<form action="/search" aria-label="Search">' + extra + '<button>Go</button></form><script>addEventListener("formdata",()=>window.observedFormData=1)</script>')
    dom = DOMSession(page)
    try:
        obs = await dom.observe()
        assert "formserialization" in obs.unsupported
        assert await page.evaluate("() => window.observedFormData || 0") == 0
        assert not any("submit" in candidate.operations for candidate in obs.candidates.values())
    finally:
        await dom.close()


@pytest.mark.parametrize("wanted,note", [("missing", "select_input:value_mismatch"), ("Requested", "select_input:ambiguous_value")])
async def test_public_supplied_select_refusal_has_no_helper_call_or_dispatch(wanted, note):
    result, driver, events = await drive('<select aria-label="Menu"><option value="x">Allowed</option><option value="x">Requested</option></select>', ["select", "DONE"], values={"Menu": wanted})
    assert result.terminal == "blocked" and note in result.evidence
    assert result.usage.steps == 1 and result.usage.calls == 0
    assert driver.hits == ["https://example.test/same"]
    assert sum(event.get("event") == "before_dispatch" for event in events) == 1


async def test_public_select_cross_origin_form_allows_only_local_mutation():
    result, driver, events = await drive('<form action="https://other.test/search"><label>Color <select><option value="a">Allowed</option><option value="b">Blue</option></select></label></form>', ["select", "DONE"], values={"Color": "Blue"})
    assert result.terminal == "completed_unverified", result.evidence
    assert driver.final_selection == 1
    assert result.usage.steps == 2 and result.usage.calls == 2
    assert driver.hits == ["https://example.test/same"]
    assert sum(event.get("event") == "before_dispatch" for event in events) == 2


async def test_get_serializes_external_multiline_and_selected_controls():
    html = '''<form id="search" action="/search" method="get" aria-label="Search"><textarea name="text">first\nsecond</textarea><select name="colors" multiple><option selected value="a">A</option><option selected disabled value="b">B</option><option selected value="c">C</option></select><input type="checkbox" name="unchecked" value="skip"></form><input form="search" name="external" value="ok">'''
    result, driver, _events = await drive(html, ["submit", "DONE"])
    assert result.terminal == "completed_unverified", result.evidence
    assert driver.hits == ["https://example.test/same", "https://example.test/search?text=first%0D%0Asecond&colors=a&colors=c&external=ok"]


async def test_unsupported_future_get_keeps_local_fill_available(page):
    await page.set_content('''<form action="https://example.test/search"><input aria-label="Query"><input type="file" name="attachment"><button>Go</button></form>''')
    dom = DOMSession(page)
    try:
        obs = await dom.observe()
        token = pick(obs, "fill", "Query")
        assert "enter" not in obs.candidates[token].operations
        assert "formserialization" in obs.unsupported
        assert (await dom.dispatch(await dom.prepare("fill", token, value="safe"))).status == "pass"
        assert await page.locator("input").first.input_value() == "safe"
    finally:
        await dom.close()


async def test_expected_same_url_document_and_unexpected_redirect_controls(redirect_server):
    base, hits = redirect_server
    # /self points at itself. The new Document is allowed only because its exact
    # destination matches the observed link, even though URL equality persists.
    result, _driver, _events = await drive("", ["click", "DONE"], start=base + "/self", drift=True)
    assert result.terminal == "completed_unverified", result.evidence
    assert [path for path in hits if path != "/favicon.ico"] == ["/self", "/self"]
    hits.clear()
    result, _driver, _events = await drive("", ["click", "DONE"], start=base + "/begin", drift=True)
    assert result.terminal == "outcome_unknown" and result.usage.calls == 1
    assert [path for path in hits if path != "/favicon.ico"] == ["/begin", "/other", "/same"]


async def test_public_unsupported_get_does_not_block_safe_sibling_fill():
    result, driver, _events = await drive('<form action="/search"><input aria-label="Query"><input type="file" name="attachment"><button>Go</button></form>', ["fill", "DONE"], values={"Query": "fixture"})
    assert result.terminal == "completed_unverified", result.evidence
    assert driver.final_value == "fixture"
    assert result.usage.steps == 2 and driver.hits == ["https://example.test/same"]
