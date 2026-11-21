"""Offline oracle contracts: no browser, API, provider or live acceptance claim."""

from __future__ import annotations

import json
import shutil
import subprocess
from copy import deepcopy

import pytest

from decision_browser.flights_evidence import (
    FLIGHTS_EVIDENCE_SCRIPT,
    FlightsQuery,
    evidence_digest,
    validate_flights_evidence,
)

ROW = (
    "From 128 British pounds. 1 stop flight with JetBlue. "
    "Leaves John F. Kennedy International Airport at 11:29 AM on Tuesday, November 10 "
    "and arrives at Los Angeles International Airport at 6:53 PM on Tuesday, November 10. "
    "Total duration 10 hr 24 min. Layover in Boston. Select flight"
)


def evidence():
    before = {
        "schema_version": 1, "captured_at_ms": 1000,
        "url": "https://www.google.com/travel/flights",
        "form": {
            "origin": [{"label": "Where from? New York JFK", "value": "New York", "expanded": "false"}],
            "destination": [{"label": "Where to? Los Angeles LAX", "value": "Los Angeles", "expanded": None}],
            "departure": [{"label": "Departure", "value": "Tue, Nov 10", "expanded": None}],
            "controls": ["One way", "1 passenger 1", "Economy (include Basic)"],
        },
        "results": {"headings": [], "query_summaries": [], "adult_summaries": [],
                    "displayed_row_count": 0, "rows": []},
        "hazards": {"loading": False, "captcha": False, "no_flights": False, "dialog": False},
        "truncated": False,
    }
    after = deepcopy(before)
    after.update(captured_at_ms=2500, url="https://www.google.com/travel/flights/search?tfs=opaque")
    after["results"] = {
        "headings": ["Search results"],
        "query_summaries": ["Track prices from New York to Los Angeles departing 2026-11-10"],
        "adult_summaries": ["Prices include required taxes + fees for 1 adult."],
        "displayed_row_count": 1,
        "rows": [{"aria_label": ROW, "text": "JetBlue 11:29 AM – 6:53 PM £128", "visible": True}],
    }
    earlier = deepcopy(after)
    earlier["captured_at_ms"] = 2100
    action = {
        "attempt_id": "search-attempt", "operation": "click", "name": "Search",
        "outcome": "acknowledged", "pre_search_digest": evidence_digest(before),
        "dispatched_at_ms": 1100, "acknowledged_at_ms": 1200,
    }
    selection = {
        "attempt_id": "done-attempt", "operation": "click", "outcome": "acknowledged",
        "name": "Done. Search for one-way flights departing on November 10, 2026",
        "acknowledged_at_ms": 900,
    }
    return before, after, action, earlier, selection


def validate(data):
    before, after, action, earlier, selection = data
    return validate_flights_evidence(before, after, action,
                                     previous_post_search=earlier, date_selection=selection)


def synchronize(data):
    before, after, action, earlier, _ = data
    for snapshot in (before, after):
        result = snapshot["results"]
        result["displayed_row_count"] = max(result["displayed_row_count"], len(result["rows"]))
    action["pre_search_digest"] = evidence_digest(before)
    earlier.clear()
    earlier.update(deepcopy(after))
    earlier["captured_at_ms"] = 2100


def test_positive_preserves_raw_and_extracts_one_row():
    data = evidence()
    report = validate(data)
    assert report["status"] == "PASS"
    assert report["reasons"] == []
    assert report["row"]["carrier"] == "JetBlue"
    assert report["row"]["departure_time"] == "11:29 AM"
    assert report["row"]["arrival_time"] == "6:53 PM"
    assert report["row"]["price"] == "128"
    assert report["row"]["currency"] == "GBP"
    assert report["raw"]["post_search"]["results"]["rows"][0]["aria_label"] == ROW
    assert report["hashes"]["pre_rows"] != report["hashes"]["post_rows"]


def test_accepts_observed_search_for_flights_name():
    data = evidence()
    data[2]["name"] = "Search for flights"
    assert validate(data)["status"] == "PASS"


