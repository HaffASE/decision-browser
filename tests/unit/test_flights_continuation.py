"""Pure closed-contract tests; no browser, network, credentials or real ledger."""
import copy
import json

import pytest

from decision_browser.flights_continuation import (
    DATE_DONE,
    JFK,
    LAX,
    allowed_url,
    canonical_control,
    project_input,
    project_report,
    validate_state,
)

URL = 'https://www.google.com/travel/flights?hl=en'
CANARY = 'PRIVATE_CANARY_42'


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


COMMITTED = frozenset({'One way', JFK, LAX, DATE_DONE})


@pytest.mark.parametrize('url', [URL, URL + '&tfs=YWJj&tfu=eHl6',
                               'https://www.google.com:443/travel/flights?hl=en'])
def test_allowed_closed_urls(url):
    assert allowed_url(url)
    assert not allowed_url('https://www.google.com/travel/flights/search?hl=en')
    assert allowed_url('https://www.google.com/travel/flights/search?hl=en', results=True)


@pytest.mark.parametrize('url', [
    URL.replace('https:', 'http:'), URL.replace('www.google.com', 'google.com'),
    URL.replace('www.google.com', 'www.google.com.evil.test'),
    URL.replace('www.google.com', 'x@www.google.com'),
    URL.replace('www.google.com', 'www.google.com:444'), URL + '#x', URL + ' ',
    URL.replace('/travel/flights', '/travel/flights/payment'),
    URL.replace('/travel/flights', '/travel/flights-collector'),
    URL.replace('/travel/flights', '/travel/flights/../flights'),
    URL.replace('/travel/flights', '/travel/%66lights'),
    URL + '&hl=en', URL + '&q=payment', URL + '&tfs=', URL + '&tfs=a.b',
    URL + '&tfs=abc&tfs=def', URL.replace('hl', '%68l'), URL.replace('en', 'fr'),
    URL + '&redirect=https%3A%2F%2Fevil.test', URL + '&tfu=abc+', URL + '&',
    URL + '\x00', URL + '&tfs=' + 'a' * 4096,
])
def test_deny_route_aliases_and_hostile_queries(url):
    assert not allowed_url(url)


def test_state_requires_committed_identity_and_exact_date():
    checked = validate_state(state(), URL, COMMITTED, search=False, continuing=True)
    assert checked['phase'] == 'form' and checked['origin_selected']
    for field, value in [('origin', 'Boston'), ('destination', 'New York'),
                         ('departure', 'November 11, 2026'), ('departure', 'Tue Nov 10 pay now')]:
        mutated = state()
        mutated['evidence']['form'][field][0]['value'] = value
        with pytest.raises(ValueError):
            validate_state(mutated, URL, COMMITTED, search=False, continuing=True)


def hidden_trip():
    value = state()
    value['evidence']['form'] = {'origin': [], 'destination': [], 'departure': [], 'controls': []}
    value.update(trip_menu=True, trip_picker=True)
    return value


def test_expected_hidden_trip_projects_only_observed_menu():
    checked = validate_state(hidden_trip(), URL, frozenset(), search=False,
                             continuing=False, trip_menu_expected=True)
    assert checked == {'phase': 'trip', 'form_observed': False, 'ready': False}


@pytest.mark.parametrize('mutation', ['no_expected_action', 'continuing', 'search', 'committed',
                                     'wrong_menu', 'modal', 'partial_form', 'captcha'])
def test_hidden_trip_exception_is_not_a_general_missing_form_permission(mutation):
    value = hidden_trip()
    committed = frozenset({'One way'}) if mutation == 'committed' else frozenset()
    if mutation == 'wrong_menu':
        value['trip_menu'] = False
    elif mutation == 'modal':
        value['dialogs'] = ['unknown']
        value['evidence']['hazards']['dialog'] = True
    elif mutation == 'partial_form':
        value['evidence']['form']['origin'] = state()['evidence']['form']['origin']
    elif mutation == 'captcha':
        value['evidence']['hazards']['captcha'] = True
    with pytest.raises(ValueError):
        validate_state(value, URL, committed, search=mutation == 'search',
                       continuing=mutation == 'continuing',
                       trip_menu_expected=mutation != 'no_expected_action')


@pytest.mark.parametrize('mutation', ['captcha', 'password', 'dialog', 'trip',
                                     'duplicate', 'missing', 'truncated', 'open'])
