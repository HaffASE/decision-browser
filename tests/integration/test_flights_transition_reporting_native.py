"""Late URL transitions stay blocked; only fixed relationship facts are exported."""
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.decision_codec import Decision
from decision_browser.dom import DOMSession
from decision_browser.experiment_budget import ExperimentLedger
from decision_browser.flights_pilot import FlightsRunManager
from decision_browser.runner import RunnerError

AIRPORT = 'John F. Kennedy International Airport (JFK)'
HTML = '''<input id="departure" role="combobox" aria-label="Where from?" value="Moscow">
<li id="airport" role="option" tabindex="0" hidden>John F. Kennedy International Airport (JFK)</li>
<input id="arrival" role="combobox" aria-label="Where to?">
<script>
departure.oninput=()=>{airport.hidden=departure.value!=='JFK'};
airport.onclick=()=>{departure.value='JFK';airport.hidden=true};
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
            await page.goto('http://127.0.0.1:48762/flights')
            yield page
        finally:
            await browser.close()


class ScriptedProvider:
    def __init__(self, page, ledger, timing):
        self.page, self.ledger, self.timing = page, ledger, timing
        self.calls = 0

    async def decide(self, _questions, input_text, **kwargs):
        # Synthetic quota entries in a tmp_path ledger; never an HTTP request.
        size = len(input_text.encode())
        kwargs['budget'].reserve_provider(size)
        self.ledger.reserve_call(size)
        self.calls += 1
        operation, name = [('fill', 'Where from?'), ('fill', 'Where from?'),
                           ('click', AIRPORT), ('fill', 'Where to?')][self.calls - 1]
        payload = json.loads(input_text)
        target = next(key for key, item in payload['candidates'][operation].items()
                      if item['name'] == name)
        if self.calls == 1:
            # One harmless stale-node retry, with unchanged URL/Document.
            await self.page.locator('#departure').evaluate("el => {el.value='Old selection'}")
        elif self.calls == 4 and self.timing == 'during_decision':
            await self.page.evaluate("history.replaceState({}, '', '?airport=JFK')")
        return Decision(operation, target, 1.0)


@pytest.mark.asyncio
@pytest.mark.parametrize('timing', ['before_decision', 'during_decision'])
async def test_late_airport_url_change_keeps_stale_action_undispatched(page, tmp_path, timing):
    class Manager(FlightsRunManager):
        async def _dispatch_one(self, run, obs, action):
            post = await super()._dispatch_one(run, obs, action)
            if timing == 'before_decision' and json.loads(action.fingerprint)['semantic']['node']['name'] == AIRPORT:
                # Change outside the production acknowledged-action boundary.
                await page.evaluate("history.replaceState({}, '', '?airport=JFK')")
            return post

    with ExperimentLedger(tmp_path / 'synthetic-ledger.jsonl', create=True) as ledger:
        provider = ScriptedProvider(page, ledger, timing)
        host = Manager(provider, ledger)
        run = host._open(TaskRequest(
            goal='Choose exact JFK suggestion, then fill LAX', start_url=page.url,
            values={'Where from?': 'JFK', 'Where to?': 'LAX'}, checks=[],
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
        original_observe = run.dom.observe
        observations = []

        async def counted_observe():
            observed = await original_observe()
            observations.append(observed)
            return observed

        run.dom.observe = counted_observe
        try:
            initial = await run.dom.observe()
            with pytest.raises(RunnerError, match='^navigation$') as caught:
                await host._loop(run, initial)
            host._apply(run, caught.value)
            assert host.failure_code == 'navigation'
            assert run.result.evidence == [
                'runner:navigation', 'stale_refresh:observation_url_changed',
            ]
            assert provider.calls == run.result.usage.calls == ledger.snapshot['calls'] == 4
            # This fixture starts after navigation: exactly the two UI effects.
            assert run.result.usage.steps == ledger.snapshot['steps'] == 2
            assert len(observations) == 5  # No diagnostic observe or settlement read.
            assert len(host.trace) == 2
            assert [row['name'] for row in host.trace] == ['Where from?', AIRPORT]
            assert all(row['acknowledged'] for row in host.trace)
            assert await page.locator('#arrival').input_value() == ''
            assert run.dom._obs.document_id == initial.document_id
            assert all(row['page_changed'] is True for row in host.trace)
            assert all(row['post_observation'] == {
                'same_document': True, 'same_origin': True,
                'url_changed': False, 'live_url_matches': True,
            } for row in host.trace)
            assert [row['outcome'] for row in host.diagnostics] == [
                'stale', 'refreshed', 'chosen', 'chosen', 'stale', 'failed',
            ]
            final_decision, refresh = host.diagnostics[-2:]
            assert final_decision['url_matches_before'] is (timing == 'during_decision')
            assert final_decision['url_matches_after'] is False
            assert final_decision['acknowledged_actions'] == refresh['acknowledged_actions'] == 2
            assert refresh['trigger'] == 'choose'
            assert refresh['reason'] == 'stale_refresh:observation_url_changed'
            assert refresh['url_matches_before'] is refresh['url_matches_after'] is False
            assert run.result.usage.calls == ledger.snapshot['calls'] == 4
            assert run.result.usage.steps == ledger.snapshot['steps'] == 2
            print(json.dumps({'timing': timing, 'safe_evidence': run.result.evidence,
                              'scripted_decisions': provider.calls, 'ui_effects': 2,
                              'page_changed_trace': [row['page_changed'] for row in host.trace],
                              'diagnostics': host.diagnostics}))
        finally:
            await run.dom.close()
