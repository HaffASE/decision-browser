"""Live-shaped pickers that hide the form (observed 2026-10-11) through the real pilot manager.

Sandboxed Chromium, every request fulfilled locally, synthetic provider and ledger.
"""
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from decision_browser.decision_codec import Decision
from decision_browser.dom import DOMSession
from decision_browser.experiment_budget import ExperimentLedger
from decision_browser.flights_continuation import COMMIT_QUERY, DATE_DONE, JFK, LAX
from decision_browser.flights_pilot import FlightsRunManager, pilot_request
from decision_browser.runner import RunnerError

URL = 'https://www.google.com/travel/flights?hl=en'
ROW = ('From 14330 Russian rubles. 1 stop flight with JetBlue. Leaves John F. Kennedy International Airport at '
       '6:00 AM on Tuesday, November 10 and arrives at Los Angeles International Airport at 12:32 PM on Tuesday, '
       'November 10. Total duration 9 hr 32 min. Select flight')
HTML = '''<!doctype html><style>input,button,[role=combobox],[role=button]{display:block;margin:8px}
[hidden]{display:none!important}</style>
<div id="panel">
 <div id="trip" role="combobox" tabindex="0">Round trip</div>
 <li id="one" role="option" tabindex="0" hidden>One way</li>
 <div role="combobox">1 passenger 1</div><div role="combobox">Economy (include Basic)</div>
 <input id="fromInput" role="combobox" aria-label="Where from?" aria-expanded="false" value="Moscow">
 <input id="toInput" role="combobox" aria-label="Where to?" aria-expanded="false">
 <input id="dep" aria-label="Departure" value="">
 <button type="button" id="search">Search</button>
</div>
<div id="idle" style="opacity:0;height:4px">Loading results</div>
<div id="results" hidden>
 <h2>Search results</h2>
 <div role="switch" aria-label="Track prices from New York to Los Angeles departing 2026-11-10">Track prices</div>
 <div>Prices include required taxes + fees for 1 adult.</div>
 <ul role="list"><li><div style="position:relative;height:74px"><div>JetBlue 6:00 AM – 12:32 PM</div>
  <div role="link" style="position:absolute;inset:0;pointer-events:none" aria-label="__ROW__"></div></div></li></ul>
</div>
<div id="originDlg" role="dialog" aria-modal="true" hidden>
 <input id="elseInput" role="combobox" aria-label="Where else?" aria-expanded="true">
 <button type="button">Origin, Select multiple airports</button>
 <ul role="listbox"><li id="jfk" role="option" tabindex="0" hidden>John F. Kennedy International Airport (JFK)</li></ul>
</div>
<div id="destDlg" role="dialog" aria-modal="true" hidden>
 <input id="destInput" role="combobox" aria-label="Where to? " aria-expanded="true">
 <ul role="listbox"><li id="lax" role="option" tabindex="0" hidden>Los Angeles International Airport (LAX)</li></ul>
</div>
<div id="calDlg" role="dialog" aria-modal="true" hidden>
 <input aria-label="Departure" value="">
 <div role="grid"><div role="row">
  <div role="gridcell"><div role="button" tabindex="-1" id="oct10"><div aria-label="Saturday, October 10, 2026">10</div></div></div>
  <div role="gridcell"><div role="button" tabindex="-1" id="nov10"><div aria-label="Tuesday, November 10, 2026">10</div>
   <div aria-label=", 14314 Russian rubles, Cheapest price">14.3K</div></div></div>
 </div></div>
 <button type="button" id="next" aria-label="Next">&gt;</button>
 <button type="button" id="done" aria-label="Done. ">Done</button>
</div>
<script>
window.effects=[];
const show=(dlg,on)=>{panel.hidden=on;dlg.hidden=!on;};
const rewrite=v=>setTimeout(()=>history.replaceState({},'','?hl=en&tfs='+v),300);
trip.onclick=()=>{one.hidden=false;effects.push('trip')};
one.onclick=()=>{trip.textContent='One way';one.hidden=true;effects.push('one');rewrite('b25l')};
fromInput.onclick=()=>{show(originDlg,true);effects.push('origin')};
elseInput.oninput=()=>{jfk.hidden=false;effects.push('JFK-fill')};
jfk.onclick=()=>{fromInput.value='New York';fromInput.setAttribute('aria-label','Where from? New York JFK');
  show(originDlg,false);effects.push('JFK');rewrite('amZr')};
toInput.onclick=()=>{show(destDlg,true);effects.push('destination')};
destInput.oninput=()=>{lax.hidden=false;effects.push('LAX-fill')};
lax.onclick=()=>{toInput.value='Los Angeles';toInput.setAttribute('aria-label','Where to? Los Angeles LAX');
  show(destDlg,false);effects.push('LAX');rewrite('bGF4')};
dep.onclick=()=>{show(calDlg,true);effects.push('calendar')};
oct10.onclick=()=>effects.push('oct10');
nov10.onclick=()=>{done.setAttribute('aria-label','Done. Search for one-way flights, departing on November 10, 2026');effects.push('nov10')};
done.onclick=()=>{dep.value='Tue, Nov 10';show(calDlg,false);effects.push('done')};
search.onclick=()=>{history.pushState({},'','/travel/flights/search?tfs=c2VhcmNo&hl=en');effects.push('search');
  setTimeout(()=>{results.hidden=false;effects.push('rows')},600)};
</script>'''.replace('__ROW__', ROW)
STEPS = [('click', 'Round trip'), ('click', 'One way'), ('click', 'Where from?'), ('fill', 'Where from?'),
         ('click', JFK), ('click', 'Where to?'), ('fill', 'Where to?'), ('click', LAX),
         ('click', 'Departure'), ('click', 'November 10, 2026'), ('click', DATE_DONE)]


