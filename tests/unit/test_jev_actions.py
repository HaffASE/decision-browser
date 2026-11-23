"""Complete-action Jev requests retain the original guarded runner bindings."""
import json

import httpx
import pytest

from decision_browser.action_context import NEXT_ACTION, TARGET
from decision_browser.budget import Budget
from decision_browser.contracts import Limits, PrivacyPolicy
from decision_browser.decision_codec import ChoiceQuestion
from decision_browser.model_context import _ARIA_KEYS
from decision_browser.privacy import PrivacyError, PrivacyGate
from decision_browser.provider_http import ProviderError
from decision_browser.systemone_provider import SystemOneProvider


def native(count=2):
    entries = {}
    for index in range(count):
        entries[f"opaque-token-{index}-with-long-identity"] = {
            "operation": "fill", "origin": "https://site.test", "url": "https://site.test/",
            "destination": None, "name": f"Field {index}",
            "node": {"name": f"Field {index}", "role": "textbox", "value": "", "state": {
                "tag": "input", "type": "text", "disabled": False, "readonly": False,
                "checked": False, "aria": dict.fromkeys(_ARIA_KEYS),
            }},
            "form": {}, "context": {},
        }
    data = {"context_format": "decision-browser-context-v1", "goal": "Fill Field 1 with blue",
            "origin": "https://site.test", "url": "https://site.test/", "text": "Field 0 Field 1",
            "candidates": {"fill": entries}, "recent_actions": [], "excluded_mechanisms": []}
    questions = (
        ChoiceQuestion("operation", NEXT_ACTION, ("UNABLE", "fill", "DONE")),
        ChoiceQuestion("target_fill", TARGET + " Operation: fill", tuple(entries)),
    )
    return data, questions


def response(body, chosen):
    criteria = body["questions"]["next_action"]["criteria"]
    return {"model": "jev-1.14.0", "usage": {}, "answers": {
        "next_action": {"type": "choice", "choice": chosen, "confidence": 1.0,
                        "probabilities": {key: float(key == chosen) for key in criteria}},
    }}


@pytest.mark.parametrize("choice,operation,target", [
    ("c2", "fill", "opaque-token-1-with-long-identity"),
    ("c0", "UNABLE", None), ("c3", "DONE", None),
])
async def test_native_complete_action_and_terminals_restore_original_binding(choice, operation, target):
    data, questions = native()
    seen = []

    def handler(request):
        body = json.loads(request.content)
        assert list(body["questions"]) == ["next_action"]
        assert "Field 1" in body["questions"]["next_action"]["criteria"]["c2"]
        assert "opaque-token" not in request.content.decode()
        state = json.loads(body["state"])
        assert state["user_goal"] == data["goal"]
        assert "user_supplied_values" not in state
        seen.append(body)
        return httpx.Response(200, json=response(body, choice))

    provider = SystemOneProvider(endpoint="https://api.typesafe.ai/v1/systemone",
                                 model="jev-latest", transport=httpx.MockTransport(handler))
    try:
        result = await provider.decide(questions, json.dumps(data), target_heads={"fill": "target_fill"},
                                       budget=Budget(Limits()), privacy=PrivacyGate(PrivacyPolicy()))
    finally:
        await provider.aclose()
    assert (result.operation, result.target) == (operation, target)
    assert len(seen) == 1


@pytest.mark.parametrize("change", ["large", "custom", "other_model", "unknown"])
async def test_unqualified_context_uses_existing_codec_without_action_pruning(change):
    data, questions = native(4 if change == "large" else 2)
    if change == "custom":
        questions = (ChoiceQuestion("operation", "custom instructions", questions[0].choices), questions[1])
    if change == "unknown":
        data["extra"] = "future evidence"
    seen = []

    def handler(request):
        body = json.loads(request.content)
        assert "next_action" not in body["questions"]
        seen.append(body)
        return httpx.Response(400, json={})

    provider = SystemOneProvider(endpoint="https://api.typesafe.ai/v1/systemone",
                                 model="future-model" if change == "other_model" else "jev-latest",
                                 max_choices=3, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError):
            await provider.decide(questions, json.dumps(data), target_heads={"fill": "target_fill"},
                                   budget=Budget(Limits()), privacy=PrivacyGate(PrivacyPolicy()))
    finally:
        await provider.aclose()
    assert len(seen) == 1


