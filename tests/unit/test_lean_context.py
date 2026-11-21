"""Lean native evidence keeps domains and meaningful observed state."""
from __future__ import annotations

import json
from copy import deepcopy

import pytest
from test_compact_context_v2 import project as v2_project
from test_compact_context_v2 import standard_questions
from test_model_context import TOKEN, context, descriptor

from decision_browser.contracts import PrivacyPolicy
from decision_browser.decision_codec import ChoiceQuestion, CodecError
from decision_browser.model_context import prepare_context
from decision_browser.privacy import PrivacyError, PrivacyGate


def native(name="Search query", operation="fill"):
    row = descriptor(name, operation=operation, form={})
    row["node"]["state"]["aria"] = dict.fromkeys(
        ("checked", "selected", "expanded", "pressed", "current", "busy"))
    if operation == "fill":
        row["fill_state"] = "differs"
    return row


def source():
    return context({"fill": {TOKEN: native()}, "click": {TOKEN: native(operation="click")}},
                   completion={"total": 2, "pending": 1}, scroll={"up": False, "down": True})


def project(data, *, lean=True, questions=None, privacy=None, compact=True):
    defaults, heads = standard_questions(data["candidates"])
    return prepare_context(questions or defaults, json.dumps(data, ensure_ascii=False),
                           target_heads=heads, compact=compact, lean=lean, privacy=privacy)


def test_lean_is_opt_in_and_keeps_questions_aliases_feedback_and_source():
    data = source()
    before = deepcopy(data)
    old_q, old_raw, old_aliases = project(data, lean=False)
    q, raw, aliases = project(data)
    result = json.loads(raw)
    assert data == before
    assert q == old_q and aliases == old_aliases
    assert result["context_format"] == "decision-browser-choice-table-v3"
    assert result["candidates"]["fill"]["1"]["fill_state"] == "differs"
    assert result["completion"] == data["completion"] and result["scroll"] == data["scroll"]
    assert result["recent_actions"] == data["recent_actions"]
    assert not {"elements", "states", "forms"} & result.keys()
    assert len(raw) < len(old_raw)
    assert project(data, lean=False) == v2_project(data, compact=True)


@pytest.mark.parametrize("where,key,value", [
    ("root", "custom", None), ("node", "future", False), ("state", "future", []),
    ("aria", "future", None), ("context", "future", ""), ("form", "future", 1),
    ("row", "future", {}), ("node", "value", False), ("state", "checked", 0),
])
def test_unknown_or_nonnative_metadata_falls_back_losslessly(where, key, value):
    data = source()
    row = data["candidates"]["fill"][TOKEN]
    target = {"root": data, "row": row, "node": row["node"], "state": row["node"]["state"],
              "aria": row["node"]["state"]["aria"], "context": row["context"], "form": row["form"]}[where]
    target[key] = value
    assert project(data) == project(data, lean=False)


def test_explicit_state_and_empty_selection_survive():
    data = source()
    row = data["candidates"]["click"][TOKEN]
    row["node"]["role"] = "checkbox"
    row["node"]["state"].update(type="checkbox", readonly=True)
    row["node"]["state"]["aria"].update(checked="mixed", expanded="false")
    row["node"]["selected"] = []
    row["context"].update(focused=False, picker=True)
    result = json.loads(project(data)[1])["candidates"]["click"]["1"]
    assert result["checked"] is False and result["readonly"] is True
    assert result["aria"] == {"checked": "mixed", "expanded": "false"}
    assert result["selected"] == [] and result["focused"] is False and result["picker"] is True


def test_long_unicode_and_unpreviewed_caption_evidence_survive():
    data = source()
    row = data["candidates"]["fill"][TOKEN]
    row["name"] = row["node"]["name"] = "界" * 180 + "DISTINCT"
    row["node"].update(fullName=row["name"], value="界" * 100 + "VALUE", caption="caption")
    q, raw, _ = project(data)
    result = json.loads(raw)["candidates"]["fill"]["1"]
    assert "[preview]" in q[1].descriptions[0]
    assert result["name"].endswith("DISTINCT") and result["value"].endswith("VALUE")
    assert result["fullName"] == row["name"] and result["caption"] == "caption"