def test_accepts_observed_passenger_button_label():
    data = evidence()
    for snapshot in (data[0], data[1]):
        snapshot["form"]["controls"][1] = "1 passenger, change number of passengers. 1"
    synchronize(data)
    assert validate(data)["status"] == "PASS"


def test_stale_rows_fail_even_with_filled_form_and_new_result_summary():
    data = evidence()
    data[0]["results"] = deepcopy(data[1]["results"])
    data[0]["results"]["query_summaries"][0] = "Track prices from Boston to Los Angeles departing 2026-11-09"
    synchronize(data)
    assert "stale_results" in validate(data)["reasons"]


def test_old_offscreen_rows_cannot_become_fresh_by_scrolling_after_search():
    data = evidence()
    # The old row is present below the fold but no viewport row, heading, or
    # summary was collected. Scrolling it into view must not count as freshness.
    data[0]["results"]["displayed_row_count"] = 1
    synchronize(data)
    report = validate(data)
    assert report["status"] == "NOT_VERIFIED"
    assert "preexisting_results" in report["reasons"]


@pytest.mark.parametrize("key,value", [
    ("headings", ["Search results"]),
    ("adult_summaries", ["Prices include required taxes + fees for 1 adult."]),
    ("query_summaries", ["Track prices from New York to Los Angeles departing 2026-11-10"]),
    ("query_summaries", ["Track prices from Boston to Los Angeles departing 2026-11-09"]),
])
def test_preexisting_result_state_rejects_even_when_current_fields_and_rows_match(key, value):
    data = evidence()
    data[0]["results"][key] = value
    synchronize(data)
    assert "preexisting_results" in validate(data)["reasons"]


def test_missing_offscreen_presence_evidence_fails_closed():
    data = evidence()
    del data[0]["results"]["displayed_row_count"]
    data[2]["pre_search_digest"] = evidence_digest(data[0])
    assert validate(data)["reasons"] == ["invalid_snapshot"]


def test_displayed_row_count_cannot_hide_collected_rows():
    data = evidence()
    data[1]["results"]["displayed_row_count"] = 0
    assert validate(data)["reasons"] == ["invalid_snapshot"]


def test_changed_decorative_text_does_not_make_stale_rows_fresh():
    data = evidence()
    data[0]["results"] = deepcopy(data[1]["results"])
    data[0]["results"]["rows"][0]["text"] = "Previous decorative text"
    synchronize(data)
    report = validate(data)
    assert report["status"] == "NOT_VERIFIED"
    assert "stale_results" in report["reasons"]
    assert report["hashes"]["pre_rows"] == report["hashes"]["post_rows"]


def test_row_order_and_duplicates_do_not_make_old_results_fresh():
    data = evidence()
    other = deepcopy(data[1]["results"]["rows"][0])
    other.update(aria_label=ROW.replace("128", "129"), text="JetBlue £129")
    data[1]["results"]["rows"].append(other)
    data[0]["results"] = deepcopy(data[1]["results"])
    data[1]["results"]["rows"].reverse()
    data[1]["results"]["rows"].append(deepcopy(other))
    synchronize(data)
    assert "stale_results" in validate(data)["reasons"]


def test_label_decoration_does_not_make_old_results_fresh():
    data = evidence()
    data[0]["results"] = deepcopy(data[1]["results"])
    data[1]["results"]["rows"][0]["aria_label"] = ROW.replace("Layover in Boston.", "Recommended.")
    synchronize(data)
    assert "stale_results" in validate(data)["reasons"]


@pytest.mark.parametrize("text", [
    "United 11:29 AM – 6:53 PM £128",
    "JetBlue 12:29 PM – 6:53 PM £128",
    "JetBlue 11:29 AM – 6:53 PM $999",
    "JetBlue 11:29 AM – 6:53 PM £999",
    "JetBlue 11:29 AM – 6:53 PM $128",
    "Delta 9:00 AM – 3:00 PM $999",
    "Unknown layout £128",
])
def test_contradictory_nonempty_row_text_fails_closed(text):
    data = evidence()
    data[1]["results"]["rows"][0]["text"] = text
    synchronize(data)
    report = validate(data)
    assert report["status"] == "NOT_VERIFIED"
    assert "complete_visible_row_missing" in report["reasons"]


