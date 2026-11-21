"""Offline wave-five boundary regressions; no live flight/provider activity."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_flights_continuation_transaction import scoped
from test_flights_evidence import ROW, evidence, synchronize, validate

from decision_browser import flights_pilot
from decision_browser.dom import Action, Candidate, Observation
from decision_browser.experiment_budget import ExperimentLedger
from decision_browser.flights_evidence import FlightsQuery, _aria_row
from decision_browser.flights_pilot import FlightsRunManager
from decision_browser.flights_provider import compact_input
from decision_browser.provider import ProviderError
from decision_browser.runner import RunManager, RunnerError


@pytest.mark.parametrize('price', ['1,234', '12,345.50'])
def test_grouped_price_text(price):
    data = evidence()
    data[1]['results']['rows'][0].update(
        aria_label=ROW.replace('128 British pounds', price+' British pounds'),
        text='JetBlue 11:29 AM – 6:53 PM £'+price)
    synchronize(data)
    assert validate(data)['status'] == 'PASS'
    data[1]['results']['rows'][0]['text'] += '9'
    synchronize(data)
    assert validate(data)['status'] == 'NOT_VERIFIED'


@pytest.mark.parametrize('arrival', ['Neverday, February 31, 9999', 'Tuesday, November 31',
                                    'Monday, November 10', 'Tuesday, November 9'])
def test_impossible_or_inconsistent_arrival_is_rejected(arrival):
    label = ROW.replace('on Tuesday, November 10. Total', 'on '+arrival+'. Total')
    assert _aria_row(label, FlightsQuery()) is None


def test_dotted_carrier_stays_complete_and_query_bound():
    assert _aria_row(ROW.replace('JetBlue.', 'Example Corp. Air.'), FlightsQuery())['carrier'] == 'Example Corp. Air'
    assert _aria_row(ROW.replace('JetBlue.', 'Example Corp. Air.').replace('Kennedy', 'Other'), FlightsQuery()) is None


def test_same_millisecond_capture_uses_comparable_precision():
    data = evidence()
    data[2]['acknowledged_at_ms'] = 2100.5
    assert validate(data)['status'] == 'PASS'
    data[2]['acknowledged_at_ms'] = 2101.0
    assert 'invalid_search_chronology' in validate(data)['reasons']
    data[2]['acknowledged_at_ms'] = 1000
    assert 'invalid_search_chronology' in validate(data)['reasons']


async def test_full_date_binding_does_not_index_unrelated_day_label(tmp_path):
    with ExperimentLedger(tmp_path/'ledger', create=True) as ledger:
        host = FlightsRunManager(object(), ledger, continuation=True)
        run = scoped(host)
        host._state = {'phase':'calendar', 'form_observed':False}
        host._day_labels = ('obs', {'other':'November 11, 2026'})
        node = {'role':'button', 'name':'Tue, Nov 10, 2026', 'fullName':'Tue, Nov 10, 2026'}
        semantics = {'click':{'origin':'https://www.google.com', 'url':run.request.start_url, 'node':node}}
        candidate = Candidate('day', ('click',), semantics, {'visible':True})
        obs = Observation('obs', 'https://www.google.com', 'doc', '', {'day':candidate}, (), url=run.request.start_url)
        # Calendar inventory may reject this intentionally partial fixture, but
        # must never crash looking up a different candidate's label mapping.
        try:
            host._questions(run, obs)
        except RunnerError:
            pass


async def test_open_one_way_does_not_commit_option(tmp_path, monkeypatch):
    with ExperimentLedger(tmp_path/'ledger', create=True) as ledger:
        host = FlightsRunManager(object(), ledger, continuation=True)
        run = scoped(host)
        obs = Observation('obs','https://www.google.com','doc','',{},(),url=run.request.start_url)
        host._state = {'phase':'form'}
        host._accepted = obs
        action = Action('click','trip',None,None,'obs',json.dumps({'semantic':{
            'node':{'role':'combobox','name':'One way'}}}))
        async def dispatch(_self,current,*_):
            current.recent_actions.append({'attempt_id':'ack'})
            return obs
        monkeypatch.setattr(RunManager,'_dispatch_one',dispatch)
        await host._dispatch_one(run,obs,action)
        assert 'One way' not in host._committed
        assert host._trip_menu == (obs.document_id,obs.url,host._generation,'ack')


async def test_last_allowed_provider_answer_can_be_used(tmp_path, monkeypatch):
    with ExperimentLedger(tmp_path/'ledger', create=True) as ledger:
        host = FlightsRunManager(object(),ledger,continuation=True)
        run = scoped(host)
        obs = Observation('obs','https://www.google.com','doc','',{},(),url=run.request.start_url)
        monkeypatch.setattr(host,'_watch',lambda _:None)
        monkeypatch.setattr(host,'_prove',AsyncMock())
        for _ in range(39):
            run.budget.reserve_provider(0)
            ledger.reserve_call(0)
        async def choose(*_):
            run.budget.reserve_provider(0)
            ledger.reserve_call(0)
        monkeypatch.setattr(RunManager,'_choose',choose)
        assert await host._choose(run,obs) is None
        with pytest.raises(RunnerError):
            await host._choose(run,obs)


async def test_read_only_snapshot_at_final_step_keeps_generation_guards(tmp_path, monkeypatch):
    from decision_browser.dom import GuardResult
    with ExperimentLedger(tmp_path/'ledger',create=True) as ledger:
        host = FlightsRunManager(object(),ledger,continuation=True)
        run = scoped(host)
        obs = Observation('obs','https://www.google.com','doc','',{},(),url=run.request.start_url)
        run.current_observation = obs
        run.dom.prepare = AsyncMock(return_value=object())
        run.dom.guard = AsyncMock(return_value=GuardResult('pass',None))
        run.dom._world = SimpleNamespace(evaluate=AsyncMock(return_value={'captured':True}))
        monkeypatch.setattr(host,'_prove',AsyncMock())
        for _ in range(40):
            run.budget.reserve_step()
            ledger.reserve_step()
        assert await host._snapshot(run) == {'captured':True}
        async def changed(*_):
            host._generation += 1
            return {}
        run.dom._world.evaluate.side_effect = changed
        with pytest.raises(RunnerError,match='navigation'):
            await host._snapshot(run)


def test_compact_context_rejects_malformed_structure():
    for data in ({'goal':'x','candidates':{'click':[]}},
                 {'goal':'x','candidates':{'click':{'a':{'node':[], 'name':'x'}}}},
                 {'goal':False,'candidates':{}},
                 {'goal':'x','candidates':{}, 'elements':{'evil':{}}}):
        with pytest.raises(ProviderError,match='invalid_pilot_context'):
            compact_input(json.dumps(data))


async def test_expired_live_query_stops_before_provider_and_reservation(tmp_path,monkeypatch):
    from datetime import UTC, datetime

    from decision_browser import flights_query
    class HistoricalClock:
        @staticmethod
        def now(tz):
            assert tz is UTC
            return datetime(2026, 11, 17, tzinfo=UTC)
    monkeypatch.setattr(flights_query, 'datetime', HistoricalClock)
    monkeypatch.setenv('OPENAI_API_KEY','test-only')
    monkeypatch.setattr(flights_pilot,'FlightsProvider',lambda *_:pytest.fail('constructed provider'))
    with ExperimentLedger(tmp_path/'ledger',create=True) as ledger:
        report = await flights_pilot.run_pilot(ledger)
        assert report == {'status':'NOT_RUN','reason':'expired_query'}
        assert ledger.snapshot['calls'] == ledger.snapshot['steps'] == 0


async def test_failure_retains_partial_diagnostics_and_both_cleanup_causes(tmp_path, monkeypatch):
    class Provider:
        def __init__(self,*_): pass
        async def aclose(self): raise RuntimeError('secret cleanup')
    class Manager:
        def __init__(self,*_,**__):
            self.trace=[{'operation':'click','name':'One way','acknowledged':True}]
            self.diagnostics=[{'phase':'choose','outcome':'failed'}]
            self.report={'status':'NOT_VERIFIED','reasons':['incomplete_capture']}
            self.continuations=1
            self.failure_code='outcome_unknown'
            self.scope_reason=None
        def start_task(self,_): raise RunnerError('outcome_unknown')
        async def aclose(self): raise RuntimeError('secret manager')
    monkeypatch.setattr(flights_pilot,'FlightsProvider',Provider)
    monkeypatch.setattr(flights_pilot,'FlightsRunManager',Manager)
    monkeypatch.setenv('OPENAI_API_KEY','test-only')
    # Explicit historical fixture clock, not a live-query authorization.
    monkeypatch.setattr(flights_pilot,'query_is_current',lambda:True,raising=False)
    with ExperimentLedger(tmp_path/'ledger',create=True) as ledger:
        report=await flights_pilot.run_pilot(ledger)
        assert report['status']=='NOT_VERIFIED' and report['failure_code']=='outcome_unknown'
        assert report['actions'][0]['name']=='One way' and report['diagnostics']
        assert len(report['cleanup_failures'])==2 and 'secret' not in json.dumps(report)


@pytest.mark.parametrize('mutation', ['bool_goal','bool_state','extra_root','unknown_phase','node_shape','node_flag','reference','extra_node','duplicate'])
def test_compact_context_closed_schema_at_owning_and_generic_boundaries(mutation):
    from test_model_context import TOKEN, context, descriptor, questions

    from decision_browser.decision_codec import CodecError
    from decision_browser.model_context import prepare_context
    source = context({'click':{TOKEN:descriptor()}})
    q, heads = questions(source['candidates'])
    compact = json.loads(compact_input(json.dumps(source)))
    if mutation == 'bool_goal': compact['goal'] = True
    elif mutation == 'bool_state': compact['state'] = {'phase':'form','ready':1}
    elif mutation == 'extra_root': compact['unexpected'] = 'x'
    elif mutation == 'unknown_phase': compact['state'] = {'phase':'arbitrary'}
    elif mutation == 'node_shape': compact['elements']['e0'] = []
    elif mutation == 'node_flag': compact['elements']['e0']['expanded'] = 'true'
    elif mutation == 'reference': compact['candidates']['click'][TOKEN]['element'] = 'missing'
    elif mutation == 'extra_node': compact['elements']['e0']['unexpected'] = 'x'
    raw = json.dumps(compact)
    if mutation == 'duplicate': raw = raw.replace('"goal":', '"goal":true,"goal":', 1)
    with pytest.raises(CodecError,match='invalid_context'):
        prepare_context(q,raw,target_heads=heads)


def test_compact_context_privacy_gate_precedes_trusted_marker():
    from test_model_context import TOKEN, context, descriptor, questions

    from decision_browser.contracts import PrivacyPolicy
    from decision_browser.model_context import prepare_context
    from decision_browser.privacy import PrivacyError, PrivacyGate
    source = context({'click':{TOKEN:descriptor()}})
    source['goal'] = 'PRIVATE_LITERAL_42'
    q, heads = questions(source['candidates'])
    raw = compact_input(json.dumps(source))
    with pytest.raises(PrivacyError):
        prepare_context(q,raw,target_heads=heads,
            privacy=PrivacyGate(PrivacyPolicy(blocked_literals=['PRIVATE_LITERAL_42'])))


@pytest.mark.parametrize('path,value', [
    (('node','options'), 'not an option array'),
    (('node','option'), {'label':'x','value':True}),
    (('context','focused'), 1),
    (('context','unexpected'), False),
    (('form','action'), False),
    (('form','defaultSubmitter'), {'name':True}),
    (('node','state'), {'disabled':1}),
])
def test_compaction_rejects_unknown_or_mistyped_semantic_metadata(path,value):
    entry={'name':'Search','node':{'role':'button','name':'Search'},'context':{},'form':{}}
    entry[path[0]][path[1]]=value
    with pytest.raises(ProviderError,match='invalid_pilot_context'):
        compact_input(json.dumps({'goal':'Flights','candidates':{'click':{'a':entry}}}))


@pytest.mark.parametrize('field,value', [
    ('completion', {'total':True,'pending':0}),
    ('scroll', {'up':True,'down':'false'}),
    ('recent_actions', [{'operation':'click','acknowledged':1}]),
    ('recent_actions', [{'operation':'click','post_observation':{'same_document':'yes'}}]),
])
def test_compaction_rejects_mistyped_trusted_metadata(field,value):
    with pytest.raises(ProviderError,match='invalid_pilot_context'):
        compact_input(json.dumps({'goal':'Flights','candidates':{},field:value}))


def test_invalid_group_suffix_is_not_a_supported_operation():
    with pytest.raises(ProviderError,match='invalid_pilot_context'):
        compact_input(json.dumps({'goal':'Flights','candidates':{'click_groupjunk':{}}}))


@pytest.mark.parametrize('failure', ['source','ledger','journal'])
async def test_partial_report_survives_final_enrichment_io_failure(tmp_path, monkeypatch, failure):
    import asyncio

    from decision_browser.experiment_budget import ExperimentBudgetError
    class Provider:
        def __init__(self,*_): pass
        async def aclose(self): pass
    class Manager:
        def __init__(self,*_,**__):
            self.trace=[{'operation':'click','name':'One way','acknowledged':True}]
            self.diagnostics=[{'phase':'choose','outcome':'failed'}]
            self.report={'status':'NOT_VERIFIED','reasons':['incomplete_capture']}
            self.continuations=1
            self.failure_code='outcome_unknown'
            self.scope_reason=None
        def start_task(self,_):
            self._run=SimpleNamespace(task=asyncio.create_task(asyncio.sleep(0)))
            return 'synthetic-run'
        def get_task(self,_):
            return SimpleNamespace(model_dump=lambda **_:{'terminal':'blocked','last_url':None})
        async def aclose(self): pass
    calls=[]
    def provenance():
        calls.append(1)
        if failure=='source' and len(calls)==2:
            raise OSError('PRIVATE_ENRICHMENT_FAILURE')
        return {'sha256':'synthetic'}
    def journal(_):
        if failure=='journal':
            raise OSError('PRIVATE_ENRICHMENT_FAILURE')
        return []
    monkeypatch.setenv('OPENAI_API_KEY','test-only')
    monkeypatch.setattr(flights_pilot,'query_is_current',lambda:True)
    monkeypatch.setattr(flights_pilot,'source_provenance',provenance)
    monkeypatch.setattr(flights_pilot,'export_journal',journal)
    monkeypatch.setattr(flights_pilot,'FlightsProvider',Provider)
    monkeypatch.setattr(flights_pilot,'FlightsRunManager',Manager)
    with ExperimentLedger(tmp_path/'ledger',create=True) as ledger:
        if failure=='ledger':
            def snapshot(_):
                raise ExperimentBudgetError('ledger_missing_or_replaced')
            monkeypatch.setattr(ExperimentLedger,'snapshot',property(snapshot))
        report=await flights_pilot.run_pilot(ledger)
        assert report['status']=='NOT_VERIFIED'
        assert report['failure_code']=='outcome_unknown'
        assert report['actions'][0]['name']=='One way' and report['diagnostics']
        assert report['run_id']=='synthetic-run'
        assert 'PRIVATE_ENRICHMENT_FAILURE' not in json.dumps(report)
        if failure=='source':
            assert report['source_stable'] is False
            assert report['report_failures']==['source_provenance_unavailable']
        elif failure=='ledger':
            assert report['experiment'] is None
            assert report['report_failures']==['experiment_snapshot_unavailable']


def test_huge_history_timestamp_has_structured_context_error():
    data={'goal':'Flights','candidates':{},'recent_actions':[{'acknowledged_at_ms':10**1000}]}
    with pytest.raises(ProviderError,match='invalid_pilot_context'):
        compact_input(json.dumps(data))


def test_legacy_native_select_descriptions_are_typed_not_new_dispatch_authority():
    node={'role':'combobox','name':'Cabin','selected':['economy'],
          'options':[['economy','Economy',False]],'option':{'value':'economy','label':'Economy'}}
    data={'goal':'Flights','candidates':{'select':{'a':{'name':'Economy','operation':'select','node':node}}}}
    assert json.loads(compact_input(json.dumps(data)))['elements']['e0']['options']==[['economy','Economy',False]]
    for field,value in [('options',[['x','X',1]]),('option',{'value':'x','label':'X','extra':'x'}),('selected',[False])]:
        changed=json.loads(json.dumps(data))
        changed['candidates']['select']['a']['node'][field]=value
        with pytest.raises(ProviderError,match='invalid_pilot_context'):
            compact_input(json.dumps(changed))
