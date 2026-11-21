"""Freshness: lost ack, stale same-node click, journal failure before dispatch."""

from __future__ import annotations

from test_runner import (
    RecordingDriver,
    ScriptedProvider,
    finish,
    manager,
    request,
)

from decision_browser import runner
from decision_browser.artifacts import Journal
from decision_browser.budget import BudgetExceeded


class LostAckSession(runner.DOMSession):
    """Native click applies, then the ack is lost. Navigate still returns."""

    async def _native(self, action):
        result = await super()._native(action)
        if action.operation == "click":
            raise RuntimeError("ack lost")
        return result


class StaleClickProvider(ScriptedProvider):
    """First decision is the live click target; then the inc label changes."""

    def __init__(self) -> None:
        super().__init__(["click", "DONE"])

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
        decision = await super().decide(
            questions,
            input_text,
            budget=budget,
            privacy=privacy,
            operation_head=operation_head,
            target_heads=target_heads,
        )
        if self.calls == 1 and decision.operation == "click":
            page = RecordingDriver.seen[-1].page
            await page.locator("#inc").evaluate(
                "(el) => { el.textContent = 'changed'; }"
            )
        return decision


def _fail_second_before_dispatch(monkeypatch) -> None:
    """Bootstrap navigate is the first journal call. The next one fails closed."""
    issued = {"n": 0}
    original = Journal.before_dispatch

    def before_dispatch(
        self, run_id, action_digest, semantic_fingerprint, admission_class
    ):
        issued["n"] += 1
        if issued["n"] == 2:
            raise BudgetExceeded("stored_bytes")
        return original(
            self,
            run_id,
            action_digest,
            semantic_fingerprint,
            admission_class,
        )

    monkeypatch.setattr(Journal, "before_dispatch", before_dispatch)


def _cancel_twice(host, run_id: str) -> None:
    host.cancel_task(run_id)
    host.cancel_task(run_id)


async def test_lost_ack_keeps_count_and_cancel_does_not_replay(
    counter_page_url, monkeypatch
) -> None:
    monkeypatch.setattr(runner, "DOMSession", LostAckSession)
    RecordingDriver.seen.clear()
    provider = ScriptedProvider(["click"])
    host = manager(counter_page_url, provider)
    run_id = host.start_task(request(counter_page_url))
    result = await finish(host, run_id)
    _cancel_twice(host, run_id)
    again = await finish(host, run_id)
    assert result.terminal == "outcome_unknown"
    assert again.terminal == "outcome_unknown"
    assert again.usage.calls == result.usage.calls == 1
    assert provider.calls == 1
    assert RecordingDriver.seen[-1].observed_count == 1


async def test_semantic_change_during_decide_drops_old_click(
    counter_page_url,
) -> None:
    RecordingDriver.seen.clear()
    provider = StaleClickProvider()
    host = manager(counter_page_url, provider)
    result = await finish(host, host.start_task(request(counter_page_url)))
    assert result.terminal == "completed_unverified"
    assert result.functional == "unverified"
    assert result.usage.calls == 2 and provider.calls == 2
    assert RecordingDriver.seen[-1].observed_count == 0


async def test_journal_before_dispatch_failure_has_zero_effect(
    counter_page_url, monkeypatch
) -> None:
    _fail_second_before_dispatch(monkeypatch)
    RecordingDriver.seen.clear()
    provider = ScriptedProvider(["click", "DONE"])
    host = manager(counter_page_url, provider)
    result = await finish(host, host.start_task(request(counter_page_url)))
    assert result.terminal == "limit_exceeded"
    assert provider.calls == 1 and result.usage.calls == 1
    assert RecordingDriver.seen[-1].observed_count == 0


"""Freshness: own HTML on real Chromium, explicit development fixtures."""

import contextlib

from playwright.async_api import Error

from decision_browser.browser import BrowserBlocked
from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.provider import ProviderError
from decision_browser.runner import DevelopmentConfig, RunManager