def test_empty_overlay_text_reports_accessibility_evidence_only():
    data = evidence()
    data[1]["results"]["rows"][0]["text"] = ""
    synchronize(data)
    report = validate(data)
    assert report["status"] == "PASS"
    assert report["row"]["evidence_source"] == "visible_row_accessible_label"
    assert report["row"]["text_corroboration"] == "empty"


@pytest.mark.parametrize("key,value,reason", [
    ("outcome", "unknown", "search_not_acknowledged"),
    ("name", "Book", "search_not_acknowledged"),
    ("operation", "fill", "search_not_acknowledged"),
    ("attempt_id", "", "search_not_acknowledged"),
    ("pre_search_digest", "0" * 64, "search_not_bound_to_query"),
    ("dispatched_at_ms", 500, "invalid_search_chronology"),
    ("acknowledged_at_ms", 2200, "invalid_search_chronology"),
    ("dispatched_at_ms", True, "invalid_search_chronology"),
    ("dispatched_at_ms", float("nan"), "invalid_search_chronology"),
])
def test_search_provenance(key, value, reason):
    data = evidence()
    data[2][key] = value
    assert reason in validate(data)["reasons"]


@pytest.mark.parametrize("field,label,value,reason", [
    ("origin", "Where from?", "JFK", "origin_not_selected"),
    ("origin", "Where from? New York", "New York", "origin_not_selected"),
    ("origin", "Where from? Newark EWR", "Newark", "origin_not_selected"),
    ("destination", "Where to? Los Angeles", "LAX", "destination_not_selected"),
    ("destination", "Where to? Ontario ONT", "Ontario", "destination_not_selected"),
    ("departure", "Departure", "Tue, Nov 17", "departure_date_mismatch"),
    ("departure", "Departure", "Nov 10, 2027", "departure_date_mismatch"),
    ("departure", "Departure", "Wed, Nov 10", "departure_date_mismatch"),
    ("departure", "Departure", "Nov 10 or Nov 17", "departure_date_mismatch"),
])
def test_pre_search_current_query_required(field, label, value, reason):
    data = evidence()
    data[0]["form"][field][0].update(label=label, value=value)
    synchronize(data)
    assert reason in validate(data)["reasons"]


def test_open_picker_is_not_committed_airport():
    data = evidence()
    data[0]["form"]["origin"][0]["expanded"] = "true"
    synchronize(data)
    assert "origin_not_selected" in validate(data)["reasons"]


@pytest.mark.parametrize("controls,reason", [
    (["Round trip", "1 passenger", "Economy"], "trip_type_mismatch"),
    (["One way", "2 passengers", "Economy"], "passenger_count_mismatch"),
    (["One way", "2 passengers, change number of passengers. 2", "Economy"], "passenger_count_mismatch"),
    (["One way", "1 passenger", "Business"], "cabin_mismatch"),
])
def test_query_dimensions(controls, reason):
    data = evidence()
    data[0]["form"]["controls"] = controls
    synchronize(data)
    assert reason in validate(data)["reasons"]


@pytest.mark.parametrize("selection", [
    None, {},
    {"attempt_id": "x", "operation": "click", "outcome": "unknown", "name": "Done November 10, 2026", "acknowledged_at_ms": 900},
    {"attempt_id": "x", "operation": "click", "outcome": "acknowledged", "name": "Done November 10, 2027", "acknowledged_at_ms": 900},
    {"attempt_id": "x", "operation": "click", "outcome": "acknowledged", "name": "Done November 10, 2026", "acknowledged_at_ms": 1100},
])
def test_year_requires_committed_prior_date_trace(selection):
    data = list(evidence())
    data[4] = selection
    assert "departure_year_unproven" in validate(data)["reasons"]


def test_full_date_in_field_needs_no_auxiliary_done_trace():
    data = list(evidence())
    for snapshot in (data[0], data[1]):
        snapshot["form"]["departure"][0]["value"] = "Nov 10, 2026"
    data[4] = None
    synchronize(data)
    assert validate(data)["status"] == "PASS"