@pytest_asyncio.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, chromium_sandbox=True, channel="chromium")
        try:
            context = await browser.new_context()
            await context.route('**/*', lambda route: route.fulfill(
                status=200, content_type='text/html', body=HTML))
            page = await context.new_page()
            await page.goto(URL)
            yield page
        finally:
            await browser.close()


class ScriptedProvider:
    def __init__(self, ledger, steps, before=None):
        self.ledger, self.steps, self.before = ledger, list(steps), before or {}
        self.calls, self.inputs, self.phases = 0, [], []

    async def decide(self, _questions, input_text, **kwargs):
        kwargs['budget'].reserve_provider(len(input_text.encode()))
        self.ledger.reserve_call(len(input_text.encode()))
        self.inputs.append(input_text)
        payload = json.loads(input_text)
        self.phases.append(payload['state']['phase'])
        if self.calls in self.before:
            await self.before[self.calls]()
        if self.calls >= len(self.steps):
            self.calls += 1
            return Decision('DONE', None, 1.)
        op, name = self.steps[self.calls]
        self.calls += 1
        matches = [key for key, entry in payload['candidates'].get(op, {}).items() if entry['name'] == name]
        assert len(matches) == 1, (op, name, payload['state'])
        return Decision(op, matches[0], 1.)


async def setup(page, ledger, steps=STEPS, before=None):
    provider = ScriptedProvider(ledger, steps, before)
    host = FlightsRunManager(provider, ledger, continuation=True)
    run = host._open(pilot_request(60))
    host._run = run
    proposal = host._admission.propose_alpha(
        run.result.run_id, [run.request.start_url], ['navigate', 'click', 'fill', 'scroll', 'wait', 'submit'],
        run.request.limits.model_dump(), run.budget.original_deadline)
    assert host._admission.admit_task_scope(proposal.proposal_id, proposal.digest)
    run.alpha = proposal
    run.driver, run.dom = SimpleNamespace(page=page), DOMSession(page)
    return host, run, provider


async def drive(host, run, obs, limit):
    for _ in range(limit):
        try:
            action = await host._choose(run, obs)
        except RunnerError as exc:
            if exc.code != 'stale':
                raise
            obs = await host._refresh_stale(run, obs)
            continue
        if action is None:
            return obs
        obs = await host._dispatch_one(run, obs, action)
    raise AssertionError('step limit')


async def test_live_shaped_pickers_commit_the_exact_query(page, tmp_path):
    with ExperimentLedger(tmp_path / 'synthetic', create=True) as ledger:
        host, run, provider = await setup(page, ledger)
        try:
            final = await drive(host, run, await run.dom.observe(), 30)
            assert host._committed == COMMIT_QUERY
            assert await page.evaluate('effects') == [
                'trip', 'one', 'origin', 'JFK-fill', 'JFK', 'destination', 'LAX-fill', 'LAX',
                'calendar', 'nov10', 'done']
            assert provider.phases == ['form', 'trip', 'form', 'origin', 'origin', 'form', 'destination',
                                       'destination', 'form', 'calendar', 'calendar', 'form']
            last = json.loads(provider.inputs[-1])
            assert last['state']['ready'] is True
            assert 'Search' in {entry['name'] for entry in last['candidates']['click'].values()}
            assert not any(word in text for text in provider.inputs
                           for word in ('Moscow', 'October', 'Where else', 'b25l', 'amZr', 'rubles'))
            assert [row['name'] for row in host.trace][-2:] == ['November 10, 2026', DATE_DONE]
            # Each delayed rewrite is either validated as a continuation or, if it lands
            # before the click's own post-observation, attributed to that click.
            assert host._picker is None and 1 <= host.continuations <= 3
            await host._prove(run, final)
        finally:
            await run.dom.close()


