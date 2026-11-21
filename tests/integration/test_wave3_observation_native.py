"""F10 controlled isolated Chromium tests; no personal tabs or live provider."""
import asyncio
import json

import pytest
from test_dom_hostile_world import page as observation_page

page = observation_page

from decision_browser.dom import DOMSession


@pytest.mark.parametrize('name', ['parentElement', 'getAttribute', 'requestSubmit'])
async def test_form_named_controls_keep_native_observe_and_submit(page, name):
    await page.set_content(f'''<form aria-label="Safe form"><input name="{name}"><button>Send</button></form>
    <script>window.effects=0;document.querySelector('form').addEventListener('submit',e=>{{e.preventDefault();window.effects++}})</script>''')
    dom = DOMSession(page)
    send = dom._world._send

    async def bounded(method, params=None):
        if method == 'Runtime.callFunctionOn' and 'executionContextId' in (params or {}) and all('value' in a for a in (params or {}).get('arguments', [])):
            expression = '(' + params['functionDeclaration'] + ')(' + ','.join(json.dumps(a['value']) for a in params.get('arguments', [])) + ')'
            return await send('Runtime.evaluate', {'expression': expression, 'contextId': params['executionContextId'], 'returnByValue': params['returnByValue'], 'timeout': 1000})
        if method == 'Runtime.evaluate':
            params = {**(params or {}), 'timeout': 1000}
        return await send(method, params)

    dom._world._send = bounded
    try:
        obs = await asyncio.wait_for(dom.observe(), 3)
        form = next(c for c in obs.candidates.values() if 'submit' in c.operations)
        action = await dom.prepare('submit', form.token)
        calls = []
        result = await dom.dispatch(action, calls.append)
        assert result.status == 'pass' and len(calls) == 1
        assert await page.evaluate('window.effects') == 1
    finally:
        await dom.close()


async def test_effective_visibility_and_hidden_unsupported(page):
    await page.set_content('''<div style="visibility:hidden"><button style="visibility:visible">Visible override</button></div>
    <button style="visibility:collapse">Collapsed</button><input type=password hidden><iframe hidden></iframe>
    <button>Safe</button><p style="visibility:collapse">collapsed text</p>
    <div hidden id="hidden-shadow"></div><script>document.getElementById('hidden-shadow').attachShadow({mode:'open'}).innerHTML='<button>Hidden shadow</button>'</script>''')
    dom = DOMSession(page)
    try:
        obs = await dom.observe()
        names = [s['node']['name'] for c in obs.candidates.values() for s in c.semantic_by_operation.values()]
        assert 'Visible override' in names and 'Safe' in names
        assert 'Collapsed' not in names and 'collapsed text' not in obs.text
        assert not obs.unsupported
        assert obs.diagnostics['counts']['hidden_unsupported'] == 2
    finally:
        await dom.close()


async def test_viewport_unicode_codepoint_boundary(page):
    text = 'a' * 4095 + '😀' + 'z'
    await page.set_content('<div style="font-size:1px;overflow-wrap:anywhere;width:700px"></div>')
    await page.locator('div').evaluate('(el,text)=>el.textContent=text', text)
    dom = DOMSession(page)
    try:
        obs = await dom.observe()
        assert obs.text == text[:4096]
        assert not any(0xd800 <= ord(c) <= 0xdfff for c in obs.text)
    finally:
        await dom.close()


async def test_scrollbar_edge_snapshot_matches_guard_viewport(page):
    await page.set_content('''<style>html{overflow:scroll}body{height:3000px;margin:0}
    button{position:fixed;top:10px;right:0;width:10px;height:30px}</style><button>Edge</button>''')
    dom = DOMSession(page)
    try:
        obs = await dom.observe()
        cand = next(c for c in obs.candidates.values() if c.semantic_by_operation['click']['node']['name'] == 'Edge')
        action = await dom.prepare('click', cand.token)
        assert (await dom.guard(action)).status == 'pass'
        metrics = await dom._world._send('Page.getLayoutMetrics', {})
        assert obs.diagnostics['viewport']['width'] == metrics['cssLayoutViewport']['clientWidth']
        assert cand.geometry['point'][0] < metrics['cssLayoutViewport']['clientWidth']
    finally:
        await dom.close()


