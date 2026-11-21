"""Scripted/development runner chains on an owned loopback page, not live acceptance.

The provider selects by the descriptors the runner sends, never by DOM token or
choice position. Native dispatch, policy, journaling, and checks stay real;
admission is the explicit development fixture mapping, never a human grant.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Page
from test_runner import finish

from decision_browser import runner
from decision_browser.browser import BrowserDriver
from decision_browser.contracts import Check, Limits, PrivacyPolicy, TaskRequest
from decision_browser.decision_codec import Decision, compile_questions
from decision_browser.runner import DevelopmentConfig, RunManager

UNICODE_VALUE = "Καλημέρα, 世界 🌱"
HTML = """<!doctype html><html><head><meta charset="utf-8">
<link rel="icon" href="data:,"></head><body>
<input id="first-field" aria-label="First field" value="untouched">
<input id="unicode-field" aria-label="Unicode field" value="seed">
<select id="first-menu" aria-label="First menu">
  <option value="first">First label</option><option value="other">Other label</option>
</select>
<select id="target-menu" aria-label="Target menu">
  <option value="seed-code">Initial label</option>
  <option value="violet-code">Violet label</option>
</select>
<a href="/left">Details</a><a href="/right">Details</a>
<button id="changing" type="button" aria-label="Old choice">Old choice</button>
<p id="route-content">Home content</p><p id="wait-state">idle</p>
<p id="old-clicks">0</p><p id="fresh-clicks">0</p>
<div style="height:5000px"></div>
<script>
window.controlEvents = {"first-field": 0, "unicode-field": 0,
                        "first-menu": 0, "target-menu": 0};