async def test_private_literal_is_blocked_before_nested_criteria_http():
    data, questions = native()
    data["goal"] = 'private"value'
    seen = []
    provider = SystemOneProvider(endpoint="https://api.typesafe.ai/v1/systemone", model="jev-latest",
                                 transport=httpx.MockTransport(lambda r: seen.append(r)))
    try:
        with pytest.raises(PrivacyError):
            await provider.decide(
                questions, json.dumps(data), target_heads={"fill": "target_fill"},
                budget=Budget(Limits()),
                privacy=PrivacyGate(PrivacyPolicy(blocked_literals=['private"value'])),
            )
    finally:
        await provider.aclose()
    assert not seen


async def test_scroll_directions_survive_aliasing_without_opaque_identity_descriptions():
    data, _ = native()
    tokens = ("opaque-direction-identifier-up", "opaque-direction-identifier-down")
    data["candidates"] = {"scroll": {
        token: {"operation": "scroll", "origin": None, "url": None, "destination": None,
                "name": direction, "node": {}, "form": {}, "context": {}}
        for token, direction in zip(tokens, ("up", "down"), strict=True)
    }}
    questions = (
        ChoiceQuestion("operation", NEXT_ACTION, ("scroll", "UNABLE")),
        ChoiceQuestion("target_scroll", TARGET + " Operation: scroll", tokens),
    )

    def handler(request):
        body = json.loads(request.content)
        options = body["questions"]["next_action"]["criteria"]
        assert "Scroll 'up'" in options["c0"]
        assert "Scroll 'down'" in options["c1"]
        assert "opaque-direction" not in request.content.decode()
        return httpx.Response(200, json=response(body, "c1"))

    provider = SystemOneProvider(endpoint="https://api.typesafe.ai/v1/systemone", model="jev-latest",
                                 transport=httpx.MockTransport(handler))
    try:
        result = await provider.decide(
            questions, json.dumps(data), target_heads={"scroll": "target_scroll"},
            budget=Budget(Limits()), privacy=PrivacyGate(PrivacyPolicy()),
        )
    finally:
        await provider.aclose()
    assert (result.operation, result.target) == ("scroll", tokens[1])


def with_wait(data, questions, *, repeats=2):
    data["candidates"]["wait"] = {"short": {
        "operation": "wait", "origin": None, "url": None, "destination": None,
        "name": "short", "node": {}, "form": {}, "context": {},
    }}
    data["recent_actions"] = [
        {"operation": "wait", "acknowledged": True, "page_changed": False}
        for _ in range(repeats)
    ]
    return (
        ChoiceQuestion("operation", NEXT_ACTION, (*questions[0].choices, "wait")),
        questions[1], ChoiceQuestion("target_wait", TARGET + " Operation: wait", ("short",)),
    )


@pytest.mark.parametrize("mode,alternative,expected", [
    ("c0", "c2", ("wait", "short")),  # Delayed content can still be arriving.
    ("c1", "c2", ("fill", "opaque-token-1-with-long-identity")),
    ("c1", "c0", ("UNABLE", None)),
])
async def test_unchanged_wait_reassessment_requires_two_valid_explicit_model_choices(mode, alternative, expected):
    data, questions = native()
    questions = with_wait(data, questions)
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert set(body["questions"]) == {"next_action", "progress_mode", "next_interaction"}
        assert set(body["questions"]["next_action"]["criteria"]) == {"c0", "c1", "c2", "c3", "c4"}
        assert body["questions"]["next_action"]["criteria"]["c4"] == (
            "Observe the current page again after 100 milliseconds without interacting."
        )
        assert "Observe the current page" not in str(body["questions"]["next_interaction"])
        answers = {}
        for name, key in (("next_action", "c4"), ("progress_mode", mode),
                          ("next_interaction", alternative)):
            criteria = body["questions"][name]["criteria"]
            answers[name] = {
                "type": "choice", "choice": key, "confidence": 0.9,
                "probabilities": {k: float(k == key) for k in criteria},
            }
        return httpx.Response(200, json={"model": "jev-1.14.0", "usage": {}, "answers": answers})

    provider = SystemOneProvider(endpoint="https://api.typesafe.ai/v1/systemone", model="jev-latest",
                                 transport=httpx.MockTransport(handler))
    try:
        budget = Budget(Limits())
        decision = await provider.decide(
            questions, json.dumps(data), target_heads={"fill": "target_fill", "wait": "target_wait"},
            budget=budget, privacy=PrivacyGate(PrivacyPolicy()),
        )
    finally:
        await provider.aclose()
    assert (decision.operation, decision.target) == expected
    assert len(requests) == budget.usage().calls == 1


