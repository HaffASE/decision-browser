"""Reusable owned workflows: real browser/runner, independent HTTP event oracle.

The default policy is scripted, not a live model. The optional provider factory
is the only seam used by the development scenario CLI. Expected events and the
script are never given to a configured model. ``delayed_menu`` delays revealing
a normal link; late unowned pushState remains a negative navigation contract.
Default scripted runs hold two asynchronous reveals until the policy observes
their absent targets and chooses WAIT; configured runs use ordinary 500 ms timers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import parse_qs, urlsplit

from decision_browser.browser import BrowserDriver
from decision_browser.contracts import Check, Limits, PrivacyPolicy, TaskRequest
from decision_browser.decision_codec import Decision, compile_questions
from decision_browser.runner import ProviderPort, RunManager

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "scenarios" / "workflows.html"


@dataclass(frozen=True)
class Step:
    operation: str
    name: str
    value: str | None = None


@dataclass(frozen=True)
class Scenario:
    id: str
    goal: str
    values: dict[str, str]
    plan: tuple[Step, ...]
    expected_status: str
    expected_events: tuple[dict, ...]
    final_path: str = ""
    value_checks: tuple[tuple[str, str], ...] = ()

    def checks(self, url: str) -> list[Check]:
        return [
            Check(kind="text", target="#status", expected=self.expected_status),
            Check(kind="route", expected=url + self.final_path),
            *(Check(kind="value", target=target, expected=value)
              for target, value in self.value_checks),
        ]


CASES = {
    case.id: case for case in (
        Scenario("hotel", "Find Lisbon stays, choose Design and Free cancellation, then open Casa Flora.",
                 {"Destination": "Lisbon"},
                 (Step("fill", "Destination"), Step("click", "Find stays"),
                  Step("select", "Stay category", "design"), Step("click", "Free cancellation"),
                  Step("click", "Casa Flora")),
                 "Casa Flora | Lisbon | Design | Free cancellation",
                 ({"name": "search", "data": {"destination": "Lisbon"}},
                  {"name": "category", "data": {"category": "design"}},
                  {"name": "cancellation", "data": {"enabled": True}},
                  {"name": "property", "data": {"property": "casa-flora", "destination": "Lisbon",
                                                   "category": "design", "cancellation": "true"}}),
                 "property/casa-flora?destination=Lisbon&category=design&cancellation=true"),
        Scenario("profile_form", "Complete the local profile for Ada Lovelace, ada@example.com, with Express delivery.",
                 {"Full name": "Ada Lovelace", "Email": "ada@example.com"},
                 (Step("fill", "Full name"), Step("fill", "Email"),
                  Step("select", "Delivery", "express"), Step("click", "Confirm profile")),
                 "Profile confirmed | Ada Lovelace | ada@example.com | Express",
                 ({"name": "profile", "data": {"name": "Ada Lovelace", "email": "ada@example.com", "delivery": "express"}},)),
        Scenario("autocomplete", "Choose Zurich Airport (ZRH), not Zurich city or Zurich Airport hotel.",
                 {"Origin": "Zurich"}, (Step("fill", "Origin"), Step("click", "Zurich Airport (ZRH)")),
                 "Selected airport: ZRH", ({"name": "airport", "data": {"value": "Zurich Airport (ZRH)", "code": "ZRH"}},),
                 value_checks=(("#origin", "Zurich Airport (ZRH)"),)),
        Scenario("calendar", "Choose September 20, 2026, and confirm the departure date.", {},
                 (Step("click", "Departure"), Step("click", "September 20, 2026"), Step("click", "Done")),
                 "Departure confirmed: 2026-09-20", ({"name": "date", "data": {"date": "2026-09-20"}},),
                 value_checks=(("#departure", "2026-09-20"),)),
        Scenario("styled_filter", "Enable the 닥터자르트 brand filter.", {}, (Step("click", "닥터자르트"),),
                 "filtered: dr-jart", ({"name": "filter", "data": {"brand": "dr-jart", "enabled": True}},)),
        Scenario("consent", "Reject all optional cookies, then search from London.", {"From": "London"},
                 (Step("click", "Reject all"), Step("fill", "From"), Step("click", "Find routes")),
                 "Routes from London | optional cookies rejected",
                 ({"name": "consent", "data": {"choice": "reject"}},
                  {"name": "routes", "data": {"origin": "London", "consent": "reject"}})),
        Scenario("delayed_menu", "Open the delayed news menu and navigate to Society headlines.", {},
                 (Step("click", "Sections"), Step("click", "Society")),
                 "Society headlines", ({"name": "section", "data": {"section": "society"}},),
                 "news/society?genre=society"),
        Scenario("loading_overlay", "Wait for the service form to load, then search for climate.",
                 {"Service query": "climate"},
                 (Step("click", "Load services"), Step("fill", "Service query"), Step("click", "Search services")),
                 "Services for climate", ({"name": "services", "data": {"query": "climate"}},)),
        Scenario("below_fold", "Scroll to the Evidence section below the first screen and mark it as read.", {},
                 (Step("scroll", "down"), Step("scroll", "down"), Step("click", "Mark evidence as read")),
                 "Evidence section read", ({"name": "evidence", "data": {"visible": True, "scrolled": True}},)),
        Scenario("article", "Search for Godel incompleteness and open the exact incompleteness theorems article, not the biography.",
                 {"Search query": "Godel incompleteness"},
                 (Step("fill", "Search query"), Step("click", "Search articles"),
                  Step("click", "Gödel's incompleteness theorems")),
                 "Gödel's incompleteness theorems",
                 ({"name": "article_search", "data": {"query": "Godel incompleteness"}},
                  {"name": "article", "data": {"slug": "godel-incompleteness"}}),
                 "article/godel-incompleteness"),
    )
}


class ScenarioSite:
    """One isolated loopback origin per run; only server-side events score success."""

    def __init__(self):
        self.events: dict[str, list[dict]] = {}
        self._lock = Lock()
        self._server = None
        self._thread = None

    def __enter__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, body, content_type="text/html; charset=utf-8", status=200):
                raw = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                parsed = urlsplit(self.path)
                parts = parsed.path.strip("/").split("/")
                case_id = parts[0]
                if case_id not in owner.events:
                    self.send_error(404)
                    return
                suffix = "/".join(parts[1:])
                if suffix:
                    query = {key: value[0] for key, value in parse_qs(parsed.query).items()}
                    if suffix.startswith("property/") and case_id == "hotel":
                        owner.record(case_id, {"name": "property", "data": {"property": suffix.split("/")[1], **query}})
                        status = " | ".join(["Casa Flora" if suffix.endswith("casa-flora") else "Casa Marina",
                                             query.get("destination", ""),
                                             "Design" if query.get("category") == "design" else "Standard",
                                             "Free cancellation" if query.get("cancellation") == "true" else "Non-refundable"])
                    elif suffix.startswith("news/") and case_id == "delayed_menu":
                        section = suffix.split("/")[1]
                        owner.record(case_id, {"name": "section", "data": {"section": section}})
                        status = section.title() + " headlines"
                    elif suffix.startswith("article/") and case_id == "article":
                        slug = suffix.split("/")[1]
                        owner.record(case_id, {"name": "article", "data": {"slug": slug}})
                        status = "Gödel's incompleteness theorems" if slug == "godel-incompleteness" else "Kurt Gödel biography"
                    else:
                        self.send_error(404)
                        return
                    self.reply('<!doctype html><meta charset="utf-8"><p id="status">' + status + '</p>')
                    return
                self.reply(_FIXTURE.read_text(encoding="utf-8").replace("__CASE_ID__", case_id))

            def do_POST(self):
                parts = urlsplit(self.path).path.strip("/").split("/")
                if len(parts) != 2 or parts[0] not in owner.events or parts[1] != "events":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    self.send_error(400)
                    return
                event = json.loads(self.rfile.read(length))
                owner.record(parts[0], event)
                self.reply('{"ok":true}', "application/json")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def start(self, case_id: str) -> str:
        if case_id not in CASES or self._server is None or case_id in self.events:
            raise ValueError("new known scenario required on an open site")
        with self._lock:
            self.events[case_id] = []
        return f"http://127.0.0.1:{self._server.server_port}/{case_id}/"

    def record(self, case_id: str, event: dict) -> None:
        with self._lock:
            self.events[case_id].append(event)

    def verify(self, case_id: str) -> dict:
        with self._lock:
            observed = list(self.events[case_id])
        expected = list(CASES[case_id].expected_events)
        success = observed == expected
        if case_id == "hotel":
            # Setting independent filters before/after searching is equally valid.
            # Only opening the property is causal: it must happen after all three
            # prerequisites, with exact values and no duplicate effects.
            packed = lambda rows: sorted(json.dumps(row, sort_keys=True) for row in rows)
            success = (len(observed) == len(expected) and observed[-1] == expected[-1]
                       and packed(observed[:-1]) == packed(expected[:-1]))
        return {"success": success, "expected": expected, "observed": observed}

    def __exit__(self, *_args):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            assert not self._thread.is_alive()


class ScriptedScenarioProvider:
    """Select only visible descriptors, advancing only on acknowledged actions."""

    def __init__(self, plan, on_wait=None, *, terminal="DONE"):
        self.plan = tuple(plan)
        self.on_wait = on_wait
        self.terminal = terminal
        self.stop_choices = None
        self.calls = 0
        self.index = 0
        self._pending = None
        self._seen = set()
        self.wait_calls = 0
        self.waited_for = set()
        self.observed_loading = False

    async def decide(self, questions, input_text, *, budget, privacy,
                     operation_head="operation", target_heads=None):
        compile_questions(questions, input_text)
        budget.reserve_provider(len(input_text.encode()))
        budget.consume_output(1)
        self.calls += 1
        state = json.loads(input_text)
        for record in state["recent_actions"]:
            attempt = record.get("attempt_id")
            if attempt in self._seen:
                continue
            self._seen.add(attempt)
            if (self._pending is not None and record["operation"] == self._pending.operation
                    and (self._pending.operation in {"scroll", "wait"}
                         or record["name"] == self._pending.name)):
                self.index += 1
                self._pending = None
        if self.index == len(self.plan):
            self.stop_choices = next(question.choices for question in questions
                                     if question.name == operation_head)
            return Decision(self.terminal, None, 1.)
        step = self.plan[self.index]
        matches = [(group, token) for group, rows in state["candidates"].items()
                   for token, row in rows.items()
                   if row["operation"] == step.operation and row["name"] == step.name
                   and (step.value is None or row["node"].get("option", {}).get("value") == step.value)]
        if not matches:
            self.wait_calls += 1
            self.waited_for.add(step.name)
            self.observed_loading |= "Loading services" in state["text"]
            if self.on_wait is not None:
                await self.on_wait()
            return Decision("wait", "short", 1.)
        assert len(matches) == 1, (step, matches)
        group, token = matches[0]
        domain = next(question.choices for question in questions if question.name == target_heads[group])
        assert token in domain
        self._pending = step
        return Decision(group, token, 1.)

    async def generate_text(self, *_args, **_kwargs):
        raise AssertionError("Known fixture values must not invoke a text helper")

    async def aclose(self):
        pass


class _ScenarioDriver(BrowserDriver):
    final_url = None
    snapshot = None

    def __init__(self, mode, *, hold_reveal=False):
        super().__init__(mode)
        self._hold_reveal = hold_reveal

    async def start(self):
        await super().start()
        if self._hold_reveal:
            await self.page.add_init_script("window.__holdScenarioReveal = true;")

    async def close(self):
        try:
            if self._state == "open":
                self.final_url = self.page.url
                self.snapshot = await self.page.evaluate("""() => ({
                  status: document.querySelector('#status')?.textContent,
                  values: Object.fromEntries([...document.querySelectorAll('input, select')]
                    .map(el => [el.id, el.type === 'checkbox' ? el.checked : el.value])),
                  options: document.querySelectorAll('[role=option]').length, scrollY,
                  overlayVisible: !!document.querySelector('#overlay:not([hidden])')
                })""")
        finally:
            await super().close()


async def run_scenario(case_id: str, provider_factory: Callable[[], ProviderPort] | None = None,
                       limits: Limits | None = None) -> dict:
    """Run one isolated scenario and return bounded JSON-safe evidence."""
    case = CASES[case_id]
    scripted = provider_factory is None
    driver = _ScenarioDriver("isolated", hold_reveal=scripted)

    async def release_reveal():
        # Only a fixture reveal is released, after WAIT has already been chosen.
        # No click, fill, navigation or success predicate is performed here.
        await driver.page.evaluate("() => window.__releaseScenarioReveal?.()")

    provider = ScriptedScenarioProvider(case.plan, on_wait=release_reveal if scripted else None)
    factory = provider_factory if provider_factory is not None else lambda: provider
    with ScenarioSite() as site:
        url = site.start(case_id)
        manager = RunManager(factory, lambda mode: driver, admission_mode="task_scope")
        request = TaskRequest(goal=case.goal, start_url=url, values=case.values,
                              checks=case.checks(url), mode="isolated", visual=False,
                              limits=limits or Limits(max_seconds=25, max_steps=30, max_provider_calls=25),
                              privacy=PrivacyPolicy())
        try:
            run_id = manager.start_task(request)
            assert manager._run is not None
            task = manager._run.task
            assert task is not None
            try:
                async with asyncio.timeout(request.limits.max_seconds + 5):
                    await task
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            result = manager.get_task(run_id)
        finally:
            await manager.aclose()
        oracle = site.verify(case_id)
        return {"case_id": case_id, "terminal": result.terminal, "functional": result.functional,
                "reason": result.reason, "oracle": oracle, "final_url": driver.final_url,
                "calls": result.usage.calls, "steps": result.usage.steps,
                "provider_mode": "scripted" if scripted else "configured",
                "success": result.terminal == "verified" and result.functional == "verified" and oracle["success"],
                "snapshot": driver.snapshot, "evidence": result.evidence,
                "development_only": result.development_only, "admission_class": result.admission_class,
                "scripted_evidence": ({"wait_calls": provider.wait_calls,
                                       "waited_for": sorted(provider.waited_for),
                                       "observed_loading": provider.observed_loading} if scripted else None)}
