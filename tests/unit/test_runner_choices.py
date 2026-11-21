"""Provider-facing choices preserve destination and the complete target domain."""
from __future__ import annotations

import json

import pytest

from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.decision_codec import Decision, compile_questions
from decision_browser.dom import Candidate, Observation
from decision_browser.runner import RunManager


def setup(candidates, goal="Review"):
    host = RunManager(lambda: None)
    run = host._open(TaskRequest(
        goal=goal, start_url="https://example.test/", values={}, checks=[],
        privacy=PrivacyPolicy(), limits=Limits(max_seconds=10),
    ))
    obs = Observation("obs", "https://example.test", "doc", "", candidates, ())
    return host, run, obs


def test_navigation_choices_keep_distinct_source_bindings_for_the_same_url():
    candidates = {name: Candidate(name, ("click",), {"click": {
        "operation": "click", "node": {"name": name, "href": "https://example.test/finish"},
    }}, None) for name in ("Details", "Purchase", "Delete account")}
    host, run, obs = setup(candidates)
    _questions, _heads, bindings = host._questions(run, obs)
    routes = list(bindings["navigate"].values())
    assert routes[0]["token"] is None
    derived = routes[1:]
    assert len(derived) == 3
    assert {item["token"] for item in derived} == set(candidates)
    assert {item["semantic"]["node"]["name"] for item in derived} == set(candidates)


async def test_navigation_payload_preserves_goal_and_describes_each_destination():
    candidate = Candidate("link", ("click",), {"click": {
        "node": {"name": "Details", "href": "https://example.test/details"},
        "routing": {"element": {"formaction": "fingerprint-only-raw-route"}},
    }}, None)
    goal = "Найти рейсы. Не переходить к покупке или бронированию."
    host, run, obs = setup({"link": candidate}, goal)

    class CapturingProvider:
        async def decide(self, questions, input_text, **kwargs):
            self.payload = json.loads(input_text)
            return Decision("DONE", None, 1.0)

    provider = CapturingProvider()
    run.provider = provider
    run.dom = object()
    await host._choose(run, obs)
    assert provider.payload["goal"] == goal
    assert "fingerprint-only-raw-route" not in json.dumps(provider.payload)
    routes = provider.payload["candidates"]["navigate"]
    assert routes["route0"]["destination"] == "https://example.test/"
    assert routes["route1"]["destination"] == "https://example.test/details"
    assert routes["route0"]["name"] != routes["route1"]["name"]


@pytest.mark.parametrize("count", [255, 256, 320])
def test_select_domain_compiles_without_discarding_options(count):
    candidates = {}
    for start in range(0, count, 80):
        token = f"select{start}"
        candidates[token] = Candidate(token, ("select",), {"select": {
            "operation": "select", "node": {"name": token, "options": [
                [str(index), f"Option {index}", False]
                for index in range(start, min(start + 80, count))
            ]}
        }}, None)
    host, run, obs = setup(candidates)
    questions, _heads, bindings = host._questions(run, obs)
    compile_questions(questions, "Review")
    selected = [item for bag in bindings.values() for item in bag.values()
                if item["token"] in candidates]
    assert len(selected) == count
    assert {item["value"] for item in selected} == {str(i) for i in range(count)}
    assert all(len(question.choices) <= 255 for question in questions)


async def test_last_select_group_maps_to_guarded_select_action():
    from decision_browser.dom import Action, GuardResult

    candidates = {}
    for index in range(4):
        token = f"s{index}"
        candidates[token] = Candidate(token, ("select",), {"select": {
            "operation": "select", "node": {"name": token, "options": [
                [str(i), str(i), False] for i in range(80)
            ]}
        }}, None)
    host, run, obs = setup(candidates)

    class Provider:
        async def decide(self, questions, input_text, **kwargs):
            compile_questions(questions, input_text)
            group = "select_group1"
            head = kwargs["target_heads"][group]
            target = next(q for q in questions if q.name == head).choices[-1]
            return Decision(group, target, 1.0)

    class DOM:
        async def prepare(self, operation, token, *, value, destination):
            assert (operation, token, value) == ("select", "s3", "79")
            return Action(operation, token, value, destination, "obs", "{}")

        async def guard(self, action):
            return GuardResult("ready", "ready")

    run.provider, run.dom = Provider(), DOM()
    action = await host._choose(run, obs)
    assert action.operation == "select"
    assert action.token == "s3"
    assert action.value == "79"


@pytest.mark.parametrize("rounded_nonmax", [False, True])
async def test_four_selects_fit_real_provider_default_budget(rounded_nonmax):
    import httpx
    from test_provider import _decision_body, _wire_answers

    from decision_browser.dom import Action, GuardResult
    from decision_browser.provider import OpenAIProvider

    candidates = {f"s{i}": Candidate(f"s{i}", ("select",), {"select": {
        "operation": "select", "node": {"name": f"s{i}", "options": [
            [str(j), f"Option {j}", False] for j in range(80)
        ]}
    }}, None) for i in range(4)}
    host, run, obs = setup(candidates)
    seen = []

    def handler(request):
        seen.append(len(request.content))
        assert len(request.content) <= Limits().max_input_bytes
        body = _decision_body(request)
        response = json.loads(_wire_answers(body, {
            "operation": "select_group1", "target_select_group1": "s3:option:79"
        }))
        if rounded_nonmax:
            for answer in response["answers"]:
                rows = answer["probabilities"]
                for row in rows:
                    row["probability"] = (
                        0.001 if row["value"] == answer["choice"]
                        else round(0.999 / (len(rows) - 1), 3)
                    )
        return httpx.Response(200, json=response)

    class DOM:
        async def prepare(self, operation, token, *, value, destination):
            return Action(operation, token, value, destination, "obs", "{}")

        async def guard(self, action):
            return GuardResult("pass", "")

    provider = OpenAIProvider("test-key", transport=httpx.MockTransport(handler))
    run.provider, run.dom = provider, DOM()
    try:
        action = await host._choose(run, obs)
        assert (action.operation, action.token, action.value) == ("select", "s3", "79")
        assert len(seen) == 1
        assert run.budget.usage().calls == 1
    finally:
        await provider.aclose()


@pytest.mark.parametrize("operation,pending,accepted", [
    ("DONE", False, True), ("UNABLE", False, True),
    ("UNABLE", True, True), ("DONE", True, False),
])
async def test_custom_provider_terminal_must_be_offered(operation, pending, accepted):
    from decision_browser.runner import RunnerError

    host, run, obs = setup({})
    if pending:
        run.completion = {"pending": True}
    questions, _heads, _bindings = host._questions(run, obs)
    assert (operation in questions[0].choices) is accepted

    class CustomProvider:
        async def decide(self, questions, input_text, **kwargs):
            return Decision(operation, None, 1.0)

    run.provider, run.dom = CustomProvider(), object()
    if accepted:
        assert await host._choose(run, obs) is None
        assert run.stop_choice == operation
    else:
        with pytest.raises(RunnerError, match="helper_schema"):
            await host._choose(run, obs)
        assert run.stop_choice is None