_HTML = """<!DOCTYPE html><html><body>
<input id="field" aria-label="Name" value="seed">
<p id="count">0</p>
<script>
document.querySelector("#field").addEventListener("input", () => {
  const node = document.querySelector("#count");
  node.textContent = String(Number(node.textContent) + 1);
});
</script>
</body></html>"""

_ACTIONS = frozenset({("navigate", "__start__"), ("fill", "Name")})


async def _fulfill(route) -> None:
    await route.fulfill(status=200, content_type="text/html", body=_HTML)


class FieldRecordingDriver(RecordingDriver):
    """Saves #field in close before RecordingDriver reads #count and closes."""

    def __init__(self, mode: str) -> None:
        super().__init__(mode)
        self.observed_field: str | None = None

    async def start(self) -> None:
        # Real browser, blank document. Static fixture route before navigation.
        await super().start()
        await self.page.route("http://127.0.0.1:*/**", _fulfill)

    async def close(self) -> None:
        try:
            page = getattr(self, "page", None)
            if page is not None:
                with contextlib.suppress(BrowserBlocked, Error, ValueError):
                    locator = page.locator("#field")
                    if await locator.count():
                        self.observed_field = await locator.input_value(timeout=0)
        finally:
            await super().close()


class TextProvider(ScriptedProvider):
    """fill then DONE. generate_text refuses, mutates, or returns GENERATED."""

    def __init__(self, behavior: str, page_of) -> None:
        super().__init__(["fill", "DONE"])
        self.behavior = behavior
        self._page_of = page_of
        self.helper_calls = 0

    async def generate_text(
        self, prompt, *, budget, privacy, max_text_chars=4096
    ) -> str:
        budget.reserve_provider(len(str(prompt).encode()))
        budget.consume_output(1)
        self.helper_calls += 1
        if self.behavior == "refusal":
            raise ProviderError("refusal")
        if self.behavior == "mutate":
            await (
                self._page_of()
                .locator("[aria-label='Name']")
                .evaluate("(el) => { el.setAttribute('aria-label', 'Replacement'); }")
            )
        return "GENERATED"


def _request(url: str, values: dict[str, str]) -> TaskRequest:
    return TaskRequest(
        goal="Update the counter.",
        start_url=url,
        values=values,
        checks=[],
        limits=Limits(max_seconds=15.0),
        privacy=PrivacyPolicy(),
    )


async def _drive(url: str, behavior: str, values: dict[str, str]):
    drivers: list[FieldRecordingDriver] = []

    def factory(mode: str) -> FieldRecordingDriver:
        driver = FieldRecordingDriver(mode)
        drivers.append(driver)
        return driver

    provider = TextProvider(behavior, lambda: drivers[-1].page)
    host = RunManager(
        lambda: provider,
        factory,
        development_config=DevelopmentConfig(url.rstrip("/"), _ACTIONS),
    )
    result = await finish(host, host.start_task(_request(url, values)))
    return result, provider, drivers[-1]


async def test_explicit_empty_name_skips_helper(counter_page_url: str) -> None:
    result, provider, driver = await _drive(counter_page_url, "unused", {"Name": ""})
    assert (
        result.terminal == "completed_unverified" and result.functional == "unverified"
    )
    assert provider.helper_calls == 0
    assert driver.observed_field == "" and driver.observed_count == 1


async def test_helper_refusal_leaves_seed(counter_page_url: str) -> None:
    result, provider, driver = await _drive(counter_page_url, "refusal", {})
    assert result.terminal in {"failed", "refused"}
    assert result.functional == "not_run"
    assert provider.helper_calls == 1
    assert driver.observed_field == "seed" and driver.observed_count == 0


async def test_helper_mutation_new_decision_done(counter_page_url: str) -> None:
    result, provider, driver = await _drive(counter_page_url, "mutate", {})
    assert (
        result.terminal == "completed_unverified" and result.functional == "unverified"
    )
    assert (
        result.usage.calls == 3 and provider.calls == 2 and provider.helper_calls == 1
    )
    assert driver.observed_field == "seed" and driver.observed_count == 0


import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlsplit

from test_runner import RecordingDriver, ScriptedProvider