def test_invalid_or_open_state_cannot_continue(mutation):
    value = state()
    if mutation == 'captcha':
        value['evidence']['hazards']['captcha'] = True
    elif mutation == 'password':
        value['password'] = True
    elif mutation == 'dialog':
        value['evidence']['hazards']['dialog'] = True
        value['dialogs'] = ['unknown']
    elif mutation == 'trip':
        value['evidence']['form']['controls'][0] = 'Round trip'
    elif mutation == 'duplicate':
        value['evidence']['form']['controls'].append('One way')
    elif mutation == 'missing':
        value['evidence']['form']['origin'] = []
    elif mutation == 'truncated':
        value['evidence']['truncated'] = True
    else:
        value['evidence']['form']['origin'][0]['expanded'] = 'true'
    with pytest.raises(ValueError):
        validate_state(value, URL, COMMITTED, search=False, continuing=True)


@pytest.mark.parametrize('name', [DATE_DONE + ' pay now', 'November 10, 2026 pay now',
                                JFK + ' booking', 'Search and pay', CANARY])
def test_canonical_bindings_are_full_matches(name):
    assert canonical_control('click', {'name': name, 'role': 'button'}) is None


def test_provider_projection_omits_all_untrusted_free_strings():
    checked = validate_state(state(), URL, COMMITTED, search=False, continuing=True)
    poisoned = {'goal': 'public goal', 'url': URL + '&tfs=' + CANARY,
                'text': CANARY, 'observation_diagnostics': {'secret': CANARY},
                'candidates': {'fill': {'token': {
                    'operation': 'fill', 'name': 'Where from?', 'url': URL + '&tfu=' + CANARY,
                    'destination': URL + '&tfs=' + CANARY, 'node': {
                        'name': 'Where from?', 'role': 'combobox', 'value': CANARY,
                        'href': URL + '&tfs=' + CANARY,
                        'state': {'aria': {'expanded': 'false'}, 'text': CANARY}},
                    'form': {'action': URL + '&tfs=' + CANARY, 'name': CANARY},
                    'context': {'nearby': CANARY}}}},
                'recent_actions': [{'operation': 'fill', 'name': 'Where from?',
                                    'url': URL + '&tfs=' + CANARY, 'value': CANARY,
                                    'context': {'url': URL + '&tfs=' + CANARY},
                                    'post_observation': {'url': URL + '&tfs=' + CANARY, 'same_origin': CANARY},
                                    'attempt_id': CANARY, 'outcome': CANARY}]}
    projected = project_input(json.dumps(poisoned), checked)
    assert CANARY not in projected and '?hl' not in projected
    clean = json.loads(projected)
    assert clean['candidates']['fill']['token']['name'] == 'Where from?'
    assert clean['candidates']['fill']['token']['node']['value_matches_request'] is False
    assert copy.deepcopy(poisoned) == poisoned


def test_complete_export_drops_raw_text_queries_and_fingerprints_without_mutating():
    raw = state()['evidence']
    raw['url'] += '&tfs=' + CANARY
    raw['form']['origin'][0]['value'] = CANARY
    report = {'status': 'NOT_VERIFIED', 'result': {'last_url': raw['url'], 'terminal': 'blocked'},
              'evidence': {'status': 'NOT_VERIFIED', 'reasons': ['invalid_snapshot'],
                           'raw': {'post_search': raw}, 'row': {'carrier': CANARY}},
              'actions': [{'name': 'Where from?', 'value': CANARY, 'url': raw['url'], 'operation': 'click',
                           'post_observation': {'href': raw['url'], 'same_document': CANARY}, 'attempt_id': CANARY}],
              'journal': [{'event': 'before_dispatch', 'semantic_fingerprint': json.dumps(raw),
                           'details': {'href': raw['url'], 'context': CANARY}}],
              'diagnostics': [{'phase': 'choose', 'outcome': 'failed'}]}
    before = copy.deepcopy(report)
    exported = project_report(report)
    assert CANARY not in json.dumps(exported)
    assert report == before
    assert exported['export_projection'] is True
    assert len(exported['journal'][0]['semantic_fingerprint_sha256']) == 64


def test_export_keeps_all_safe_action_metadata_and_query_free_last_url():
    rows=[{'operation':'click','name':'One way','value':None,'url':URL,'acknowledged':True,
           'outcome':'acknowledged','page_changed':None,'attempt_id':f'{index:032x}',
           'dispatched_at_ms':1000.+index,'acknowledged_at_ms':1100.+index,
           'post_observation':{'same_document':True,'same_origin':True,'url_changed':False,'live_url_matches':True}}
          for index in range(40)]
    exported=project_report({'result':{'last_url':'https://www.google.com/travel/flights'},'actions':rows})
    assert exported['result']['last_url']=='https://www.google.com/travel/flights'
    assert len(exported['actions'])==40
    assert exported['actions'][0]['page_changed'] is None
    assert exported['actions'][0]['acknowledged'] is True
    assert exported['actions'][0]['outcome']=='acknowledged'
    assert exported['actions'][0]['attempt_id']==rows[0]['attempt_id']
    assert exported['actions'][0]['post_observation']==rows[0]['post_observation']