@pytest.mark.parametrize('mutation', ['before_helper', 'during_helper', 'never'])
async def test_public_runner_freshness_limits_helper_and_effects(mutation):
    from test_helper_prompt_privacy_runner import _OwnedFillDriver
    from test_runner import finish

    from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
    from decision_browser.decision_codec import Decision
    from decision_browser.runner import RunManager

    drivers = []

    class Provider:
        decisions = 0
        helpers = 0

        async def decide(self, questions, input_text, *, budget, target_heads, **kwargs):
            budget.reserve_provider(len(input_text.encode()))
            budget.consume_output(1)
            self.decisions += 1
            if mutation != 'before_helper' and self.decisions > 1:
                return Decision('DONE', None, 1.0)
            head = target_heads['fill']
            token = next(q for q in questions if q.name == head).choices[0]
            if mutation == 'before_helper':
                await drivers[0].page.locator('input').evaluate('(el,n)=>el.setAttribute("aria-label","Name "+n)', self.decisions)
            return Decision('fill', token, 1.0)

        async def generate_text(self, prompt, *, budget, **kwargs):
            budget.reserve_provider(len(prompt.encode()))
            budget.consume_output(1)
            self.helpers += 1
            if mutation == 'during_helper':
                await drivers[0].page.locator('input').evaluate('(el)=>el.setAttribute("aria-label","Changed")')
            return 'Ada'

        async def aclose(self):
            pass

    provider = Provider()
    host = RunManager(lambda: provider, lambda mode: drivers.append(_OwnedFillDriver(mode)) or drivers[-1], admission_mode='task_scope')
    request = TaskRequest(goal='Enter the name Ada', start_url='https://example.test/', values={}, checks=[],
                          limits=Limits(max_seconds=15, max_provider_calls=16), privacy=PrivacyPolicy())
    result = await finish(host, host.start_task(request))
    if mutation == 'before_helper':
        assert (result.terminal, result.reason) == ('blocked', 'no_progress')
        assert 'stale_refresh:limit' in result.evidence
        assert provider.decisions == 3 and provider.helpers == 0
        assert result.usage.calls == 3 and result.usage.steps == 1
        assert (drivers[0].final_value, drivers[0].input_events) == ('', 0)
    elif mutation == 'during_helper':
        assert result.terminal == 'completed_unverified'
        assert provider.helpers == 1 and result.usage.calls == 3
        assert result.usage.steps == 1
        assert (drivers[0].final_value, drivers[0].input_events) == ('', 0)
    else:
        assert result.terminal == 'completed_unverified'
        assert provider.helpers == 1 and result.usage.calls == 3
        assert result.usage.steps == 2
        assert (drivers[0].final_value, drivers[0].input_events) == ('Ada', 1)
    from decision_browser.flights_pilot import export_journal
    journal = export_journal(host._run)
    before = [row for row in journal if row.get('event') == 'before_dispatch']
    operations = [json.loads(row['semantic_fingerprint'])['semantic']['operation'] for row in before]
    assert operations == (['navigate', 'fill'] if mutation == 'never' else ['navigate'])
    outcomes = [row for row in journal if row.get('event') == 'outcome']
    assert len(outcomes) == result.usage.steps
    assert all(row['status'] == 'acknowledged' for row in outcomes)


async def test_role_button_inside_clobbering_form_has_bounded_recipient_guard(page):
    await page.set_content('<form><input name="parentElement"><div role="button" style="width:100px;height:40px" onclick="window.effects++">Safe action</div></form><script>window.effects=0</script>')
    dom = DOMSession(page)
    try:
        obs = await dom.observe()
        cand = next(c for c in obs.candidates.values() if 'click' in c.operations and c.semantic_by_operation['click']['node']['name'] == 'Safe action')
        action = await dom.prepare('click', cand.token)
        calls = []
        assert (await dom.dispatch(action, calls.append)).status == 'pass'
        assert len(calls) == 1 and await page.evaluate('window.effects') == 1
    finally:
        await dom.close()


_CLOBBER_NAMES = [
    'ordinary', 'matches', 'tagName', 'disabled', 'cloneNode', 'ownerDocument',
    'children', 'hasAttribute', 'querySelector', 'querySelectorAll', 'nodeType',
    'hidden', 'inert', 'shadowRoot', 'checked', 'readOnly', 'labels', 'innerText',
    'textContent', 'isConnected', 'getAttribute', 'parentElement', 'contains',
    'namespaceURI', 'getBoundingClientRect', 'getRootNode', 'form', 'requestSubmit',
    'willValidate', 'validity', 'options', 'selectedOptions', 'type', 'value',
]


@pytest.mark.parametrize('name', _CLOBBER_NAMES)
async def test_followup_form_interface_matrix_preserves_primary_and_sibling(page, name):
    await page.set_content(f'''<!doctype html><form aria-label="Safe form" action="https://fixture.test/search" method="get">
    Form text<input aria-label="Field" name="{name}" value="kept"><button>Send</button></form>
    <button type="button">Safe sibling</button><script>window.effects=0;
    document.querySelector('form').addEventListener('submit',event=>{{event.preventDefault();window.effects++}})</script>''')
    dom = DOMSession(page)
    try:
        obs = await dom.observe()
        form = next(c for c in obs.candidates.values() if 'submit' in c.operations)
        sibling = next(c for c in obs.candidates.values() if 'click' in c.operations and c.semantic_by_operation['click']['node']['name'] == 'Safe sibling')
        field = next(c for c in obs.candidates.values() if 'enter' in c.operations and c.semantic_by_operation['enter']['node']['name'] == 'Field')
        state = form.semantic_by_operation['submit']['node']['state']
        assert state['tag'] == 'form' and not state['disabled'] and not state['readonly'] and not state['checked']
        assert not obs.unsupported and 'Form text' in obs.text and sibling.operations
        default = field.semantic_by_operation['enter']['form']['defaultSubmitter']
        assert default['name'] == 'Send' and default['path'].endswith('/1')
        assert name + '=kept' in form.semantic_by_operation['submit']['form']['destination']
        action = await dom.prepare('submit', form.token)
        callbacks = []
        assert (await dom.dispatch(action, callbacks.append)).status == 'pass'
        assert len(callbacks) == 1 and await page.evaluate('window.effects') == 1
    finally:
        await dom.close()


async def test_followup_gridcell_form_query_uses_intrinsic_method(page):
    await page.set_content('<form role="gridcell"><input name="querySelector"><button>Send</button></form><button type="button">Safe sibling</button>')
    dom = DOMSession(page)
    try:
        obs = await dom.observe()
        names = [sem['node']['name'] for c in obs.candidates.values() for sem in c.semantic_by_operation.values()]
        assert 'Send' in names and 'Safe sibling' in names
    finally:
        await dom.close()
