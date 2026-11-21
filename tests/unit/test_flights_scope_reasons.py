"""Closed reasons behind pilot_out_of_scope; the gate itself is unchanged."""
import copy
import json

import pytest

from decision_browser.dom import Action, Observation
from decision_browser.experiment_budget import ExperimentLedger
from decision_browser.flights_continuation import (
    DATE_DONE,
    JFK,
    LAX,
    STATE_REASONS,
    PilotStateError,
    state_shape,
    validate_state,
)
from decision_browser.flights_pilot import (
    SCOPE_REASONS,
    FlightsRunManager,
    pilot_request,
)
from decision_browser.runner import RunnerError

URL = 'https://www.google.com/travel/flights?hl=en'
CANARY = 'PRIVATE_PAGE_VALUE_TEST_ONLY'
COMMITTED = frozenset({'One way', JFK, LAX, DATE_DONE})


def state():
    return {'evidence': {
        'schema_version': 1, 'captured_at_ms': 1000., 'url': URL,
        'form': {'origin': [{'label': 'Where from? New York JFK', 'value': 'New York', 'expanded': 'false'}],
                 'destination': [{'label': 'Where to? Los Angeles LAX', 'value': 'Los Angeles', 'expanded': 'false'}],
                 'departure': [{'label': 'Departure', 'value': 'Tue, Nov 10', 'expanded': None}],
                 'controls': ['One way', '1 passenger 1', 'Economy (include Basic)']},
        'results': {'headings': [], 'query_summaries': [], 'adult_summaries': [],
                    'displayed_row_count': 0, 'rows': []},
        'hazards': {'loading': False, 'captcha': False, 'no_flights': False, 'dialog': False},
        'truncated': False}, 'dialogs': [], 'password': False, 'calendar': False,
        'trip_picker': False}


def action(name):
    return Action('click', 'a', None, None, 'obs', json.dumps({'semantic': {
        'origin': 'https://www.google.com', 'url': 'https://www.google.com/travel/flights',
        'node': {'name': name[:160], 'fullName': name}}}))


def where_else_picker():
    """Shape observed on real Google Flights after clicking Where from? (2026-10-11 probe)."""
    value = state()
    value['evidence']['form'] = {'origin': [], 'destination': [], 'departure': [], 'controls': ['Where else?']}
    value['evidence']['hazards']['dialog'] = True
    value['dialogs'] = ['unknown']
    return value


def reason(value, committed=frozenset(), **kwargs):
    kwargs = {'search': False, 'continuing': False, **kwargs}
    with pytest.raises(PilotStateError) as caught:
        validate_state(value, URL, committed, **kwargs)
    assert caught.value.reason in STATE_REASONS and str(caught.value) == 'pilot_state'
    return caught.value.reason


def mutate(path, item):
    value = state()
    target = value
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = item
    return value


def test_observed_where_else_picker_names_first_failed_check():
    assert reason(where_else_picker()) == 'trip_control_count'


@pytest.mark.parametrize('value,expected', [
    (None, 'state_not_object'),
    ({'evidence': {}}, 'snapshot_invalid'),
    (mutate(('evidence', 'url'), URL + '&tfs=YWJj'), 'url_mismatch'),
    (mutate(('evidence', 'truncated'), True), 'evidence_truncated'),
    (mutate(('evidence', 'hazards', 'captcha'), True), 'captcha'),
    (mutate(('password',), True), 'password_or_signin'),
    (mutate(('evidence', 'form', 'controls'), ['One way', 'Round trip', '1 passenger 1', 'Economy (include Basic)']),
     'trip_control_count'),
    (mutate(('evidence', 'form', 'controls'), ['One way', '1 passenger 1']), 'cabin_control_count'),
    (mutate(('evidence', 'form', 'controls'), ['One way', 'Economy (include Basic)']), 'passenger_control_count'),
    (mutate(('evidence', 'form', 'departure'), []), 'departure_field_count'),
    (mutate(('evidence', 'form', 'origin'), []), 'origin_field_count'),
    (mutate(('evidence', 'form', 'origin'), [{'label': 'Where from?', 'value': '', 'expanded': 'false'}] * 2),
     'origin_expanded_count'),
    (mutate(('evidence', 'form', 'destination'), [{'label': 'Where else?', 'value': '', 'expanded': 'false'}]),
     'destination_label_noncanonical'),
    (mutate(('evidence', 'form', 'origin'), [{'label': 'Where from? New York JFK', 'value': '', 'expanded': 'true'}]),
     'origin_expanded_label'),
    (mutate(('dialogs',), 'x'), 'dialogs_not_list'),
    (mutate(('dialogs',), ['calendar', 'calendar']), 'multiple_dialogs'),
    (mutate(('dialogs',), ['calendar']), 'dialog_hazard_mismatch'),
    (mutate(('evidence', 'form', 'departure'), [{'label': 'Return', 'value': '', 'expanded': None}]), 'departure_label'),
])
def test_each_rejection_has_its_own_reason(value, expected):
    assert reason(value) == expected