@pytest.mark.parametrize("exhausted", [False, True])
async def test_search_waits_for_settled_rows_then_verifies(page, tmp_path, exhausted):
    with ExperimentLedger(tmp_path / 'synthetic', create=True) as ledger:
        host, run, provider = await setup(page, ledger, [*STEPS, ('click', 'Search')])
        try:
            await drive(host, run, await run.dom.observe(), 30)
            # The post-Search observation waited for rows instead of the 200 ms render.
            settle = [row for row in host.diagnostics if row['phase'] == 'results_settle']
            assert [row['outcome'] for row in settle] == ['settled'] and settle[0]['elapsed_ms'] >= 500
            assert (await page.evaluate('effects'))[-2:] == ['search', 'rows']
            assert provider.phases[-1] == 'results'
            if exhausted:
                while run.budget.usage().calls < 40:
                    run.budget.reserve_provider(0)
                while ledger.snapshot['calls'] < 40:
                    ledger.reserve_call(0)
                while run.budget.usage().steps < 40:
                    run.budget.reserve_step()
                while ledger.snapshot['steps'] < 40:
                    ledger.reserve_step()
            before = (run.budget.usage(), ledger.snapshot)
            assert await host._verify(run, finish=True)
            assert before == (run.budget.usage(), ledger.snapshot)
            assert host.report['status'] == 'PASS', host.report['reasons']
            assert (host.report['row']['currency'], run.result.terminal) == ('RUB', 'verified')
        finally:
            await run.dom.close()


async def test_results_settle_is_bounded_and_only_after_search(page, tmp_path, monkeypatch):
    monkeypatch.setattr('decision_browser.flights_pilot.RESULTS_SETTLE_SECONDS', .4)
    await page.evaluate("() => { search.onclick = () => effects.push('search'); }")
    with ExperimentLedger(tmp_path / 'synthetic', create=True) as ledger:
        host, run, _provider = await setup(page, ledger, [*STEPS, ('click', 'Search')])
        try:
            await drive(host, run, await run.dom.observe(), 30)
            settle = [row for row in host.diagnostics if row['phase'] == 'results_settle']
            assert [row['outcome'] for row in settle] == ['timeout']
            assert 400 <= settle[0]['elapsed_ms'] < 2000
            assert not await host._verify(run, finish=True)
            assert host.report['status'] == 'NOT_VERIFIED'
        finally:
            await run.dom.close()


@pytest.mark.parametrize('attack,reason', [
    ('unbound_picker', 'trip_control_count'), ('drift_in_picker', 'trip_control_count'),
    ('second_dialog', 'trip_control_count'), ('extra_field', 'picker_kind_mismatch'),
    ('unknown_dialog_input', 'picker_kind_mismatch'), ('ambiguous_day', 'duplicate_offered_control')])
async def test_picker_attacks_stop_before_effect(page, tmp_path, attack, reason):
    async def mutate(script):
        await page.evaluate('() => {' + script + '}')

    before = {
        # The page opens the picker by itself: no host-confirmed click binds it.
        'unbound_picker': {2: lambda: mutate("show(originDlg,true)")},
        'drift_in_picker': {3: lambda: mutate("history.replaceState({},'','?hl=en&tfs=ZHJpZnQ')")},
        'second_dialog': {3: lambda: mutate("calDlg.hidden=false")},
        'extra_field': {3: lambda: mutate("originDlg.append(dep.cloneNode())")},
        'unknown_dialog_input': {3: lambda: mutate("originDlg.insertAdjacentHTML('beforeend','<input type=email>')")},
        'ambiguous_day': {8: lambda: mutate("oct10.firstElementChild.setAttribute('aria-label','Tuesday, November 10, 2026')")},
    }[attack]
    with ExperimentLedger(tmp_path / 'synthetic', create=True) as ledger:
        # An unbound picker is opened by the page while the model only waits.
        steps = STEPS[:2] + [('wait', 'short')] if attack == 'unbound_picker' else STEPS
        host, run, provider = await setup(page, ledger, steps, before)
        try:
            with pytest.raises(RunnerError, match='pilot_out_of_scope'):
                await drive(host, run, await run.dom.observe(), 30)
            assert host.scope_reason == reason
            effects = await page.evaluate('effects')
            # A change made during a decision is caught at the next state proof
            # (or, for URL drift, by the dispatch staleness check).
            stop = {'unbound_picker': 'origin', 'drift_in_picker': 'JFK-fill', 'second_dialog': 'JFK',
                    'extra_field': 'JFK', 'unknown_dialog_input': 'JFK', 'ambiguous_day': 'nov10'}[attack]
            assert stop not in effects and 'oct10' not in effects and 'done' not in effects
            assert provider.calls == ledger.snapshot['calls']
        finally:
            await run.dom.close()
