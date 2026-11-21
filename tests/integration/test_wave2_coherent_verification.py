"""F07: owned native state must satisfy the complete final check conjunction."""

from __future__ import annotations

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from test_runner import RecordingDriver, finish, record_final_verification, request

from decision_browser.contracts import Check
from decision_browser.decision_codec import Decision
from decision_browser.runner import RunManager
from decision_browser.verification import verify_functional

URL = "https://example.test/expected"
HTML = '<p id="count">0</p><p id="early">old</p>'
MUTATIONS = {
    "route": "history.pushState(null,'','/other');document.body.insertAdjacentHTML('beforeend','<p id=late>ready</p>')",
    "dom": "document.getElementById('early').textContent='new';document.body.insertAdjacentHTML('beforeend','<p id=late>ready</p>')",
}


def checks(kind):
    first = Check(kind="route", expected=URL) if kind == "route" else Check(
        kind="text", target="#early", expected="old",
    )
    return [first, Check(kind="text", target="#late", expected="ready")]


@pytest.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, chromium_sandbox=True, channel="chromium",
        )
        try:
            tab = await browser.new_page()
            await tab.route("**/*", lambda route: route.fulfill(content_type="text/html", body=HTML))
            await tab.goto(URL)
            yield tab
        finally:
            await browser.close()


@pytest.mark.parametrize("kind", ["route", "dom"])
@pytest.mark.parametrize("stable", [False, True])
async def test_native_final_read_rejects_temporally_incompatible_checks(page, kind, stable):
    if stable:
        await page.evaluate("document.body.insertAdjacentHTML('beforeend','<p id=late>ready</p>')")
    else:
        await page.evaluate("setTimeout(() => {" + MUTATIONS[kind] + "}, 100)")
    report = await verify_functional(page, checks(kind))
    assert report.terminal == ("verified" if stable else "verification_failed")
    if not stable:
        assert not all(row.passed for row in report.evidence)
        if kind == "route":
            assert page.url == "https://example.test/other"
        else:
            assert page.url == URL
            assert await page.locator("#early").text_content() == "new"


@pytest.mark.parametrize("kind", ["text", "value", "visible"])
@pytest.mark.parametrize("matches", [False, True])
async def test_final_read_preserves_isolation_from_page_prototype_spoofing(page, kind, matches):
    await page.set_content('''<p id="note">actual</p><input id="field" value="actual">
        <div id="hidden" style="display:none">hidden</div>''')
    await page.evaluate("""() => {
      Object.defineProperty(Node.prototype, 'textContent', {get: () => 'forged'});
      Object.defineProperty(HTMLInputElement.prototype, 'value', {get: () => 'forged'});
      window.getComputedStyle = () => ({display:'block', visibility:'visible'});
      Element.prototype.checkVisibility = () => true;
      Element.prototype.getBoundingClientRect = () => ({width:10,height:10});
    }""")
    target = {"text": "#note", "value": "#field", "visible": "#hidden"}[kind]
    expected = not matches if kind == "visible" else ("actual" if matches else "forged")
    report = await verify_functional(page, [Check(kind=kind, target=target, expected=expected)])
    assert report.terminal == ("verified" if matches else "verification_failed")


@pytest.mark.parametrize("target,expected_terminal", [
    ("#label", "verified"), ("#caption", "verified"),
    ("#field", "verified"), ("#button", "verification_failed"),
])
async def test_final_value_read_preserves_native_label_retargeting(page, target, expected_terminal):
    await page.set_content('''<label id="label" for="field"><span id="caption">Name</span>
      <button id="button">Other control</button></label><input id="field" value="actual">''')
    report = await verify_functional(page, [Check(kind="value", target=target, expected="actual")])
    assert report.terminal == expected_terminal


@pytest.mark.parametrize("kind,expected", [("text", "actual"), ("value", "actual"), ("visible", True), ("count", 2)])
async def test_final_read_preserves_strict_single_target_and_count_domain(page, kind, expected):
    await page.set_content('<input class="duplicate" value="actual"><input class="duplicate" value="actual">')
    report = await verify_functional(page, [Check(kind=kind, target=".duplicate", expected=expected)])
    assert report.terminal == ("verified" if kind == "count" else "verification_failed")


