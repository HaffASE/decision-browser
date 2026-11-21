"""Continuation accounting and provider/report boundaries with synthetic state."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from decision_browser.budget import Budget, BudgetExceeded
from decision_browser.contracts import Limits, PrivacyPolicy
from decision_browser.decision_codec import ChoiceQuestion
from decision_browser.dom import Action, Observation
from decision_browser.experiment_budget import ExperimentLedger
from decision_browser.flights_continuation import ProjectedProvider, project_input
from decision_browser.flights_pilot import FlightsRunManager, pilot_request
from decision_browser.flights_provider import FlightsProvider
from decision_browser.privacy import PrivacyGate
from decision_browser.runner import RunManager, RunnerError

URL = 'https://www.google.com/travel/flights?hl=en'
CANARY = 'PRIVATE_CANARY_42'


def scoped(host):
    run = host._open(pilot_request(30))
    host._run = run
    grant = host._admission.propose_alpha(run.result.run_id, [URL], ['navigate', 'click', 'fill', 'wait'],
        run.request.limits.model_dump(), run.budget.original_deadline)
    assert host._admission.admit_task_scope(grant.proposal_id, grant.digest)
    run.alpha, run.dom = grant, SimpleNamespace()
    run.driver = SimpleNamespace(page=SimpleNamespace(url=URL))
    return run


async def test_consumption_precedes_first_await_and_failed_proof_cannot_refund(tmp_path, monkeypatch):
    with ExperimentLedger(tmp_path/'synthetic', create=True) as ledger:
        host = FlightsRunManager(object(), ledger, continuation=True)
        run = scoped(host)
        prior = Observation('old', 'https://www.google.com', 'doc', '', {}, (), url=URL)
        host._completed = 'completed-attempt'
        run.driver.page.url += '&tfs=YWJj'
        async def failing_observe():
            assert host.continuations == 1 and host._spent == {'completed-attempt'}
            raise RunnerError('blocked')
        run.dom.observe = AsyncMock(side_effect=failing_observe)
        with pytest.raises(RunnerError, match='blocked'):
            await host._continue(run, prior)
        with pytest.raises(RunnerError, match='navigation'):
            await host._continue(run, prior)
        assert run.dom.observe.await_count == 1
        assert ledger.snapshot['calls'] == ledger.snapshot['steps'] == 0


async def test_four_total_and_one_per_completed_action_preserve_all_objects(tmp_path, monkeypatch):
    with ExperimentLedger(tmp_path/'synthetic', create=True) as ledger:
        host = FlightsRunManager(object(), ledger, continuation=True)
        run = scoped(host)
        prior = Observation('old', 'https://www.google.com', 'doc', '', {}, (), url=URL)
        fresh = Observation('new', 'https://www.google.com', 'doc', '', {}, (), url=URL+'&tfs=YWJj')
        run.driver.page.url = fresh.url
        run.dom.observe = AsyncMock(return_value=fresh)
        monkeypatch.setattr(host, '_prove', AsyncMock())
        originals = (run.alpha, run.budget, run.budget.original_deadline, ledger, host._provider_factory())
        for index in range(4):
            host._completed = str(index)
            assert await host._continue(run, prior) is fresh
            with pytest.raises(RunnerError, match='navigation'):
                await host._continue(run, prior)
        host._completed = 'fifth'
        with pytest.raises(RunnerError, match='navigation'):
            await host._continue(run, prior)
        assert host.continuations == run.dom.observe.await_count == 4
        assert originals == (run.alpha, run.budget, run.budget.original_deadline, ledger, host._provider_factory())
        assert ledger.snapshot['calls'] == ledger.snapshot['steps'] == run.budget.usage().steps == 0


@pytest.mark.parametrize('failure', ['revoke', 'deadline', 'cancel', 'queued', 'calls', 'steps', 'bytes', 'expired'])
async def test_no_preflight_reset_on_revocation_expiry_or_quota(tmp_path, failure):
    with ExperimentLedger(tmp_path/'synthetic', create=True) as ledger:
        host = FlightsRunManager(object(), ledger, continuation=True)
        run = scoped(host)
        if failure == 'revoke':
            host._admission.revoke(run.result.run_id)
        elif failure == 'deadline':
            host._now = lambda: run.budget.original_deadline
        elif failure == 'cancel':
            run.cancel_event.set()
        elif failure == 'queued':
            run.queued = True
        elif failure == 'expired':
            ledger._window_end = 0.
        elif failure == 'calls':
            for _ in range(40):
                run.budget.reserve_provider(0)
        elif failure == 'steps':
            for _ in range(40):
                run.budget.reserve_step()
        else:
            run.budget.reserve_provider(512*1024)
        before = run.budget.usage(), ledger.snapshot, run.budget.original_deadline
        with pytest.raises((RunnerError, BudgetExceeded)):
            host._preflight(run)
        assert before == (run.budget.usage(), ledger.snapshot, run.budget.original_deadline)


@pytest.mark.parametrize('operation,outcome', [('click','unknown'), ('wait','complete'), ('click','complete')])
async def test_only_returned_nonwait_dispatch_can_issue_capacity(tmp_path, monkeypatch, operation, outcome):
    with ExperimentLedger(tmp_path/'synthetic', create=True) as ledger:
        host = FlightsRunManager(object(), ledger, continuation=True)
        run = scoped(host)
        obs = Observation('obs', 'https://www.google.com', 'doc', '', {}, (), url=URL)
        action = Action(operation,'token' if operation=='click' else None,None,None,'obs', json.dumps({
            'semantic': {'origin':'https://www.google.com', 'url':URL,
                         'node': {'role':'option', 'name':'One way'} if operation=='click' else {}}}))
        async def fake_dispatch(_self, current, _obs, _action):
            current.recent_actions.append({'attempt_id':'actual-ack', 'page_changed':True})
            if outcome == 'unknown':
                current.queued = True
                raise RunnerError('outcome_unknown')
            return obs
        monkeypatch.setattr(RunManager,'_dispatch_one',fake_dispatch)
        if outcome == 'unknown':
            with pytest.raises(RunnerError,match='outcome_unknown'):
                await host._dispatch_one(run,obs,action)
        else:
            await host._dispatch_one(run,obs,action)
        assert host._completed == ('actual-ack' if operation=='click' and outcome=='complete' else None)
        assert host._committed == (frozenset({'One way'}) if operation=='click' and outcome=='complete' else frozenset())
        assert ledger.snapshot['steps'] == 1


async def test_already_validated_observation_repair_is_capture_free(tmp_path, monkeypatch):
    with ExperimentLedger(tmp_path/'synthetic',create=True) as ledger:
        host=FlightsRunManager(object(),ledger,continuation=True)
        run=scoped(host)
        old=Observation('old','https://www.google.com','doc','',{},(),url=URL)
        fresh=Observation('fresh','https://www.google.com','doc','',{},(),url=URL+'&tfs=YWJj')
        host._accepted=fresh
        host._accepted_generation=host._generation=host._baseline=2
        run.driver.page.url=fresh.url
        run.dom.observe=AsyncMock(side_effect=AssertionError('free recapture'))
        assert await host._refresh_stale(run,old) is fresh
        assert host.continuations==0 and run.dom.observe.await_count==0
        host._generation+=1
        with pytest.raises(RunnerError,match='navigation'):
            await host._refresh_stale(run,old)
        assert run.dom.observe.await_count==0


async def test_real_provider_mock_transport_serialized_bytes_are_projected(tmp_path):
    raw={'goal':'public goal','url':URL+'&tfs='+CANARY,'text':CANARY,
         'candidates':{'fill':{'token':{'operation':'fill','name':'Where from?',
             'node':{'role':'combobox','name':'Where from?','value':CANARY},
             'url':URL+'&tfs='+CANARY,'form':{'action':URL+'&tfs='+CANARY},
             'context':{'href':CANARY}}}},'recent_actions':[]}
    calls=[]
    with ExperimentLedger(tmp_path/'synthetic',create=True) as ledger:
        def handler(request):
            calls.append(request.content)
            assert CANARY.encode() not in request.content
            assert str(request.url)=='https://api.openai.com/v1/decisions'
            assert json.loads(request.content)['model']=='gpt-6-luna'
            assert ledger.snapshot['calls']==1
            return httpx.Response(200,json={'model':'gpt-6-luna','usage':{},'answers':[{
                'name':'operation','type':'choice','choice':'DONE','confidence':1,
                'probabilities':[{'value':'DONE','probability':1},{'value':False,'probability':0}]}]})
        original=FlightsProvider('test-only',ledger,transport=httpx.MockTransport(handler))
        wrapper=ProjectedProvider(original,lambda text:project_input(text,{'phase':'form'}))
        budget=Budget(Limits())
        result=await wrapper.decide((ChoiceQuestion('operation','choose',('DONE',)),),json.dumps(raw),
            budget=budget,privacy=PrivacyGate(PrivacyPolicy()))
        assert result.operation=='DONE' and len(calls)==budget.usage().calls==1
        assert wrapper.provider is original
        await wrapper.aclose()


async def test_run_pilot_complete_serialized_report_boundary(tmp_path,monkeypatch):
    from decision_browser import flights_pilot
    poison=URL+'&tfs='+CANARY
    class FakeProvider:
        def __init__(self,key,ledger):
            assert key=='test-only'
        async def aclose(self):
            pass
    class FakeManager:
        def __init__(self,provider,ledger,*,continuation):
            assert continuation is True
            self.failure_code=None
            self.scope_reason=None
            self.report={'status':'NOT_VERIFIED','raw':{'post_search':{'url':poison,'text':CANARY}},
                         'row':{'carrier':CANARY},'reasons':['invalid_snapshot']}
            self.trace=[{'name':CANARY,'value':CANARY,'url':poison,'operation':'click'}]
            self.diagnostics=[]
            self.continuations=1
        def start_task(self,request):
            self._run=SimpleNamespace(task=asyncio.create_task(asyncio.sleep(0)))
            return 'synthetic-run'
        def get_task(self,run_id):
            return SimpleNamespace(model_dump=lambda **_: {'terminal':'blocked','last_url':poison})
        async def aclose(self):
            pass
    monkeypatch.setenv('OPENAI_API_KEY','test-only')
    monkeypatch.setattr(flights_pilot,'query_is_current',lambda:True)
    monkeypatch.setattr(flights_pilot,'FlightsProvider',FakeProvider)
    monkeypatch.setattr(flights_pilot,'FlightsRunManager',FakeManager)
    monkeypatch.setattr(flights_pilot,'export_journal',lambda _: [{'event':'before_dispatch',
        'semantic_fingerprint':json.dumps({'url':poison,'node':{'value':CANARY}})}])
    with ExperimentLedger(tmp_path/'synthetic',create=True) as ledger:
        report=await flights_pilot.run_pilot(ledger)
        assert report['status']=='NOT_VERIFIED' and report['export_projection']
        assert CANARY not in json.dumps(report)
        assert ledger.snapshot['calls']==0
