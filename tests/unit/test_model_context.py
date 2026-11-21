"""Compact model context keeps the complete local action domain reversible."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from decision_browser.decision_codec import (
    ChoiceQuestion,
    CodecError,
    compile_questions,
)
from decision_browser.model_context import prepare_context

MARKER = "decision-browser-context-v1"
ORIGIN = "https://example.test"
URL = ORIGIN + "/results"
TOKEN = "0123456789abcdef0123456789abcdef-0"


def descriptor(name="Search query", *, node=None, form=None, operation="click"):
    return {
        "operation": operation,
        "origin": ORIGIN,
        "url": URL,
        "destination": None,
        "name": name,
        "node": {"role": "textbox", "name": name, "value": "", "state": {
            "tag": "input", "type": "text", "disabled": False, "readonly": False,
            "checked": False, "aria": {"checked": "false", "expanded": "false"},
        }} if node is None else node,
        "form": {"action": ORIGIN + "/search", "method": "get"} if form is None else form,
        "context": {"focused": False},
    }


def context(candidates, **updates):
    body = {
        "context_format": MARKER, "goal": "Find orbital mechanics", "url": URL,
        "origin": ORIGIN, "text": "Article search", "candidates": candidates,
        "recent_actions": [], "observation_diagnostics": {"scanned": 99},
        "excluded_mechanisms": ["iframe"],
    }
    body.update(updates)
    return body


def questions(candidates):
    heads = {group: "target_" + group for group in candidates}
    rows = (ChoiceQuestion("operation", "Choose an operation", ("DONE", *heads)), *(
        ChoiceQuestion(heads[group], "Choose " + group, tuple(entries))
        for group, entries in candidates.items()
    ))
    return rows, heads


def prepare(body, question_rows=None, heads=None):
    if question_rows is None:
        question_rows, heads = questions(body["candidates"])
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    wire_questions, packed, aliases = prepare_context(question_rows, raw, target_heads=heads)
    return wire_questions, json.loads(packed), aliases


def expanded(packed, group, wire_id):
    row = deepcopy(packed["candidates"][group][wire_id])
    element = deepcopy(packed["elements"][row.pop("element")])
    result = {**element, **row}
    result["form"] = deepcopy(packed["forms"][result["form"]])
    result.setdefault("origin", packed["origin"])
    result.setdefault("url", packed["url"])
    result.setdefault("operation", group.split("_group", 1)[0])
    if "option" in result:
        result["node"]["option"] = result.pop("option")
    return result


def test_same_element_and_form_are_described_once_without_mutating_source():
    candidates = {op: {TOKEN: descriptor(operation=op)} for op in ("click", "fill", "enter")}
    source = context(candidates)
    before = deepcopy(source)
    original_questions, heads = questions(candidates)
    wire, packed, aliases = prepare(source, original_questions, heads)
    assert len(packed["elements"]) == len(packed["forms"]) == 1
    assert source == before
    assert "observation_diagnostics" not in packed
    for group in candidates:
        wire_id = next(iter(aliases[group]))
        assert aliases[group][wire_id] == TOKEN and len(wire_id) < len(TOKEN)
        assert expanded(packed, group, wire_id) == candidates[group][TOKEN]
    assert wire[0] is original_questions[0]
    assert all(a.name == b.name and a.instructions == b.instructions
               for a, b in zip(wire, original_questions, strict=True))


def test_false_empty_values_aria_and_focus_remain_explicit():
    item = descriptor()
    item["node"].update(selected=[], caption="", fullCaption="")
    source = context({"fill": {TOKEN: item}})
    _, packed, aliases = prepare(source)
    restored = expanded(packed, "fill", next(iter(aliases["fill"])))
    assert restored == item
    assert restored["node"]["value"] == ""
    assert restored["node"]["selected"] == []
    assert restored["node"]["state"]["checked"] is False
    assert restored["context"]["focused"] is False


def test_unicode_names_destinations_and_acknowledged_history_are_preserved():
    name = "Направление 東京 " * 35
    item = descriptor(name, operation="navigate")
    item["destination"] = ORIGIN + "/東京?город=Москва"
    item["origin"] = "https://different.test"
    item["url"] = "https://different.test/source"
    history = [{"name": name, "value": "", "acknowledged": True, "page_changed": False}]
    source = context({"navigate": {"route0": item}}, goal=name, recent_actions=history)
    _, packed, aliases = prepare(source)
    assert packed["goal"] == name and packed["recent_actions"] == history
    assert packed["excluded_mechanisms"] == ["iframe"]
    assert aliases["navigate"] == {"route0": "route0"}
    assert expanded(packed, "navigate", "route0") == item


def test_same_named_distinct_tokens_remain_distinct_choices():
    other = TOKEN[:-1] + "1"
    source = context({"click": {TOKEN: descriptor(), other: descriptor()}})
    wire, packed, aliases = prepare(source)
    assert len(wire[1].choices) == len(aliases["click"]) == 2
    assert set(aliases["click"].values()) == {TOKEN, other}
    assert len(packed["elements"]) == 1  # Description equality grants no target identity.


def test_long_id_alias_cannot_collide_with_short_original_ids():
    source = context({"click": {"1": descriptor(), TOKEN: descriptor(), "2": descriptor()}})
    wire, _packed, aliases = prepare(source)
    assert aliases["click"]["1"] == "1" and aliases["click"]["2"] == "2"
    assert len(set(wire[1].choices)) == 3
    assert {aliases["click"][choice] for choice in wire[1].choices} == set(source["candidates"]["click"])


@pytest.mark.parametrize("count", [1, 255, 256, 320])
def test_grouped_select_domain_and_every_option_survive(count):
    candidates = {}
    for index in range(count):
        group = "select" if count <= 255 else "select_group" + str(index // 255)
        token = TOKEN + ":option:" + str(index)
        node = {"name": "Sort order", "role": "combobox", "value": "oldest", "selected": ["oldest"],
                "option": {"value": str(index), "label": "Option " + str(index)}}
        candidates.setdefault(group, {})[token] = descriptor("Sort order", node=node, operation="select")
    source = context(candidates)
    wire, packed, aliases = prepare(source)
    compile_questions(wire, json.dumps(packed))
    restored = [expanded(packed, group, alias) for group, mapping in aliases.items() for alias in mapping]
    assert len(restored) == count
    assert {row["node"]["option"]["value"] for row in restored} == {str(i) for i in range(count)}
    assert len(packed["elements"]) == 1
    assert all(len(q.choices) <= 255 for q in wire)
    for group, mapping in aliases.items():
        for alias, original in mapping.items():
            assert expanded(packed, group, alias) == source["candidates"][group][original]


def test_operation_specific_forms_and_other_metadata_do_not_merge():
    click = descriptor(operation="click")
    enter = descriptor(operation="enter", form={"action": ORIGIN + "/other", "method": "post"})
    click["special"] = {"enabled": False}
    source = context({"click": {TOKEN: click}, "enter": {TOKEN: enter}})
    _, packed, aliases = prepare(source)
    assert len(packed["forms"]) == 2
    for group in source["candidates"]:
        assert expanded(packed, group, next(iter(aliases[group]))) == source["candidates"][group][TOKEN]


@pytest.mark.parametrize("raw", [
    "plain input", '{"goal":"opaque"}', '{"x":1,"x":2}',
    'Plain text mentions "context_format" and ' + MARKER,
    '{"nested":{"context_format":"' + MARKER + '"}}',
    '{"nested":{"context_format":"' + MARKER + '"},"unfinished":',
    json.dumps({"text": '{"context_format":"' + MARKER + '"}'}),
])
def test_unmarked_input_is_byte_identical_passthrough(raw):
    q = (ChoiceQuestion("operation", "choose", ("DONE",)),)
    actual, packed, aliases = prepare_context(q, raw)
    assert actual == q and packed == raw and aliases == {}


def test_flights_projected_input_remains_unchanged():
    from decision_browser.flights_provider import compact_input

    source = context({"click": {TOKEN: descriptor()}})
    q, heads = questions(source["candidates"])
    raw = compact_input(json.dumps(source))
    assert prepare_context(q, raw, target_heads=heads) == (q, raw, {})


@pytest.mark.parametrize("mutation", [
    "missing_group", "extra_group", "missing_choice", "extra_choice", "bad_row", "bad_node",
    "bad_form", "bad_context", "bad_history", "bad_head", "non_string_choice", "fake_projection",
])
def test_invalid_marked_context_fails_instead_of_falling_back(mutation):
    source = context({"click": {TOKEN: descriptor()}})
    q, heads = questions(source["candidates"])
    if mutation == "missing_group":
        source["candidates"] = {}
    elif mutation == "extra_group":
        source["candidates"]["fill"] = {TOKEN: descriptor(operation="fill")}
    elif mutation == "missing_choice":
        source["candidates"]["click"] = {}
    elif mutation == "extra_choice":
        source["candidates"]["click"]["extra"] = descriptor()
    elif mutation == "bad_row":
        source["candidates"]["click"][TOKEN] = []
    elif mutation.startswith("bad_") and mutation[4:] in {"node", "form", "context"}:
        source["candidates"]["click"][TOKEN][mutation[4:]] = []
    elif mutation == "bad_history":
        source["recent_actions"] = "not history"
    elif mutation == "bad_head":
        heads["click"] = "missing"
    elif mutation == "fake_projection":
        source["elements"] = {}
    else:
        q = (q[0], ChoiceQuestion("target_click", "choose", (True,)))
    with pytest.raises(CodecError, match="^invalid_context$"):
        prepare(source, q, heads)


@pytest.mark.parametrize("raw", [
    '{"context_format":"' + MARKER + '","candidates":',
    '{"context_format":"' + MARKER + '","goal":"one","goal":"two"}',
    '{"context_format":"' + MARKER + '","context_format":"other","candidates":[]}',
    '{"context_format":"other","context_format":"' + MARKER + '","candidates":[]}',
    r'{"context_\u0066ormat":"' + MARKER + '","candidates":',
    r'{"context_\u0066ormat":"' + MARKER + '","context_format":"other"}',
    r'{"context_format":"decision-browser-context-v\u0031","candidates":',
    r'{"note":"opaque","context_\u0066ormat":"' + MARKER + '","candidates":',
])
def test_marked_invalid_json_or_duplicate_keys_fail_closed(raw):
    with pytest.raises(CodecError, match="^invalid_context$"):
        prepare_context((ChoiceQuestion("operation", "choose", ("DONE",)),), raw)


def test_repeated_realistic_descriptors_reduce_actual_wire_json_by_at_least_half():
    candidates = {op: {} for op in ("click", "fill", "enter")}
    for index in range(24):
        token = TOKEN[:-1] + str(index)
        for op, entries in candidates.items():
            entries[token] = descriptor("Search field " + str(index), operation=op)
    source = context(candidates)
    q, heads = questions(candidates)
    raw = json.dumps(source, ensure_ascii=False, separators=(",", ":"))
    wire, packed, _aliases = prepare_context(q, raw, target_heads=heads)
    original_size = len(json.dumps(compile_questions(q, raw), ensure_ascii=False).encode())
    packed_size = len(json.dumps(compile_questions(wire, packed), ensure_ascii=False).encode())
    assert packed_size < original_size / 2, (original_size, packed_size)
