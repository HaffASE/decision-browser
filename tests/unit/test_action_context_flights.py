import json
from dataclasses import replace
from unittest.mock import AsyncMock

from decision_browser.action_context import (
    context_payload,
    observation_signature,
    recent_context,
)
from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.dom import Candidate, Observation
from decision_browser.runner import RunManager


def test_semantic_progress_ignores_regenerated_ids_but_tracks_value():
    candidate = Candidate('a', ('click',), {'click': {'node': {'value': 'JFK'}}}, None)
    obs = Observation('first', 'https://example.test', 'doc', 'text', {'a': candidate}, ())
    new = replace(obs, id='second', candidates={'b': replace(candidate, token='b')})
    assert observation_signature(obs) == observation_signature(new)
    changed = replace(candidate, semantic_by_operation={'click': {'node': {'value': 'LAX'}}})
    assert observation_signature(obs) != observation_signature(replace(obs, candidates={'a':changed}))


def test_history_is_bounded_and_visible_in_context():
    history = [{'name': str(i), 'value': 'x'*512} for i in range(20)]
    rows = recent_context(history)
    assert len(rows) == 10 and rows[0]['name'] == '10'
    obs = Observation('a', 'https://example.test', 'doc', '', {}, ('iframe',))
    payload = json.loads(context_payload('goal', obs, {}, history))
    assert payload['recent_actions'] == rows
    assert payload['excluded_mechanisms'] == ['iframe']


async def test_task_scope_ignores_inert_iframe_but_stops_three_unchanged_effects():
    host = RunManager(lambda: None, admission_mode='task_scope')
    run = host._open(TaskRequest(goal='Test', start_url='https://example.test/', values={},
        checks=[], limits=Limits(max_seconds=10), privacy=PrivacyPolicy()))
    host._run = run
    host._admission.task_scope_allows = lambda *_: True
    run.recent_actions = [{'operation':'click', 'page_changed':False} for _ in range(3)]
    host._choose = AsyncMock(side_effect=AssertionError('no additional model call'))
    await host._loop(run, Observation('a','https://example.test','doc','',{},('iframe',)))
    assert run.result.reason == 'no_progress'
    host._choose.assert_not_awaited()


async def test_owned_spa_click_is_not_unexpected_navigation():
    from types import SimpleNamespace

    from test_task_scope_manager import open_scoped_run

    from decision_browser.dom import Action, DispatchResult
    host, run = open_scoped_run()
    host._run = run
    prior = 'https://example.test/'
    run.driver = SimpleNamespace(page=SimpleNamespace(url=prior))
    obs = Observation('a','https://example.test','doc','before',{},())
    post = replace(obs, id='b', text='after')
    class DOM:
        async def dispatch(self, action, before):
            before(action)
            run.driver.page.url = 'https://example.test/results'
            return DispatchResult('pass','')
        async def observe(self):
            return post
    run.dom = DOM()
    action = Action('click','a',None,None,'a',json.dumps({'semantic':{
        'origin':'https://example.test','url':prior,'node':{'name':'Search'},'form':None}}))
    assert await host._dispatch_one(run, obs, action) is post
    assert run.recent_actions[-1]['page_changed'] is True
    assert run.recent_actions[-1]['attempt_id']


async def test_same_origin_new_document_is_not_spa_exception():
    from types import SimpleNamespace

    import pytest
    from test_task_scope_manager import open_scoped_run

    from decision_browser.dom import Action, DispatchResult
    from decision_browser.runner import RunnerError
    host, run = open_scoped_run()
    host._run = run
    prior = 'https://example.test/'
    run.driver = SimpleNamespace(page=SimpleNamespace(url=prior))
    obs = Observation('a','https://example.test','doc','before',{},())
    class DOM:
        async def dispatch(self, action, before):
            before(action)
            run.driver.page.url = 'https://example.test/results'
            return DispatchResult('pass','')
        async def observe(self):
            return replace(obs, document_id='replacement')
    run.dom = DOM()
    action = Action('click','a',None,None,'a',json.dumps({'semantic':{
        'origin':'https://example.test','url':prior,'node':{'name':'Search'},'form':None}}))
    with pytest.raises(RunnerError, match='outcome_unknown'):
        await host._dispatch_one(run, obs, action)


async def test_stale_refresh_rejects_delayed_url_or_document_transition():
    from types import SimpleNamespace

    import pytest

    from decision_browser.runner import RunnerError
    host = RunManager(lambda:None)
    run = host._open(TaskRequest(goal='Test',start_url='https://example.test/',values={},
        checks=[],limits=Limits(max_seconds=10),privacy=PrivacyPolicy()))
    prior=Observation('o','https://example.test','doc','',{},(),url='https://example.test/')
    for fresh in [replace(prior,url='https://example.test/later'),replace(prior,document_id='new')]:
        run.driver=SimpleNamespace(page=SimpleNamespace(url=fresh.url))
        run.dom=SimpleNamespace(observe=AsyncMock(return_value=fresh))
        with pytest.raises(RunnerError,match='navigation'):
            await host._refresh_stale(run,prior)