def test_forms_intern_full_identity_before_removing_proof_paths():
    data = source()
    for index, row in enumerate(data["candidates"][group][TOKEN] for group in ("fill", "click")):
        row["form"] = {"path": f"html/form[{index}]", "id": None, "name": "search", "action": "https://example.test/search",
                       "method": "get", "formAction": None, "formMethod": None, "formTarget": "",
                       "defaultSubmitter": {"path": "button[1]", "name": "Search", "role": "button", "type": "submit", "caption": "Go"}}
    result = json.loads(project(data)[1])
    assert len(result["forms"]) == 2
    assert result["candidates"]["fill"]["1"]["form"] != result["candidates"]["click"]["1"]["form"]
    for form in result["forms"].values():
        assert "path" not in form and form["formTarget"] == ""
        assert form["defaultSubmitter"]["caption"] == "Go"
        assert "path" not in form["defaultSubmitter"]


def test_non_dom_controls_have_no_dummy_tables_and_all_choices_survive():
    rows = {str(i): descriptor(str(i), operation="scroll", node={}, form={}) for i in range(320)}
    for row in rows.values():
        row.update(origin=None, url=None, context={})
    data = context({"scroll": dict(list(rows.items())[:255]), "scroll_group2": dict(list(rows.items())[255:])})
    q, raw, aliases = project(data)
    result = json.loads(raw)
    assert sum(len(rows) for rows in result["candidates"].values()) == 320
    assert sum(len(rows) for rows in aliases.values()) == 320
    assert all(row == {} for rows in result["candidates"].values() for row in rows.values())
    assert all(question.descriptions for question in q[1:])
    assert not {"elements", "states", "forms"} & result.keys()


@pytest.mark.parametrize("mutation", ["null_path", "foreign_method", "missing_submitter_role"])
def test_nonnative_form_shapes_remain_in_lossless_view(mutation):
    data = source()
    form = {"path": "0/1", "id": None, "name": None, "action": "https://example.test/search",
            "method": "get", "formAction": None, "formMethod": None, "formTarget": None}
    if mutation == "null_path":
        form["path"] = None
    elif mutation == "foreign_method":
        form["method"] = "PATCH"
    else:
        form["defaultSubmitter"] = {"path": "0/1/2", "name": "Go", "type": "submit"}
    data["candidates"]["fill"][TOKEN]["form"] = form
    assert project(data) == project(data, lean=False)


@pytest.mark.parametrize("key", ["origin", "url", "name"])
def test_null_dom_identity_fields_use_lossless_fallback(key):
    data = source()
    data["candidates"]["fill"][TOKEN][key] = None
    assert project(data) == project(data, lean=False)


def test_custom_descriptions_and_instructions_and_compact_false_preserved():
    data = source()
    q, _ = standard_questions(data["candidates"])
    custom = (q[0], ChoiceQuestion(q[1].name, q[1].instructions, q[1].choices, ("caller",)), q[2])
    assert project(data, questions=custom) == project(data, questions=custom, lean=False)
    custom = (ChoiceQuestion(q[0].name, "custom", q[0].choices), *q[1:])
    assert project(data, questions=custom) == project(data, questions=custom, lean=False)
    assert project(data, compact=False) == project(data, compact=False, lean=False)


def test_source_privacy_checks_even_omitted_form_path_first():
    data = source()
    data["candidates"]["fill"][TOKEN]["form"] = {"path": "private-marker"}
    gate = PrivacyGate(PrivacyPolicy(blocked_literals=["private-marker"]))
    with pytest.raises(PrivacyError):
        project(data, privacy=gate)


def test_malformed_marked_context_fails_without_fallback():
    data = source()
    data["completion"]["pending"] = True
    with pytest.raises(CodecError):
        project(data)


def test_link_destination_and_full_preview_evidence_remain_distinct():
    data = source()
    href = "https://example.test/" + "x" * 300 + "?distinct=one#end"
    link = native("Requested article", "click")
    link["node"] = {"role": "link", "name": link["name"], "href": href,
                    "state": {**link["node"]["state"], "tag": "a", "type": None}}
    data["candidates"]["click"][TOKEN] = link
    data["candidates"]["navigate"] = {"route1": descriptor(link["name"], operation="navigate", node={}, form={})}
    data["candidates"]["navigate"]["route1"].update(destination=href, context={}, origin=None, url=None)
    result = json.loads(project(data)[1])
    assert result["candidates"]["click"]["1"]["href"] == href
    assert result["candidates"]["navigate"]["route1"]["destination"] == href


@pytest.mark.parametrize("private_field", ["path", "formAction", "defaultSubmitter"])
def test_source_privacy_precedes_omission_and_fallback(private_field):
    data = source()
    data["candidates"]["fill"][TOKEN]["form"][private_field] = "blocked-source-evidence"
    with pytest.raises(PrivacyError):
        project(data, privacy=PrivacyGate(PrivacyPolicy(blocked_literals=["blocked-source-evidence"])))


