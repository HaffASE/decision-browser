"""Host-bound pickers that hide the Google Flights form (shapes observed live on 2026-10-11)."""
import pytest

from decision_browser.flights_continuation import (
    DATE_DONE,
    JFK,
    LAX,
    PilotStateError,
    canonical_control,
    day_label,
    validate_state,
)

URL = 'https://www.google.com/travel/flights?hl=en'
FIELD = {'origin': [], 'destination': [], 'departure': []}


def picker(kind, controls, *, origin=(), destination=(), departure=(), calendar=False):
    return {'evidence': {
        'schema_version': 1, 'captured_at_ms': 1000., 'url': URL,
        'form': {'origin': list(origin), 'destination': list(destination),
                 'departure': list(departure), 'controls': list(controls)},
        'results': {'headings': [], 'query_summaries': [], 'adult_summaries': [],
                    'displayed_row_count': 0, 'rows': []},
        'hazards': {'loading': False, 'captcha': False, 'no_flights': False, 'dialog': True},
        'truncated': False}, 'dialogs': [kind], 'password': False, 'calendar': calendar,
        'trip_menu': False, 'trip_picker': False}


def origin_else():
    return picker('airport', ['Where else?'])


def destination_open():
    return picker('destination', ['Where to?'],
                  destination=[{'label': 'Where to?', 'value': '', 'expanded': 'true'}])


def calendar_open():
    return picker('calendar', ['One way'], calendar=True,
                  departure=[{'label': 'Departure', 'value': '', 'expanded': None}])


def check(value, expected, committed=frozenset({'One way'}), **kwargs):
    kwargs = {'search': False, 'continuing': False, **kwargs}
    return validate_state(value, URL, committed, picker_expected=expected, **kwargs)


@pytest.mark.parametrize('value,expected', [(origin_else, 'origin'), (destination_open, 'destination'),
                                            (calendar_open, 'calendar')])
def test_observed_hidden_pickers_are_accepted_only_with_host_binding(value, expected):
    assert check(value(), expected) == {'phase': expected, 'form_observed': False, 'ready': False}
    with pytest.raises(PilotStateError) as caught:
        check(value(), None)
    assert caught.value.reason in {'trip_control_count', 'cabin_control_count'}


@pytest.mark.parametrize('kwargs', [{'continuing': True}, {'search': True}])
def test_hidden_picker_never_continues_or_searches(kwargs):
    with pytest.raises(PilotStateError):
        check(origin_else(), 'origin', **kwargs)


@pytest.mark.parametrize('value,expected,reason', [
    (calendar_open, 'origin', 'picker_kind_mismatch'),
    (destination_open, 'origin', 'picker_kind_mismatch'),
    (lambda: picker('unknown', ['Where else?']), 'origin', 'picker_kind_mismatch'),
    (lambda: picker('airport', ['Where else?'], departure=[{'label': 'Departure', 'value': '', 'expanded': None}]),
     'origin', 'picker_form_fields'),
    (lambda: picker('airport', ['Where else?', 'Where to?']), 'origin', 'picker_controls'),
    (lambda: picker('airport', ['Search everywhere']), 'origin', 'picker_controls'),
    (lambda: picker('destination', ['Where to?'],
                    destination=[{'label': 'Where to? Los Angeles LAX', 'value': '', 'expanded': 'true'}]),
     'destination', 'picker_form_fields'),
    (lambda: picker('calendar', ['One way'], departure=[{'label': 'Departure', 'value': '', 'expanded': None}]),
     'calendar', 'picker_kind_mismatch'),
    (lambda: picker('calendar', ['One way'], calendar=True,
                    origin=[{'label': 'Where from?', 'value': '', 'expanded': 'false'}],
                    departure=[{'label': 'Departure', 'value': '', 'expanded': None}]),
     'calendar', 'picker_form_fields'),
    (lambda: picker('calendar', ['Round trip'], calendar=True,
                    departure=[{'label': 'Departure', 'value': '', 'expanded': None}]),
     'calendar', 'picker_trip_lost'),
])
def test_hidden_picker_shape_is_closed(value, expected, reason):
    with pytest.raises(PilotStateError) as caught:
        check(value(), expected)
    assert caught.value.reason == reason


def test_where_else_dialog_belongs_to_whichever_field_the_host_clicked():
    assert check(origin_else(), 'destination')['phase'] == 'destination'


def test_visible_cabin_or_results_use_the_ordinary_contract():
    value = origin_else()
    value['evidence']['form']['controls'].append('Economy (include Basic)')
    with pytest.raises(PilotStateError) as caught:
        check(value, 'origin')
    assert caught.value.reason == 'trip_control_count'
    value = origin_else()
    value['evidence']['results']['displayed_row_count'] = 1
    with pytest.raises(PilotStateError):
        check(value, 'origin')
    value = origin_else()
    value['dialogs'] = ['airport', 'calendar']
    with pytest.raises(PilotStateError):
        check(value, 'origin')


def test_where_else_maps_only_inside_a_bound_airport_picker():
    node = {'name': 'Where else?', 'role': 'combobox'}
    assert canonical_control('fill', node, 'origin') == 'Where from?'
    assert canonical_control('click', node, 'destination') == 'Where to?'
    for phase in (None, 'form', 'calendar', 'trip', 'results'):
        assert canonical_control('fill', node, phase) is None
    assert canonical_control('fill', {'name': 'Where else?', 'role': 'button'}, 'origin') is None
    assert canonical_control('click', {'name': 'Where to? ', 'role': 'combobox'}) == 'Where to?'


def test_live_done_label_and_bare_month_buttons():
    live = 'Done. Search for one-way flights, departing on November 10, 2026'
    assert canonical_control('click', {'name': live, 'role': 'button'}) == DATE_DONE
    assert canonical_control('click', {'name': live.replace('10', '11'), 'role': 'button'}) is None
    assert canonical_control('click', {'name': 'Done.', 'role': 'button'}) is None
    assert canonical_control('click', {'name': 'Next', 'role': 'button'}, 'calendar') == 'Next month'
    assert canonical_control('click', {'name': 'Previous', 'role': 'button'}, 'calendar') == 'Previous month'
    assert canonical_control('click', {'name': 'Next', 'role': 'button'}, 'origin') is None
    assert {JFK, LAX} == {canonical_control('click', {'name': name, 'role': 'option'}, 'origin')
                          for name in (JFK, LAX)}


@pytest.mark.parametrize('number,label,expected', [
    ('10', 'Tuesday, November 10, 2026', 'Tuesday, November 10, 2026'),
    ('10 14.3K₽', 'Tuesday, November 10, 2026', 'Tuesday, November 10, 2026'),
    ('10', 'November 10, 2026', 'November 10, 2026'),
    ('11', 'Tuesday, November 10, 2026', None),
    ('10', 'Tuesday, November 10, 2026 buy now', None),
    ('10', None, None),
    ('Done', 'Tuesday, November 10, 2026', None),
])
def test_day_label_requires_agreeing_day_number(number, label, expected):
    assert day_label(number, label) == expected
    if expected is not None:
        assert canonical_control('click', {'name': expected, 'role': 'button'}, 'calendar') == 'November 10, 2026'