from decision_browser import runner
from decision_browser.admission import AdmissionStore, AlphaProposal, BetaProposal
from decision_browser.contracts import Check
from decision_browser.desk import DevelopmentDesk
from decision_browser.policy import Policy


def _origin(url: str) -> str:
    part = urlsplit(url)
    return f"{part.scheme}://{part.netloc}"


class LinkDriver(RecordingDriver):
    """Owned Chromium. Blank until bootstrap; A is only the static review link."""

    page_url = ""
    link_url = ""

    async def start(self) -> None:
        await super().start()
        link_url = type(self).link_url

        async def fulfill(route):
            body = f'<p id="count">0</p><a href="{link_url}" name="Review">Review</a>'
            await route.fulfill(status=200, content_type="text/html", body=body)

        await self.page.route(type(self).page_url, fulfill)


def _bind(hits: list[int]):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if urlsplit(self.path).path != "/result":
                self.send_error(404)
                return
            hits[0] += 1
            body = b'<p id="count">1</p><p id="reached">Reached</p>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread


async def _pending(host, run_id, kind):
    async with asyncio.timeout(5):
        while not isinstance(proposal := host.pending_proposal(run_id), kind):
            await asyncio.sleep(0)
        return proposal


async def test_cross_origin_review_ordinary_beta(
    counter_page_url: str, monkeypatch
) -> None:
    hits, drivers, held, trace = [0], [], {}, []
    httpd, thread = _bind(hits)
    origin_a = _origin(counter_page_url)
    origin_b = f"http://{httpd.server_address[0]}:{httpd.server_address[1]}"
    result_url = f"{origin_b}/result"
    LinkDriver.page_url, LinkDriver.link_url = counter_page_url, result_url
    store, host = AdmissionStore(), None
    original_eval = Policy.evaluate

    class SpySession(runner.DOMSession):
        async def _native(self, action):
            beta = held.get("beta")
            if beta is not None:
                assert store.is_live(beta.proposal_id) is False
                assert held["alpha"].origins == frozenset({origin_a})
            return await super()._native(action)

    def evaluate(self, scope, consume: bool = False):
        decision = original_eval(self, scope, consume=consume)
        if (
            consume
            and scope.operation == "click"
            and scope.destination_origin != scope.document_origin
        ):
            kind = getattr(decision.classification, "value", decision.classification)
            admit = getattr(decision.admission_class, "value", decision.admission_class)
            trace.append((kind, admit))
        return decision

    monkeypatch.setattr(Policy, "evaluate", evaluate)
    monkeypatch.setattr(runner, "DOMSession", SpySession)
    try:
        host = RunManager(
            lambda: ScriptedProvider(["click", "DONE"]),
            lambda mode: drivers.append(LinkDriver(mode)) or drivers[-1],
            admission_store=store,
            desk=DevelopmentDesk(store, ["accept", "accept"]),
        )
        run_id = host.start_task(
            request(
                counter_page_url,
                [
                    Check(kind="route", target="", expected=result_url),
                    Check(kind="text", target="#reached", expected="Reached"),
                ],
                Limits(max_seconds=15.0),
            )
        )
        alpha = held["alpha"] = await _pending(host, run_id, AlphaProposal)
        await host.present_admission(run_id, alpha.proposal_id, alpha.digest)
        beta = await _pending(host, run_id, BetaProposal)
        assert beta.proposal_id != alpha.proposal_id
        assert beta.document_origin == origin_a and beta.destination_origin == origin_b
        assert beta.destination_url == result_url and alpha.origins == frozenset(
            {origin_a}
        )
        held["beta"] = beta
        await host.present_admission(run_id, beta.proposal_id, beta.digest)
        result = await finish(host, run_id)
        assert result.terminal == "verified" and result.functional == "verified"
        assert result.development_only is True and result.effect_guarantee == "none"
        assert (
            getattr(result.admission_class, "value", result.admission_class)
            == "ordinary_beta_admitted"
        )
        assert trace == [("uncertain", "ordinary_beta_admitted")]
        assert hits[0] == 1 and drivers[-1].observed_count == 1
        assert alpha.origins == frozenset({origin_a})
        assert (
            store.consume_beta(
                run_id=beta.run_id,
                proposal_id=beta.proposal_id,
                digest=beta.digest,
                semantic_fingerprint=beta.semantic_fingerprint,
                operation=beta.operation,
                document_origin=beta.document_origin,
                destination_origin=beta.destination_origin,
                destination_url=beta.destination_url,
            )
            is False
            and hits[0] == 1
        )
    finally:
        task = host._run.task if host is not None and host._run is not None else None
        if task is not None and not task.done():
            host.cancel_task(run_id)
            with contextlib.suppress(asyncio.CancelledError):
                async with asyncio.timeout(20):
                    await task
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