@pytest.mark.parametrize("repeats,change", [(0, None), (1, None), (2, "changed"), (2, "unacknowledged")])
async def test_reassessment_uses_actual_acknowledged_unchanged_history(repeats, change):
    data, questions = native()
    questions = with_wait(data, questions, repeats=repeats)
    if change:
        data["recent_actions"][-1]["page_changed" if change == "changed" else "acknowledged"] = change == "changed"
    seen = []

    def handler(request):
        body = json.loads(request.content)
        assert set(body["questions"]) == {"next_action"}
        seen.append(body)
        return httpx.Response(200, json=response(body, "c4"))

    provider = SystemOneProvider(endpoint="https://api.typesafe.ai/v1/systemone", model="jev-latest",
                                 transport=httpx.MockTransport(handler))
    try:
        decision = await provider.decide(
            questions, json.dumps(data), target_heads={"fill": "target_fill", "wait": "target_wait"},
            budget=Budget(Limits()), privacy=PrivacyGate(PrivacyPolicy()),
        )
    finally:
        await provider.aclose()
    assert (decision.operation, decision.target) == ("wait", "short")
    assert len(seen) == 1


async def test_singleton_alternative_rejection_authorizes_no_action_without_second_post():
    data, questions = native()
    questions = with_wait(data, questions)
    del data["candidates"]["fill"]
    questions = (
        ChoiceQuestion("operation", NEXT_ACTION, ("UNABLE", "wait")), questions[-1],
    )
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        assert set(body["questions"]["next_interaction"]["criteria"]) == {"c0", "reject"}
        picks = {"next_action": "c1", "progress_mode": "c1", "next_interaction": "reject"}
        answers = {
            name: {"type": "choice", "choice": picks[name], "confidence": 1.0,
                   "probabilities": {key: float(key == picks[name]) for key in row["criteria"]}}
            for name, row in body["questions"].items()
        }
        return httpx.Response(200, json={"model": "jev-1.14.0", "usage": {}, "answers": answers})

    provider = SystemOneProvider(endpoint="https://api.typesafe.ai/v1/systemone", model="jev-latest",
                                 transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError, match="rejected_sentinel"):
            await provider.decide(
                questions, json.dumps(data), target_heads={"wait": "target_wait"},
                budget=Budget(Limits()), privacy=PrivacyGate(PrivacyPolicy()),
            )
    finally:
        await provider.aclose()
    assert len(seen) == 1


@pytest.mark.parametrize("up,down", [(False, True), (True, False), (True, True)])
async def test_scroll_descriptions_report_only_observed_boundaries(up, down):
    data, _ = native()
    data["scroll"] = {"up": up, "down": down}
    data["candidates"] = {"scroll": {
        direction: {"operation": "scroll", "origin": None, "url": None, "destination": None,
                    "name": direction, "node": {}, "form": {}, "context": {}}
        for direction in ("up", "down")
    }}
    questions = (
        ChoiceQuestion("operation", NEXT_ACTION, ("scroll", "UNABLE")),
        ChoiceQuestion("target_scroll", TARGET + " Operation: scroll", ("up", "down")),
    )

    def handler(request):
        body = json.loads(request.content)
        options = body["questions"]["next_action"]["criteria"]
        assert ("more content above" in options["c0"]) is up
        assert ("top boundary" in options["c0"]) is not up
        assert ("more content below" in options["c1"]) is down
        assert ("bottom boundary" in options["c1"]) is not down
        return httpx.Response(200, json=response(body, "c1"))

    provider = SystemOneProvider(endpoint="https://api.typesafe.ai/v1/systemone", model="jev-latest",
                                 transport=httpx.MockTransport(handler))
    try:
        result = await provider.decide(
            questions, json.dumps(data), target_heads={"scroll": "target_scroll"},
            budget=Budget(Limits()), privacy=PrivacyGate(PrivacyPolicy()),
        )
    finally:
        await provider.aclose()
    assert (result.operation, result.target) == ("scroll", "down")


