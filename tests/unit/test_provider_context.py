"""Compact wire choices retain the runner's original guarded target contract."""

from __future__ import annotations

import json

import httpx
import pytest
from test_provider import _budget, _decision_body, _gate, _wire_answers

from decision_browser.action_context import NEXT_ACTION, TARGET, context_payload
from decision_browser.decision_codec import ChoiceQuestion, Decision
from decision_browser.dom import Observation
from decision_browser.privacy import PrivacyError
from decision_browser.provider import OpenAIProvider, ProviderError
from decision_browser.systemone_provider import SystemOneProvider

TOKEN = "0123456789abcdef0123456789abcdef-0"
URL = "https://example.test/search"
PRIVATE = "TEST_ONLY_BLOCKED_CONTEXT_VALUE"


def request_context():
    node = {
        "name": "Search query", "role": "textbox", "value": "",
        "state": {"tag": "input", "type": "text", "disabled": False,
                  "readonly": False, "checked": False,
                  "aria": {"expanded": "false", "selected": None}},
        "caption": "Find an article",
    }
    entry = {
        "operation": "fill", "origin": "https://example.test", "url": URL,
        "destination": None, "name": "Search query", "node": node,
        "form": {"action": URL, "method": "get", "name": "Search"},
        "context": {"focused": False},
    }
    obs = Observation("obs", "https://example.test", "doc", "Article search",
                      {}, (), url=URL)
    text = context_payload("Search for orbital mechanics", obs,
                           {"fill": {TOKEN: entry}},
                           [{"operation": "click", "name": "Search", "acknowledged": True}])
    questions = (
        ChoiceQuestion("operation", "Choose observed operation", ("fill", "DONE")),
        ChoiceQuestion("target_fill", "Choose offered field", (TOKEN,)),
    )
    return questions, text


async def test_compact_wire_target_decodes_to_original_runner_token():
    questions, text = request_context()
    seen = []

    def handler(request):
        body = _decision_body(request)
        seen.append(body)
        target = body["questions"][1]["choices"][0]["value"]
        assert target != TOKEN and len(target) < len(TOKEN)
        assert TOKEN not in request.content.decode()
        return httpx.Response(200, content=_wire_answers(body, {
            "operation": "fill", "target_fill": target,
        }))

    provider = OpenAIProvider("test-only", transport=httpx.MockTransport(handler))
    budget = _budget()
    try:
        decision = await provider.decide(questions, text, budget=budget, privacy=_gate(),
                                         target_heads={"fill": "target_fill"})
    finally:
        await provider.aclose()
    assert decision == Decision("fill", TOKEN, 1.0)
    assert questions[1].choices == (TOKEN,)
    assert json.loads(text)["candidates"]["fill"][TOKEN]["node"]["value"] == ""
    assert len(seen) == budget.usage().calls == 1