@pytest.mark.parametrize("html,target,expected", [
    ('<div id="target" style="display:contents"><span>visible text</span></div>', "#target", True),
    ('<div id="target" style="opacity:0">transparent text</div>', "#target", True),
    ('<details><summary>Closed</summary><div id="target">closed content</div></details>', "#target", False),
])
async def test_final_visibility_read_preserves_chromium_visibility_contract(page, html, target, expected):
    await page.set_content(html)
    report = await verify_functional(page, [Check(kind="visible", target=target, expected=expected)])
    assert report.terminal == "verified"


async def test_repeated_final_reads_reuse_document_realm_and_detach_sessions(page, monkeypatch):
    create_session = page.context.new_cdp_session
    sessions, contexts = [], []

    async def tracked_session(target):
        session = await create_session(target)
        send = session.send

        async def tracked_send(method, params=None):
            result = await send(method, params)
            if method == "Page.createIsolatedWorld":
                contexts.append(result["executionContextId"])
            return result

        monkeypatch.setattr(session, "send", tracked_send)
        sessions.append(session)
        return session

    monkeypatch.setattr(page.context, "new_cdp_session", tracked_session)
    for _ in range(4):
        report = await verify_functional(page, [Check(kind="text", target="#early", expected="old")])
        assert report.terminal == "verified"
    assert len(contexts) == len(sessions) == 4
    assert len(set(contexts)) == 1
    await page.reload()
    report = await verify_functional(page, [Check(kind="text", target="#early", expected="old")])
    assert report.terminal == "verified"
    assert len(contexts) == len(sessions) == 5
    assert contexts[-1] != contexts[0]
    for session in sessions:
        with pytest.raises(PlaywrightError):
            await session.send("Runtime.evaluate", {"expression": "1"})


class OwnedDriver(RecordingDriver):
    def __init__(self, mode, stable):
        super().__init__(mode)
        self.stable = stable

    async def start(self):
        await super().start()
        body = HTML + ('<p id="late">ready</p>' if self.stable else "")
        await self.page.context.route("**/*", lambda route: route.fulfill(
            content_type="text/html", body=body,
        ))


class ScheduledDoneProvider:
    def __init__(self, drivers, kind, *, terminal="DONE"):
        self.drivers, self.kind, self.calls = drivers, kind, 0
        self.terminal = terminal
        self.stop_choices = None

    async def decide(self, questions, input_text, *, budget, **kwargs):
        budget.reserve_provider(len(input_text.encode()))
        budget.consume_output(1)
        self.calls += 1
        # Initial verification cannot find #late. Schedule the state change
        # immediately before the explicit stop. Only offered UNABLE may enter
        # final verification; custom unoffered DONE is a separate rejection.
        await self.drivers[0].page.evaluate(
            "setTimeout(() => {" + MUTATIONS[self.kind] + "}, 100)"
        )
        self.stop_choices = next(q.choices for q in questions if q.name == "operation")
        return Decision(self.terminal, None, 1.0)

    async def aclose(self):
        pass


@pytest.mark.parametrize("kind", ["route", "dom"])
@pytest.mark.parametrize("stable", [False, True])
@pytest.mark.parametrize("terminal", ["DONE", "UNABLE"])
async def test_public_runner_never_verifies_non_simultaneous_conditions(
    kind, stable, terminal, monkeypatch,
):
    final_verification_calls = record_final_verification(monkeypatch)
    drivers = []
    provider = ScheduledDoneProvider(drivers, kind, terminal=terminal)
    manager = RunManager(
        lambda: provider,
        lambda mode: drivers.append(OwnedDriver(mode, stable)) or drivers[-1],
        admission_mode="task_scope",
    )
    run_id = manager.start_task(request(URL, checks(kind)))
    result = await finish(manager, run_id)
    if stable:
        assert result.terminal == "verified"
        assert provider.stop_choices is None
        assert final_verification_calls == []
    else:
        assert "DONE" not in provider.stop_choices and "UNABLE" in provider.stop_choices
        if terminal == "DONE":
            assert result.terminal == "failed"
            assert "runner:helper_schema" in result.evidence
            assert final_verification_calls == []
        else:
            assert result.terminal == "verification_failed"
            assert "runner:helper_schema" not in result.evidence
            assert "model:unable" in result.evidence
            assert final_verification_calls == [True]
    assert result.functional == ("verified" if stable else "failed")
    assert result.usage.steps == 1
    assert provider.calls == (0 if stable else 1)
