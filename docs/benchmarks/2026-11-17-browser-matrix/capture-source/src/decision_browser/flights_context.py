"""Closed structure of the trusted Flights projection and compact references.

DOM strings and generic metadata remain untrusted JSON data. Validation grants
no DOM authority; choice bindings and the outgoing privacy gate stay mandatory.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

FLIGHTS_FORMAT = (
    "Every offered choice refers to elements[id] for role, name, "
    "value and state. Omitted boolean fields are false."
)
# The continuation manager filters its own narrower pilot offers. This schema
# also serves the existing non-continuation manager's ordinary finite domain;
# describing these operations does not grant admission or dispatch authority.
_OPERATIONS = frozenset({'click', 'fill', 'select', 'enter', 'submit', 'navigate',
                         'screenshot', 'scroll', 'wait'})
_ROOT = {'goal', 'url', 'origin', 'text', 'context_format', 'state', 'candidates',
         'recent_actions', 'observation_diagnostics', 'excluded_mechanisms',
         'previous_pages', 'completion', 'scroll'}


def read_context(raw: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_pilot_field')
            result[key] = value
        return result
    def constant(_: str) -> Any:
        raise ValueError('nonfinite_pilot_field')
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(value, dict):
        raise TypeError('invalid_pilot_context')
    return value


def validate_context(data: dict[str, Any], *, compact: bool = False) -> None:
    allowed = _ROOT | ({'elements', 'candidate_format'} if compact else set())
    if (not set(data) <= allowed or not isinstance(data.get('goal'), str)
            or not isinstance(data.get('candidates'), dict)):
        raise ValueError('invalid_pilot_context')
    # Enforce bounded finite JSON throughout, including retained opaque metadata.
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(encoded) > 512 * 1024:
        raise ValueError('oversized_pilot_context')
    for key in ('url', 'origin', 'text', 'context_format'):
        if key in data and data[key] is not None and not isinstance(data[key], str):
            raise ValueError('invalid_pilot_text')
    for key in ('recent_actions', 'excluded_mechanisms', 'previous_pages'):
        if key in data and not isinstance(data[key], list):
            raise ValueError('invalid_pilot_list')
    if any(not isinstance(row, dict) for row in data.get('recent_actions', [])):
        raise ValueError('invalid_pilot_history')
    for key in ('excluded_mechanisms', 'previous_pages'):
        if any(not isinstance(item, str) for item in data.get(key, [])):
            raise ValueError('invalid_pilot_list')
    for key in ('observation_diagnostics', 'completion', 'scroll'):
        if key in data and not isinstance(data[key], dict):
            raise ValueError('invalid_pilot_metadata')
    if 'completion' in data:
        value = data['completion']
        if (set(value) != {'total', 'pending'} or any(type(v) is not int for v in value.values())
                or not 0 <= value['pending'] <= value['total'] <= 32):
            raise ValueError('invalid_pilot_completion')
    if 'scroll' in data and (set(data['scroll']) != {'up', 'down'}
            or any(type(v) is not bool for v in data['scroll'].values())):
        raise ValueError('invalid_pilot_scroll')
    for row in data.get('recent_actions', []):
        _history_row(row)
    if 'state' in data:
        state = data['state']
        flags = {'ready', 'form_observed', 'origin_selected', 'destination_selected', 'date_selected', 'loading'}
        if (not isinstance(state, dict) or not set(state) <= flags | {'phase', 'trip', 'adults', 'cabin'}
                or state.get('phase') not in {'form', 'trip', 'origin', 'destination', 'calendar', 'results'}
                or any(type(state[key]) is not bool for key in flags & set(state))
                or 'trip' in state and state['trip'] not in {'one way', 'round trip'}
                or 'adults' in state and (type(state['adults']) is not int or state['adults'] != 1)
                or 'cabin' in state and state['cabin'] != 'economy including Basic'):
            raise ValueError('invalid_pilot_state')
    elements = data.get('elements', {})
    if compact:
        if not isinstance(elements, dict) or data.get('candidate_format') != FLIGHTS_FORMAT:
            raise ValueError('invalid_pilot_elements')
        for name, node in elements.items():
            if not isinstance(name, str) or not name:
                raise ValueError('invalid_pilot_element')
            _node(node)
    used = set()
    for group, bag in data['candidates'].items():
        if (not isinstance(group, str) or re.fullmatch(r'(?:click|fill|select|enter|submit|navigate|screenshot|scroll|wait)(?:_group[1-9][0-9]*)?', group) is None
                or not isinstance(bag, dict)):
            raise ValueError('invalid_pilot_group')
        for key, entry in bag.items():
            fields = ({'element', 'name', 'destination', 'form', 'context'} if compact else
                      {'operation', 'origin', 'url', 'destination', 'name', 'node', 'form', 'context', 'fill_state'})
            if (not isinstance(key, str) or not key or not isinstance(entry, dict)
                    or not set(entry) <= fields or not isinstance(entry.get('name'), str)):
                raise ValueError('invalid_pilot_candidate')
            for field in ('destination', 'origin', 'url'):
                if field in entry and entry[field] is not None and not isinstance(entry[field], str):
                    raise ValueError('invalid_pilot_destination')
            _form(entry.get('form', {}))
            context = entry.get('context', {})
            if (not isinstance(context, dict) or not set(context) <= {'focused', 'picker'}
                    or any(type(value) is not bool for value in context.values())):
                raise ValueError('invalid_pilot_context_flags')
            if compact:
                ref = entry.get('element')
                if not isinstance(ref, str) or ref not in elements:
                    raise ValueError('invalid_pilot_reference')
                used.add(ref)
            else:
                if 'operation' in entry and entry['operation'] != group.split('_group')[0]:
                    raise ValueError('invalid_pilot_operation')
                if 'fill_state' in entry and entry['fill_state'] not in {'unbound', 'matches', 'differs'}:
                    raise ValueError('invalid_pilot_fill')
                _node(entry.get('node', {}))
    if compact and used != set(elements):
        raise ValueError('unused_pilot_element')


def _node(node: object) -> None:
    if not isinstance(node, dict):
        raise TypeError('invalid_pilot_node')
    if not set(node) <= {'role', 'name', 'fullName', 'state', 'value', 'href', 'caption',
                         'fullCaption', 'valueMissing', 'selected', 'options', 'option',
                         'value_matches_request', 'expanded'}:
        raise ValueError('unknown_pilot_node_field')
    for key in ('role', 'name', 'fullName', 'value', 'href', 'caption', 'fullCaption'):
        if key in node and node[key] is not None and not isinstance(node[key], str):
            raise ValueError('invalid_pilot_node_text')
    for key in ('value_matches_request', 'expanded', 'valueMissing'):
        if key in node and type(node[key]) is not bool:
            raise ValueError('invalid_pilot_node_flag')
    if 'selected' in node and not (type(node['selected']) is bool or
            isinstance(node['selected'], list) and all(isinstance(v, str) for v in node['selected'])):
        raise ValueError('invalid_pilot_selected')
    if 'option' in node and (not isinstance(node['option'], dict)
            or set(node['option']) != {'label', 'value'}
            or any(not isinstance(v, str) for v in node['option'].values())):
        raise ValueError('invalid_pilot_option')
    if 'options' in node and (not isinstance(node['options'], list) or len(node['options']) > 80
            or any(not isinstance(row, list) or len(row) != 3
                   or not isinstance(row[0], str) or not isinstance(row[1], str) or type(row[2]) is not bool
                   for row in node['options'])):
        raise ValueError('invalid_pilot_options')
    if 'state' in node and not isinstance(node['state'], dict):
        raise ValueError('invalid_pilot_node_state')
    if 'state' in node:
        state = node['state']
        if not set(state) <= {'tag', 'type', 'disabled', 'readonly', 'checked', 'aria'}:
            raise ValueError('unknown_pilot_node_state')
        if any(type(state[key]) is not bool for key in {'disabled', 'readonly', 'checked'} & set(state)):
            raise ValueError('invalid_pilot_node_state')
        if any(state[key] is not None and not isinstance(state[key], str) for key in {'tag', 'type'} & set(state)):
            raise ValueError('invalid_pilot_node_state')
        if 'aria' in state and (not isinstance(state['aria'], dict)
                or not set(state['aria']) <= {'checked', 'selected', 'expanded', 'pressed', 'current', 'busy'}
                or any(value is not None and not isinstance(value, str) for value in state['aria'].values())):
            raise ValueError('invalid_pilot_aria')


def _form(form: object) -> None:
    strings = {'path', 'id', 'name', 'action', 'method', 'formAction', 'formMethod', 'formTarget', 'destination'}
    if (not isinstance(form, dict) or not set(form) <= strings | {'defaultSubmitter', 'submits'}
            or any(value is not None and not isinstance(value, str) for key, value in form.items() if key in strings)
            or 'submits' in form and type(form['submits']) is not bool
            or 'method' in form and form['method'] not in {'get', 'post', 'dialog'}):
        raise ValueError('invalid_pilot_form')
    submitter = form.get('defaultSubmitter')
    if submitter is not None and (not isinstance(submitter, dict)
            or not set(submitter) <= {'path', 'role', 'name', 'type', 'fullName', 'caption', 'fullCaption'}
            or any(value is not None and not isinstance(value, str) for value in submitter.values())):
        raise ValueError('invalid_pilot_submitter')


def _history_row(row: dict[str, Any]) -> None:
    strings = {'name', 'value', 'url', 'attempt_id', 'pre_search_digest'}
    flags = {'page_changed', 'acknowledged'}
    times = {'dispatched_at_ms', 'acknowledged_at_ms'}
    if (not set(row) <= strings | flags | times | {'operation', 'outcome', 'post_observation'}
            or any(value is not None and not isinstance(value, str) for key, value in row.items() if key in strings)
            or any(value is not None and type(value) is not bool for key, value in row.items() if key in flags)
            or 'operation' in row and row['operation'] is not None and row['operation'] not in _OPERATIONS
            or 'outcome' in row and row['outcome'] not in {'prepared', 'acknowledged', 'outcome_unknown', 'not_dispatched'}
            or any(type(value) not in (int, float) or not 0 <= value < 1e15 or not math.isfinite(value)
                   for key, value in row.items() if key in times)):
        raise ValueError('invalid_pilot_history')
    if 'post_observation' in row:
        value = row['post_observation']
        if (not isinstance(value, dict) or not set(value) <= {'same_document', 'same_origin', 'url_changed', 'live_url_matches'}
                or any(v is not None and type(v) is not bool for v in value.values())):
            raise ValueError('invalid_pilot_post_observation')