from decision_browser.admission import canonical_origin


async def test_ordinary_alpha_admits_click_and_verifies(counter_page_url):
    store = AdmissionStore()
    desk = DevelopmentDesk(store, ["accept"])
    provider = ScriptedProvider(["click", "DONE"])
    host = RunManager(
        provider_factory=lambda: provider,
        driver_factory=lambda mode: RecordingDriver(mode),
        admission_store=store,
        desk=desk,
    )
    req = request(
        counter_page_url,
        [Check(kind="text", target="#count", expected="1")],
    )
    run_id = host.start_task(req)
    try:
        async with asyncio.timeout(5):
            proposal = host.pending_proposal(run_id)
            while proposal is None:
                await asyncio.sleep(0)
                proposal = host.pending_proposal(run_id)
        assert isinstance(proposal, AlphaProposal)
        origins = frozenset({canonical_origin(counter_page_url)})
        assert proposal.origins == origins
        await host.present_admission(run_id, proposal.proposal_id, proposal.digest)
        result = await finish(host, run_id)
        assert result.terminal == "verified"
        assert result.functional == "verified"
        assert result.admission_class == "ordinary_admitted"
        assert result.development_only is True
        assert result.effect_guarantee == "none"
        assert RecordingDriver.seen[-1].observed_count == 1
        assert proposal.origins == origins
        assert store.is_live(proposal.proposal_id) is False
    finally:
        run = host._run
        task = None if run is None else run.task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                async with asyncio.timeout(5):
                    await task


from test_verification import _search_handler


async def test_scripted_development_posts_atlas_once() -> None:
    state = {"submitted": False, "query": "", "posts": 0}

    class CountingHandler(_search_handler(state)):
        def do_POST(self) -> None:
            state["posts"] += 1
            super().do_POST()

    server = ThreadingHTTPServer(("127.0.0.1", 0), CountingHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = None
    run_id = None
    try:
        base = f"http://127.0.0.1:{server.server_address[1]:d}"
        store = AdmissionStore()
        desk = DevelopmentDesk(store, ["accept"])
        provider = ScriptedProvider(["fill", "submit", "DONE"])
        host = RunManager(
            provider_factory=lambda: provider,
            driver_factory=lambda mode: RecordingDriver(mode),
            admission_store=store,
            desk=desk,
        )
        req = request(
            base + "/",
            checks=[
                Check(kind="route", target="", expected=base + "/result"),
                Check(kind="value", target="#query", expected="atlas"),
                Check(kind="text", target="#result", expected="Found atlas"),
            ],
        ).model_copy(update={"goal": "Find atlas", "values": {"Query": "atlas"}})
        run_id = host.start_task(req)
        proposal = await _pending(host, run_id, AlphaProposal)
        await host.present_admission(run_id, proposal.proposal_id, proposal.digest)
        result = await finish(host, run_id)
        assert result.terminal == "verified"
        assert result.functional == "verified"
        assert result.development_only is True
        assert result.effect_guarantee == "none"
        assert result.admission_class == "ordinary_admitted"
        assert state["posts"] == 1
        assert state["submitted"] is True
        assert state["query"] == "atlas"
    finally:
        if host is not None and run_id is not None:
            task = None if host._run is None else host._run.task
            if task is not None and not task.done():
                host.cancel_task(run_id)
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
