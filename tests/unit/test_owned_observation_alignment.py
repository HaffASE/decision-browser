"""One corrective read cannot adopt a new scope/document or replay a click."""
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_task_scope_manager import open_scoped_run

from decision_browser.dom import Action, DispatchResult, Observation
from decision_browser.flights_pilot import export_journal
from decision_browser.runner import RunManager, RunnerError

CANARY = 'TEST_ONLY_PRIVATE_VALUE_NOT_A_CREDENTIAL'
OLD = 'https://example.test/'
ACCEPTED = 'https://example.test/?trip=oneway'


def raced_dispatch(kind='stable'):
    host, run = open_scoped_run()
    host._run = run
    provider = Mock(side_effect=AssertionError('No provider during corrective read'))
    host._provider_factory = provider
    page = SimpleNamespace(url=OLD)
    run.driver = SimpleNamespace(page=page)
    obs = Observation('before', 'https://example.test', 'document', '', {}, (), url=OLD)
    captured = replace(obs, id='captured', text='One way selected')
    corrected = replace(captured, id='corrected', url=ACCEPTED)
    if kind == 'document':
        corrected = replace(corrected, document_id='replacement-' + CANARY)
    elif kind == 'origin':
        corrected = replace(corrected, origin='https://foreign.test')
    elif kind == 'url':
        corrected = replace(corrected, url='https://example.test/later?' + CANARY)
    elif kind == 'popup':
        corrected = replace(corrected, unsupported=('popup:1',))

    class DOM:
        dispatches = 0
        reads = 0

        async def dispatch(self, action, before):
            self.dispatches += 1
            before(action)
            page.url = ACCEPTED
            return DispatchResult('pass', '')

        async def observe(self):
            self.reads += 1
            assert self.reads <= 2, 'Corrective observations must not loop'
            if self.reads == 1:
                if kind == 'aligned':
                    return corrected
                if kind == 'legacy':
                    return replace(captured, url=None)
                if kind == 'first_popup':
                    return replace(captured, unsupported=('popup:1',))
                if kind == 'first_document':
                    return replace(captured, document_id='new-document')
                if kind == 'cross_origin_live':
                    page.url = 'https://foreign.test/'
                return captured
            if kind == 'read_failed':
                raise RuntimeError(CANARY)
            if kind == 'live_url':
                page.url = 'https://example.test/later?' + CANARY
            return corrected

    dom = DOM()
    run.dom = dom
    action = Action('click', 'oneway', None, None, obs.id, json.dumps({'semantic': {
        'origin': obs.origin, 'url': OLD, 'node': {'name': 'One way'}, 'form': None,
    }}))
    return host, run, obs, action, dom, corrected, provider


async def test_corrective_read_returns_aligned_snapshot_without_effect_or_provider_replay():
    host, run, obs, action, dom, corrected, provider = raced_dispatch()
    post = await host._dispatch_one(run, obs, action)
    assert post is corrected and post.url == run.driver.page.url
    assert (dom.dispatches, dom.reads, run.result.usage.steps) == (1, 2, 1)
    assert run.result.evidence == ['post_observation:owned_url_reobserved']
    assert run.queued is False and run.attempt_id is None
    events = export_journal(run)
    assert len([x for x in events if x['event'] == 'before_dispatch']) == 1
    assert [x['status'] for x in events if x['event'] == 'outcome'] == ['acknowledged']
    provider.assert_not_called()


@pytest.mark.parametrize('kind', ['document', 'origin', 'url', 'popup', 'live_url', 'read_failed'])
async def test_unstable_corrective_read_stays_unknown_and_consumed(kind):
    host, run, obs, action, dom, _corrected, provider = raced_dispatch(kind)
    with pytest.raises(RunnerError, match='^outcome_unknown$') as caught:
        await host._dispatch_one(run, obs, action)
    host._apply(run, caught.value)
    assert (dom.dispatches, dom.reads, run.result.usage.steps) == (1, 2, 1)
    assert run.result.terminal == 'outcome_unknown'
    assert 'post_observation:owned_url_reobserved' not in run.result.evidence
    assert CANARY not in run.result.model_dump_json()
    events = export_journal(run)
    assert [x['status'] for x in events if x['event'] == 'outcome'] == ['acknowledged']
    failures = [x for x in events if x['event'] == 'post_observe_failure']
    assert len(failures) == 1 and failures[0]['status'] == 'outcome_unknown'
    provider.assert_not_called()


@pytest.mark.parametrize('kind', ['first_popup', 'cross_origin_live'])
async def test_existing_scope_or_popup_failure_never_gets_corrective_read(kind):
    host, run, obs, action, dom, _corrected, provider = raced_dispatch(kind)
    with pytest.raises(RunnerError, match='^outcome_unknown$'):
        await host._dispatch_one(run, obs, action)
    assert (dom.dispatches, dom.reads, run.result.usage.steps) == (1, 1, 1)
    provider.assert_not_called()


@pytest.mark.parametrize('kind', ['aligned', 'legacy'])
async def test_aligned_and_explicit_legacy_observation_need_no_extra_read(kind):
    host, run, obs, action, dom, _corrected, provider = raced_dispatch(kind)
    await host._dispatch_one(run, obs, action)
    assert (dom.dispatches, dom.reads, run.result.usage.steps) == (1, 1, 1)
    assert run.result.evidence == []
    provider.assert_not_called()


@pytest.mark.parametrize('kind', ['first_document', 'wrong_href', 'nonisolated', 'standard_form'])
async def test_correction_cannot_expand_original_transition_or_mode_gates(kind):
    host, run, obs, action, dom, _corrected, provider = raced_dispatch(kind)
    if kind == 'wrong_href':
        fingerprint = json.loads(action.fingerprint)
        fingerprint['semantic']['node']['href'] = 'https://example.test/expected'
        action = replace(action, fingerprint=json.dumps(fingerprint))
    elif kind == 'nonisolated':
        # An explicit run double tests this post-action mode boundary; real
        # task-scope admission rejects this mode earlier, in _open.
        run.request = run.request.model_copy(update={'mode': 'existing_chrome'})
    elif kind == 'standard_form':
        # An already admitted action double isolates the post-action mode gate.
        admitted = await host._admit_scope(run, host._scope(run, obs, action))
        host = RunManager(provider, admission_mode='standard_form')
        host._run = run
        host._admit_scope = AsyncMock(return_value=admitted)
    with pytest.raises(RunnerError, match='^outcome_unknown$'):
        await host._dispatch_one(run, obs, action)
    assert (dom.dispatches, dom.reads, run.result.usage.steps) == (1, 1, 1)
    assert 'post_observation:owned_url_reobserved' not in run.result.evidence
    provider.assert_not_called()


async def test_explicit_destination_preserves_existing_path_without_corrective_read():
    host, run, obs, action, dom, _corrected, provider = raced_dispatch()
    fingerprint = json.loads(action.fingerprint)
    fingerprint['semantic']['node']['href'] = ACCEPTED
    action = replace(action, fingerprint=json.dumps(fingerprint))
    await host._dispatch_one(run, obs, action)
    assert (dom.dispatches, dom.reads, run.result.usage.steps) == (1, 1, 1)
    assert run.result.evidence == []
    provider.assert_not_called()