@pytest.mark.parametrize("hazard", ["loading", "captcha", "no_flights", "dialog"])
def test_page_hazards(hazard):
    data = evidence()
    data[1]["hazards"][hazard] = True
    synchronize(data)
    assert hazard in validate(data)["reasons"]


@pytest.mark.parametrize("replacement", [
    "Track prices from New York to Los Angeles departing 2026-11-09",
    "Track prices from Boston to Los Angeles departing 2026-11-10", "",
])
def test_result_query_cannot_be_replaced_by_form_or_url(replacement):
    data = evidence()
    data[1]["results"]["query_summaries"] = [replacement]
    synchronize(data)
    assert "result_query_mismatch" in validate(data)["reasons"]


@pytest.mark.parametrize("old,new", [
    ("From 128 British pounds. ", ""),
    ("British pounds", "credits"),
    ("128 British", "0 British"),
    ("11:29 AM", "11:99 AM"),
    ("John F. Kennedy International Airport", "Newark Liberty International Airport"),
    ("Los Angeles International Airport", "Ontario International Airport"),
    ("Tuesday, November 10", "Monday, November 9"),
    ("JetBlue", ""),
])
def test_one_row_requires_all_fields_and_correct_airports_date(old, new):
    data = evidence()
    data[1]["results"]["rows"][0]["aria_label"] = ROW.replace(old, new)
    synchronize(data)
    assert "complete_visible_row_missing" in validate(data)["reasons"]


def test_carrier_time_and_price_cannot_be_joined_across_rows():
    data = evidence()
    data[1]["results"]["rows"] = [
        {"aria_label": "From 128 British pounds. Select flight", "text": "£128", "visible": True},
        {"aria_label": ROW.replace("From 128 British pounds. ", ""), "text": "JetBlue 11:29 AM", "visible": True},
    ]
    synchronize(data)
    assert "complete_visible_row_missing" in validate(data)["reasons"]


def test_mixed_current_and_stale_rows_fail():
    data = evidence()
    stale = deepcopy(data[1]["results"]["rows"][0])
    stale["aria_label"] = ROW.replace("Tuesday, November 10", "Monday, November 9")
    data[1]["results"]["rows"].append(stale)
    synchronize(data)
    assert "ambiguous_visible_rows" in validate(data)["reasons"]


def test_hidden_row_does_not_count():
    data = evidence()
    data[1]["results"]["rows"][0]["visible"] = False
    synchronize(data)
    assert "complete_visible_row_missing" in validate(data)["reasons"]


def test_result_change_and_short_settle_do_not_pass():
    data = evidence()
    data[1]["results"]["rows"][0]["aria_label"] = ROW.replace("128", "129")
    assert "results_not_settled" in validate(data)["reasons"]
    data = evidence()
    data[1]["captured_at_ms"] = 2200
    assert "invalid_search_chronology" in validate(data)["reasons"]


@pytest.mark.parametrize("key,value,reason", [
    ("truncated", True, "incomplete_capture"),
    ("url", "https://www.google.com.evil.test/travel/flights", "unexpected_url"),
    ("url", "http://www.google.com/travel/flights", "unexpected_url"),
    ("captured_at_ms", float("inf"), "invalid_snapshot"),
    ("schema_version", 2, "invalid_snapshot"),
])
def test_invalid_and_partial_snapshots(key, value, reason):
    data = evidence()
    data[1][key] = value
    assert reason in validate(data)["reasons"]


def test_oversized_and_malformed_input_never_passes():
    data = evidence()
    data[1]["results"]["rows"][0]["text"] = "x" * 65537
    assert validate(data)["reasons"] == ["invalid_snapshot"]
    assert validate_flights_evidence(None, {}, None, previous_post_search={})["status"] == "NOT_VERIFIED"
    with pytest.raises(ValueError):
        evidence_digest({"x": float("nan")})
    with pytest.raises(ValueError):
        evidence_digest({"x": "x" * 65537})