def test_committed_and_mode_reasons():
    assert reason(mutate(('evidence', 'form', 'controls'), ['Round trip', '1 passenger 1', 'Economy (include Basic)']),
                  COMMITTED) == 'committed_trip_lost'
    assert reason(state(), frozenset({'One way'}), search=True) == 'search_not_ready'
    picker = mutate(('calendar',), True)
    picker['evidence']['hazards']['dialog'], picker['dialogs'] = True, ['calendar']
    assert reason(picker, COMMITTED, continuing=True) == 'continuation_picker_open'


def test_reasons_never_widen_acceptance():
    assert validate_state(state(), URL, COMMITTED, search=False, continuing=True)['phase'] == 'form'
    with pytest.raises(ValueError):
        validate_state(where_else_picker(), URL, frozenset({'One way'}), search=False, continuing=False)
    with pytest.raises(AssertionError):
        PilotStateError('free text ' + CANARY)


def test_state_shape_is_counts_booleans_and_closed_enums_only():
    value = where_else_picker()
    value['evidence']['form']['controls'] = [CANARY]
    value['evidence']['form']['origin'] = [{'label': CANARY, 'value': CANARY, 'expanded': 'true'}]
    value['evidence']['url'] = URL + '&tfs=' + CANARY
    value['dialogs'] = [CANARY, 'origin']
    shape = state_shape(value)
    assert CANARY not in json.dumps(shape)
    assert shape['dialogs'] == ['unknown', 'origin'] and shape['origin_fields'] == 1
    assert shape['origin_canonical_labels'] == 0 and shape['other_controls'] == 1
    assert state_shape(None) is None
    assert state_shape({'evidence': CANARY, 'trip_menu': CANARY})['trip_menu'] is None


async def test_out_of_scope_records_reason_without_changing_failure_code(tmp_path):
    with ExperimentLedger(tmp_path / 'ledger', create=True) as ledger:
        manager = FlightsRunManager(object(), ledger)  # type: ignore[arg-type]
        run = manager._open(pilot_request(30))
        obs = Observation('obs', 'https://www.google.com', 'doc', '', {}, ())
        with pytest.raises(RunnerError, match='pilot_out_of_scope'):
            await manager._dispatch_one(run, obs, action('Select flight ' + CANARY))
        assert manager.scope_reason == 'forbidden_control_name'
        assert manager.diagnostics[-1] == {'phase': 'scope_check', 'outcome': 'rejected',
                                           'reason': 'forbidden_control_name', 'acknowledged_actions': 0}
        assert CANARY not in json.dumps(manager.diagnostics) and ledger.snapshot['steps'] == 0
        with pytest.raises(AssertionError):
            manager._out_of_scope('not_a_closed_reason')


async def test_preflight_names_request_mismatch(tmp_path):
    with ExperimentLedger(tmp_path / 'ledger', create=True) as ledger:
        manager = FlightsRunManager(object(), ledger)  # type: ignore[arg-type]
        request = copy.deepcopy(pilot_request(30))
        request.goal = request.goal + ' ' + CANARY
        run = manager._open(request)
        with pytest.raises(RunnerError, match='pilot_out_of_scope'):
            manager._preflight(run)
        assert manager.scope_reason == 'request_mismatch'
        assert CANARY not in json.dumps(manager.diagnostics)


def test_scope_reasons_are_a_closed_superset():
    assert STATE_REASONS <= SCOPE_REASONS
    assert all(reason.isidentifier() and reason.islower() for reason in SCOPE_REASONS)
