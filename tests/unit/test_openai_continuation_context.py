"""Continuation criteria clarify native choices while preserving action bindings."""
import json

import httpx
import pytest
from test_compact_context_v2 import standard_questions
from test_lean_context import native
from test_model_context import context
from test_provider import _budget, _gate, _wire_answers

from decision_browser.decision_codec import ChoiceQuestion, Decision
from decision_browser.provider import OpenAIProvider


@pytest.mark.parametrize("choice", ["click", "UNABLE"])
@pytest.mark.parametrize("custom", [False, True])
async def test_native_continuation_guidance_preserves_choices_and_stop(choice, custom):
    links = {}
    for key, name in (("paper", "Peer-reviewed edition"), ("abstract", "Conference abstract")):
        links[key] = native(name, "click")
        links[key]["node"].update(role="link", href="https://example.test/" + key)
        links[key]["node"]["state"].update(tag="a", type=None)
    form = native("Reference", "fill")
    form["node"].update(value="REF-12")
    form["fill_state"] = "matches"
    data = context({"click": links, "fill": {"reference": form}},
                   goal="Choose the peer-reviewed edition and save the reading list.",
                   completion={"total": 2, "pending": 2})
    questions, heads = standard_questions(data["candidates"])
    op = questions[0]
    descriptions = ("Custom completion.", "Custom click.", "Custom fill.", "Custom stop.") if custom else ()
    questions = (ChoiceQuestion(op.name, op.instructions, (*op.choices, "UNABLE"), descriptions), *questions[1:])
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if custom:
            target = next(q for q in body["questions"] if q["name"] == "target_click")["choices"][0]["value"]
            selection = {"operation": choice, "target_click": target}
        else:
            actions = body["questions"][0]["choices"]
            selected = next(row["value"] for row in actions
                            if row["value"].startswith("action_")
                            and json.loads(row["description"])["operation"] == "click"
                            and json.loads(row["description"])["target"] == "paper")
            selection = {"operation": selected if choice == "click" else choice}
        return httpx.Response(200, content=_wire_answers(body, selection))

    provider = OpenAIProvider("fixture", transport=httpx.MockTransport(handler))
    try:
        result = await provider.decide(questions, json.dumps(data), budget=_budget(), privacy=_gate(), target_heads=heads)
    finally:
        await provider.aclose()
    assert result == Decision(choice, "paper" if choice == "click" else None, 1.0)
    body = seen[0]
    operations = body["questions"][0]["choices"]
    by_choice = {row["value"]: row.get("description", "") for row in operations}
    assert "UNABLE" in by_choice
    packed = json.loads(body["input"])
    assert packed["goal"] == data["goal"]
    if custom:
        assert "click" in by_choice
        assert tuple(by_choice.values()) == descriptions
        assert {q["name"] for q in body["questions"]} == {"operation", "target_click", "target_fill"}
    else:
        assert len(body["questions"]) == 1
        assert "next unfinished requirement" in packed["operation_guidance"]["click"]
        assert "no offered safe operation and target" in by_choice["UNABLE"]
        bound_fill = next(json.loads(row["description"]) for row in operations
                          if row["value"].startswith("action_")
                          and json.loads(row["description"])["operation"] == "fill")
        assert "matching supplied value" in packed["operation_guidance"]["fill"]
        assert "fill=matches" in bound_fill["evidence"]
        assert {(row["operation"], row["target"]) for row in
                (json.loads(c["description"]) for c in operations if c["value"].startswith("action_"))} == {
                    ("click", "paper"), ("click", "abstract"), ("fill", "reference"),
                }


