"""Opt-in OpenAI projection is reversible and does not change legacy callers."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from test_model_context import TOKEN, context, descriptor

from decision_browser.action_context import NEXT_ACTION, TARGET
from decision_browser.contracts import PrivacyPolicy
from decision_browser.decision_codec import ChoiceQuestion, compile_questions
from decision_browser.model_context import prepare_context
from decision_browser.privacy import PrivacyError, PrivacyGate


def standard_questions(candidates):
    heads = {group: "target_" + group for group in candidates}
    return (ChoiceQuestion("operation", NEXT_ACTION, ("DONE", *heads)), *(
        ChoiceQuestion(heads[group], TARGET + " Operation: " + group, tuple(rows))
        for group, rows in candidates.items()
    )), heads


def project(source, *, compact=False, question_rows=None, heads=None, privacy=None):
    if question_rows is None:
        question_rows, heads = standard_questions(source["candidates"])
    return prepare_context(question_rows, json.dumps(source, ensure_ascii=False),
                           target_heads=heads, privacy=privacy, compact=compact)


def restore_v1(v2, legacy):
    restored = deepcopy(v2)
    states = restored.pop("states", {})
    for element in restored["elements"].values():
        if "state" in element:
            element["node"]["state"] = deepcopy(states[element.pop("state")])
        if "name" not in element:
            element["name"] = element["node"]["name"]
    restored["context_format"] = legacy["context_format"]
    restored["candidate_format"] = legacy["candidate_format"]
    return restored


def linked_source():
    candidates = {"click": {}, "navigate": {}, "fill": {}}
    for index, (label, path) in enumerate([
        ("Kurt Gödel biography", "/article/kurt-godel"),
        ("Gödel's incompleteness theorems", "/article/godel-incompleteness"),
        ("Gödel's completeness theorem", "/article/godel-completeness"),
    ]):
        href = "https://example.test" + path
        node = {"name": label, "href": href, "role": "link", "value": None,
                "state": {"tag": "a", "type": "", "disabled": False, "readonly": False,
                          "checked": False, "aria": {"expanded": None, "selected": None}}}
        token = TOKEN + str(index)
        candidates["click"][token] = descriptor(label, node=node)
        candidates["navigate"]["route" + str(index)] = descriptor(label, node={}, operation="navigate")
        candidates["navigate"]["route" + str(index)]["destination"] = href
    candidates["fill"][TOKEN] = descriptor(operation="fill")
    candidates["fill"][TOKEN]["fill_state"] = "differs"
    return context(candidates, completion={"total": 2, "pending": 1},
                   scroll={"up": False, "down": True}, extra={"null": None, "false": False, "empty": ""})


def test_default_projection_remains_byte_identical_to_explicit_legacy():
    source = linked_source()
    q, heads = standard_questions(source["candidates"])
    default = prepare_context(q, json.dumps(source), target_heads=heads)
    explicit = prepare_context(q, json.dumps(source), target_heads=heads, compact=False)
    assert default == explicit
    assert json.loads(default[1])["context_format"] == "decision-browser-choice-table-v1"


def test_v2_state_pool_names_feedback_and_unknown_metadata_round_trip_to_v1():
    source = linked_source()
    before = deepcopy(source)
    legacy_q, legacy_raw, legacy_aliases = project(source)
    v2_q, v2_raw, aliases = project(source, compact=True)
    legacy, v2 = json.loads(legacy_raw), json.loads(v2_raw)
    assert source == before
    assert v2["context_format"] == "decision-browser-choice-table-v2"
    assert len(v2["states"]) == 1
    assert restore_v1(v2, legacy) == legacy
    assert aliases == legacy_aliases
    assert [q.choices for q in v2_q] == [q.choices for q in legacy_q]
    assert v2["completion"] == {"total": 2, "pending": 1}
    assert v2["scroll"] == {"up": False, "down": True}
    assert v2["candidates"]["fill"]["1"]["fill_state"] == "differs"


@pytest.mark.parametrize("state", [None, False, "", {}, {"tiny": False}])
def test_unshared_and_small_states_stay_inline_with_explicit_values(state):
    item = descriptor()
    item["node"]["state"] = state
    source = context({"click": {TOKEN: item}})
    _, legacy_raw, _ = project(source)
    _, raw, _ = project(source, compact=True)
    packed, legacy = json.loads(raw), json.loads(legacy_raw)
    assert "states" not in packed
    assert packed["elements"]["e1"]["node"]["state"] == state
    assert restore_v1(packed, legacy) == legacy


@pytest.mark.parametrize("node_name,element_name", [(None, None), (None, ""), ("", ""), ("Node", "Other")])
def test_name_elision_preserves_null_empty_and_different_names(node_name, element_name):
    item = descriptor(name=element_name)
    item["node"]["name"] = node_name
    source = context({"click": {TOKEN: item}})
    _, legacy_raw, _ = project(source)
    _, raw, _ = project(source, compact=True)
    packed, legacy = json.loads(raw), json.loads(legacy_raw)
    assert restore_v1(packed, legacy) == legacy
    assert ("name" not in packed["elements"]["e1"]) is (isinstance(node_name, str) and node_name == element_name)


def test_missing_node_state_and_name_remain_missing():
    item = descriptor()
    del item["node"]["state"]
    del item["node"]["name"]
    source = context({"click": {TOKEN: item}})
    _, legacy_raw, _ = project(source)
    _, raw, _ = project(source, compact=True)
    v2, legacy = json.loads(raw), json.loads(legacy_raw)
    assert "state" not in v2["elements"]["e1"]["node"]
    assert "name" not in v2["elements"]["e1"]["node"]
    assert restore_v1(v2, legacy) == legacy


def test_existing_states_metadata_is_not_overwritten():
    source = linked_source()
    source["states"] = {"caller-data": {"explicit": None}}
    assert project(source, compact=True) == project(source)


@pytest.mark.parametrize("which", ["operation", "target"])
def test_custom_instructions_do_not_use_the_compact_profile(which):
    source = linked_source()
    q, heads = standard_questions(source["candidates"])
    index = 0 if which == "operation" else 1
    q = tuple(ChoiceQuestion(row.name, "Caller-specific instruction" if n == index else row.instructions,
                             row.choices, row.descriptions) for n, row in enumerate(q))
    assert project(source, compact=True, question_rows=q, heads=heads) == project(source, question_rows=q, heads=heads)


@pytest.mark.parametrize("raw", ["opaque custom text", '{"goal":"not marked"}', '{"x":1,"x":2}'])
def test_unmarked_context_remains_byte_identical(raw):
    q = (ChoiceQuestion("operation", NEXT_ACTION, ("DONE",)),)
    assert prepare_context(q, raw, compact=True) == (q, raw, {})


def test_flights_projection_is_identical_even_when_compact_requested():
    from decision_browser.flights_provider import compact_input
    # A closed Flights context, rather than linked_source's deliberately opaque
    # generic extra metadata, reaches the explicit trusted schema seam.
    source = context({"click": {TOKEN: descriptor("Search", node={"role": "button", "name": "Search"})},
                      "fill": {TOKEN: descriptor(operation="fill")}})
    q, heads = standard_questions(source["candidates"])
    raw = compact_input(json.dumps(source))
    assert prepare_context(q, raw, target_heads=heads, compact=True) == (q, raw, {})


def test_flights_trusted_projection_rejects_opaque_generic_metadata():
    from decision_browser.flights_provider import compact_input
    from decision_browser.provider import ProviderError
    with pytest.raises(ProviderError, match="invalid_pilot_context"):
        compact_input(json.dumps(linked_source()))


def test_link_descriptions_are_exact_observed_labels_and_paths_in_alias_order():
    source = linked_source()
    wire, _raw, aliases = project(source, compact=True)
    for group in ("click", "navigate"):
        question = next(q for q in wire if q.name == "target_" + group)
        assert len(question.descriptions) == len(question.choices)
        for alias, description in zip(question.choices, question.descriptions, strict=True):
            source_row = source["candidates"][group][aliases[group][alias]]
            path = (source_row["node"].get("href") or source_row["destination"]).removeprefix("https://example.test")
            assert description == "Observed link " + json.dumps(source_row["name"], ensure_ascii=False) + " -> " + json.dumps(path)
    fill_description = next(q for q in wire if q.name == "target_fill").descriptions[0]
    assert fill_description.startswith(json.dumps("Search query"))
    assert "untrusted" in wire[0].instructions.lower()
    assert "description" in wire[0].instructions.lower()


def test_filled_click_field_and_search_button_have_distinct_observed_evidence():
    field = descriptor("Search query", operation="fill")
    field["node"]["value"] = "Godel incompleteness"
    field["fill_state"] = "matches"
    click_field = deepcopy(field)
    click_field["operation"] = "click"
    del click_field["fill_state"]
    button = descriptor("Search articles", node={"name": "Search articles", "role": "button", "state": {"tag": "button"}})
    source = context({"click": {TOKEN: click_field, "button": button}, "fill": {TOKEN: field}})
    wire, _, aliases = project(source, compact=True)
    question = next(q for q in wire if q.name == "target_click")
    descriptions = {aliases["click"][alias]: description
                    for alias, description in zip(question.choices, question.descriptions, strict=True)}
    assert descriptions[TOKEN] == '"Search query" ("textbox"); current="Godel incompleteness"; fill=matches'
    assert descriptions["button"] == '"Search articles" ("button")'
    assert "fill=" not in descriptions["button"]
    assert "search/submit" in question.instructions.lower()


def test_click_fill_feedback_uses_original_token_not_equal_name_or_wire_alias():
    filled = descriptor("Same label", operation="fill")
    filled["fill_state"] = "matches"
    clicked = descriptor("Same label")
    source = context({"click": {"1": clicked}, "fill": {TOKEN: filled}})
    wire, _, aliases = project(source, compact=True)
    assert aliases["click"] == {"1": "1"} and aliases["fill"] == {"1": TOKEN}
    description = next(q for q in wire if q.name == "target_click").descriptions[0]
    assert "fill=" not in description


@pytest.mark.parametrize("operation,names", [("scroll", ("up", "down")), ("wait", ("short",)),
                                           ("screenshot", ("capture",))])
def test_non_dom_control_descriptions_preserve_every_choice_and_label(operation, names):
    source = context({operation: {name: descriptor(name, node={}, form={}, operation=operation) for name in names}})
    wire, _, aliases = project(source, compact=True)
    target = next(q for q in wire if q.name == "target_" + operation)
    assert target.choices == names and aliases[operation] == {name: name for name in names}
    assert list(target.descriptions) == [json.dumps(name) for name in names]
    assert "Assume this operation is chosen" in target.instructions


def test_select_descriptions_distinguish_options_and_current_observed_value():
    candidates = {"select": {}}
    for value, label in (("oldest", "Oldest first"), ("newest", "Newest first")):
        node = {"name": "Sort order", "role": "combobox", "value": "oldest",
                "state": {"tag": "select"}, "option": {"value": value, "label": label}}
        candidates["select"][value] = descriptor("Sort order", node=node, operation="select")
    candidates["click"] = {str(checked): descriptor(str(checked), node={
        "name": str(checked), "role": "checkbox", "state": {"type": "checkbox", "checked": checked},
    }) for checked in (False, True)}
    wire, _, _ = project(context(candidates), compact=True)
    descriptions = wire[1].descriptions
    for row, value, label in zip(descriptions, ("oldest", "newest"), ("Oldest first", "Newest first"), strict=True):
        assert "option=" + json.dumps(label) + " (value=" + json.dumps(value) + ")" in row
        assert 'current="oldest"' in row
    checkbox = next(q for q in wire if q.name == "target_click").descriptions
    assert "checked=false" in checkbox[0] and "checked=true" in checkbox[1]
    assert "already selected" in wire[1].instructions


def test_each_target_instruction_is_conditional_and_operation_specific():
    source = linked_source()
    wire, _, _ = project(source, compact=True)
    assert all("Assume this operation is chosen" in row.instructions for row in wire[1:])
    assert all("whole input.goal" in row.instructions and "current state" in row.instructions
               and "acknowledged history" in row.instructions for row in wire[1:])
    assert "editable field" in next(q for q in wire if q.name == "target_fill").instructions
    assert "exclusions" in next(q for q in wire if q.name == "target_navigate").instructions
    assert "NEVER choose DONE while completion.pending>0" in wire[0].instructions
    assert "Do not click the filled field again" in wire[0].instructions
    assert wire[0].instructions.startswith(NEXT_ACTION)


def test_ambiguous_same_token_fill_feedback_is_not_inferred_for_click():
    first = descriptor("Field", operation="fill")
    first["fill_state"] = "matches"
    other = deepcopy(first)
    other["fill_state"] = "differs"
    source = context({"click": {TOKEN: descriptor("Field")},
                      "fill_group0": {TOKEN: first}, "fill_group1": {TOKEN: other}})
    wire, _, _ = project(source, compact=True)
    click = next(q for q in wire if q.name == "target_click")
    assert "fill=" not in click.descriptions[0]


def test_singleton_description_does_not_describe_the_rejected_sentinel():
    source = context({"click": {TOKEN: descriptor("Search")}})
    wire, raw, _ = project(source, compact=True)
    compiled = compile_questions(wire, raw)
    assert "Reject the sole target" not in compiled["questions"][1]["instructions"]
    choices = compiled["questions"][1]["choices"]
    assert choices[0]["value"] == "1" and choices[0]["description"].startswith(json.dumps("Search"))
    assert choices[1] == {"value": False}


def test_non_string_role_and_state_metadata_round_trip_without_description_errors():
    node = {"name": "Field", "role": {"custom": None}, "value": "Observed",
            "state": {"tag": ["custom"], "type": {"custom": False}, "readonly": None},
            "unknown": {"empty": "", "null": None, "false": False}}
    source = context({"fill": {TOKEN: descriptor("Field", node=node, operation="fill")}})
    _, legacy_raw, _ = project(source)
    wire, raw, _ = project(source, compact=True)
    assert 'current="Observed"' in wire[1].descriptions[0]
    assert restore_v1(json.loads(raw), json.loads(legacy_raw)) == json.loads(legacy_raw)


def test_field_description_previews_stay_bounded_and_privacy_checks_full_value():
    secret = "PRIVATE_VALUE_SUFFIX"
    text = '東京😀"' * 1000 + secret
    node = {"name": text, "role": "textbox", "value": text,
            "state": {"tag": "input", "type": "text", "readonly": True},
            "option": {"label": text, "value": text}}
    field = descriptor(text, node=node, operation="fill")
    field["fill_state"] = "differs"
    source = context({"fill": {TOKEN: field}})
    _, legacy_raw, _ = project(source)
    wire, raw, _ = project(source, compact=True)
    description = wire[1].descriptions[0]
    assert len(description.encode("utf-8")) <= 600
    assert description.endswith("[preview]") and secret not in description
    assert restore_v1(json.loads(raw), json.loads(legacy_raw)) == json.loads(legacy_raw)
    with pytest.raises(PrivacyError):
        project(source, compact=True, privacy=PrivacyGate(PrivacyPolicy(blocked_literals=[secret])))


def test_same_path_other_origins_and_query_fragments_remain_distinguishable():
    source = linked_source()
    row = next(iter(source["candidates"]["navigate"].values()))
    row["destination"] = "https://elsewhere.test/article?id=2#section"
    wire, _, _ = project(source, compact=True)
    description = next(q for q in wire if q.name == "target_navigate").descriptions[0]
    assert description.endswith(' -> "https://elsewhere.test/article?id=2#section"')


@pytest.mark.parametrize("text", ["long" * 1000, "東京😀\"" * 500], ids=["ascii", "unicode-quotes"])
def test_long_descriptions_are_bounded_previews_without_losing_source_suffix(text):
    source = linked_source()
    suffix = "DISTINGUISHING_PRIVATE_SUFFIX"
    row = next(iter(source["candidates"]["click"].values()))
    row["name"] = row["node"]["name"] = text + suffix
    row["node"]["href"] = "https://example.test/" + text + "?x=" + suffix
    wire, raw, _ = project(source, compact=True)
    description = next(q for q in wire if q.name == "target_click").descriptions[0]
    assert description.endswith("[preview]")
    assert len(description.encode("utf-8")) <= 600
    assert suffix not in description
    nodes = [element["node"] for element in json.loads(raw)["elements"].values()]
    assert any(node.get("name") == text + suffix for node in nodes)
    assert any(node.get("href") == row["node"]["href"] for node in nodes)
    with pytest.raises(PrivacyError):
        project(source, compact=True, privacy=PrivacyGate(PrivacyPolicy(blocked_literals=[suffix])))


def test_malformed_observed_href_uses_literal_evidence_without_raising():
    source = linked_source()
    row = next(iter(source["candidates"]["click"].values()))
    row["node"]["href"] = "https://[malformed"
    wire, raw, _ = project(source, compact=True)
    description = next(q for q in wire if q.name == "target_click").descriptions[0]
    assert description.endswith(' -> "https://[malformed"')
    assert "https://[malformed" in raw


def test_arbitrary_caller_descriptions_survive_aliasing_in_both_profiles():
    source = linked_source()
    q, heads = standard_questions(source["candidates"])
    q = tuple(ChoiceQuestion(row.name, row.instructions, row.choices,
                             ("Caller description", None, "") if row.name == "target_click" else row.descriptions)
              for row in q)
    for compact in (False, True):
        wire, _, _ = project(source, compact=compact, question_rows=q, heads=heads)
        assert next(row for row in wire if row.name == "target_click").descriptions == ("Caller description", None, "")


@pytest.mark.parametrize("count", [255, 256, 320])
def test_grouped_domains_and_alias_collisions_round_trip_without_pruning(count):
    candidates = {}
    for index in range(count):
        group = "select" if count <= 255 else "select_group" + str(index // 255)
        token = "1" if index == 0 else TOKEN + str(index)
        item = descriptor("Menu " + str(index // 80), operation="select")
        item["node"]["option"] = {"value": str(index), "label": "Option " + str(index)}
        candidates.setdefault(group, {})[token] = item
    source = context(candidates)
    legacy_q, legacy_raw, legacy_aliases = project(source)
    wire, raw, aliases = project(source, compact=True)
    assert aliases == legacy_aliases
    assert [q.choices for q in wire] == [q.choices for q in legacy_q]
    assert all(len(q.choices) <= 255 for q in wire)
    assert sum(len(mapping) for mapping in aliases.values()) == count
    assert restore_v1(json.loads(raw), json.loads(legacy_raw)) == json.loads(legacy_raw)
    compile_questions(wire, raw)


def test_private_state_and_descriptions_are_checked_before_compaction():
    source = linked_source()
    secret = 'BLOCKED quoted "state"'
    for row in source["candidates"]["click"].values():
        row["node"]["state"]["private"] = secret
    with pytest.raises(PrivacyError):
        project(source, compact=True, privacy=PrivacyGate(PrivacyPolicy(blocked_literals=[secret])))


@pytest.mark.parametrize("kind", ["form", "article"])
def test_lossless_metadata_savings_and_bounded_full_wire_grounding_overhead(kind):
    source = linked_source()
    if kind == "form":
        source = context({group: {TOKEN + str(i): descriptor("Field " + str(i), operation=group)
                                  for i in range(4)} for group in ("click", "fill", "enter", "submit")})
    else:
        # Ordinary RunManager always offers these controls, even on an article.
        for operation, names in (("scroll", ("up", "down")), ("wait", ("short",)),
                                 ("screenshot", ("capture",))):
            source["candidates"][operation] = {
                name: descriptor(name, node={}, form={}, operation=operation) for name in names
            }
    old_q, old_raw, _ = project(source)
    new_q, new_raw, _ = project(source, compact=True)
    old_bytes = len(json.dumps(compile_questions(old_q, old_raw), ensure_ascii=False).encode())
    new_bytes = len(json.dumps(compile_questions(new_q, new_raw), ensure_ascii=False).encode())
    # Preserve the complete original policy and every target's evidence. This
    # small fixture bounds overhead instead of pretending every request shrinks;
    # the native corpus experiment measures actual aggregate model cost.
    assert len(json.dumps(new_raw).encode()) < len(json.dumps(old_raw).encode())
    assert new_bytes <= old_bytes * 1.05, (kind, old_bytes, new_bytes)


def test_small_custom_domain_with_standard_rules_bounds_grounding_overhead():
    source = linked_source()
    old_q, old_raw, _ = project(source)
    new_q, new_raw, _ = project(source, compact=True)
    assert new_q[0].instructions.startswith(NEXT_ACTION)
    assert len(json.dumps(compile_questions(new_q, new_raw)).encode()) <= 1.05 * len(
        json.dumps(compile_questions(old_q, old_raw)).encode()
    )


@pytest.mark.parametrize("previous,latest,expect_hint", [
    ("unchanged", "unchanged", True),
    ("unchanged", "changed", False),
    ("click", "unchanged", False),
    ("unchanged", "unacknowledged", False),
])
def test_repeated_wait_feedback_preserves_wait_and_resets_on_progress(previous, latest, expect_hint):
    from decision_browser.systemone_provider import _wire

    def record(state):
        return {"operation": "click" if state == "click" else "wait",
                "acknowledged": state != "unacknowledged", "page_changed": state == "changed"}

    source = context({"click": {TOKEN: descriptor()}, "wait": {"short": descriptor(operation="wait")}},
                     recent_actions=[record(previous), record(latest)])
    questions, heads = standard_questions(source["candidates"])
    questions = (ChoiceQuestion("operation", NEXT_ACTION, ("UNABLE", "click", "wait")), *questions[1:])
    wire, raw, _ = prepare_context(questions, json.dumps(source), target_heads=heads, compact=True, lean=True)
    openai = compile_questions(wire, raw)["questions"][0]["choices"]
    systemone = _wire(wire[0], wire[0].choices, 255)
    jev_options = [json.loads(v) for v in systemone.criteria.values()]
    assert [o["value"] for o in openai] == ["UNABLE", "click", "wait"]
    assert jev_options[:2] == ["UNABLE", "click"]
    if expect_hint:
        hint = openai[-1]["description"]
        assert "last two waits" in hint and "Wait again" in hint
        assert jev_options[-1] == {"choice": "wait", "description": hint}
    else:
        assert "description" not in openai[-1] and jev_options[-1] == "wait"
