"""Decision evidence is descriptive: no raw caller values or success overrides."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from test_runner_control_bindings import RecordingDOM, candidate, manager, observation

from decision_browser import runner
from decision_browser.contracts import Check
from decision_browser.decision_codec import Decision, compile_questions
from decision_browser.verification import CheckEvidence, FunctionalReport


class Capture:
    def __init__(self, *, terminal="DONE"):
        self.terminal = terminal

    async def decide(self, questions, input_text, **kwargs):
        compile_questions(questions, input_text)
        self.context = json.loads(input_text)
        self.questions = questions
        return Decision(self.terminal, None, 1.)


@pytest.mark.parametrize("current,values,status", [
    ("", {"Full name": "Ada Lovelace"}, "differs"),
    ("Ada Lovelace", {"Full name": "Ada Lovelace"}, "matches"),
    ("", {}, "unbound"),
    ("", {"unrelated": "unused-private-value"}, "unbound"),
    ("", {"Full name": ""}, "matches"),
])
async def test_fill_status_compares_current_value_without_changing_choices(current, values, status):
    host, run = manager(values)
    item = candidate("field", "fill", "Full name")
    item.semantic_by_operation["fill"]["node"]["value"] = current
    provider = Capture()
    run.provider, run.dom = provider, RecordingDOM()
    assert await host._choose(run, observation({"field": item})) is None
    assert provider.context["candidates"]["fill"]["field"]["fill_state"] == status
    assert "DONE" in provider.questions[0].choices
    assert next(q for q in provider.questions if q.name == "target_fill").choices == ("field",)
    assert "unused-private-value" not in json.dumps(provider.context)
    assert run.dom.prepared == run.dom.guarded == []


async def test_bound_value_is_not_serialized_before_the_field_is_chosen():
    private = "CALLER_VALUE_NOT_FOR_OTHER_MODEL_CHOICES"
    host, run = manager({"Full name": private})
    item = candidate("field", "fill", "Full name")
    item.semantic_by_operation["fill"]["node"]["value"] = ""
    provider = Capture()
    run.provider, run.dom = provider, RecordingDOM()
    await host._choose(run, observation({"field": item}))
    assert private not in json.dumps(provider.context)
    assert provider.context["candidates"]["fill"]["field"]["fill_state"] == "differs"


async def test_ambiguous_value_binding_remains_unbound_for_the_model():
    host, run = manager({"name": "private-name"})
    items = {str(i): candidate(str(i), "fill", name) for i, name in enumerate(("First name", "Last name"))}
    provider = Capture()
    run.provider, run.dom = provider, RecordingDOM()
    await host._choose(run, observation(items))
    assert [row["fill_state"] for row in provider.context["candidates"]["fill"].values()] == ["unbound", "unbound"]
    assert "private-name" not in json.dumps(provider.context)


@pytest.mark.parametrize("passed", [False, True])
async def test_completion_feedback_comes_from_independent_checks_without_assertion_data(monkeypatch, passed):
    host, run = manager()
    run.request.checks = [
        Check(kind="text", target="#private-assertion-target", expected="private expected text"),
        Check(kind="route", expected="https://example.test/"),
    ]
    report = FunctionalReport(
        terminal="verified" if passed else "verification_failed",
        functional="verified" if passed else "failed",
        evidence=[
            CheckEvidence(kind="text", target="#private-assertion-target", passed=passed,
                          reason="ok" if passed else "mismatch"),
            CheckEvidence(kind="route", target="", passed=True, reason="ok"),
        ],
    )

    async def verify(*_args):
        return report

    monkeypatch.setattr(runner, "verify_functional", verify)
    run.driver = SimpleNamespace(page=object())
    assert await host._verify(run) is passed
    assert run.completion == {"total": 2, "pending": int(not passed)}
    if not passed:
        provider = Capture(terminal="UNABLE")
        run.provider, run.dom = provider, RecordingDOM()
        assert await host._choose(run, observation({})) is None
        assert provider.context["completion"] == {"total": 2, "pending": 1}
        raw = json.dumps(provider.context)
        assert "private expected text" not in raw and "#private-assertion-target" not in raw
        assert "DONE" not in provider.questions[0].choices
        assert "UNABLE" in provider.questions[0].choices
        assert run.stop_choice == "UNABLE"
        assert run.dom.prepared == run.dom.guarded == []


@pytest.mark.parametrize("route", ["openai", "systemone"])
async def test_pending_checks_offer_a_safe_stop_in_each_native_protocol(route):
    import httpx
    from test_provider import _wire_answers

    from decision_browser.provider import OpenAIProvider
    from decision_browser.systemone_provider import SystemOneProvider

    def handler(request):
        body = json.loads(request.content)
        if route == "openai":
            op = next(q for q in body["questions"] if q["name"] == "operation")
            choices = [entry["value"] for entry in op["choices"]]
            assert "UNABLE" in choices and "DONE" not in choices and "click" in choices
            return httpx.Response(200, content=_wire_answers(body, {"operation": "UNABLE"}))
        answers = {}
        for head, question in body["questions"].items():
            keys = question["criteria"]
            decoded = {k: json.loads(value) for k, value in keys.items()}
            values = {(value["choice"] if isinstance(value, dict) else value): k
                      for k, value in decoded.items()} if head == "operation" else {}
            if head == "operation":
                assert "UNABLE" in values and "DONE" not in values and "click" in values
            key = values["UNABLE"] if head == "operation" else next(iter(keys))
            answers[head] = {"type": "choice", "choice": key, "confidence": 1,
                             "probabilities": {k: float(k == key) for k in keys}}
        return httpx.Response(200, json={"model": "jev-1.13", "usage": {}, "answers": answers})

    transport = httpx.MockTransport(handler)
    provider = (OpenAIProvider("test-key", transport=transport) if route == "openai" else
                SystemOneProvider(endpoint="http://127.0.0.1:8000/v1/systemone", model="jev-1.13",
                                  transport=transport))
    host, run = manager()
    run.provider, run.dom = provider, RecordingDOM()
    run.completion = {"total": 1, "pending": 1}
    try:
        item = candidate("button", "click", "Continue")
        assert await host._choose(run, observation({"button": item})) is None
        assert run.dom.prepared == run.dom.guarded == []
        assert run.budget.usage().calls == 1
    finally:
        await provider.aclose()
