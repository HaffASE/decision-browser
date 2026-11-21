"""Pilot metadata must never change guards, budgets, exceptions or private data."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from decision_browser.dom import Action, Observation
from decision_browser.experiment_budget import ExperimentLedger
from decision_browser.flights_pilot import FlightsRunManager, pilot_request
from decision_browser.runner import RunManager, RunnerError

CANARY = 'PRIVATE_PAGE_VALUE_TEST_ONLY'
OLD = 'https://www.google.com/travel/flights?private=' + CANARY
NEW = OLD + '&changed=1'


@pytest.fixture
def manager(tmp_path):
    path = tmp_path / 'synthetic-ledger.jsonl'
    with ExperimentLedger(path, create=True) as ledger:
        host = FlightsRunManager(SimpleNamespace(), ledger)
        run = host._open(pilot_request(30))
        host._run = run
        run.driver = SimpleNamespace(page=SimpleNamespace(url=OLD))
        obs = Observation('obs-' + CANARY, 'https://www.google.com',
                          'doc-' + CANARY, CANARY, {}, (), url=OLD)
        yield host, run, obs, path


@pytest.mark.parametrize('transition', [False, True])
async def test_decision_metadata_compares_without_provider_or_ledger_effect(manager, monkeypatch, transition):
    host, run, obs, path = manager
    before = path.read_bytes()
    calls = []
    selected = object()

    async def choose(_self, actual_run, actual_obs):
        calls.append((actual_run, actual_obs))
        if transition:
            actual_run.driver.page.url = NEW
        return selected

    monkeypatch.setattr(RunManager, '_choose', choose)
    assert await host._choose(run, obs) is selected
    assert calls == [(run, obs)] and path.read_bytes() == before
    assert host.diagnostics == [{'phase': 'choose', 'outcome': 'chosen',
                                 'acknowledged_actions': 0, 'url_matches_before': True,
                                 'url_matches_after': not transition}]
    assert CANARY not in json.dumps(host.diagnostics)
    assert run.result.usage.calls == run.result.usage.steps == 0


@pytest.mark.parametrize('exc', [RunnerError('stale'), RunnerError('blocked'),
                                RuntimeError(CANARY), asyncio.CancelledError(CANARY)])
async def test_decision_exception_and_cancellation_are_preserved(manager, monkeypatch, exc):
    host, run, obs, path = manager
    before = path.read_bytes()

    async def fail(*_args):
        run.driver.page.url = NEW
        raise exc

    monkeypatch.setattr(RunManager, '_choose', fail)
    with pytest.raises(type(exc)) as caught:
        await host._choose(run, obs)
    assert caught.value is exc and path.read_bytes() == before
    record = host.diagnostics[-1]
    assert record['outcome'] == ('stale' if isinstance(exc, RunnerError) and exc.code == 'stale' else 'failed')
    assert record['url_matches_before'] is True and record['url_matches_after'] is False
    assert CANARY not in json.dumps(host.diagnostics)


async def test_metadata_failure_cannot_mask_success_and_trace_is_bounded(manager, monkeypatch):
    host, run, obs, path = manager
    before = path.read_bytes()

    class BrokenPage:
        @property
        def url(self):
            raise RuntimeError(CANARY)

    run.driver.page = BrokenPage()

    async def done(*_args):
        return None

    monkeypatch.setattr(RunManager, '_choose', done)
    for _ in range(45):
        assert await host._choose(run, obs) is None
    assert len(host.diagnostics) == 40
    assert all(r['outcome'] == 'done' and r['url_matches_before'] is None
               and r['url_matches_after'] is None for r in host.diagnostics)
    assert path.read_bytes() == before and CANARY not in json.dumps(host.diagnostics)


@pytest.mark.parametrize('reason', ['stale_refresh:document_changed',
                                   'stale_refresh:observation_url_changed',
                                   'stale_refresh:live_page_url_changed', CANARY])
async def test_failed_refresh_records_only_fixed_reason_and_trigger(manager, monkeypatch, reason):
    host, run, obs, path = manager
    before = path.read_bytes()
    host.diagnostics.append({'phase': 'choose', 'outcome': 'stale'})
    exc = RunnerError('navigation')

    async def fail(*_args):
        run.result.evidence.append(reason)
        raise exc

    monkeypatch.setattr(RunManager, '_refresh_stale', fail)
    with pytest.raises(RunnerError) as caught:
        await host._refresh_stale(run, obs)
    assert caught.value is exc and path.read_bytes() == before
    record = host.diagnostics[-1]
    assert record['phase'] == 'stale_refresh' and record['outcome'] == 'failed'
    assert record['trigger'] == 'choose'
    assert record['reason'] == (None if reason == CANARY else reason)
    assert CANARY not in json.dumps(host.diagnostics)


async def test_refreshed_observation_is_returned_unchanged_without_budget_effect(manager, monkeypatch):
    host, run, obs, path = manager
    before = path.read_bytes()
    fresh = replace(obs, id='fresh')

    async def refresh(*_args):
        return fresh

    monkeypatch.setattr(RunManager, '_refresh_stale', refresh)
    assert await host._refresh_stale(run, obs) is fresh
    assert host.diagnostics[-1]['outcome'] == 'refreshed'
    assert host.diagnostics[-1]['trigger'] is None
    assert path.read_bytes() == before


@pytest.mark.parametrize('exc', [RuntimeError(CANARY), asyncio.CancelledError(CANARY)])
async def test_refresh_read_failure_and_cancellation_are_not_masked(manager, monkeypatch, exc):
    host, run, obs, path = manager
    before = path.read_bytes()

    async def fail(*_args):
        raise exc

    monkeypatch.setattr(RunManager, '_refresh_stale', fail)
    with pytest.raises(type(exc)) as caught:
        await host._refresh_stale(run, obs)
    assert caught.value is exc and path.read_bytes() == before
    assert host.diagnostics[-1]['outcome'] == 'failed'
    assert host.diagnostics[-1]['reason'] is None
    assert CANARY not in json.dumps(host.diagnostics)


@pytest.mark.parametrize('progress', [True, False, None, CANARY])
async def test_trace_copies_boolean_progress_and_post_relationships_only(manager, monkeypatch, progress):
    host, run, obs, _path = manager
    post = replace(obs, id='post', url=NEW)
    run.driver.page.url = NEW
    action = Action('click', 'a', None, None, obs.id, json.dumps({'semantic': {
        'url': OLD, 'node': {'name': 'One way'}, 'origin': obs.origin}}))

    async def dispatch(_self, current, _obs, _action):
        current.recent_actions.append({'attempt_id': 'synthetic-attempt', 'page_changed': progress})
        return post

    monkeypatch.setattr(RunManager, '_dispatch_one', dispatch)
    assert await host._dispatch_one(run, obs, action) is post
    record = host.trace[-1]
    assert record['page_changed'] == (progress if type(progress) is bool else None)
    assert record['post_observation'] == {'same_document': True, 'same_origin': True,
                                        'url_changed': True, 'live_url_matches': True}
    assert CANARY not in json.dumps(record['post_observation'])
    assert host.ledger.snapshot['steps'] == 1


async def test_dispatch_stale_exception_identifies_refresh_trigger_without_replay(manager, monkeypatch):
    host, run, obs, _path = manager
    action = Action('click', 'a', None, None, obs.id,
                    json.dumps({'semantic': {'node': {'name': 'One way'}}}))
    dispatches = []
    exc = RunnerError('stale')

    async def dispatch(*args):
        dispatches.append(args)
        raise exc

    monkeypatch.setattr(RunManager, '_dispatch_one', dispatch)
    with pytest.raises(RunnerError) as caught:
        await host._dispatch_one(run, obs, action)
    assert caught.value is exc and len(dispatches) == 1
    assert host.diagnostics[-1]['phase'] == 'dispatch'
    assert host.diagnostics[-1]['outcome'] == 'stale'
    assert host.trace[-1]['outcome'] == 'not_dispatched'
    assert host.ledger.snapshot['steps'] == 1


async def test_missing_post_relations_remain_unknown_and_prepared_actions_are_not_counted(manager, monkeypatch):
    host, run, obs, _path = manager
    obs = replace(obs, url=None, origin='', document_id='')
    host.trace.extend([{'acknowledged': False}, {'acknowledged': 'true'}])
    action = Action('click', 'a', None, None, obs.id,
                    json.dumps({'semantic': {'node': {'name': 'One way'}}}))

    async def dispatch(_self, current, _obs, _action):
        current.recent_actions.append({'attempt_id': 'new-attempt'})
        return replace(obs, id='post')

    async def done(*_args):
        return None

    monkeypatch.setattr(RunManager, '_dispatch_one', dispatch)
    monkeypatch.setattr(RunManager, '_choose', done)
    await host._dispatch_one(run, obs, action)
    assert host.trace[-1]['page_changed'] is None
    assert all(value is None for value in host.trace[-1]['post_observation'].values())
    await host._choose(run, obs)
    assert host.diagnostics[-1]['acknowledged_actions'] == 1
    assert host.diagnostics[-1]['url_matches_before'] is None
