"""A URL race inside an acknowledged action must not publish a stale observation."""
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.decision_codec import Decision
from decision_browser.dom import DOMSession
from decision_browser.runner import RunManager, RunnerError

HTML = '''<div role="combobox" tabindex="0" aria-expanded="false" id="trip">Round trip</div>
<li role="option" tabindex="0" aria-selected="false" id="one" hidden>One way</li>
<input role="combobox" aria-label="Where from?" id="airport">
<script>
trip.onclick=()=>{one.hidden=false;trip.setAttribute('aria-expanded','true')};
one.onclick=()=>{trip.textContent='One way';trip.setAttribute('aria-expanded','false');one.hidden=true};
</script>'''


@pytest_asyncio.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, chromium_sandbox=True, channel="chromium")
        try:
            context = await browser.new_context()
            await context.route('**/*', lambda route: route.fulfill(
                status=200, content_type='text/html', body=HTML))
            page = await context.new_page()
            await page.goto('http://127.0.0.1:48761/flights')
            yield page
        finally:
            await browser.close()


class ScriptedProvider:
    def __init__(self):
        self.calls = 0

    async def decide(self, _questions, input_text, **_kwargs):
        self.calls += 1
        payload = json.loads(input_text)
        operation, name = [('click', 'Round trip'), ('click', 'One way'),
                           ('fill', 'Where from?')][self.calls - 1]
        target = next(key for key, item in payload['candidates'][operation].items()
                      if item['name'] == name)
        return Decision(operation, target, 1.0)


def scoped_run(page):
    provider = ScriptedProvider()
    host = RunManager(lambda: provider, admission_mode='task_scope')
    run = host._open(TaskRequest(
        goal='Choose One way, then fill the airport with JFK', start_url=page.url,
        values={'Where from?': 'JFK'}, checks=[],
        limits=Limits(max_seconds=30), privacy=PrivacyPolicy(),
    ))
    host._run = run
    proposal = host._admission.propose_alpha(
        run.result.run_id, [run.request.start_url], ['navigate', 'click', 'fill'],
        run.request.limits.model_dump(), run.budget.original_deadline,
    )
    assert host._admission.admit_task_scope(proposal.proposal_id, proposal.digest)
    run.alpha = proposal
    run.driver, run.dom = SimpleNamespace(page=page), DOMSession(page)
    return host, run, provider


@pytest.mark.asyncio
async def test_owned_same_document_url_race_returns_aligned_observation(page):
    host, run, provider = scoped_run(page)
    dom = run.dom
    try:
        obs = await dom.observe()
        action = await host._choose(run, obs)
        obs = await host._dispatch_one(run, obs, action)
        action = await host._choose(run, obs)
        original_observe = dom.observe
        injected = False

        async def racing_observe():
            nonlocal injected
            captured = await original_observe()
            if not injected:
                injected = True
                # Deterministic native URL change after capture, before the
                # runner reads the live URL inside the acknowledged action.
                await page.evaluate("history.replaceState({}, '', '?trip=oneway')")
            return captured

        dom.observe = racing_observe
        post = await host._dispatch_one(run, obs, action)
        aligned = post.url == page.url
        if not aligned:
            with pytest.raises(RunnerError, match='^stale$'):
                await host._choose(run, post)
            with pytest.raises(RunnerError, match='^navigation$'):
                await host._refresh_stale(run, post)
            assert run.result.evidence == ['stale_refresh:observation_url_changed']
            print(json.dumps({'baseline_failure': run.result.evidence[0],
                              'same_document': obs.document_id == post.document_id,
                              'snapshot_aligned': False, 'synthetic_calls': provider.calls,
                              'acknowledged_effects': run.result.usage.steps}))
        assert aligned, 'An owned SPA transition returned an observation from the old URL'
        assert post.document_id == obs.document_id
        third = await host._choose(run, post)
        await host._dispatch_one(run, post, third)
        assert await page.locator('#airport').input_value() == 'JFK'
        assert provider.calls == 3 and run.result.usage.steps == 3
        assert 'post_observation:owned_url_reobserved' in run.result.evidence
    finally:
        await dom.close()


@pytest.mark.asyncio
async def test_url_change_after_owned_action_still_rejects_stale_refresh(page):
    host, run, _provider = scoped_run(page)
    try:
        obs = await run.dom.observe()
        action = await host._choose(run, obs)
        post = await host._dispatch_one(run, obs, action)
        await page.evaluate("history.replaceState({}, '', '?unowned=later')")
        with pytest.raises(RunnerError, match='^stale$'):
            await host._choose(run, post)
        with pytest.raises(RunnerError, match='^navigation$'):
            await host._refresh_stale(run, post)
        assert run.result.evidence == ['stale_refresh:observation_url_changed']
        assert run.result.usage.steps == 1
    finally:
        await run.dom.close()