def test_root_states_collision_and_unmarked_input_stay_on_existing_paths():
    data = source()
    data["states"] = {"custom": None}
    assert project(data) == project(data, lean=False)
    q, heads = standard_questions(data["candidates"])
    assert prepare_context(q, "opaque text", target_heads=heads, compact=True, lean=True) == (q, "opaque text", {})


def test_lean_has_material_escaped_input_savings_without_policy_shortening():
    data = source()
    for index in range(20):
        data["candidates"]["click"][TOKEN + str(index)] = native("Control " + str(index), "click")
    q, raw, aliases = project(data)
    old_q, old_raw, old_aliases = project(data, lean=False)
    assert q == old_q and aliases == old_aliases
    assert len(json.dumps(raw).encode()) < .75 * len(json.dumps(old_raw).encode())


def test_nonstandard_anchor_role_and_distinct_observed_name_preserved():
    data = source()
    row = data["candidates"]["click"][TOKEN]
    row["name"] = "Whole observed name"
    row["node"].update(name="Short observed name", role="button", href="https://example.test/detail")
    result = json.loads(project(data)[1])["candidates"]["click"]["1"]
    assert result["role"] == "button" and result["node_name"] == "Short observed name"


@pytest.mark.parametrize("key", ["role", "name", "state"])
def test_missing_native_node_fields_use_lossless_fallback(key):
    data = source()
    del data["candidates"]["fill"][TOKEN]["node"][key]
    assert project(data) == project(data, lean=False)


def test_alias_collision_domain_and_acknowledged_history_unchanged():
    data = source()
    data["candidates"]["fill"]["1"] = native("Other field")
    data["recent_actions"] = [{"operation": "fill", "acknowledged": True, "page_changed": False,
                               "value": "filled", "name": "Search query", "url": None}]
    q, raw, aliases = project(data)
    assert aliases["fill"] == {"2": TOKEN, "1": "1"}
    assert q[1].choices == ("2", "1")
    assert json.loads(raw)["recent_actions"] == data["recent_actions"]


@pytest.mark.parametrize("name", ["Feature status", "Feature current= status", "Feature [preview] status"])
def test_page_labels_cannot_control_preservation_of_checkbox_value(name):
    data = context({"click": {TOKEN: native(name, "click")}})
    row = data["candidates"]["click"][TOKEN]
    row["node"].update(role="checkbox", value="premium-plan")
    row["node"]["state"].update(tag="input", type="checkbox")
    result = json.loads(project(data)[1])["candidates"]["click"]["1"]
    assert result["name"] == name
    assert result["role"] == "checkbox" and result["value"] == "premium-plan"
    assert result["checked"] is False


def test_operation_head_shares_control_names_values_and_selection_options():
    data = source()
    selection = native("Room type", "select")
    selection["node"].update(role="combobox", value="standard",
                             option={"label": "Design", "value": "design"}, selected=["Standard"])
    selection["node"]["state"].update(tag="select", type="select-one")
    data["candidates"]["select"] = {"option-design": selection}
    data["candidates"]["click"][TOKEN]["name"] = "Load services"
    result = json.loads(project(data)[1])["candidates"]
    assert result["click"]["1"]["name"] == "Load services"
    assert result["click"]["1"]["role"] == data["candidates"]["click"][TOKEN]["node"]["role"]
    assert result["fill"]["1"]["name"] == "Search query"
    assert result["select"]["option-design"]["name"] == "Room type"
    assert result["select"]["option-design"]["value"] == "standard"
    assert result["select"]["option-design"]["option"] == {"label": "Design", "value": "design"}


def test_operation_head_shares_full_link_and_navigation_destinations():
    row = native("Exact requested title", "click")
    row["node"].update(role="link", href="https://example.test/article?edition=2#section")
    row["node"]["state"].update(tag="a", type=None)
    route = descriptor("Exact requested title", operation="navigate", node={}, form={})
    route.update(destination=row["node"]["href"], context={}, origin=None, url=None)
    result = json.loads(project(context({"click": {TOKEN: row}, "navigate": {"route": route}}))[1])["candidates"]
    assert result["click"]["1"]["name"] == "Exact requested title"
    assert result["click"]["1"]["href"] == row["node"]["href"]
    assert result["navigate"]["route"]["name"] == "Exact requested title"
    assert result["navigate"]["route"]["destination"] == row["node"]["href"]
