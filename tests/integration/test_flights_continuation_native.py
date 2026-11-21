"""Sandboxed Chromium, intercepted public origin, synthetic provider and ledger.

No live Google or OpenAI request: every page request is fulfilled locally.
"""
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from decision_browser.decision_codec import Decision
from decision_browser.dom import DOMSession
from decision_browser.experiment_budget import ExperimentLedger
from decision_browser.flights_continuation import COMMIT_QUERY, JFK
from decision_browser.flights_pilot import FlightsRunManager, pilot_request
from decision_browser.runner import RunnerError

URL = 'https://www.google.com/travel/flights?hl=en'
HTML = '''<!doctype html><style>input,button,[role=combobox]{display:block;margin:8px} [hidden]{display:none!important}</style>
<div id="trip" role="combobox" tabindex="0">Round trip</div>
<li id="one" role="option" tabindex="0" hidden>One way</li>
<div role="combobox">1 passenger 1</div><div role="combobox">Economy (include Basic)</div>
<input id="fromInput" role="combobox" aria-label="Where from?" aria-expanded="false" value="Moscow">
<li id="airport" role="option" tabindex="0" hidden>John F. Kennedy International Airport (JFK)</li>
<input id="arrival" role="combobox" aria-label="Where to?" aria-expanded="false">
<li id="lax" role="option" tabindex="0" hidden>Los Angeles International Airport (LAX)</li>
<input aria-label="Departure" value="Thu, Oct 15"><button type="button">Search</button>
<script>
window.effects=[];
trip.onclick=()=>{one.hidden=false;effects.push('trip')};
one.onclick=()=>{trip.textContent='One way';one.hidden=true;effects.push('one')};
fromInput.oninput=()=>{fromInput.setAttribute('aria-expanded','true');airport.hidden=false;effects.push('JFK-fill')};
airport.onclick=()=>{fromInput.value='New York';fromInput.setAttribute('aria-label','Where from? New York JFK');fromInput.setAttribute('aria-expanded','false');airport.hidden=true;effects.push('JFK-select')};
arrival.oninput=()=>{arrival.setAttribute('aria-expanded','true');lax.hidden=false;effects.push('LAX-fill')};
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
            await page.goto(URL)
            yield page
        finally:
            await browser.close()


class ScriptedProvider:
    def __init__(self, page, ledger, attack='allowed'):
        self.page, self.ledger, self.attack = page, ledger, attack
        self.calls, self.inputs = 0, []

    async def decide(self, _questions, input_text, **kwargs):
        self.calls += 1
        self.inputs.append(input_text)
        kwargs['budget'].reserve_provider(len(input_text.encode()))
        self.ledger.reserve_call(len(input_text.encode()))
        if self.calls == 8:
            return Decision('DONE', None, 1.)
        op, name = [('click', 'Round trip'), ('click', 'One way'),
                    ('fill', 'Where from?'), ('fill', 'Where from?'),
                    ('click', JFK), ('fill', 'Where to?'),
                    ('fill', 'Where to?')][self.calls - 1]
        payload = json.loads(input_text)
        target = next(key for key, entry in payload['candidates'][op].items()
                      if entry['name'] == name)
        if self.calls in {3, 6} or self.calls == 4 and self.attack == 'repeated':
            if self.attack == 'document':
                await self.page.goto(URL + '&tfs=YWJj')
            elif self.attack == 'return':
                await self.page.evaluate("history.replaceState({}, '', '?hl=en&tfs=YWJj');history.replaceState({}, '', '?hl=en')")
            elif self.attack == 'captcha':
                await self.page.evaluate("document.body.insertAdjacentHTML('beforeend','<div id=captcha>verify you are human</div>');history.replaceState({}, '', '?hl=en&tfs=YWJj')")
            elif self.attack == 'popup':
                await self.page.evaluate("window.open('about:blank')")
            elif self.attack == 'hostile':
                await self.page.evaluate("history.replaceState({}, '', '/travel/flights/payment?hl=en')")
            else:
                opaque = 'PRIVATE_CANARY_42' if self.calls == 3 else 'ZGVm'
                if self.calls == 4:
                    opaque = 'bmV3'
                await self.page.evaluate("url=>history.replaceState({},'',url)", '?hl=en&tfs=' + opaque)
        return Decision(op, target, 1.)


async def setup(page, ledger, attack='allowed'):
    provider = ScriptedProvider(page, ledger, attack)
    host = FlightsRunManager(provider, ledger, continuation=True)
    run = host._open(pilot_request(30))
    host._run = run
    proposal = host._admission.propose_alpha(
        run.result.run_id, [run.request.start_url], ['navigate', 'click', 'fill', 'scroll', 'wait', 'submit'],
        run.request.limits.model_dump(), run.budget.original_deadline)
    assert host._admission.admit_task_scope(proposal.proposal_id, proposal.digest)
    run.alpha = proposal
    run.driver, run.dom = SimpleNamespace(page=page), DOMSession(page)
    return host, run, provider


async def hidden_trip_fixture(page):
    await page.evaluate('''() => {
      const panel=document.createElement('div');panel.id='formPanel';
      panel.append(...document.body.childNodes);document.body.append(panel);
      const menu=document.createElement('ul');menu.id='tripMenu';
      menu.setAttribute('role','listbox');menu.hidden=true;
      const round=document.createElement('li');round.setAttribute('role','option');round.textContent='Round trip';
      const multi=document.createElement('li');multi.setAttribute('role','option');multi.textContent='Multi-city';
      one.hidden=false;menu.append(round,one,multi);document.body.append(menu);
      trip.onclick=()=>{panel.setAttribute('aria-hidden','true');menu.hidden=false;effects.push('trip')};
      one.onclick=()=>{trip.textContent='One way';panel.removeAttribute('aria-hidden');menu.hidden=true;effects.push('one')};
    }''')


@pytest.mark.parametrize('kind',['listbox','menu'])
async def test_real_shaped_hidden_trip_menu_then_restored_form(page, tmp_path,kind):
    await hidden_trip_fixture(page)
    if kind=='menu':
        await page.evaluate("tripMenu.setAttribute('role','menu');tripMenu.querySelectorAll('li').forEach(e=>e.setAttribute('role','menuitemradio'))")
    with ExperimentLedger(tmp_path/'synthetic',create=True) as ledger:
        host,run,provider=await setup(page,ledger)
        originals=run.alpha,run.budget,run.budget.original_deadline,ledger,host._provider_factory()
        try:
            before=await run.dom.observe()
            menu=await host._dispatch_one(run,before,await host._choose(run,before))
            assert host._trip_menu==(before.document_id,before.url,host._generation,host._completed)
            choice=await host._choose(run,menu)
            data=json.loads(provider.inputs[-1])
            assert data['state']=={'phase':'trip','form_observed':False,'ready':False}
            assert set(data['candidates'])=={'click'}
            assert [item['name'] for item in data['candidates']['click'].values()]==['One way']
            assert 'Moscow' not in provider.inputs[-1]
            restored=await host._dispatch_one(run,menu,choice)
            assert host._trip_menu is None
            await host._prove(run,restored)
            assert host._state['phase']=='form' and host._state['trip']=='one way'
            assert await page.evaluate('effects')==['trip','one']
            assert provider.calls==ledger.snapshot['calls']==run.budget.usage().calls==2
            assert ledger.snapshot['steps']==run.budget.usage().steps==2
            assert host.continuations==0
            assert originals==(run.alpha,run.budget,run.budget.original_deadline,ledger,run.provider)
        finally:
            await run.dom.close()


@pytest.mark.parametrize('attack', ['duplicate','missing','wrong_role','outside_menu','unknown_option',
                                     'swapped_labels','labelledby','external_labels',
                                     'partial_form','calendar','password','modal','href','form','covered',
                                     'url','generation','document','revoke','cancel','no_ack'])
async def test_hidden_trip_menu_denied_before_next_provider(page,tmp_path,attack):
    await hidden_trip_fixture(page)
    with ExperimentLedger(tmp_path/'synthetic',create=True) as ledger:
        host,run,provider=await setup(page,ledger)
        try:
            initial=await run.dom.observe()
            if attack=='no_ack':
                await page.locator('#trip').click()
            else:
                await host._dispatch_one(run,initial,await host._choose(run,initial))
            mutations={
                'duplicate':"tripMenu.append(one.cloneNode(true))",
                'missing':"one.remove()",
                'wrong_role':"one.setAttribute('role','button')",
                'outside_menu':"document.body.append(one)",
                'unknown_option':"tripMenu.lastElementChild.textContent='PRIVATE_CANARY_42'",
                'swapped_labels':"tripMenu.firstElementChild.setAttribute('aria-label','One way');one.setAttribute('aria-label','Round trip')",
                'labelledby':"one.setAttribute('aria-labelledby','trip')",
                'external_labels':"Array.from(tripMenu.children).forEach((old,i)=>{const b=document.createElement('button');b.type='button';b.id='labelled-menu-'+i;b.setAttribute('role','option');b.textContent=old.textContent;old.replaceWith(b);if(i<2){const label=document.createElement('label');label.htmlFor=b.id;label.textContent=i===0?'One way':'Round trip';document.body.append(label)}})",
                'partial_form':"document.body.append(arrival.cloneNode(true))",
                'calendar':"document.body.insertAdjacentHTML('beforeend','<div role=grid>calendar</div>')",
                'password':"document.body.insertAdjacentHTML('beforeend','<input type=password>')",
                'modal':"document.body.insertAdjacentHTML('beforeend','<div role=dialog aria-modal=true>unknown</div>')",
                'href':"one.setAttribute('href','https://evil.test/collector')",
                'form':"const f=document.createElement('form');f.action='https://www.google.com/travel/flights/payment?hl=en';document.body.append(f);f.append(tripMenu)",
                'covered':"document.body.insertAdjacentHTML('beforeend','<div style=\"position:fixed;inset:0;z-index:9999\">cover</div>')",
                'url':"history.replaceState({},'', '?hl=en&tfs=YWJj')",
                'generation':"history.replaceState({},'', '?hl=en&tfs=YWJj');history.replaceState({},'', '?hl=en')",
            }
            if attack in mutations:
                await page.evaluate('() => {'+mutations[attack]+'}')
            elif attack=='document':
                await page.goto(URL)
                await hidden_trip_fixture(page)
                await page.locator('#trip').click()
            elif attack=='revoke':
                host._admission.revoke(run.result.run_id)
            elif attack=='cancel':
                run.cancel_event.set()
            fresh=await run.dom.observe()
            before=provider.calls,ledger.snapshot['calls'],ledger.snapshot['steps']
            with pytest.raises(RunnerError):
                await host._choose(run,fresh)
            assert before==(provider.calls,ledger.snapshot['calls'],ledger.snapshot['steps'])
            assert provider.calls==(0 if attack=='no_ack' else 1)
            assert 'one' not in await page.evaluate('effects')
            assert host.continuations==0
        finally:
            await run.dom.close()


@pytest.mark.parametrize('failure',['not_closed','wrong_trip','duplicate_field'])
async def test_after_one_way_restored_form_must_pass_before_provider(page,tmp_path,failure):
    await hidden_trip_fixture(page)
    with ExperimentLedger(tmp_path/'synthetic',create=True) as ledger:
        host,run,provider=await setup(page,ledger)
        try:
            initial=await run.dom.observe()
            menu=await host._dispatch_one(run,initial,await host._choose(run,initial))
            choice=await host._choose(run,menu)
            mutation={
                'not_closed':"one.onclick=()=>effects.push('one')",
                'wrong_trip':"one.onclick=()=>{formPanel.removeAttribute('aria-hidden');tripMenu.hidden=true;effects.push('one')}",
                'duplicate_field':"one.onclick=()=>{trip.textContent='One way';formPanel.removeAttribute('aria-hidden');tripMenu.hidden=true;formPanel.append(fromInput.cloneNode(true));effects.push('one')}",
            }[failure]
            # The observed meaning remains identical, but the untrusted
            # application handler produces a deliberately invalid result.
            await page.evaluate('() => {'+mutation+'}')
            restored=await host._dispatch_one(run,menu,choice)
            assert host._trip_menu is None and host._committed==frozenset({'One way'})
            with pytest.raises(RunnerError,match='pilot_out_of_scope'):
                await host._choose(run,restored)
            assert provider.calls==ledger.snapshot['calls']==2
            assert ledger.snapshot['steps']==2 and host.continuations==0
            assert await page.evaluate('effects')==['trip','one']
        finally:
            await run.dom.close()


async def test_two_unattributed_delayed_transitions_replan_with_fresh_targets(page, tmp_path):
    with ExperimentLedger(tmp_path / 'synthetic', create=True) as ledger:
        host, run, provider = await setup(page, ledger)
        original = (run, run.alpha, run.budget, run.budget.original_deadline, ledger, host._provider_factory())
        try:
            initial = await run.dom.observe()
            await host._loop(run, initial)
            assert host.continuations == 2 and len(host._spent) == 2
            assert provider.calls == run.budget.usage().calls == ledger.snapshot['calls'] == 8
            assert run.budget.usage().steps == ledger.snapshot['steps'] == 5
            assert await page.evaluate('effects') == ['trip', 'one', 'JFK-fill', 'JFK-select', 'LAX-fill']
            assert all('PRIVATE_CANARY_42' not in item for item in provider.inputs)
            assert run.dom._obs.document_id == initial.document_id
            assert original == (run, run.alpha, run.budget, run.budget.original_deadline, ledger, run.provider)
            assert run.result.functional == 'unverified' and host.report['status'] == 'NOT_VERIFIED'
            records = [row for row in host.diagnostics if row['phase'] == 'state_continuation']
            assert len(records) == 2 and all(row['ownership'] == 'unattributed' for row in records)
        finally:
            await run.dom.close()


@pytest.mark.parametrize('attack', ['hostile', 'repeated', 'document', 'return', 'captcha', 'popup'])
async def test_hostile_or_repeated_drift_keeps_original_choice_undispatched(page, tmp_path, attack):
    with ExperimentLedger(tmp_path / 'synthetic', create=True) as ledger:
        host, run, provider = await setup(page, ledger, attack)
        try:
            initial = await run.dom.observe()
            with pytest.raises(RunnerError):
                await host._loop(run, initial)
            assert provider.calls == (4 if attack == 'repeated' else 3)
            assert len(host.trace) == run.budget.usage().steps == ledger.snapshot['steps'] == 2
            assert host.continuations <= 1
            assert await page.locator('#arrival').input_value() == ''
            if attack != 'document':
                assert await page.evaluate('effects') == ['trip', 'one']
            assert host.report['status'] == 'NOT_VERIFIED'
        finally:
            await run.dom.close()


@pytest.mark.parametrize('attack', ['href', 'form', 'ambiguous', 'password', 'missing', 'wrong_role', 'extra_control'])
async def test_initial_state_or_required_destination_denied_before_provider(page, tmp_path, attack):
    mutation = {
        'href': "fromInput.setAttribute('href','https://evil.test/collector')",
        'form': "fromInput.outerHTML='<form action=\"https://www.google.com/travel/flights/payment?hl=en\">'+fromInput.outerHTML+'</form>'",
        'ambiguous': "fromInput.insertAdjacentHTML('afterend',fromInput.outerHTML)",
        'password': "document.body.insertAdjacentHTML('beforeend','<input type=password>')",
        'missing': 'fromInput.remove()',
        'wrong_role': "fromInput.setAttribute('role','link')",
        'extra_control': "document.body.insertAdjacentHTML('beforeend','<div role=combobox tabindex=0 aria-label=\"Where from?\">Where from?</div>')",
    }[attack]
    if attack == 'extra_control':
        mutation = "fromInput.value='New York';fromInput.setAttribute('aria-label','Where from? New York JFK');" + mutation
    await page.evaluate(mutation)
    with ExperimentLedger(tmp_path / 'synthetic', create=True) as ledger:
        host, run, provider = await setup(page, ledger)
        if attack == 'extra_control':
            host._committed = frozenset({JFK})
        try:
            with pytest.raises(RunnerError):
                await host._choose(run, await run.dom.observe())
            assert provider.calls == ledger.snapshot['calls'] == run.budget.usage().calls == 0
        finally:
            await run.dom.close()


async def test_leave_return_during_completed_wait_preserves_unknown_ack_and_spend(page,tmp_path):
    with ExperimentLedger(tmp_path/'synthetic',create=True) as ledger:
        host,run,provider=await setup(page,ledger)
        try:
            obs=await run.dom.observe()
            host._watch(run)
            await host._prove(run,obs)
            wait=await run.dom.prepare('wait',value=100)
            # Inject the unrelated event at the real native call boundary,
            # after its native guard/admission, then let the ordinary wait settle.
            original_native=run.dom._native
            async def with_timer(action):
                await page.evaluate("setTimeout(()=>{history.replaceState({},'', '?hl=en&tfs=YWJj');history.replaceState({},'', '?hl=en')},20)")
                return await original_native(action)
            run.dom._native=with_timer
            with pytest.raises(RunnerError,match='outcome_unknown') as caught:
                await host._dispatch_one(run,obs,wait)
            host._apply(run,caught.value)
            assert run.result.terminal=='outcome_unknown'
            assert host._generation>=2 and host._baseline==0
            assert host._completed is None and host.continuations==0
            assert host.trace[-1]['acknowledged'] and host.trace[-1]['outcome']=='outcome_unknown'
            assert ledger.snapshot['steps']==run.budget.usage().steps==1
            assert provider.calls==ledger.snapshot['calls']==0
        finally:
            await run.dom.close()


async def test_discordant_trip_option_is_not_offered_in_closed_form(page,tmp_path):
    await page.evaluate("document.body.insertAdjacentHTML('beforeend','<div role=option tabindex=0 aria-label=\"One way\">PRIVATE_CANARY_42</div>')")
    with ExperimentLedger(tmp_path/'synthetic',create=True) as ledger:
        host,run,provider=await setup(page,ledger)
        try:
            await host._choose(run,await run.dom.observe())
            payload=json.loads(provider.inputs[0])
            assert payload['state']['phase']=='form'
            assert all(item['name']!='One way' for item in payload['candidates']['click'].values())
            assert 'PRIVATE_CANARY_42' not in provider.inputs[0]
        finally:
            await run.dom.close()


async def test_pure_post_search_gate_consumes_same_capacity_without_search_replay(page, tmp_path):
    await page.evaluate("""() => {
      trip.textContent='One way';
      fromInput.value='New York';fromInput.setAttribute('aria-label','Where from? New York JFK');
      arrival.value='Los Angeles';arrival.setAttribute('aria-label','Where to? Los Angeles LAX');
      document.querySelector('[aria-label=Departure]').value='Tue, Nov 10';
      document.querySelector('button').onclick=()=>effects.push('Search');
    }""")
    with ExperimentLedger(tmp_path/'synthetic',create=True) as ledger:
        host,run,provider=await setup(page,ledger)
        host._committed=COMMIT_QUERY  # Fixture starts after the completed query setup.
        try:
            obs=await run.dom.observe()
            run.current_observation=obs
            host._watch(run)
            await host._prove(run,obs)
            token=next(key for key,candidate in obs.candidates.items()
                       if any(sem.get('node',{}).get('name')=='Search' for sem in candidate.semantic_by_operation.values()))
            action=await run.dom.prepare('click',token)
            post=await host._dispatch_one(run,obs,action)
            run.current_observation=post
            search=host.search_action
            before=run.budget.usage(),ledger.snapshot['steps']
            await page.evaluate("history.replaceState({},'', '/travel/flights/search?hl=en&tfs=YWJj')")
            snapshot=await host._snapshot(run)
            assert snapshot['url']==page.url and host.continuations==1
            assert host.search_action is search and host._accepted is run.current_observation
            assert await host._refresh_stale(run,post) is host._accepted
            assert host.continuations==1
            assert before==(run.budget.usage(),ledger.snapshot['steps'])
            assert provider.calls==ledger.snapshot['calls']==0
            assert await page.evaluate('effects')==['Search']
            assert host.report['status']=='NOT_VERIFIED'
            await page.evaluate("history.replaceState({},'', '?hl=en&tfs=ZGVm')")
            with pytest.raises(RunnerError,match='navigation'):
                await host._snapshot(run)
            assert host.continuations==1 and await page.evaluate('effects')==['Search']
        finally:
            await run.dom.close()