@pytest.mark.parametrize("mode,expected", [
    ("c0", ("screenshot", "capture")),
    ("c1", ("fill", "opaque-token-1-with-long-identity")),
])
async def test_repeat_capture_is_opt_in_and_reassessment_offers_actual_interactions(mode, expected):
    data, questions = native()
    data["goal"] = "Capture the current viewport repeatedly as requested, or advance the page."
    data["candidates"]["screenshot"] = {"capture": {
        "operation": "screenshot", "origin": None, "url": None, "destination": None,
        "name": "capture", "node": {}, "form": {}, "context": {},
    }}
    data["recent_actions"] = [
        {"operation": "screenshot", "acknowledged": True, "page_changed": False}
        for _ in range(2)
    ]
    questions = (
        ChoiceQuestion("operation", NEXT_ACTION, (*questions[0].choices, "screenshot")),
        questions[1],
        ChoiceQuestion("target_screenshot", TARGET + " Operation: screenshot", ("capture",)),
    )
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        capture = body["questions"]["next_action"]["criteria"]["c4"]
        assert "capturing cannot reveal offscreen controls or move the page" in capture
        assert "previous capture was acknowledged" in capture
        alternatives = body["questions"]["next_interaction"]["criteria"]
        assert len(alternatives) == 4
        assert all("Save a still image" not in value for value in alternatives.values())
        assert "goal requires it" in body["questions"]["progress_mode"]["criteria"]["c0"]
        picks = {"next_action": "c4", "progress_mode": mode, "next_interaction": "c2"}
        answers = {
            name: {"type": "choice", "choice": picks[name], "confidence": 1.0,
                   "probabilities": {key: float(key == picks[name]) for key in row["criteria"]}}
            for name, row in body["questions"].items()
        }
        return httpx.Response(200, json={"model": "jev-1.14.0", "usage": {}, "answers": answers})

    provider = SystemOneProvider(endpoint="https://api.typesafe.ai/v1/systemone", model="jev-latest",
                                 transport=httpx.MockTransport(handler))
    try:
        result = await provider.decide(
            questions, json.dumps(data),
            target_heads={"fill": "target_fill", "screenshot": "target_screenshot"},
            budget=Budget(Limits()), privacy=PrivacyGate(PrivacyPolicy()),
        )
    finally:
        await provider.aclose()
    assert (result.operation, result.target) == expected
    assert len(seen) == 1


@pytest.mark.parametrize("passive", ["wait", "screenshot"])
@pytest.mark.parametrize("head", ["progress_mode", "next_interaction"])
@pytest.mark.parametrize("fault,code", [
    ("refusal", "refusal"),
    ("unknown_choice", "unknown_choice"),
    ("incomplete_distribution", "incomplete_distribution"),
    ("unnormalized", "probabilities"),
])
async def test_used_followup_refusal_or_malformed_answer_authorizes_no_action(passive, head, fault, code):
    data, questions = native()
    questions = with_wait(data, questions)
    if passive == "screenshot":
        row = data["candidates"].pop("wait")["short"]
        row["operation"] = "screenshot"
        row["name"] = "capture"
        data["candidates"]["screenshot"] = {"capture": row}
        questions = (
            ChoiceQuestion("operation", NEXT_ACTION, ("UNABLE", "fill", "DONE", "screenshot")),
            questions[1],
            ChoiceQuestion("target_screenshot", TARGET + " Operation: screenshot", ("capture",)),
        )
        for event in data["recent_actions"]:
            event["operation"] = "screenshot"
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        picks = {"next_action": "c4", "progress_mode": "c1", "next_interaction": "c2"}
        answers = {
            name: {"type": "choice", "choice": picks[name], "confidence": 1.0,
                   "probabilities": {key: float(key == picks[name]) for key in row["criteria"]}}
            for name, row in body["questions"].items()
        }
        if fault == "refusal":
            answers[head] = {"type": "refusal"}
        elif fault == "unknown_choice":
            answers[head]["choice"] = "unoffered"
        elif fault == "incomplete_distribution":
            answers[head]["probabilities"].pop("c0")
        else:
            answers[head]["probabilities"][picks[head]] = 0.5
        return httpx.Response(200, json={"model": "jev-1.14.0", "usage": {}, "answers": answers})

    provider = SystemOneProvider(endpoint="https://api.typesafe.ai/v1/systemone", model="jev-latest",
                                 transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError, match=code):
            await provider.decide(
                questions, json.dumps(data),
                target_heads={"fill": "target_fill", passive: "target_" + passive},
                budget=Budget(Limits()), privacy=PrivacyGate(PrivacyPolicy()),
            )
    finally:
        await provider.aclose()
    assert len(seen) == 1


def viewport_source(direction="down", *, other_direction=True):
    data, _ = native()
    data["candidates"] = {"scroll": {
        name: {"operation": "scroll", "origin": None, "url": None, "destination": None,
               "name": name, "node": {}, "form": {}, "context": {}}
        for name in ("up", "down")
    }}
    data["scroll"] = {"up": other_direction, "down": other_direction, direction: True}
    data["recent_actions"] = [{
        "operation": "scroll", "value": direction, "acknowledged": True,
        "page_changed": True, "url": data["url"],
    }]
    return data


