"""Owned benchmark regressions: real Chromium and scripted Decisions, no live API."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_runner import ScriptedProvider, finish

from decision_browser.browser import BrowserDriver
from decision_browser.contracts import Check, Limits, PrivacyPolicy, TaskRequest
from decision_browser.decision_codec import Decision
from decision_browser.runner import RunManager

_CASES = {
    "click": (
        '<button type="button" onclick="record(\'activate\')">Activate</button>',
        "Activated", [("click", "Activate", None)],
    ),
    "search": (
        ('<form onsubmit="event.preventDefault();record(\'search\',query.value)">'
         '<label for="query">Search query</label><input id="query" name="query">'
         '<button type="submit">Search</button></form>'),
        "Results for orbital mechanics",
        [("fill", "Search query", None), ("click", "Search", None)],
    ),
    "select": (
        ('<label for="sort">Sort order</label><select id="sort">'
         '<option value="oldest">Oldest first</option>'
         '<option value="newest">Newest first</option></select>'
         '<button type="button" onclick="record(\'sort\',sort.value)">Apply order</button>'),
        "Sorted newest first",
        [("select", "Sort order", "newest"), ("click", "Apply order", None)],
    ),
    "navigate": (
        '<a href="/details">Observatory details</a>',
        "Observatory details opened", [("click", "Observatory details", None)],
    ),
    "counter": (
        '<button type="button" onclick="record(\'increment\')">Increase</button>',
        "Count: 3", [("click", "Increase", None)] * 3,
    ),
}

_SCRIPT = """<script>
let pending = false;
async function record(name, value) {
  if (pending) return;
  pending = true;
  try {
    const response = await fetch('/event', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, value})});
    const data = await response.json();
    document.getElementById('status').textContent = data.status;
  } finally { pending = false; }
}
</script>"""


@contextmanager
def _site(case, redirect=None):
    """Keep the action oracle outside both the provider and the browser DOM."""
    state = {"events": [], "details": 0, "redirected": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def reply(self, body, content_type="text/html; charset=utf-8"):
            raw = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/details":
                with lock:
                    state["details"] += 1
                if redirect is not None:
                    destination = "/unexpected"
                    if redirect == "cross_origin":
                        destination = f"http://localhost:{self.server.server_port}/unexpected"
                    self.send_response(302)
                    self.send_header("Location", destination)
                    self.end_headers()
                    return
                body = '<p id="status">Observatory details opened</p>'
            elif self.path == "/unexpected":
                with lock:
                    state["redirected"] += 1
                body = '<p id="status">Observatory details opened</p>'
            elif self.path == "/":
                body = _CASES[case][0] + '<p id="status">Not complete</p>' + _SCRIPT
            else:
                self.send_error(404)
                return
            self.reply('<!doctype html><html><meta charset="utf-8"><body>' + body + '</body></html>')

        def do_POST(self):
            if self.path != "/event":
                self.send_error(404)
                return
            event = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                state["events"].append(event)
                name, value = event["name"], event.get("value")
                if name == "activate":
                    status = "Activated"
                elif name == "search":
                    status = "Results for " + str(value)
                elif name == "sort":
                    status = "Sorted newest first" if value == "newest" else "Sorted oldest first"
                else:
                    status = f"Count: {len(state['events'])}"
            self.reply(json.dumps({"status": status}), "application/json")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _BenchmarkProvider(ScriptedProvider):
    def __init__(self, actions):
        super().__init__([])
        self.actions = list(actions)

    async def decide(self, questions, input_text, *, budget, privacy,
                     operation_head="operation", target_heads=None):
        budget.reserve_provider(len(input_text.encode()))
        budget.consume_output(1)
        self.calls += 1
        operation, name, value = self.actions.pop(0)
        candidates = json.loads(input_text)["candidates"][operation]
        targets = [key for key, row in candidates.items()
                   if row["name"] == name
                   and (value is None or row["node"]["option"]["value"] == value)]
        assert len(targets) == 1, (operation, name, candidates)
        assert targets[0] in next(q for q in questions if q.name == target_heads[operation]).choices
        return Decision(operation, targets[0], 1.0)


class _FinalURLDriver(BrowserDriver):
    final_url = None

    async def close(self):
        try:
            self.final_url = self.page.url
        finally:
            await super().close()


async def _run(case, url):
    _, expected, actions = _CASES[case]
    provider = _BenchmarkProvider(actions)
    driver = _FinalURLDriver("isolated")
    manager = RunManager(lambda: provider, lambda mode: driver, admission_mode="task_scope")
    request = TaskRequest(
        goal=f"Complete {case} and verify {expected}.", start_url=url, mode="isolated",
        values={"query": "orbital mechanics"} if case == "search" else {},
        checks=[Check(kind="text", target="#status", expected=expected)],
        limits=Limits(max_seconds=15), privacy=PrivacyPolicy(), visual=False,
    )
    try:
        result = await finish(manager, manager.start_task(request))
        return result, provider, driver
    finally:
        await manager.aclose()


@pytest.mark.parametrize("case", _CASES)
async def test_comparison_case_is_verified_by_runner_and_server(case):
    with _site(case) as (url, state):
        result, provider, driver = await _run(case, url)
        assert (result.terminal, result.functional) == ("verified", "verified"), result
        assert result.development_only is False
        assert result.admission_class == "ordinary_task_scoped"
        assert result.usage.calls == provider.calls == len(_CASES[case][2])
        assert result.usage.steps == provider.calls + 1  # Includes initial navigation.
        assert not provider.actions
        expected_events = {
            "click": [{"name": "activate"}],
            "search": [{"name": "search", "value": "orbital mechanics"}],
            "select": [{"name": "sort", "value": "newest"}],
            "navigate": [],
            "counter": [{"name": "increment"}] * 3,
        }
        assert state["events"] == expected_events[case]
        assert state["details"] == (1 if case == "navigate" else 0)
        assert state["redirected"] == 0
        expected_url = url + "details" if case == "navigate" else url
        assert driver.final_url == expected_url


@pytest.mark.parametrize("redirect", ["same_origin", "cross_origin"])
async def test_navigation_redirect_cannot_be_upgraded_by_satisfied_text_check(redirect):
    with _site("navigate", redirect=redirect) as (url, state):
        result, provider, driver = await _run("navigate", url)
        assert result.terminal == "outcome_unknown"
        assert result.functional != "verified"
        assert result.usage.steps == 2  # Bootstrap plus one click, never replayed.
        assert result.usage.calls == provider.calls == 1
        assert state == {"events": [], "details": 1, "redirected": 1}
        assert driver.final_url.endswith("/unexpected")
