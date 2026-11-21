"""Owned browser scenarios for refusals, unsupported controls and action races.

The decision port is scripted. Chromium, guards, runner policy, the unconfigured
text-helper refusal and the independent loopback event oracle are real. A wall
is not claimed to have a dedicated detector: its unmet task cannot pass on DONE.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
import pytest
from test_runner import ScriptedProvider, finish, record_final_verification

from decision_browser import runner
from decision_browser.browser import BrowserDriver
from decision_browser.contracts import Check, Limits, PrivacyPolicy, TaskRequest
from decision_browser.decision_codec import Decision
from decision_browser.provider import ProviderError
from decision_browser.systemone_provider import SystemOneProvider

_SCRIPT = """<script>
async function record(name, value) {
  const response = await fetch('/event', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({name,value})});
  const event = await response.json();
  document.getElementById('status').textContent = event.status;
}
</script>"""


@contextmanager
def _site(body: str) -> Iterator[tuple[str, dict]]:
    """The provider and DOM cannot manufacture this server's event history."""
    state = {"events": [], "result_visits": 0, "other_visits": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def reply(self, body, content_type="text/html; charset=utf-8"):
            raw = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/":
                content = body + '<p id="status">Not complete</p>' + _SCRIPT
            elif path == "/result":
                with lock:
                    state["result_visits"] += 1
                content = '<p id="status">Newest selected</p>'
            elif path == "/frame":
                content = '<button onclick="parent.record(\'frame\')">Frame action</button>'
            elif path == "/other":
                with lock:
                    state["other_visits"] += 1
                content = "Foreign tab must remain unopened"
            else:
                self.send_error(404)
                return
            self.reply('<!doctype html><html><meta charset="utf-8"><body>' + content + '</body></html>')

        def do_POST(self):
            if self.path != "/event":
                self.send_error(404)
                return
            event = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                state["events"].append(event)
            self.reply(json.dumps({"status": "Report opened"}), "application/json")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _CaptureDriver(BrowserDriver):
    def __init__(self, mode):
        super().__init__(mode)
        self.final_url = None
        self.final_fields = None

    async def close(self):
        try:
            self.final_url = self.page.url
            self.final_fields = await self.page.locator("input").evaluate_all(
                "nodes => Object.fromEntries(nodes.map(n => [n.id, n.value]))"
            )
        finally:
            await super().close()


class _SemanticProvider(ScriptedProvider):
    def __init__(self, plan, mutate: Callable | None = None):
        super().__init__([])
        self.plan = list(plan)
        self.mutate = mutate
        self.contexts = []
        self.targets = []

    async def decide(self, questions, input_text, *, budget, privacy,
                     operation_head="operation", target_heads=None):
        budget.reserve_provider(len(input_text.encode()))
        budget.consume_output(1)
        self.calls += 1
        context = json.loads(input_text)
        self.contexts.append(context)
        operation, name, value = self.plan.pop(0)
        target = None
        if operation not in {"DONE", "UNABLE"}:
            candidates = context["candidates"][operation]
            matches = [key for key, row in candidates.items()
                       if row["name"] == name
                       and (value is None or row["node"]["option"]["value"] == value)]
            assert len(matches) == 1, (name, candidates)
            target = matches[0]
            assert target in next(q for q in questions if q.name == target_heads[operation]).choices
            self.targets.append(target)
        else:
            self.stop_choices = next(q.choices for q in questions if q.name == operation_head)
        if self.mutate is not None:
            await self.mutate(self.calls)
        return Decision(operation, target, 1.0)


async def _run(url, provider, *, checks=None, values=None, goal="Open the report."):
    driver = _CaptureDriver("isolated")
    host = runner.RunManager(lambda: provider, lambda _mode: driver, admission_mode="task_scope")
    request = TaskRequest(
        goal=goal, start_url=url, mode="isolated", values=values or {},
        checks=checks or [Check(kind="text", target="#status", expected="Report opened")],
        limits=Limits(max_seconds=15.0), privacy=PrivacyPolicy(), visual=False,
    )
    try:
        result = await finish(host, host.start_task(request))
        return result, driver
    finally:
        await host.aclose()


async def test_missing_required_value_without_text_helper_never_fills_or_submits():
    """Actual configured helper refusal, not an invented needs_value terminal."""
    requests = []

    def unexpected_http(request):
        requests.append(request)
        raise AssertionError("an unconfigured helper must not make HTTP requests")

    helper = SystemOneProvider(
        endpoint="http://127.0.0.1:1/v1/systemone", model="fixture",
        transport=httpx.MockTransport(unexpected_http),
    )

    class MissingValueProvider(_SemanticProvider):
        helper_calls = 0
        helper_error = None

        async def generate_text(self, prompt, **kwargs):
            self.helper_calls += 1
            try:
                return await helper.generate_text(prompt, **kwargs)
            except ProviderError as exc:
                self.helper_error = exc.code
                raise

    provider = MissingValueProvider([("fill", "Full name", None)])
    body = ('<form onsubmit="event.preventDefault();record(\'submit\',fullName.value)">'
            '<label for="fullName">Full name</label><input id="fullName" required '
            'oninput="record(\'input\',this.value)"><button>Continue</button></form>')
    try:
        with _site(body) as (url, state):
            result, driver = await _run(url, provider, goal="Complete the profile; the name was not supplied.")
            assert (result.terminal, result.functional) == ("failed", "failed")
            assert provider.calls == provider.helper_calls == 1
            assert provider.helper_error == "text_provider_not_configured"
            assert driver.final_fields == {"fullName": ""}
            assert state == {"events": [], "result_visits": 0, "other_visits": 0}
            assert requests == []
    finally:
        await helper.aclose()


async def test_textless_overlay_after_model_choice_blocks_all_click_effects():
    body = '<button id="target" onclick="record(\'target\')">Continue</button>'
    driver = None

    async def overlay(_call):
        await driver.page.evaluate("""() => {
          const cover = document.createElement('div'); cover.id = 'cover';
          cover.style = 'position:fixed;inset:0;z-index:999;background:transparent';
          cover.onclick = () => record('overlay'); document.body.appendChild(cover);
        }""")

    provider = _SemanticProvider([("click", "Continue", None)], overlay)
    with _site(body) as (url, state):
        driver = _CaptureDriver("isolated")
        host = runner.RunManager(lambda: provider, lambda _mode: driver, admission_mode="task_scope")
        try:
            request = TaskRequest(goal="Continue to the report.", start_url=url, values={},
                                  checks=[Check(kind="text", target="#status", expected="Report opened")],
                                  limits=Limits(max_seconds=15.0), privacy=PrivacyPolicy())
            result = await finish(host, host.start_task(request))
            assert result.terminal == "blocked" and result.functional != "verified"
            assert "action_guard:covered" in result.evidence
            assert provider.calls == result.usage.calls == 1
            assert state["events"] == []
        finally:
            await host.aclose()


async def test_identical_cloned_node_drops_old_choice_then_executes_replacement_once():
    body = ('<button id="target" data-generation="original" '
            'onclick="record(this.dataset.generation)">Continue</button>')
    driver = None
    events_before_retry = []

    async def replace(call):
        if call == 1:
            await driver.page.evaluate("""() => {
              const old = document.getElementById('target');
              const replacement = old.cloneNode(true);
              replacement.dataset.generation = 'replacement'; old.replaceWith(replacement);
            }""")
        else:
            events_before_retry.extend(state["events"])

    provider = _SemanticProvider([("click", "Continue", None)] * 2, replace)
    with _site(body) as (url, state):
        driver = _CaptureDriver("isolated")
        host = runner.RunManager(lambda: provider, lambda _mode: driver, admission_mode="task_scope")
        try:
            request = TaskRequest(goal="Continue to the report.", start_url=url, values={},
                                  checks=[Check(kind="text", target="#status", expected="Report opened")],
                                  limits=Limits(max_seconds=15.0), privacy=PrivacyPolicy())
            result = await finish(host, host.start_task(request))
            assert (result.terminal, result.functional) == ("verified", "verified")
            assert provider.calls == result.usage.calls == 2
            assert len(set(provider.targets)) == 2
            assert events_before_retry == []
            assert state["events"] == [{"name": "replacement"}]
            assert result.usage.steps == 2  # Bootstrap plus the one fresh click.
        finally:
            await host.aclose()


async def test_native_select_navigation_lost_ack_is_never_replayed(monkeypatch):
    attempts = []

    class LostSelectAck(runner.DOMSession):
        async def _native(self, action):
            result = await super()._native(action)
            if action.operation == "select":
                attempts.append(action.value)
                await self._page.wait_for_url("**/result?choice=newest", timeout=3000)
                raise RuntimeError("owned select acknowledgement lost after navigation")
            return result

    monkeypatch.setattr(runner, "DOMSession", LostSelectAck)
    body = ('<label for="sort">Sort order</label><select id="sort" onchange="'
            'const selected=this.value;record(\'selected\',selected).then(() =&gt; '
            'location.href=\'/result?choice=\'+selected)">'
            '<option value="oldest">Oldest first</option>'
            '<option value="newest">Newest first</option></select>')
    provider = _SemanticProvider([("select", "Sort order", "newest")])
    with _site(body) as (url, state):
        result, driver = await _run(
            url, provider,
            checks=[Check(kind="route", expected=url + "result?choice=newest")],
            goal="Choose Newest first and inspect its result.",
        )
        assert result.terminal == "outcome_unknown" and result.functional != "verified"
        assert provider.calls == result.usage.calls == 1
        assert result.usage.steps == 2 and attempts == ["newest"]
        assert driver.final_url == url + "result?choice=newest"
        assert state == {"events": [{"name": "selected", "value": "newest"}],
                         "result_visits": 1, "other_visits": 0}


@pytest.mark.parametrize("wall", [True, False], ids=["real-wall", "article-about-access-denied"])
async def test_wall_cannot_pass_done_but_article_wording_does_not_block_controls(wall, monkeypatch):
    final_verification_calls = record_final_verification(monkeypatch)
    body = ('<h1>Access denied</h1><p>This site has blocked access to the report.</p>' if wall else
            '<h1>Access denied: understanding HTTP 403</h1>'
            '<p>An article about why access can be denied.</p>'
            '<button onclick="record(\'report\')">Open report</button>')
    provider = _SemanticProvider([("UNABLE", None, None)] if wall else [("click", "Open report", None)])
    with _site(body) as (url, state):
        result, _driver = await _run(url, provider)
        assert provider.calls == 1
        if wall:
            assert (result.terminal, result.functional) == ("verification_failed", "failed")
            assert "UNABLE" in provider.stop_choices and "DONE" not in provider.stop_choices
            assert "runner:helper_schema" not in result.evidence
            assert final_verification_calls == [True]
            assert not provider.contexts[0]["candidates"].get("click")
            assert state["events"] == []
        else:
            assert (result.terminal, result.functional) == ("verified", "verified")
            assert final_verification_calls == []
            assert state["events"] == [{"name": "report"}]


async def test_wall_refuses_unoffered_done_without_final_verification(monkeypatch):
    final_verification_calls = record_final_verification(monkeypatch)
    provider = _SemanticProvider([("DONE", None, None)])
    body = '<h1>Access denied</h1><p>This site has blocked access to the report.</p>'
    with _site(body) as (url, state):
        result, driver = await _run(url, provider)
        assert "DONE" not in provider.stop_choices and "UNABLE" in provider.stop_choices
        assert result.terminal == "failed" and result.functional == "failed"
        assert "runner:helper_schema" in result.evidence
        assert final_verification_calls == []
        assert provider.calls == result.usage.calls == 1 and result.usage.steps == 1
        assert state == {"events": [], "result_visits": 0, "other_visits": 0}
        assert driver.final_url == url


_UNSUPPORTED = {
    "iframe": '<iframe title="Frame action" src="/frame" style="width:200px;height:100px"></iframe>',
    "shadow": ('<div id="host" tabindex="0" style="width:200px;height:100px"></div>'
               '<script>host.attachShadow({mode:"open"}).innerHTML='
               '"<button onclick=\\"window.record(\'shadow\')\\">Shadow action</button>";</script>'),
    "newwindow": '<a href="/other" target="_blank">New tab action</a>',
    "file": '<input id="upload" type="file" aria-label="Upload" onchange="record(\'upload\')">',
    "canvas": '<canvas width="200" height="100" onclick="record(\'canvas\')"></canvas>',
    "password": '<input id="password" type="password" aria-label="Password" value="not-for-model" '
                'oninput="record(\'password\')">',
}


@pytest.mark.parametrize("mechanism", _UNSUPPORTED)
@pytest.mark.parametrize("terminal", ["DONE", "UNABLE"])
async def test_unsupported_workflow_excludes_controls_and_never_causes_effects(
    mechanism, terminal, monkeypatch,
):
    final_verification_calls = record_final_verification(monkeypatch)
    provider = _SemanticProvider([(terminal, None, None)])
    with _site(_UNSUPPORTED[mechanism]) as (url, state):
        result, driver = await _run(url, provider, goal="Complete the displayed unsupported workflow.")
        assert "DONE" not in provider.stop_choices and "UNABLE" in provider.stop_choices
        if terminal == "DONE":
            assert (result.terminal, result.functional) == ("failed", "failed")
            assert "runner:helper_schema" in result.evidence
            assert final_verification_calls == []
        else:
            assert (result.terminal, result.functional) == ("verification_failed", "failed")
            assert "runner:helper_schema" not in result.evidence
            assert "model:unable" in result.evidence
            assert final_verification_calls == [True]
        context = provider.contexts[0]
        assert mechanism in context["excluded_mechanisms"]
        assert all(not context["candidates"].get(op) for op in ("click", "fill", "select", "submit", "enter"))
        assert "not-for-model" not in json.dumps(context)
        assert provider.calls == result.usage.calls == 1 and result.usage.steps == 1
        assert state == {"events": [], "result_visits": 0, "other_visits": 0}
        assert driver.final_url == url


async def test_hover_only_menu_cannot_execute_an_unoffered_operation():
    class HoverProvider(_SemanticProvider):
        async def decide(self, *args, **kwargs):
            decision = await super().decide(*args, **kwargs)
            return Decision("hover", decision.target, 1.0)

    body = ('<style>#menu{display:none}#category:hover+#menu{display:block}</style>'
            '<button id="category" onmouseenter="record(\'hover\')">Categories</button>'
            '<div id="menu"><button onclick="record(\'submenu\')">Skin care</button></div>')
    provider = HoverProvider([("click", "Categories", None)])
    with _site(body) as (url, state):
        result, _driver = await _run(url, provider, goal="Open Skin care from the hover-only menu.")
        assert result.terminal == "failed" and result.functional != "verified"
        assert provider.calls == 1 and result.usage.steps == 1
        assert "hover" not in provider.contexts[0]["candidates"]
        assert all(row["name"] != "Skin care"
                   for row in provider.contexts[0]["candidates"].get("click", {}).values())
        assert state == {"events": [], "result_visits": 0, "other_visits": 0}