@pytest.mark.parametrize("operation", ["click", "click_implicit", "submit", "enter"])
@pytest.mark.parametrize("already_selected", [False, True])
@pytest.mark.parametrize("evidence_size", [20, 3000])
async def test_native_submission_evidence_uses_only_its_form(operation, already_selected, evidence_size):
    implicit = operation == "click_implicit"
    operation = "click" if implicit else operation
    form = {"path": "0/1", "id": None, "name": None, "action": "https://example.test/next", "method": "post",
            "formAction": None, "formMethod": None, "formTarget": None}
    submit = native("Save", operation)
    submit["form"] = {**form, "submits": True}
    submit["node"]["state"].update(tag="button" if operation == "click" else "input" if operation == "enter" else "form",
                                      type="submit" if operation == "click" and not implicit else "text" if operation == "enter" else None)
    menu = native("Category", "select")
    menu["form"] = {**form, "submits": False}
    menu["node"].update(role="combobox", value="camera" if already_selected else "",
                        selected=["camera" if already_selected else ""],
                        option={"label": "Camera", "value": "camera"})
    menu["node"]["state"].update(tag="select", type="select-one")
    if not already_selected:
        menu["node"]["valueMissing"] = True
    checkbox = native("Coverage " + "X" * evidence_size, "click")
    checkbox["form"] = form
    checkbox["node"].update(role="checkbox")
    checkbox["node"]["state"].update(tag="input", type="checkbox", checked=False)
    foreign = native("Other category", "fill")
    foreign["form"] = {**form, "path": "0/2"}
    foreign["node"]["valueMissing"] = True
    candidates = {operation: {"save": submit}, "select": {"camera": menu}, "click": {"check": checkbox},
                  "fill": {"other": foreign}}
    candidates[operation]["save"] = submit
    data = context(candidates, goal="Choose the category and save according to the requested checkbox state.")
    questions, heads = standard_questions(candidates)
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, content=_wire_answers(body, {"operation": "DONE"}))

    provider = OpenAIProvider("fixture", transport=httpx.MockTransport(handler))
    try:
        await provider.decide(questions, json.dumps(data), budget=_budget(), privacy=_gate(), target_heads=heads)
    finally:
        await provider.aclose()
    body = seen[0]
    if evidence_size > 2048 and operation in {"submit", "enter"}:
        assert [q["name"] for q in body["questions"]] == ["operation", "target_click"]
        assert body["questions"][1]["choices"][-1] == {"value": False}
    else:
        assert len(body["questions"]) == 1
    bound = next(json.loads(c["description"]) for c in body["questions"][0]["choices"]
                 if c["value"].startswith("action_")
                 and json.loads(c["description"])["operation"] == operation
                 and "Save" in json.loads(c["description"])["evidence"])
    description = bound["evidence"]
    if evidence_size > 2048:
        assert "Observed form fields" not in description
        return
    fields = json.loads(description.split("Observed form fields: ", 1)[1])
    assert not any(row["name"] == "Other category" for row in fields)
    assert next(row for row in fields if row["name"] == "Coverage " + "X" * evidence_size)["checked"] is False
    category = next(row for row in fields if row["name"] == "Category")
    assert category["value"] == ("camera" if already_selected else "")
    assert category.get("valueMissing", False) is not already_selected
    assert "unchecked may be correct" in description
    assert "Submission uses the current checkbox states" in description


@pytest.mark.parametrize("size", [20, 3000])
@pytest.mark.parametrize("scroll", [
    {"up": False, "down": False}, {"up": False, "down": True},
    {"up": True, "down": False}, {"up": True, "down": True},
])
async def test_scroll_boundaries_and_click_names_describe_their_own_operation(size, scroll):
    first = native("A" * size, "click")
    second = native("B" * size, "click")
    scrolling = native("down", "scroll")
    scrolling["node"] = {}
    data = context({"click": {"first": first, "second": second}, "scroll": {"down": scrolling}},
                   goal="Open the requested result.", scroll=scroll)
    questions, heads = standard_questions(data["candidates"])
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, content=_wire_answers(body, {"operation": "DONE"}))

    provider = OpenAIProvider("fixture", transport=httpx.MockTransport(handler))
    try:
        await provider.decide(questions, json.dumps(data), budget=_budget(), privacy=_gate(), target_heads=heads)
    finally:
        await provider.aclose()
    assert len(seen[0]["questions"]) == 1
    operation = seen[0]["questions"][0]
    actions = [json.loads(c["description"]) for c in operation["choices"]
               if c["value"].startswith("action_")]
    assert {(row["operation"], row["target"]) for row in actions} == {
        ("click", "first"), ("click", "second"), ("scroll", "down"),
    }
    scroll_action = next(row for row in actions if row["operation"] == "scroll")
    content = scroll_action["evidence"]
    observed = json.loads(content.split("Observed evidence: ", 1)[1])
    assert observed == {"scroll": scroll}
    assert first["name"] not in content and second["name"] not in content
    assert ("Neither direction can move" in content) == (not any(scroll.values()))
    guidance = json.loads(seen[0]["input"])["operation_guidance"]
    assert first["name"] not in guidance["scroll"] and second["name"] not in guidance["scroll"]
    if size == 20:
        names = json.loads(guidance["click"].split("Observed click controls: ", 1)[1])
        assert names == [first["name"], second["name"]]
    else:
        assert "Observed click controls" not in guidance["click"]


