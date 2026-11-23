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
        target = next(q for q in body["questions"] if q["name"] == "target_click")["choices"][0]["value"]
        return httpx.Response(200, content=_wire_answers(body, {"operation": choice, "target_click": target}))

    provider = OpenAIProvider("fixture", transport=httpx.MockTransport(handler))
    try:
        result = await provider.decide(questions, json.dumps(data), budget=_budget(), privacy=_gate(), target_heads=heads)
    finally:
        await provider.aclose()
    assert result == Decision(choice, "paper" if choice == "click" else None, 1.0)
    body = seen[0]
    operations = body["questions"][0]["choices"]
    by_choice = {row["value"]: row.get("description", "") for row in operations}
    assert "UNABLE" in by_choice and "click" in by_choice
    assert json.loads(body["input"])["goal"] == data["goal"]
    if custom:
        assert tuple(by_choice.values()) == descriptions
    else:
        assert "next unfinished requirement" in by_choice["click"]
        assert "no offered safe operation and target" in by_choice["UNABLE"]
        bound_fill = next(row for row in operations if row["value"].startswith("action_"))
        description = json.loads(bound_fill["description"])
        assert description["operation"] == "fill"
        assert "matching supplied value" in description["operation_hint"]
        assert "fill=matches" in description["evidence"]


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
    if operation in {"submit", "enter"}:
        bound = next(c for c in body["questions"][0]["choices"]
                     if c["value"].startswith("action_") and json.loads(c["description"])["operation"] == operation)
        description = json.loads(bound["description"])["evidence"]
    else:
        description = next(c["description"] for q in body["questions"] if q["name"] == "target_click"
                           for c in q["choices"] if "Save" in c.get("description", ""))
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
async def test_scroll_evidence_is_complete_or_omitted(size):
    first = native("A" * size, "click")
    second = native("B" * size, "click")
    scrolling = native("down", "scroll")
    scrolling["node"] = {}
    data = context({"click": {"first": first, "second": second}, "scroll": {"down": scrolling}},
                   goal="Open the requested result.", scroll={"up": False, "down": False})
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
    operation = seen[0]["questions"][0]
    descriptions = [c.get("description", "") for c in operation["choices"]]
    evidence = [d for d in descriptions if "Observed evidence" in d]
    if size == 20:
        assert len(evidence) == 1
        content = evidence[0]
        if content.startswith("{"):
            content = json.loads(content)["evidence"]
        observed = json.loads(content.split("Observed evidence: ", 1)[1])
        assert observed == {"scroll": {"up": False, "down": False}, "visible_controls": ["A" * size, "B" * size]}
    else:
        assert not evidence


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