for (const id of ["first-field", "unicode-field", "first-menu", "target-menu"]) {
  document.getElementById(id).addEventListener(id.includes("field") ? "input" : "change",
    () => { window.controlEvents[id]++; });
}
document.getElementById("changing").addEventListener("click", event => {
  const id = event.currentTarget.getAttribute("aria-label") === "Fresh choice"
    ? "fresh-clicks" : "old-clicks";
  const count = document.getElementById(id);
  count.textContent = String(Number(count.textContent) + 1);
});
</script></body></html>"""


@pytest.fixture
def controls_site():
    """Owned IPv4 listener; unrelated paths are 404 and route hits are recorded."""
    hits = Counter()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            hits[path] += 1
            body = {
                "/": HTML,
                "/left": HTML.replace("Home content", "Left content"),
                "/right": HTML.replace("Home content", "Right content"),
            }.get(path)
            data = (body or "Not found").encode("utf-8")
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


class SemanticProvider:
    """Finite script matched against provider-visible semantics, with no API calls."""

    def __init__(self, steps, during_decide=None):
        self.steps = steps
        self.during_decide = during_decide
        self.calls = 0
        self.selected = []
        self.helper_calls = 0

    async def decide(
        self,
        questions,
        input_text,
        *,
        budget,
        privacy,
        operation_head="operation",
        target_heads=None,
    ):
        compile_questions(questions, input_text)
        budget.reserve_provider(len(input_text.encode()))
        budget.consume_output(1)
        step = self.steps[self.calls]  # Unexpected extra decisions fail the test.
        self.calls += 1
        operation, match = step
        groups = json.loads(input_text)["candidates"]
        matches = []
        for group, descriptors in groups.items():
            for key, descriptor in descriptors.items():
                if descriptor["operation"] == operation and match(descriptor):
                    matches.append((group, key, descriptor))
        assert len(matches) == 1, matches
        group, key, descriptor = matches[0]
        domain = next(q.choices for q in questions if q.name == target_heads[group])
        assert key in domain
        self.selected.append((key, descriptor, domain.index(key)))
        if self.during_decide is not None:
            await self.during_decide(self.calls)
        return Decision(group, key, 1.0)

    async def generate_text(self, *_args, **_kwargs):
        self.helper_calls += 1
        raise AssertionError("Known values must never use the text helper")

    async def aclose(self):
        pass


class ControlDriver(BrowserDriver):
    """Independent close-time reads survive normal runner cleanup."""

    def __init__(self, mode):
        assert mode == "isolated"
        super().__init__(mode)
        self.snapshot = None
        self.navigation_urls = []

    async def start(self):
        await super().start()
        self.page.on(
            "framenavigated",
            lambda frame: (
                self.navigation_urls.append(frame.url)
                if frame == self.page.main_frame
                else None
            ),
        )

    async def close(self):
        try:
            if self._state == "open":
                self.snapshot = await self.page.evaluate("""() => ({
                  url: location.href, content: document.getElementById('route-content').textContent,
                  values: Object.fromEntries([...document.querySelectorAll('input, select')]
                    .map(el => [el.id, el.value])), events: window.controlEvents,
                  scrollY, waitState: document.getElementById('wait-state').textContent,
                  oldClicks: Number(document.getElementById('old-clicks').textContent),
                  freshClicks: Number(document.getElementById('fresh-clicks').textContent)
                })""")
        finally:
            await super().close()


@pytest.fixture
def native_trace(monkeypatch):
    """Observe the real native boundary without replacing its behavior."""
    calls, guards, scroll_positions = [], [], []

    class TracedSession(runner.DOMSession):
        async def guard(self, action):
            verdict = await super().guard(action)
            guards.append((action, verdict.status))
            return verdict

        async def _native(self, action):
            calls.append(action)
            if action.operation == "scroll":
                scroll_positions.append(await self._page.evaluate("scrollY"))
            result = await super()._native(action)
            if action.operation == "scroll":
                scroll_positions.append(await self._page.evaluate("scrollY"))
            return result

    monkeypatch.setattr(runner, "DOMSession", TracedSession)
    return calls, guards, scroll_positions


def make_run(site, provider, checks, *, values=None, max_calls=1):
    origin, _hits = site
    drivers = []

    def factory(mode):
        driver = ControlDriver(mode)
        drivers.append(driver)
        return driver

    actions = frozenset(
        {
            ("navigate", "__start__"),
            ("navigate", origin + "/left"),
            ("navigate", origin + "/right"),
            ("fill", "First field"),
            ("fill", "Unicode field"),
            ("select", "First menu"),
            ("select", "Target menu"),
            ("scroll", "up"),
            ("scroll", "down"),
            ("click", "Old choice"),
            ("click", "Fresh choice"),
        }
    )
    host = RunManager(
        lambda: provider, factory, development_config=DevelopmentConfig(origin, actions)
    )
    request = TaskRequest(
        goal="Exercise the owned control fixture.",
        start_url=origin + "/",
        values={} if values is None else values,
        checks=checks,
        privacy=PrivacyPolicy(send_images=False),
        visual=False,
        limits=Limits(max_steps=2, max_provider_calls=max_calls, max_seconds=15),
    )
    return host, request, drivers


def assert_evidence(host, result, provider, trace, operation, *, calls=1):
    assert result.terminal == "verified" and result.functional == "verified"
    assert result.development_only is True and result.effect_guarantee == "none"
    assert result.visual_diagnostic == "not_requested"
    assert result.usage.steps == 2 and result.usage.calls == calls == provider.calls
    assert provider.helper_calls == 0
    assert [action.operation for action in trace[0]] == ["navigate", operation]
    events = [
        (key, json.loads(host.read_artifact(result.run_id, key).content))
        for key in result.artifact_ids
    ]
    attempts = [
        (key, event) for key, event in events if event.get("event") == "before_dispatch"
    ]
    outcomes = [event for _key, event in events if event.get("event") == "outcome"]
    assert len(attempts) == len(outcomes) == 2
    assert [
        json.loads(event["semantic_fingerprint"])["semantic"]["operation"]
        for _key, event in attempts
    ] == ["navigate", operation]
    assert Counter(event["attempt_id"] for event in outcomes) == Counter(
        key for key, _ in attempts
    )
    assert all(event["status"] == "acknowledged" for event in outcomes)
    assert all(event["run_id"] == result.run_id for _key, event in attempts)
    assert all(event["run_id"] == result.run_id for event in outcomes)


async def test_runner_fills_nonfirst_unicode_field_once(controls_site, native_trace):
    provider = SemanticProvider(
        [("fill", lambda item: item["name"] == "Unicode field")]
    )
    host, request, drivers = make_run(
        controls_site,
        provider,
        [
            Check(kind="value", target="#unicode-field", expected=UNICODE_VALUE),
        ],
        values={"First field": "wrong field", "Unicode field": UNICODE_VALUE},
    )
    result = await finish(host, host.start_task(request))
    assert_evidence(host, result, provider, native_trace, "fill")
    assert provider.selected[0][2] > 0
    assert native_trace[0][-1].value == UNICODE_VALUE
    state = drivers[0].snapshot
    assert state["values"]["unicode-field"] == UNICODE_VALUE
    assert state["values"]["first-field"] == "untouched"
    assert state["events"] == {
        "first-field": 0,
        "unicode-field": 1,
        "first-menu": 0,
        "target-menu": 0,
    }


async def test_runner_selects_nonfirst_menu_and_option_by_label(
    controls_site, native_trace
):
    provider = SemanticProvider(
        [
            (
                "select",
                lambda item: (
                    item["name"] == "Target menu"
                    and item["node"]["option"]["label"] == "Violet label"
                ),
            )
        ]
    )
    host, request, drivers = make_run(
        controls_site,
        provider,
        [
            Check(kind="value", target="#target-menu", expected="violet-code"),
        ],
    )
    result = await finish(host, host.start_task(request))
    assert_evidence(host, result, provider, native_trace, "select")
    assert provider.selected[0][2] > 0 and provider.selected[0][0].endswith(":option:1")
    assert native_trace[0][-1].value == "violet-code"
    state = drivers[0].snapshot
    assert state["values"]["target-menu"] == "violet-code"
    assert state["values"]["first-menu"] == "first"
    assert state["events"] == {
        "first-field": 0,
        "unicode-field": 0,
        "first-menu": 0,
        "target-menu": 1,
    }


async def test_runner_navigates_by_destination_without_wrong_route(
    controls_site, native_trace
):
    origin, hits = controls_site
    wanted = origin + "/right"
    provider = SemanticProvider(
        [("navigate", lambda item: item["destination"] == wanted)]
    )
    host, request, drivers = make_run(
        controls_site,
        provider,
        [
            Check(kind="route", target="", expected=wanted),
            Check(kind="text", target="#route-content", expected="Right content"),
        ],
    )
    result = await finish(host, host.start_task(request))
    assert_evidence(host, result, provider, native_trace, "navigate")
    assert provider.selected[0][2] == 2  # Start URL, left route, right route.
    assert [action.destination for action in native_trace[0]] == [origin + "/", wanted]
    assert drivers[0].navigation_urls == [origin + "/", wanted]
    assert drivers[0].snapshot["url"] == wanted
    assert drivers[0].snapshot["content"] == "Right content"
    assert hits["/"] == hits["/right"] == 1 and hits["/left"] == 0


async def test_runner_scroll_moves_independently_of_wait(controls_site, native_trace):
    provider = SemanticProvider([("scroll", lambda item: item["name"] == "down")])
    # Empty checks intentionally cannot turn movement evidence into verification.
    host, request, drivers = make_run(controls_site, provider, [])
    result = await finish(host, host.start_task(request))
    assert result.terminal == "limit_exceeded" and result.functional == "unverified"
    assert result.development_only is True and result.effect_guarantee == "none"
    assert result.usage.steps == 2 and result.usage.calls == provider.calls == 1
    assert [(a.operation, a.value) for a in native_trace[0]] == [
        ("navigate", None),
        ("scroll", "down"),
    ]
    state = drivers[0].snapshot
    assert native_trace[2] == [0, state["scrollY"]]
    assert state["scrollY"] > 0
    assert state["waitState"] == "idle" and not any(state["events"].values())


async def test_runner_wait_dispatches_bounded_delay_and_observes_state(
    controls_site,
    native_trace,
    monkeypatch,
):
    waits = []
    original_wait = Page.wait_for_timeout

    async def observed_wait(page, milliseconds):
        waits.append(milliseconds)
        # Arm only when native wait begins, never before or during model decide.
        await page.evaluate("""() => {
          const state = document.getElementById('wait-state');
          state.textContent = 'armed';
          window.controlWaitReady = new Promise(resolve => {
            setTimeout(() => { state.textContent = 'ready'; resolve(); }, 0);
          });
        }""")
        await original_wait(page, milliseconds)
        # Renderer scheduling is not an elapsed-time assertion: separately await
        # the owned signal before the runner's quota-bound functional check.
        async with asyncio.timeout(2):
            await page.evaluate("() => window.controlWaitReady")

    monkeypatch.setattr(Page, "wait_for_timeout", observed_wait)
    provider = SemanticProvider([("wait", lambda item: item["name"] == "short")])
    host, request, drivers = make_run(
        controls_site,
        provider,
        [
            Check(kind="text", target="#wait-state", expected="ready"),
        ],
    )
    result = await finish(host, host.start_task(request))
    assert_evidence(host, result, provider, native_trace, "wait")
    assert waits == [100] and native_trace[0][-1].value == 100
    state = drivers[0].snapshot
    assert state["waitState"] == "ready" and state["scrollY"] == 0
    assert not any(state["events"].values())


async def test_runner_discards_stale_choice_then_executes_fresh_choice_once(
    controls_site,
    native_trace,
):
    before_fresh = []

    async def mutate_first_decision(call):
        page = drivers[0].page
        if call == 1:
            await page.locator("#changing").evaluate("""el => {
              el.setAttribute('aria-label', 'Fresh choice'); el.textContent = 'Fresh choice';
            }""")
        else:
            before_fresh.append(await page.locator("#fresh-clicks").inner_text())
            assert await page.locator("#old-clicks").inner_text() == "0"
            assert [a.operation for a in native_trace[0]] == ["navigate"]

    provider = SemanticProvider(
        [
            ("click", lambda item: item["name"] == "Old choice"),
            ("click", lambda item: item["name"] == "Fresh choice"),
        ],
        during_decide=mutate_first_decision,
    )
    host, request, drivers = make_run(
        controls_site,
        provider,
        [
            Check(kind="text", target="#fresh-clicks", expected="1"),
            Check(kind="text", target="#old-clicks", expected="0"),
        ],
        max_calls=2,
    )
    result = await finish(host, host.start_task(request))
    assert_evidence(host, result, provider, native_trace, "click", calls=2)
    assert before_fresh == ["0"]
    assert provider.selected[0][0] != provider.selected[1][0]
    assert any(
        action.operation == "click" and status == "stale"
        for action, status in native_trace[1]
    )
    assert drivers[0].snapshot["oldClicks"] == 0
    assert drivers[0].snapshot["freshClicks"] == 1
