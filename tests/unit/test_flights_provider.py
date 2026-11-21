"""No network: pilot cannot silently spend on Responses or reset reservations."""
import json

import httpx
import pytest

from decision_browser.budget import Budget
from decision_browser.contracts import Limits, PrivacyPolicy
from decision_browser.experiment_budget import ExperimentLedger
from decision_browser.flights_provider import FlightsProvider
from decision_browser.privacy import PrivacyGate
from decision_browser.provider import ProviderError


@pytest.mark.parametrize('path,payload,image', [
    ('/v1/responses', {'model': 'gpt-6-luna'}, False),
    ('/v1/decisions', {'model': 'other'}, False),
    ('/v1/decisions', {'model': 'gpt-6-luna'}, True),
])
async def test_reject_before_socket_and_reservation(tmp_path, path, payload, image):
    with ExperimentLedger(tmp_path/'ledger', create=True) as ledger:
        provider = FlightsProvider('test-key', ledger, transport=httpx.MockTransport(
            lambda _: pytest.fail('socket attempted')))
        with pytest.raises(ProviderError, match='flights_decisions_only'):
            await provider._post(path, payload, contains_image=image,
                budget=Budget(Limits()), privacy=PrivacyGate(PrivacyPolicy()))
        assert ledger.snapshot['calls'] == 0
        await provider.aclose()


async def test_reserved_before_socket_usage_recorded_no_retry(tmp_path):
    with ExperimentLedger(tmp_path/'ledger', create=True) as ledger:
        def handler(request):
            assert ledger.snapshot['calls'] == 1
            assert json.loads(request.content)['model'] == 'gpt-6-luna'
            return httpx.Response(200, json={'usage': {'input_tokens': 8, 'output_tokens': 0}})
        provider = FlightsProvider('test-key', ledger, transport=httpx.MockTransport(handler))
        await provider._post('/v1/decisions', {'model':'gpt-6-luna'},
            budget=Budget(Limits()), privacy=PrivacyGate(PrivacyPolicy()))
        assert ledger.snapshot['calls'] == 1
        await provider.aclose()


async def test_text_and_visual_helpers_structurally_disabled(tmp_path):
    with ExperimentLedger(tmp_path/'ledger', create=True) as ledger:
        provider = FlightsProvider('test-key', ledger)
        for method, code in [(provider.generate_text, "flights_missing_bound_value"),
                             (provider.inspect_visual, "flights_decisions_only")]:
            with pytest.raises(ProviderError, match=code):
                await method('anything')
        assert ledger.snapshot['calls'] == 0
        await provider.aclose()


def test_compaction_preserves_all_choices_and_state_with_shared_elements():
    from decision_browser.flights_provider import compact_input
    entry = {'name':'Origin', 'node':{'role':'combobox','value':'New York',
        'state':{'disabled':False,'aria':{'expanded':'false','selected':None}}},
        'context':{'focused':True},'form':{},'destination':None}
    data = {'goal':'Flights','candidates':{'click':{'a':entry},'fill':{'a':entry}}}
    packed = json.loads(compact_input(json.dumps(data)))
    assert set(packed['candidates']) == {'click','fill'}
    assert packed['candidates']['click']['a']['element'] == packed['candidates']['fill']['a']['element']
    assert len(packed['elements']) == 1
    assert packed['elements']['e0']['value'] == 'New York'
    assert packed['elements']['e0']['state']['aria']['expanded'] == 'false'
