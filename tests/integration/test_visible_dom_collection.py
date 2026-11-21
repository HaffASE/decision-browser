"""Owned native fixture: hidden-prefix, picker replacement, calendar, focus hazards."""
import pytest
import pytest_asyncio
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from decision_browser.dom import DOMSession


@pytest_asyncio.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, chromium_sandbox=True, channel="chromium")
        try:
            yield await browser.new_page(viewport={'width': 1200, 'height': 900})
        finally:
            await browser.close()


def target(obs, name, operation='click'):
    return next(c.token for c in obs.candidates.values() if operation in c.operations
                and c.semantic_by_operation[operation]['node']['name'] == name)


@pytest.mark.asyncio
async def test_hidden_prefix_picker_and_calendar(page):
    await page.set_content(''.join('<button hidden>Hidden</button><span role="presentation"></span>' for _ in range(120)) + '''
      <input id="collapsed" aria-label="Where from?" readonly value="Old airport">
      <div id="picker"></div><div role="gridcell"><button>November 10, 2026</button></div>
      <div role="gridcell" tabindex="0">November 11, 2026</div>
      <iframe style="position:absolute;left:1000px;top:700px;width:40px;height:40px" srcdoc="irrelevant"></iframe>
      <p style="position:absolute;top:3000px">OFFSCREEN SECRET</p>
      <script>
      collapsed.onclick=()=>{collapsed.remove();picker.innerHTML='<input id="editable" aria-label="Where from?" role="combobox"><div id="options"></div>';editable.oninput=()=>{options.innerHTML='<div role="option" tabindex="0">JFK John F. Kennedy</div>';options.firstChild.onclick=()=>{editable.value='JFK';options.innerHTML='';};};};
      </script>''')
    dom = DOMSession(page)
    try:
        obs = await dom.observe()
        assert 'iframe' in obs.unsupported
        assert 'OFFSCREEN SECRET' not in obs.text
        collapsed = obs.candidates[target(obs, 'Where from?')]
        assert collapsed.semantic_by_operation['click']['node']['value'] == 'Old airport'
        assert 'fill' not in collapsed.operations
        assert target(obs, 'November 10, 2026')
        assert target(obs, 'November 11, 2026')
        click = await dom.prepare('click', collapsed.token)
        assert (await dom.dispatch(click)).status == 'pass'
        await dom.settle(click)
        obs = await dom.observe()
        fill = await dom.prepare('fill', target(obs, 'Where from?', 'fill'), value='JFK')
        assert (await dom.dispatch(fill)).status == 'pass'
        await dom.settle(fill)
        obs = await dom.observe()
        assert (await dom.dispatch(await dom.prepare('click', target(obs, 'JFK John F. Kennedy')))).status == 'pass'
        assert await page.locator('#editable').input_value() == 'JFK'
    finally:
        await dom.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('handler', [
    "document.getElementById('other').focus()",
    "this.outerHTML='<input aria-label=Target id=replacement>'",
    "history.pushState({}, '', '#changed')",
    "window.open('about:blank')",
    "document.open();document.write('<input id=replacement>');document.close()",
])
async def test_fill_focus_handler_cannot_redirect_text(page, handler):
    await page.set_content('<input id="target" aria-label="Target"><input id="other" aria-label="Other">')
    await page.locator('#target').evaluate('(el, source) => el.addEventListener("focus", new Function(source))', handler)
    dom = DOMSession(page)
    try:
        obs = await dom.observe()
        action = await dom.prepare('fill', target(obs, 'Target', 'fill'), value='NEVER LEAK')
        attempts = []
        with pytest.raises((RuntimeError, PlaywrightError)):
            await dom.dispatch(action, attempts.append)
        assert len(attempts) == 1  # already consumed, never reported as pre-dispatch stale
        assert await page.locator('input').evaluate_all('(els)=>els.every(el=>el.value!=="NEVER LEAK")')
    finally:
        await dom.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['immediate_spa', 'delayed_spa', 'new_document', 'wrong_href'])
async def test_runner_owns_only_bounded_same_document_transition(page, kind):
    import asyncio
    from types import SimpleNamespace

    from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
    from decision_browser.runner import RunManager, RunnerError

    source = {
        'immediate_spa': '<button onclick="history.pushState({},\'\',\'/results\')">Search</button>',
        'delayed_spa': '<button onclick="setTimeout(()=>history.pushState({},\'\',\'/results\'),500)">Search</button>',
        'new_document': '<button onclick="location.href=\'/results\'">Search</button>',
        'wrong_href': '<a href="/wanted" onclick="event.preventDefault();history.pushState({},\'\',\'/wrong\')">Search</a>',
    }[kind]
    await page.route('https://fixture.test/**', lambda route: route.fulfill(
        status=200, content_type='text/html', body=source))
    await page.goto('https://fixture.test/')
    host = RunManager(lambda: None, admission_mode='task_scope')
    run = host._open(TaskRequest(goal='Exercise owned navigation fixture',
        start_url='https://fixture.test/', values={}, checks=[],
        limits=Limits(max_seconds=10), privacy=PrivacyPolicy()))
    host._run = run
    proposal = host._admission.propose_alpha(run.result.run_id, [run.request.start_url],
        ['navigate','click'], run.request.limits.model_dump(), run.budget.original_deadline)
    assert host._admission.admit_task_scope(proposal.proposal_id, proposal.digest)
    run.alpha = proposal
    run.driver = SimpleNamespace(page=page)
    run.dom = DOMSession(page)
    try:
        obs = await run.dom.observe()
        action = await run.dom.prepare('click', target(obs, 'Search'))
        if kind in {'new_document','wrong_href'}:
            with pytest.raises(RunnerError, match='outcome_unknown'):
                await host._dispatch_one(run, obs, action)
        else:
            post = await host._dispatch_one(run, obs, action)
            if kind == 'immediate_spa':
                assert post.url == 'https://fixture.test/results'
                assert post.document_id == obs.document_id
            else:
                assert post.url == obs.url
                await asyncio.sleep(.6)
                with pytest.raises(RunnerError, match='navigation'):
                    await host._refresh_stale(run, post)
                assert run.result.evidence == ['stale_refresh:observation_url_changed']
                assert run.result.usage.steps == 1
    finally:
        await run.dom.close()