def test_digest_order_independent_and_query_scope_closed():
    assert evidence_digest({"b": 2, "a": 1}) == evidence_digest({"a": 1, "b": 2})
    assert validate_flights_evidence({}, {}, {}, previous_post_search={},
                                     expected=FlightsQuery(origin="EWR"))["reasons"] == ["unsupported_query"]


def test_collector_has_no_dispatch_network_or_mutable_page_state():
    for forbidden in (".click(", ".fill(", "dispatchEvent", "fetch(", "XMLHttpRequest",
                      "localStorage", "sessionStorage", "window.__", ".value =", "location.href ="):
        assert forbidden not in FLIGHTS_EVIDENCE_SCRIPT
    assert 'shown(el, true, rowBox(el))' in FLIGHTS_EVIDENCE_SCRIPT
    assert '[role="link"][aria-label]' in FLIGHTS_EVIDENCE_SCRIPT


@pytest.mark.parametrize("occluded", [False, True])
def test_collector_executes_read_only_against_pure_dom_double(occluded):
    """Exercise JavaScript without a browser or a network surface."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable for pure DOM-double test")
    script = r"""
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const element = (label, value = '', innerText = '', box = {}) => Object.freeze({
  type: 'text', isConnected: true, parentElement: null, value, innerText,
  contains: () => false,
  closest: () => null,
  getAttribute: key => key === 'aria-label' ? label : null,
  getClientRects: () => [1],
  getBoundingClientRect: () => ({top: 10, left: 10, right: 200, bottom: 40, width: 190, height: 30, ...box})
});
const combo = value => Object.freeze({...element(null, '', value),
  getAttribute: key => key === 'role' ? 'combobox' : null});
const fields = [element('Where from? New York JFK', 'New York'),
  element('Where to? Los Angeles LAX', 'Los Angeles'), element('Departure', 'Tue, Nov 10')];
const selectors = {
  'input': fields,
  '[role="combobox"],button[aria-label]': [combo('One way'), combo('Economy (include Basic)'),
    element('1 passenger, change number of passengers.', '', '1')],
  'h1,h2,h3,[role="heading"]': [element(null, '', 'Search results')],
  '[role="switch"][aria-label]': [element('Track prices from New York to Los Angeles departing 2026-11-10')],
  '[role="link"][aria-label]': [
    element(input.row, '', 'JetBlue £128'),
    element(input.row.replace('128', '999'), '', 'Offscreen row', {top: 900, bottom: 940})]
};
global.document = Object.freeze({readyState: 'complete',
  body: Object.freeze({innerText: 'Prices include required taxes + fees for 1 adult.'}),
  elementFromPoint: () => input.occluded ? element(null, '', 'Opaque overlay') : selectors['[role="link"][aria-label]'][0],
  querySelectorAll: selector => selectors[selector] || []});
global.getComputedStyle = () => ({display: 'block', visibility: 'visible', opacity: '1'});
global.innerHeight = 800;
global.innerWidth = 1200;
global.location = Object.freeze({href: 'https://www.google.com/travel/flights/search'});
const collect = eval('(' + input.script + ')');
process.stdout.write(JSON.stringify(collect()));
"""
    result = subprocess.run([node, "-e", script], input=json.dumps({
        "script": FLIGHTS_EVIDENCE_SCRIPT, "row": ROW, "occluded": occluded,
    }), text=True, capture_output=True, check=True, timeout=10)
    snapshot = json.loads(result.stdout)
    assert snapshot["form"]["controls"] == [
        "One way", "Economy (include Basic)", "1 passenger, change number of passengers. 1",
    ]
    assert snapshot["results"]["rows"] == ([] if occluded else [
        {"aria_label": ROW, "text": "JetBlue £128", "visible": True},
    ])
    assert snapshot["results"]["displayed_row_count"] == 2
    assert snapshot["hazards"] == {"loading": False, "captcha": False, "no_flights": False, "dialog": False}
    assert snapshot["truncated"] is False
    data = evidence()
    data[1].update(snapshot)
    data[1]["captured_at_ms"] = 2500
    data[0]["form"] = deepcopy(snapshot["form"])
    synchronize(data)
    assert validate(data)["status"] == ("NOT_VERIFIED" if occluded else "PASS")
