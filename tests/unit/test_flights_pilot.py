import json

import pytest

from decision_browser.dom import Action, Observation
from decision_browser.experiment_budget import ExperimentLedger
from decision_browser.flights_pilot import (
    FlightsRunManager,
    pilot_request,
    source_provenance,
)
from decision_browser.flights_provider import FlightsProvider
from decision_browser.runner import RunManager, RunnerError


def action(name):
    return Action('click','a',None,None,'obs',json.dumps({'semantic':{
        'origin':'https://www.google.com','url':'https://www.google.com/travel/flights',
        'node':{'name':name[:160],'fullName':name}}}))


async def test_long_select_flight_is_rejected_before_attempt(tmp_path):
    with ExperimentLedger(tmp_path/'ledger', create=True) as ledger:
        provider = FlightsProvider('test-only',ledger)
        manager = FlightsRunManager(provider,ledger)
        run = manager._open(pilot_request(30))
        obs = Observation('obs','https://www.google.com','doc','',{},())
        with pytest.raises(RunnerError,match='pilot_out_of_scope'):
            await manager._dispatch_one(run,obs,action('Long route details '*40+' Select flight'))
        assert ledger.snapshot['steps'] == 0
        await provider.aclose()


async def test_failed_post_observation_retains_acknowledged_unknown_trace(tmp_path,monkeypatch):
    with ExperimentLedger(tmp_path/'ledger', create=True) as ledger:
        provider = FlightsProvider('test-only',ledger)
        manager = FlightsRunManager(provider,ledger)
        run = manager._open(pilot_request(30))
        async def partial(_self, current, _obs, _action):
            current.queued = True
            current.attempt_id = 'attempt'
            current.recent_actions.append({'attempt_id':'attempt'})
            raise RunnerError('outcome_unknown')
        monkeypatch.setattr(RunManager,'_dispatch_one',partial)
        with pytest.raises(RunnerError,match='outcome_unknown'):
            await manager._dispatch_one(run,Observation('o','','d','',{},()),action('One way'))
        assert manager.trace[0]['outcome'] == 'outcome_unknown'
        assert manager.trace[0]['acknowledged'] is True
        assert manager.trace[0]['attempt_id'] == 'attempt'
        assert ledger.snapshot['steps'] == 1
        await provider.aclose()


def test_source_provenance_has_exact_package_hashes():
    data=source_provenance()
    assert len(data['sha256']) == 64
    assert 'flights_pilot.py' in data['files'] and 'collector.js' in data['files']


@pytest.mark.parametrize('statuses', [('stale',),('pass','stale')])
async def test_evidence_read_rejects_unowned_navigation_before_or_after(tmp_path,statuses):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from decision_browser.dom import GuardResult
    with ExperimentLedger(tmp_path/'ledger', create=True) as ledger:
        provider=FlightsProvider('test-only',ledger)
        manager=FlightsRunManager(provider,ledger)
        run=manager._open(pilot_request(30))
        run.dom=SimpleNamespace(prepare=AsyncMock(return_value=object()),
            guard=AsyncMock(side_effect=[GuardResult(s,'document') for s in statuses]),
            _world=SimpleNamespace(evaluate=AsyncMock(return_value={})))
        with pytest.raises(RunnerError,match='navigation'):
            await manager._snapshot(run)
        assert run.dom._world.evaluate.await_count == len(statuses)-1
        await provider.aclose()


async def test_only_bound_airport_fills_are_offered_calendar_remains_clickable(tmp_path):
    from decision_browser.dom import Candidate
    with ExperimentLedger(tmp_path/'ledger',create=True) as ledger:
        provider=FlightsProvider('test-only',ledger)
        manager=FlightsRunManager(provider,ledger)
        run=manager._open(pilot_request(30))
        candidates={}
        for name in ['Where from?','Where to?','Departure']:
            semantics={op:{'origin':'https://www.google.com','url':run.request.start_url,
                'node':{'name':name}} for op in ['click','fill']}
            candidates[name]=Candidate(name,('click','fill'),semantics,None)
        _,_,bindings=manager._questions(run,Observation('o','https://www.google.com','d','',candidates,()))
        assert {row['name'] for row in bindings['fill'].values()} == {'Where from?','Where to?'}
        assert 'Departure' in {row['name'] for row in bindings['click'].values()}
        await provider.aclose()


def test_failure_diagnostic_is_closed_whitelist():
    from decision_browser.flights_pilot import safe_failure
    assert safe_failure(RunnerError('navigation')) == 'navigation'
    assert safe_failure(RunnerError('secret-user-controlled-text')) == 'unclassified_failure'