@pytest.mark.parametrize("checked", [False, True])
async def test_native_checkbox_click_describes_toggle_effect_under_negative_goal(checked):
    row = native("Optional coverage", "click")
    row["node"].update(role="checkbox")
    row["node"]["state"].update(tag="input", type="checkbox", checked=checked)
    data = context({"click": {"coverage": row}}, goal="Do not enable Optional coverage.")
    questions, heads = standard_questions(data["candidates"])
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, content=_wire_answers(body, {"operation": "DONE"}))

    provider = OpenAIProvider("fixture", transport=httpx.MockTransport(handler))
    try:
        await provider.decide(questions, json.dumps(data), budget=_budget(), privacy=_gate(), target_heads=heads)
    finally:
        await provider.aclose()
    body = seen[0]
    action = next(c for c in body["questions"][0]["choices"] if c["value"].startswith("action_"))
    evidence = json.loads(action["description"])["evidence"]
    assert ("currently checked checkbox off" if checked else "currently unchecked checkbox on") in evidence
    assert json.loads(body["input"])["goal"] == data["goal"]
    assert data["candidates"]["click"]["coverage"]["node"]["state"]["checked"] == checked


@pytest.mark.parametrize("checked", [False, True])
@pytest.mark.parametrize("requested", ["enable", "disable"])
async def test_optional_checkbox_submission_retains_user_requirement(checked, requested):
    form = {"path": "0/1", "id": None, "name": None, "action": "https://example.test/next", "method": "post",
            "formAction": None, "formMethod": None, "formTarget": None}
    checkbox = native("Audio description", "click")
    checkbox["form"] = {**form, "submits": False}
    checkbox["node"].update(role="checkbox")
    checkbox["node"]["state"].update(tag="input", type="checkbox", checked=checked)
    submit = native("Continue", "submit")
    submit["form"] = {**form, "submits": True}
    submit["node"]["state"].update(tag="form", type=None)
    data = context({"click": {"audio": checkbox}, "submit": {"next": submit}},
                   goal=f"{requested.capitalize()} Audio description and continue.")
    questions, heads = standard_questions(data["candidates"])
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, content=_wire_answers(body, {"operation": "DONE"}))

    provider = OpenAIProvider("fixture", transport=httpx.MockTransport(handler))
    try:
        await provider.decide(questions, json.dumps(data), budget=_budget(), privacy=_gate(), target_heads=heads)
    finally:
        await provider.aclose()
    choices = seen[0]["questions"][0]["choices"]
    submission = next(json.loads(c["description"]) for c in choices if c["value"].startswith("action_")
                      and json.loads(c["description"])["operation"] == "submit")
    evidence = submission["evidence"]
    fields = json.loads(evidence.split("Observed form fields: ", 1)[1])
    assert fields == [{"name": "Audio description", "checked": checked}]
    assert "must be satisfied even if the HTML field is optional" in evidence
    assert "If its state already matches the request, leave it unchanged" in evidence
    assert "choose the offered checkbox click before submitting" in evidence
    assert json.loads(seen[0]["input"])["goal"] == data["goal"]


async def test_split_click_groups_describe_only_their_own_targets():
    candidates = {
        "click_group0": {f"a{i}": native(f"First link {i}", "click") for i in range(2)},
        "click_group1": {f"b{i}": native(f"Second link {i}", "click") for i in range(2)},
    }
    data = context(candidates, goal="Open the requested link.")
    questions, heads = standard_questions(candidates)
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, content=_wire_answers(body, {"operation": "DONE"}))

    provider = OpenAIProvider("fixture", transport=httpx.MockTransport(handler))
    try:
        await provider.decide(questions, json.dumps(data), budget=_budget(), privacy=_gate(), target_heads=heads)
    finally:
        await provider.aclose()
    assert len(seen[0]["questions"]) == 1
    guidance = json.loads(seen[0]["input"])["operation_guidance"]
    actions = [json.loads(c["description"]) for c in seen[0]["questions"][0]["choices"]
               if c["value"].startswith("action_")]
    for group, rows in candidates.items():
        assert {row["target"] for row in actions if row["operation"] == group} == set(rows)
        names = json.loads(guidance[group].split("Observed click controls: ", 1)[1])
        assert names == [row["name"] for row in rows.values()]