@pytest.mark.parametrize("target,code", [(TOKEN, "unknown_choice"), (False, "rejected_sentinel")])
async def test_original_id_or_wire_padding_cannot_bypass_wire_domain(target, code):
    questions, text = request_context()

    def handler(request):
        body = _decision_body(request)
        return httpx.Response(200, content=_wire_answers(body, {
            "operation": "fill", "target_fill": target,
        }))

    provider = OpenAIProvider("test-only", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.decide(questions, text, budget=_budget(), privacy=_gate(),
                                  target_heads={"fill": "target_fill"})
        assert caught.value.code == code
    finally:
        await provider.aclose()


@pytest.mark.parametrize("location", ["name", "caption", "value", "form", "history"])
async def test_compact_context_keeps_sensitive_semantics_behind_privacy_gate(location):
    questions, text = request_context()
    data = json.loads(text)
    entry = data["candidates"]["fill"][TOKEN]
    if location == "name":
        entry["name"] = entry["node"]["name"] = PRIVATE
    elif location in {"caption", "value"}:
        entry["node"][location] = PRIVATE
    elif location == "form":
        entry["form"]["action"] = "https://example.test/" + PRIVATE
    else:
        data["recent_actions"][0]["name"] = PRIVATE
    hits = []

    def forbidden(request):
        hits.append(request)
        raise AssertionError("private context must not reach HTTP")

    provider = OpenAIProvider("test-only", transport=httpx.MockTransport(forbidden))
    budget = _budget()
    try:
        with pytest.raises(PrivacyError, match="blocked_literal"):
            await provider.decide(questions, json.dumps(data), budget=budget,
                                  privacy=_gate(PRIVATE), target_heads={"fill": "target_fill"})
    finally:
        await provider.aclose()
    assert not hits and budget.usage().calls == 0


async def test_malformed_marked_context_fails_before_http_or_call_reservation():
    questions, text = request_context()
    data = json.loads(text)
    data["context_format"] = "decision-browser-context-v1"
    data["candidates"]["fill"] = {}
    hits = []

    def forbidden(request):
        hits.append(request)
        raise AssertionError("invalid mapping must not reach HTTP")

    provider = OpenAIProvider("test-only", transport=httpx.MockTransport(forbidden))
    budget = _budget()
    try:
        with pytest.raises(ProviderError, match="invalid_context"):
            await provider.decide(questions, json.dumps(data), budget=budget, privacy=_gate(),
                                  target_heads={"fill": "target_fill"})
    finally:
        await provider.aclose()
    assert not hits and budget.usage().calls == 0


@pytest.mark.parametrize("text", [
    '{"context_format":"decision-browser-context-v1","context_format":"other","candidates":[]}',
    r'{"context_\u0066ormat":"decision-browser-context-v1",',
    '{"context_format":"decision-browser-context-v1","context_format":',
])
async def test_duplicate_or_escaped_reserved_header_cannot_reach_http(text):
    questions, _text = request_context()
    hits = []

    def forbidden(request):
        hits.append(request)
        raise AssertionError("malformed reserved header must not reach HTTP")

    provider = OpenAIProvider("test-only", transport=httpx.MockTransport(forbidden))
    budget = _budget()
    try:
        with pytest.raises(ProviderError, match="invalid_context"):
            await provider.decide(questions, text, budget=budget, privacy=_gate(),
                                  target_heads={"fill": "target_fill"})
    finally:
        await provider.aclose()
    assert not hits and budget.usage().calls == 0


@pytest.mark.parametrize("route", ["openai", "systemone"])
@pytest.mark.parametrize("location", ["goal", "caption"])
@pytest.mark.parametrize("secret", ['quoted"value', r"back\slash", "line\nbreak", "tab\tstop"])
async def test_escaped_semantic_context_literal_is_blocked_before_nested_json(route, location, secret):
    questions, text = request_context()
    data = json.loads(text)
    if location == "goal":
        data["goal"] = secret
    else:
        data["candidates"]["fill"][TOKEN]["node"]["caption"] = secret
    hits = []

    def forbidden(request):
        hits.append(request)
        raise AssertionError("escaped private value reached HTTP")

    transport = httpx.MockTransport(forbidden)
    provider = (OpenAIProvider("test-only", transport=transport) if route == "openai"
                else SystemOneProvider(endpoint="http://127.0.0.1:8000/v1/systemone",
                                       model="fixture", transport=transport))
    budget = _budget()
    try:
        with pytest.raises(PrivacyError, match="blocked_literal"):
            await provider.decide(questions, json.dumps(data), budget=budget,
                                  privacy=_gate(secret), target_heads={"fill": "target_fill"})
    finally:
        await provider.aclose()
    assert not hits and budget.usage().calls == 0


@pytest.mark.parametrize("route", ["openai", "systemone"])
async def test_native_singleton_rejection_guidance_depends_on_provider(route):
    original, text = request_context()
    questions = (
        ChoiceQuestion("operation", NEXT_ACTION, original[0].choices),
        ChoiceQuestion("target_fill", TARGET + " Operation: fill", original[1].choices),
    )
    seen = []

    def capture(request):
        seen.append(json.loads(request.content))
        return httpx.Response(400, json={})

    transport = httpx.MockTransport(capture)
    provider = (OpenAIProvider("test-only", transport=transport) if route == "openai"
                else SystemOneProvider(endpoint="http://127.0.0.1:8000/v1/systemone",
                                       model="fixture", transport=transport))
    try:
        with pytest.raises(ProviderError):
            await provider.decide(questions, text, budget=_budget(), privacy=_gate(),
                                  target_heads={"fill": "target_fill"})
    finally:
        await provider.aclose()

    assert len(seen) == 1
    if route == "openai":
        target = seen[0]["questions"][1]
        assert target["instructions"].endswith(" Operation: fill")
        assert target["choices"][-1] == {"value": False}
    else:
        target = seen[0]["questions"]["target_fill"]
        assert "Operation: fill Reject the sole target only if it is unsuitable" in target["instructions"]
        rejection = json.loads(target["criteria"]["reject"])
        assert rejection["reject"] is True and "authorize no action" in rejection["description"]


@pytest.mark.parametrize("scenario", ["selected", "empty", "other_form", "disabled_submit", "custom_description"])
async def test_luna_form_hints_preserve_the_requested_clear_action(scenario):
    from test_compact_context_v2 import standard_questions
    from test_lean_context import native
    from test_model_context import context

    form = {"path": "0/2", "id": None, "name": None, "action": URL, "method": "post",
            "formAction": None, "formMethod": None, "formTarget": None}
    select = native("Preference", "select")
    select["form"] = form
    select["node"]["state"]["tag"] = "select"
    select["node"].update(selected=["" if scenario == "empty" else "standard"],
                          option={"value": "", "label": "Clear preference"})
    submit = native("Apply", "submit")
    submit["form"] = {**form, "path": "0/3" if scenario == "other_form" else form["path"]}
    submit["node"]["state"].update(tag="form", disabled=scenario == "disabled_submit")
    candidates = {"select_group0": {"clear": select}, "submit": {"apply": submit},
                  "wait": {"short": native("Wait", "wait")}}
    data = context(candidates, goal="Clear the preference",
                   recent_actions=[{"operation": "wait", "acknowledged": True, "page_changed": False}] * 2)
    questions, heads = standard_questions(candidates)
    op = questions[0]
    choices = (*op.choices, "UNABLE")
    custom = ("User-supplied choice detail.", None, None, None, None) if scenario == "custom_description" else ()
    questions = (ChoiceQuestion(op.name, op.instructions, choices, custom), *questions[1:])
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        option = next(row["value"] for row in body["questions"][0]["choices"]
                      if row["value"] == "select_group0" or row["value"].startswith("action_")
                      and json.loads(row["description"])["operation"] == "select_group0")
        return httpx.Response(200, content=_wire_answers(body, {"operation": option, "target_select_group0": "clear"}))

    provider = OpenAIProvider("fixture", transport=httpx.MockTransport(handler))
    try:
        decision = await provider.decide(questions, json.dumps(data), budget=_budget(), privacy=_gate(), target_heads=heads)
    finally:
        await provider.aclose()
    assert decision == Decision("select_group0", "clear", 1.0)
    assert len(seen) == 1
    options = {}
    for row in seen[0]["questions"][0]["choices"]:
        description = row.get("description", "")
        key = json.loads(description)["operation"] if row["value"].startswith("action_") else row["value"]
        options[key] = description
    assert tuple(options) == choices
    if scenario == "selected":
        assert ("Change a control to a different value required by the goal. "
                                            "An empty option clears the field; leave a matching value in place.") in options["select_group0"]
        assert "Advance the visible form when its current fields and selections match the requested values." in options["submit"]
    else:
        assert "Advance the visible form" not in options["submit"]
    if scenario != "custom_description":
        assert "Change a control" in options["select_group0"]
    if custom:
        assert options["DONE"] == custom[0] and options["wait"] == ""
    else:
        assert "last two waits" in options["wait"]


@pytest.mark.parametrize("selection,error", [
    ("pair", None), ("multiple", None), ("UNABLE", None), ("DONE", None),
    ("click", "unknown_choice"), ("action_unknown", "unknown_choice"),
    ("refuse_operation", "refusal"), ("refuse_target", "refusal"),
    ("unknown_target", "unknown_choice"), ("missing_probability", "incomplete_distribution"),
    ("extra_answer", "extra_answer"),
])
async def test_native_explicit_pair_and_conditional_target_share_a_validated_decision(selection, error):
    from test_compact_context_v2 import standard_questions
    from test_lean_context import native
    from test_model_context import context

    candidates = {"click": {TOKEN: native("Preview route", "click")},
                  "fill": {"room": native("Room"), "guest": native("Guest")}}
    questions, heads = standard_questions(candidates)
    op = questions[0]
    questions = (ChoiceQuestion(op.name, op.instructions, (*op.choices, "UNABLE")), *questions[1:])

    def handler(request):
        body = json.loads(request.content)
        operation, target = body["questions"]
        assert (operation["name"], target["name"]) == ("operation", "target_fill")
        choices = operation["choices"]
        token = choices[1]["value"]
        assert token.startswith("action_") and token not in {*heads, TOKEN, "1"}
        pair = json.loads(choices[1]["description"])
        assert pair["operation"] == "click" and pair["target"] == "1"
        assert pair["evidence"].startswith('"Preview route"')
        assert "Operation: click" in operation["instructions"]
        assert "Operation: click" not in choices[1]["description"]
        assert [row["value"] for row in choices] == ["DONE", token, "fill", "UNABLE"]
        assert [row["value"] for row in target["choices"]] == ["room", "guest"]
        assert TOKEN not in request.content.decode()
        picked = {"pair": token, "multiple": "fill", "refuse_target": "fill",
                  "unknown_target": "fill", "missing_probability": token, "extra_answer": token}.get(selection, selection)
        reply = json.loads(_wire_answers(body, {"operation": picked, "target_fill": "guest"}))
        if selection == "refuse_operation":
            reply["answers"][0] = {"name": "operation", "type": "refusal"}
        elif selection == "refuse_target":
            reply["answers"][1] = {"name": "target_fill", "type": "refusal"}
        elif selection == "unknown_target":
            reply["answers"][1]["choice"] = "foreign"
        elif selection == "missing_probability":
            reply["answers"][0]["probabilities"].pop()
        elif selection == "extra_answer":
            reply["answers"].append({"name": "target_click", "type": "refusal"})
        return httpx.Response(200, json=reply)

    provider = OpenAIProvider("fixture", transport=httpx.MockTransport(handler))
    budget = _budget()
    try:
        call = provider.decide(questions, json.dumps(context(candidates)), budget=budget, privacy=_gate(), target_heads=heads)
        if error:
            with pytest.raises(ProviderError) as caught:
                await call
            assert caught.value.code == error
        else:
            expected = {"pair": ("click", TOKEN), "multiple": ("fill", "guest")}.get(selection, (selection, None))
            assert await call == Decision(*expected, 1.0)
    finally:
        await provider.aclose()
    assert budget.usage().calls == 1


@pytest.mark.parametrize("scenario", ["caller_supplied_v3", "truncated_evidence"])
async def test_ineligible_singleton_keeps_an_explicit_rejectable_target(scenario):
    from test_compact_context_v2 import standard_questions
    from test_lean_context import native
    from test_model_context import context

    candidates = {"click": {TOKEN: native("Very long label " * 20, "click")}}
    questions, heads = standard_questions(candidates)
    if scenario == "caller_supplied_v3":
        text = '{"context_format":"decision-browser-choice-table-v3"}'
    else:
        text = json.dumps(context(candidates))

    def handle(request):
        body = json.loads(request.content)
        assert [q["name"] for q in body["questions"]] == ["operation", "target_click"]
        assert [o["value"] for o in body["questions"][0]["choices"]] == ["DONE", "click"]
        return httpx.Response(200, content=_wire_answers(body, {"operation": "click", "target_click": False}))

    provider = OpenAIProvider("fixture", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.decide(questions, text, budget=_budget(), privacy=_gate(), target_heads=heads)
        assert caught.value.code == "rejected_sentinel"
    finally:
        await provider.aclose()


@pytest.mark.parametrize("availability", ["editable", "disabled", "readonly"])
async def test_luna_standalone_native_select_has_operation_guidance_and_observed_options(availability):
    from test_compact_context_v2 import standard_questions
    from test_lean_context import native
    from test_model_context import context

    candidates = {"select": {}}
    for label, value in (("Standard", "standard"), ("Design", "design")):
        row = native("Stay category", "select")
        row["form"] = {}
        row["node"].update(role="combobox", value="standard", selected=["Standard"],
                            option={"label": label, "value": value})
        row["node"]["state"].update(tag="select", type="select-one",
                                    disabled=availability == "disabled", readonly=availability == "readonly")
        candidates["select"][value] = row
    candidates["click"] = {"open": native("Open result", "click")}
    data = context(candidates, goal="Choose Design, then open the requested result.")
    questions, heads = standard_questions(candidates)
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        target = next(q for q in body["questions"] if q["name"] == "target_select")
        design = next(row["value"] for row in target["choices"] if "Design" in row.get("description", ""))
        return httpx.Response(200, content=_wire_answers(body, {"operation": "select", "target_select": design}))

    provider = OpenAIProvider("fixture", transport=httpx.MockTransport(handler))
    try:
        decision = await provider.decide(questions, json.dumps(data), budget=_budget(), privacy=_gate(), target_heads=heads)
    finally:
        await provider.aclose()
    assert decision == Decision("select", "design", 1.0)
    assert len(seen) == 1
    body = seen[0]
    operation = body["questions"][0]
    select = next(row for row in operation["choices"] if row["value"] == "select")
    if availability == "editable":
        assert "Change a control" in select["description"]
        assert "current value with offered option labels and values" in select["description"]
    else:
        assert "description" not in select
    state = json.loads(body["input"])
    assert state["goal"] == data["goal"]
    entries = state["candidates"]["select"]
    assert len(entries) == 2
    for entry in entries.values():
        assert entry["value"] == "standard" and entry["selected"] == ["Standard"]
    assert {row["option"]["value"] for row in entries.values()} == {"standard", "design"}