@pytest.mark.parametrize("direction,adverb,side", [("up", "upward", "above"), ("down", "downward", "below")])
def test_empty_viewport_facts_are_symmetric_and_do_not_predict_goal_location(direction, adverb, side):
    from decision_browser.systemone_actions import viewport_facts

    data = viewport_source(direction)
    assert viewport_facts(data) == [
        "No current actionable page controls are observed.",
        f"The previous {adverb} scroll changed the visible page.",
        f"More document content remains {side} the viewport.",
    ]
    data["recent_actions"][-1]["url"] = "https://other.test/"
    assert viewport_facts(data) == []
    data["recent_actions"] = []
    assert viewport_facts(data) == []
    data["scroll"]["down" if direction == "up" else "up"] = False
    assert viewport_facts(data) == [
        "No current actionable page controls are observed.",
        f"More document content remains {side} the viewport.",
    ]


@pytest.mark.parametrize("mutation", ["control", "no_scroll", "unknown_scroll", "contradictory", "wrong_history"])
def test_empty_viewport_facts_skip_missing_or_contradictory_evidence(mutation):
    from decision_browser.systemone_actions import viewport_facts

    data = viewport_source()
    if mutation == "control":
        ordinary, _ = native()
        data["candidates"]["fill"] = ordinary["candidates"]["fill"]
    elif mutation == "no_scroll":
        del data["scroll"]
    elif mutation == "unknown_scroll":
        data["scroll"]["down"] = None
    elif mutation == "contradictory":
        data["scroll"] = {"up": False, "down": False}
    else:
        data["recent_actions"][-1]["acknowledged"] = False
    assert viewport_facts(data) == []


async def test_viewport_facts_do_not_remove_honest_stop_or_add_another_head():
    data = viewport_source()
    questions = (
        ChoiceQuestion("operation", NEXT_ACTION, ("UNABLE", "scroll", "DONE")),
        ChoiceQuestion("target_scroll", TARGET + " Operation: scroll", ("up", "down")),
    )

    def handler(request):
        body = json.loads(request.content)
        assert list(body["questions"]) == ["next_action"]
        state = json.loads(body["state"])
        assert len(state["observed_facts"]) == 3
        assert len(body["questions"]["next_action"]["criteria"]) == 4
        return httpx.Response(200, json=response(body, "c0"))

    provider = SystemOneProvider(endpoint="https://api.typesafe.ai/v1/systemone", model="jev-latest",
                                 transport=httpx.MockTransport(handler))
    try:
        decision = await provider.decide(
            questions, json.dumps(data), target_heads={"scroll": "target_scroll"},
            budget=Budget(Limits()), privacy=PrivacyGate(PrivacyPolicy()),
        )
    finally:
        await provider.aclose()
    assert (decision.operation, decision.target) == ("UNABLE", None)


async def test_marker_free_delay_with_visible_cancel_can_still_choose_wait():
    data, questions = native(1)
    row = next(iter(data["candidates"]["fill"].values()))
    row.update(operation="click", name="Cancel")
    row["node"].update(name="Cancel", role="button")
    row["node"]["state"].update(tag="button", type="button")
    data["candidates"] = {"click": {"cancel-control": row}}
    data["goal"] = "Wait for the requested content to arrive."
    data["scroll"] = {"up": False, "down": True}
    questions = (
        ChoiceQuestion("operation", NEXT_ACTION, ("UNABLE", "click", "DONE")),
        ChoiceQuestion("target_click", TARGET + " Operation: click", ("cancel-control",)),
    )
    questions = with_wait(data, questions)

    def handler(request):
        body = json.loads(request.content)
        assert "observed_facts" not in json.loads(body["state"])
        picks = {"next_action": "c3", "progress_mode": "c0", "next_interaction": "c0"}
        answers = {
            name: {"type": "choice", "choice": picks[name], "confidence": 1.0,
                   "probabilities": {key: float(key == picks[name]) for key in entry["criteria"]}}
            for name, entry in body["questions"].items()
        }
        return httpx.Response(200, json={"model": "jev-1.14.0", "usage": {}, "answers": answers})

    provider = SystemOneProvider(endpoint="https://api.typesafe.ai/v1/systemone", model="jev-latest",
                                 transport=httpx.MockTransport(handler))
    try:
        decision = await provider.decide(
            questions, json.dumps(data), target_heads={"click": "target_click", "wait": "target_wait"},
            budget=Budget(Limits()), privacy=PrivacyGate(PrivacyPolicy()),
        )
    finally:
        await provider.aclose()
    assert (decision.operation, decision.target) == ("wait", "short")
