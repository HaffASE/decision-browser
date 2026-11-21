"""A repeated acknowledged focus-click can be ineligible for one observation.

Real Chromium and server events remain independent of scripted choices. A focus
click may have external effects; unchanged visible state is not a no-effect claim.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.async_api import async_playwright
from test_runner import ScriptedProvider, finish

from decision_browser import runner
from decision_browser.browser import BrowserDriver
from decision_browser.contracts import Check, Limits, PrivacyPolicy, TaskRequest
from decision_browser.decision_codec import Decision
from decision_browser.dom import DOMSession


@contextmanager
def site(*, attributes="", value="", duplicate=False, clone=False, options=False,
         hidden_progress=False, picker_after=False, blur_after=False):
    events = []
    lock = threading.Lock()
    second = ('<label>From<input id="right" value="London" '
              'onclick="focusClick(this)"></label>') if duplicate else ""
    option = '<button role="option">London option</button>' if options else ""
    body = f'''<!doctype html><meta charset="utf-8"><link rel="icon" href="data:,">
      <label>From<input id="left" value="{value}" {attributes}
        oninput="send('input',this.id,this.value)" onclick="focusClick(this)"></label>
      {second}{option}<datalist id="airports"><option value="London"></datalist>
      <button onclick="search()">Find routes</button>
      <p id="status">Not complete</p><p id="hidden-progress" hidden>pending</p><script>
      window.focusSent = 0;
      async function send(name, field, value) {{
        await fetch('/event',{{method:'POST',headers:{{'Content-Type':'application/json'}},
          body:JSON.stringify({{name,field,value}})}});
        if(name === 'focus') window.focusSent++;
      }}
      function focusClick(el) {{
        send('focus',el.id,el.value);
        if ({str(hidden_progress).lower()}) document.getElementById('hidden-progress').textContent='done';
        if ({str(picker_after).lower()}) el.setAttribute('aria-controls','new-options');
        if ({str(blur_after).lower()}) el.blur();
        if ({str(clone).lower()}) {{
          const next=el.cloneNode(true); el.replaceWith(next); next.focus();
        }}
      }}
      async function search() {{
        await send('search','left',document.getElementById('left').value);
        document.getElementById('status').textContent='Routes from London';
      }}
      </script>'''

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            raw = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            event = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                events.append(event)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/", events
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def fields(context, operation="click"):
    return {key: row for key, row in context["candidates"].get(operation, {}).items()
            if row["name"] == "From"}


def choose(context, operation, name, *, focused=None, value=None):
    rows = context["candidates"][operation]
    matches = [key for key, row in rows.items() if row["name"] == name
               and (focused is None or row["context"]["focused"] is focused)
               and (value is None or row["node"]["value"] == value)]
    assert len(matches) == 1, (operation, name, matches)
    return operation, matches[0]


class Provider(ScriptedProvider):
    def __init__(self, steps):
        super().__init__([])
        self.steps = steps
        self.contexts = []
        self.stop_choices = None

    async def decide(self, questions, input_text, **kwargs):
        kwargs["budget"].reserve_provider(len(input_text.encode()))
        kwargs["budget"].consume_output(1)
        context = json.loads(input_text)
        self.contexts.append(context)
        operation, target = self.steps[self.calls](context)
        self.calls += 1
        if operation in {"DONE", "UNABLE"}:
            self.stop_choices = next(q.choices for q in questions if q.name == "operation")
            return Decision(operation, None, 1.)
        head = kwargs["target_heads"][operation]
        assert target in next(q.choices for q in questions if q.name == head)
        return Decision(operation, target, 1.)


async def run(url, provider, *, checks=None, value="London"):
    host = runner.RunManager(lambda: provider, BrowserDriver, admission_mode="task_scope")
    request = TaskRequest(goal="Search routes from London.", start_url=url,
                          values={"From": value},
                          checks=([Check(kind="text", target="#status", expected="Routes from London")]
                                  if checks is None else checks),
                          limits=Limits(max_seconds=20), privacy=PrivacyPolicy())
    try:
        run_id = host.start_task(request)
        result = await finish(host, run_id)
        state = host._run
        return result, state
    finally:
        await host.aclose()


async def test_redundant_focused_matching_click_omits_only_that_click_then_searches():
    def after_click(context):
        assert fields(context) == {}
        assert len(fields(context, "fill")) == 1
        return choose(context, "click", "Find routes")

    provider = Provider([
        lambda c: choose(c, "fill", "From"),
        lambda c: choose(c, "click", "From", focused=True), after_click,
    ])
    with site() as (url, events):
        result, state = await run(url, provider)
        assert (result.terminal, result.functional) == ("verified", "verified")
        assert provider.calls == result.usage.calls == 3
        assert result.usage.steps == 4
        assert state.focus_click_exclusion is None
        assert [e["name"] for e in events] == ["input", "focus", "search"]
        assert events[-1]["value"] == "London"


async def test_first_legitimate_focus_click_remains_available_before_redundant_click():
    def first(context):
        assert next(iter(fields(context).values()))["context"]["focused"] is False
        return choose(context, "click", "From")

    def second(context):
        assert len(fields(context)) == 1
        return choose(context, "click", "From", focused=True)

    def third(context):
        assert not fields(context)
        return choose(context, "click", "Find routes")

    with site(value="London") as (url, events):
        result, _state = await run(url, Provider([first, second, third]))
        assert result.terminal == "verified"
        assert [e["name"] for e in events] == ["focus", "focus", "search"]


@pytest.mark.parametrize("attributes,value,options", [
    ("", "", False),
    ("readonly", "London", False),
    ('role="combobox" aria-expanded="false"', "London", False),
    ('aria-expanded="false"', "London", False),
    ('list="airports"', "London", False),
    ('aria-autocomplete="list"', "London", False),
    ('aria-controls="suggestions"', "London", False),
    ('aria-haspopup="listbox"', "London", False),
    ('href="/other"', "London", False),
    ("", "London", True),
])
async def test_other_fields_and_picker_semantics_keep_their_click(attributes, value, options):
    def retained(context):
        assert len(fields(context)) == 1
        if "href" in attributes:
            assert any(row["destination"].endswith("/other")
                       for row in context["candidates"]["navigate"].values())
        return "UNABLE", None

    provider = Provider([lambda c: choose(c, "click", "From"),
                         lambda c: choose(c, "click", "From", focused=True), retained])
    with site(attributes=attributes, value=value, options=options) as (url, events):
        result, state = await run(url, provider)
        assert result.terminal == "verification_failed"
        assert provider.stop_choices is not None and "UNABLE" in provider.stop_choices
        assert "DONE" not in provider.stop_choices
        assert state.stop_choice == "UNABLE"
        assert state.focus_click_exclusion is None
        assert [e["name"] for e in events] == ["focus", "focus"]


async def test_native_date_picker_keeps_its_click():
    def retained(context):
        assert len(fields(context)) == 1
        return "UNABLE", None

    provider = Provider([lambda c: choose(c, "click", "From"),
                         lambda c: choose(c, "click", "From", focused=True), retained])
    with site(attributes='type="date"', value="2026-09-20") as (url, events):
        result, state = await run(url, provider, value="2026-09-20")
        assert result.terminal == "verification_failed"
        assert provider.stop_choices is not None and "UNABLE" in provider.stop_choices
        assert "DONE" not in provider.stop_choices
        assert state.stop_choice == "UNABLE"
        assert state.focus_click_exclusion is None
        assert [e["name"] for e in events] == ["focus", "focus"]


async def test_unoffered_early_done_is_refused_without_dispatching_another_click():
    def retained(context):
        assert len(fields(context)) == 1
        return "DONE", None

    provider = Provider([lambda c: choose(c, "click", "From"),
                         lambda c: choose(c, "click", "From", focused=True), retained])
    with site(attributes="readonly", value="London") as (url, events):
        result, state = await run(url, provider)
        assert provider.stop_choices is not None and "DONE" not in provider.stop_choices
        assert "UNABLE" in provider.stop_choices
        assert result.terminal == "failed" and result.functional == "failed"
        assert "runner:helper_schema" in result.evidence
        assert state.stop_choice is None
        assert state.focus_click_exclusion is None
        assert provider.calls == result.usage.calls == 3
        assert result.usage.steps == 3
        assert [e["name"] for e in events] == ["focus", "focus"]


@pytest.mark.parametrize("change", ["picker_after", "blur_after"])
async def test_post_click_picker_or_focus_change_prevents_exclusion(change):
    def retained(context):
        assert len(fields(context)) == 1
        field = next(iter(fields(context).values()))
        assert (field["context"].get("picker") is True) if change == "picker_after" else not field["context"]["focused"]
        return choose(context, "click", "Find routes")

    provider = Provider([lambda c: choose(c, "fill", "From"),
                         lambda c: choose(c, "click", "From", focused=True), retained])
    with site(**{change: True}) as (url, events):
        result, _state = await run(url, provider)
        assert result.terminal == "verified"
        assert [e["name"] for e in events] == ["input", "focus", "search"]


async def test_wait_observation_restores_click_eligibility():
    def wait(context):
        assert not fields(context)
        return "wait", "short"

    def restored(context):
        assert len(fields(context)) == 1
        return choose(context, "click", "Find routes")

    provider = Provider([lambda c: choose(c, "fill", "From"),
                         lambda c: choose(c, "click", "From", focused=True), wait, restored])
    with site() as (url, events):
        result, _state = await run(url, provider)
        assert result.terminal == "verified"
        assert [e["name"] for e in events] == ["input", "focus", "search"]


async def test_unchecked_goal_keeps_all_focus_click_choices():
    def retained(context):
        assert len(fields(context)) == 1
        assert "completion" not in context
        return "DONE", None

    provider = Provider([lambda c: choose(c, "fill", "From"),
                         lambda c: choose(c, "click", "From", focused=True), retained])
    with site() as (url, events):
        result, state = await run(url, provider, checks=[])
        assert result.terminal == "completed_unverified"
        assert provider.stop_choices is not None and "DONE" in provider.stop_choices
        assert state.stop_choice == "DONE"
        assert state.focus_click_exclusion is None
        assert [e["name"] for e in events] == ["input", "focus"]


async def test_independent_hidden_check_progress_restores_the_click():
    def retained(context):
        assert len(fields(context)) == 1
        assert context["completion"] == {"total": 2, "pending": 1}
        return choose(context, "click", "Find routes")

    provider = Provider([lambda c: choose(c, "fill", "From"),
                         lambda c: choose(c, "click", "From", focused=True), retained])
    with site(hidden_progress=True) as (url, events):
        result, state = await run(url, provider, checks=[
            Check(kind="text", target="#status", expected="Routes from London"),
            Check(kind="text", target="#hidden-progress", expected="done"),
        ])
        assert result.terminal == "verified"
        assert state.focus_click_exclusion is None
        assert [e["name"] for e in events] == ["input", "focus", "search"]


async def test_duplicate_labels_only_omit_clicked_backend_node():
    def remaining(context):
        assert len(fields(context)) == 1
        return choose(context, "click", "From", focused=False)

    provider = Provider([lambda c: choose(c, "fill", "From", value=""),
                         lambda c: choose(c, "click", "From", focused=True), remaining,
                         lambda c: choose(c, "click", "Find routes")])
    with site(duplicate=True) as (url, events):
        result, _state = await run(url, provider)
        assert result.terminal == "verified"
        assert [(e["name"], e["field"]) for e in events] == [
            ("input", "left"), ("focus", "left"), ("focus", "right"), ("search", "left"),
        ]


async def test_identical_replacement_refocused_node_is_not_omitted():
    def replacement(context):
        assert len(fields(context)) == 1
        assert next(iter(fields(context).values()))["context"]["focused"] is True
        return choose(context, "click", "Find routes")

    provider = Provider([lambda c: choose(c, "fill", "From"),
                         lambda c: choose(c, "click", "From", focused=True), replacement])
    with site(clone=True) as (url, events):
        result, _state = await run(url, provider)
        assert result.terminal == "verified"
        assert [e["name"] for e in events] == ["input", "focus", "search"]


async def test_unknown_click_acknowledgement_never_installs_exclusion_or_replays(monkeypatch):
    class LostAck(DOMSession):
        async def _native(self, action):
            result = await super()._native(action)
            if action.operation == "click":
                await self._page.wait_for_function("window.focusSent === 1")
                raise RuntimeError("owned acknowledgement lost")
            return result

    monkeypatch.setattr(runner, "DOMSession", LostAck)
    provider = Provider([lambda c: choose(c, "fill", "From"),
                         lambda c: choose(c, "click", "From", focused=True)])
    with site() as (url, events):
        result, state = await run(url, provider)
        assert result.terminal == "outcome_unknown"
        assert state.focus_click_exclusion is None
        assert provider.calls == result.usage.calls == 2
        assert [e["name"] for e in events] == ["input", "focus"]


async def test_target_identity_is_current_document_backend_identity_without_io():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, chromium_sandbox=True, channel="chromium")
        try:
            page = await browser.new_page()
            await page.set_content('<input aria-label="From" value="London">')
            dom = DOMSession(page)
            try:
                first = await dom.observe()
                token = next(iter(first.candidates))
                identity = dom._target_identity(token)
                assert identity[0] == first.document_id and type(identity[1]) is int
                second = await dom.observe()
                assert dom._target_identity(token) is None
                assert dom._target_identity(next(iter(second.candidates))) == identity
                await page.locator("input").evaluate("el => el.replaceWith(el.cloneNode(true))")
                third = await dom.observe()
                assert dom._target_identity(next(iter(third.candidates))) != identity
            finally:
                await dom.close()
                assert dom._target_identity(token) is None
        finally:
            await browser.close()
