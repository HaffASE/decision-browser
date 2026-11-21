"""System One wire contracts with controlled transports; no model credentials."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from test_provider_context import TOKEN, request_context

from decision_browser.action_context import NEXT_ACTION, TARGET
from decision_browser.budget import Budget, BudgetExceeded
from decision_browser.contracts import Limits, PrivacyPolicy
from decision_browser.decision_codec import ChoiceQuestion, Decision
from decision_browser.privacy import CaptureEvidence, PrivacyError, PrivacyGate
from decision_browser.provider import ProviderError
from decision_browser.provider_config import TextSettings
from decision_browser.systemone_provider import SystemOneProvider

ENDPOINT = "http://127.0.0.1:8000/v1/systemone"
QUESTIONS = (
    ChoiceQuestion("operation", "Choose operation", ("click", "DONE")),
    ChoiceQuestion("target_click", "Choose observed button", ("first", "last")),
)
HEADS = {"click": "target_click"}


def budget(**limits):
    return Budget(Limits(**limits))


def gate(secret=None):
    return PrivacyGate(PrivacyPolicy(blocked_literals=[secret] if secret else []))


def key_for(question, wanted):
    for key, description in question["criteria"].items():
        value = json.loads(description)
        members = value if isinstance(value, list) else [value]
        values = [member["choice"] if isinstance(member, dict) and "choice" in member else member for member in members]
        if any(type(member) is type(wanted) and member == wanted for member in values):
            return key
    raise AssertionError((question, wanted))


def reply(body, picks=None):
    picks = picks or {"operation": "click", "target_click": "last"}
    answers = {}
    for name, question in body["questions"].items():
        choice = key_for(question, picks[name])
        answers[name] = {
            "type": "choice", "choice": choice, "confidence": 0.9,
            "probabilities": {key: float(key == choice) for key in question["criteria"]},
        }
    return {"model": "jev-1.13.0", "usage": {}, "answers": answers,
            "id": "request-id", "provider": "TypeSafe"}


def provider(handler, **kwargs):
    return SystemOneProvider(endpoint=ENDPOINT, model="nimble-latest",
                             transport=httpx.MockTransport(handler), **kwargs)


async def choose(active, questions=QUESTIONS, *, heads=HEADS, input_text="observed page", **kwargs):
    return await active.decide(questions, input_text, target_heads=heads,
                               budget=kwargs.pop("budget", budget()),
                               privacy=kwargs.pop("privacy", gate()), **kwargs)


@pytest.mark.parametrize("endpoint,key", [
    ("https://api.typesafe.ai/v1/systemone", "typesafe-test"),
    ("https://openrouter.ai/api/alpha/decisions", "router-test"),
    (ENDPOINT, None),
])
async def test_native_schema_alias_model_and_exact_selected_endpoint(endpoint, key):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(request)
        assert set(body) == {"model", "state", "questions"}
        assert body["model"] == "chosen-model" and body["state"] == "observed page"
        assert all(question["type"] == "choice" and 2 <= len(question["criteria"]) <= 255
                   for question in body["questions"].values())
        return httpx.Response(200, json=reply(body))

    active = SystemOneProvider(endpoint=endpoint, model="chosen-model", api_key=key,
                               transport=httpx.MockTransport(handler))
    used = budget()
    try:
        assert await choose(active, budget=used) == Decision("click", "last", 0.9)
    finally:
        await active.aclose()
    assert len(seen) == used.usage().calls == 1
    assert str(seen[0].url) == endpoint
    assert seen[0].headers.get("authorization") == ("Bearer " + key if key else None)


async def test_context_is_compacted_once_and_target_alias_restored():
    questions, text = request_context()

    def handler(request):
        body = json.loads(request.content)
        assert TOKEN not in request.content.decode()
        state = json.loads(body["state"])
        assert state["context_format"] == "decision-browser-choice-table-v1"
        return httpx.Response(200, json=reply(body, {"operation": "fill", "target_fill": "1"}))

    active = provider(handler)
    try:
        decision = await choose(active, questions, heads={"fill": "target_fill"}, input_text=text)
        assert decision == Decision("fill", TOKEN, 0.9)
    finally:
        await active.aclose()


async def test_native_fill_criterion_names_the_observed_field_and_keeps_the_reject_choice():
    questions, text = request_context()
    operation, target = questions
    questions = (ChoiceQuestion(operation.name, NEXT_ACTION, operation.choices),
                 ChoiceQuestion(target.name, TARGET + " Operation: fill", target.choices))

    def handler(request):
        body = json.loads(request.content)
        criteria = body["questions"]["target_fill"]["criteria"]
        assert TOKEN not in request.content.decode()
        rejection = json.loads(criteria["reject"])
        assert rejection["reject"] is True and "authorize no action" in rejection["description"]
        real = json.loads(criteria["c0"])
        assert real["choice"] == "1"
        assert "Search query" in real["description"] and "textbox" in real["description"]
        assert "assume its operation has already been chosen" in body["questions"]["target_fill"]["instructions"]
        assert "not whether the operation alone finishes the goal" in body["questions"]["target_fill"]["instructions"]
        operation_question = body["questions"]["operation"]
        operation = json.loads(operation_question["criteria"][key_for(operation_question, "fill")])
        assert operation["choice"] == "fill" and "Search query" in operation["description"]
        return httpx.Response(200, json=reply(body, {"operation": "fill", "target_fill": real["choice"]}))

    active = provider(handler)
    try:
        assert await choose(active, questions, heads={"fill": "target_fill"}, input_text=text) == Decision("fill", TOKEN, 0.9)
    finally:
        await active.aclose()


@pytest.mark.parametrize("reject", [False, True])
async def test_native_intermediate_singleton_remains_explicit_and_rejection_stops_without_an_action(reject):
    from test_compact_context_v2 import standard_questions
    from test_lean_context import native
    from test_model_context import context

    row = native("Choose departure date", operation="click")
    row["node"]["state"]["readonly"] = True
    source = context({"click": {TOKEN: row}}, goal="Choose a departure date and confirm it",
                     completion={"total": 2, "pending": 2})
    questions, heads = standard_questions(source["candidates"])
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        assert set(body) == {"model", "state", "questions"}
        operation = json.loads(body["questions"]["operation"]["criteria"]["c1"])
        assert "Choose departure date" in operation["description"]
        target = body["questions"]["target_click"]
        assert "A useful intermediate step does not require rejection" in target["instructions"]
        result = reply(body, {"operation": "click", "target_click": "1"})
        if reject:
            answer = result["answers"]["target_click"]
            answer["choice"] = "reject"
            answer["probabilities"] = {key: float(key == "reject") for key in target["criteria"]}
        return httpx.Response(200, json=result)

    active = provider(handler)
    used = budget(max_provider_calls=1)
    try:
        if reject:
            with pytest.raises(ProviderError, match="rejected_sentinel"):
                await choose(active, questions, heads=heads, input_text=json.dumps(source), budget=used)
        else:
            assert await choose(active, questions, heads=heads, input_text=json.dumps(source), budget=used) == Decision(
                "click", TOKEN, 0.9,
            )
    finally:
        await active.aclose()
    assert len(seen) == used.usage().calls == 1


@pytest.mark.parametrize("unchanged_waits", [False, True])
async def test_wait_criterion_distinguishes_pending_loading_from_ready_and_offscreen_actions(unchanged_waits):
    from test_compact_context_v2 import standard_questions
    from test_lean_context import native
    from test_model_context import context

    source = context({"fill": {TOKEN: native("Search query")},
                      "wait": {"short": native("Wait briefly", operation="wait")}},
                     completion={"total": 2, "pending": 1}, scroll={"up": False, "down": True},
                     previous_pages=["Earlier observed subject"])
    if unchanged_waits:
        source["recent_actions"] = [{"operation": "wait", "acknowledged": True, "page_changed": False}] * 2
    questions, heads = standard_questions(source["candidates"])

    def handler(request):
        body = json.loads(request.content)
        state = json.loads(body["state"])
        for key in ("completion", "scroll", "recent_actions", "previous_pages"):
            assert state[key] == source[key]
        operations = {row["choice"]: row["description"] for row in (
            json.loads(value) for value in body["questions"]["operation"]["criteria"].values()
        )}
        assert "specific observed pending asynchronous change" in operations["wait"]
        assert "Use fill for an editable field" in operations["wait"]
        assert "scroll for off-screen content" in operations["wait"]
        assert ("last two waits" in operations["wait"]) is unchanged_waits
        return httpx.Response(200, json=reply(body, {"operation": "wait", "target_fill": "1", "target_wait": "short"}))

    active = provider(handler)
    try:
        # Guidance never removes the offered wait or manufactures progress.
        assert await choose(active, questions, heads=heads, input_text=json.dumps(source)) == Decision("wait", "short", 0.9)
    finally:
        await active.aclose()


@pytest.mark.parametrize("custom", [False, True])
def test_operation_evidence_does_not_promote_a_preview_or_replace_caller_descriptions(custom):
    from copy import deepcopy

    from test_compact_context_v2 import standard_questions
    from test_lean_context import native
    from test_model_context import context

    from decision_browser.model_context import prepare_context

    source = context({"fill": {TOKEN: native("Long field " * 40)}})
    before = deepcopy(source)
    questions, heads = standard_questions(source["candidates"])
    if custom:
        operation = questions[0]
        questions = (ChoiceQuestion(operation.name, operation.instructions, operation.choices,
                                    ("Caller DONE description", "Caller fill description")), *questions[1:])
    prepared, state, aliases = prepare_context(questions, json.dumps(source), target_heads=heads,
                                               compact=True, lean=True, provider="systemone")
    assert source == before and aliases["fill"]["1"] == TOKEN
    assert "[preview]" in prepared[1].descriptions[0]
    if custom:
        assert prepared[0].descriptions == questions[0].descriptions
    else:
        assert "Offered target" not in prepared[0].descriptions[1]
    # The full observed name remains available to the model even when its criterion is a preview.
    assert source["candidates"]["fill"][TOKEN]["name"] in state


async def test_narrowed_targets_keep_the_description_of_each_original_choice():
    targets = ChoiceQuestion("target_click", "Choose the field guide", ("1", "2", "3"),
                             ("First atlas", "Field guide", "Third atlas"))
    seen = []

    def handler(request):
        body = json.loads(request.content)
        question = body["questions"]["target_click"]
        seen.append(question)
        matching = next(json.loads(value) for value in question["criteria"].values() if "Field guide" in value)
        members = matching if isinstance(matching, list) else [matching]
        pick = next(member["choice"] for member in members if member["description"] == "Field guide")
        return httpx.Response(200, json=reply(body, {"operation": "click", "target_click": pick}))

    active = provider(handler, max_choices=2)
    try:
        assert await choose(active, (QUESTIONS[0], targets)) == Decision("click", "2", 0.9)
    finally:
        await active.aclose()
    assert len(seen) == 2
    assert json.loads(seen[0]["criteria"]["g0"])[1] == json.loads(seen[1]["criteria"]["c1"]) == {
        "choice": "2", "description": "Field guide",
    }


@pytest.mark.parametrize("wanted", [True, "true", False, "false"])
async def test_typed_choices_remain_distinct(wanted):
    questions = (QUESTIONS[0], ChoiceQuestion("target_click", "choose", (True, "true", False, "false")))

    def handler(request):
        body = json.loads(request.content)
        assert len(body["questions"]["target_click"]["criteria"]) == 4
        return httpx.Response(200, json=reply(body, {"operation": "click", "target_click": wanted}))

    active = provider(handler)
    try:
        decision = await choose(active, questions)
        assert type(decision.target) is type(wanted) and decision.target == wanted
    finally:
        await active.aclose()


@pytest.mark.parametrize("reject", [False, True])
async def test_singleton_is_padded_but_padding_never_authorizes(reject):
    questions = (ChoiceQuestion("operation", "choose", ("DONE",)),)

    def handler(request):
        body = json.loads(request.content)
        result = reply(body, {"operation": "DONE"})
        criteria = body["questions"]["operation"]["criteria"]
        assert len(criteria) == 2
        if reject:
            choice = "reject"
            assert json.loads(criteria[choice])["reject"] is True
            answer = result["answers"]["operation"]
            answer["choice"] = choice
            answer["probabilities"] = {key: float(key == choice) for key in criteria}
        return httpx.Response(200, json=result)

    active = provider(handler)
    try:
        if reject:
            with pytest.raises(ProviderError, match="rejected_sentinel"):
                await choose(active, questions, heads=None)
        else:
            assert await choose(active, questions, heads=None) == Decision("DONE", None, 0.9)
    finally:
        await active.aclose()


@pytest.mark.parametrize("cap,count", [(26, 255), (2, 65)])
async def test_large_target_is_narrowed_without_losing_last_candidate(cap, count):
    target = "candidate-" + str(count - 1)
    questions = (QUESTIONS[0], ChoiceQuestion("target_click", "choose", tuple(f"candidate-{i}" for i in range(count))))
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        assert all(2 <= len(q["criteria"]) <= cap for q in body["questions"].values())
        return httpx.Response(200, json=reply(body, {"operation": "click", "target_click": target}))

    active = provider(handler, max_choices=cap)
    used = budget(max_provider_calls=12)
    try:
        assert (await choose(active, questions, budget=used)).target == target
    finally:
        await active.aclose()
    assert used.usage().calls == len(seen) > 1
    all_initial = [value for desc in seen[0]["questions"]["target_click"]["criteria"].values()
                   for value in json.loads(desc)]
    assert all_initial == list(questions[1].choices)
    assert all(set(body["questions"]) == {"target_click"} for body in seen[1:])


@pytest.mark.parametrize("count", [30, 70])
async def test_large_operation_domain_and_more_than_64_heads_are_not_truncated(count):
    operations = tuple(f"click_group{i}" for i in range(count))
    questions = (ChoiceQuestion("operation", "choose group", (*operations, "DONE")),
                 *(ChoiceQuestion("target_" + op, "choose target", ("target-" + op,)) for op in operations))
    wanted = operations[-1]
    heads = {op: "target_" + op for op in operations}
    picks = {"operation": wanted, **{heads[op]: "target-" + op for op in operations}}
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        assert len(body["questions"]) <= 64
        assert all(len(q["criteria"]) <= 26 for q in body["questions"].values())
        return httpx.Response(200, json=reply(body, picks))

    active = provider(handler, max_choices=26)
    try:
        assert await choose(active, questions, heads=heads) == Decision(wanted, "target-" + wanted, 0.9)
    finally:
        await active.aclose()
    if count > 64:
        assert [set(body["questions"]) for body in seen] == [{"operation"}, {"operation"}, {heads[wanted]}]
    else:
        assert [set(body["questions"]) for body in seen] == [
            {question.name for question in questions}, {"operation"},
        ]


@pytest.mark.parametrize("unused", [None, [], {"type": "refusal"}, {"type": "choice", "choice": "bogus"}])
async def test_unused_target_semantics_do_not_override_selected_head(unused):
    questions = (*QUESTIONS, ChoiceQuestion("unused", "not selected", ("x", "y")))

    def handler(request):
        body = json.loads(request.content)
        result = reply(body, {"operation": "click", "target_click": "last", "unused": "x"})
        result["answers"]["unused"] = unused
        return httpx.Response(200, json=result)

    active = provider(handler)
    try:
        assert await choose(active, questions) == Decision("click", "last", 0.9)
    finally:
        await active.aclose()


@pytest.mark.parametrize("mutation,code", [
    ("missing", "missing_answer"), ("extra", "extra_answer"),
    ("refusal", "refusal"), ("unknown", "unknown_choice"),
    ("incomplete", "incomplete_distribution"), ("bool", "probability_range"),
    ("sum", "probabilities"), ("argmax", "probabilities"),
    ("confidence", "confidence"), ("huge", "confidence"),
    ("model", "invalid_model"), ("error", "invalid_response"),
])
async def test_selected_native_response_contract_fails_closed(mutation, code):
    def handler(request):
        body = json.loads(request.content)
        result = reply(body)
        answer = result["answers"]["target_click"]
        if mutation == "missing":
            result["answers"].pop("target_click")
        elif mutation == "extra":
            result["answers"]["unexpected"] = {}
        elif mutation == "refusal":
            result["answers"]["target_click"] = {"type": "refusal"}
        elif mutation == "unknown":
            answer["choice"] = "foreign"
        elif mutation == "incomplete":
            answer["probabilities"].pop(next(iter(answer["probabilities"])))
        elif mutation == "bool":
            answer["probabilities"][answer["choice"]] = True
        elif mutation == "sum":
            answer["probabilities"] = dict.fromkeys(answer["probabilities"], 0.1)
        elif mutation == "argmax":
            answer["probabilities"] = {key: float(key != answer["choice"]) for key in answer["probabilities"]}
        elif mutation == "confidence":
            answer["confidence"] = False
        elif mutation == "huge":
            answer["confidence"] = 10 ** 1000
        elif mutation == "error":
            result["error"] = {"message": "not a successful answer"}
        else:
            result["model"] = ""
        return httpx.Response(200, json=result)

    active = provider(handler)
    try:
        with pytest.raises(ProviderError) as caught:
            await choose(active)
        assert caught.value.code == code
    finally:
        await active.aclose()


@pytest.mark.parametrize("heads", [{"click": "missing"}, {}, {"foreign": "target_click"}])
async def test_bad_mapping_fails_before_network_or_budget(heads):
    seen = []
    active = provider(lambda request: seen.append(request))
    used = budget()
    try:
        with pytest.raises(ProviderError):
            await choose(active, heads=heads, budget=used)
    finally:
        await active.aclose()
    assert not seen and used.usage().calls == 0


@pytest.mark.parametrize("overrides", [
    {"endpoint": "http://remote.test/v1/systemone"},
    {"endpoint": "https://user:secret@remote.test/v1/systemone"},
    {"endpoint": "https://remote.test/v1/systemone?token=secret"},
    {"model": "unsafe\nmodel"}, {"api_key": "unsafe\nkey"},
    {"max_choices": True}, {"max_choices": 1}, {"max_choices": 256},
])
def test_direct_constructor_validates_operator_values_without_http(overrides):
    values = {"endpoint": ENDPOINT, "model": "nimble-latest", **overrides}
    with pytest.raises(ProviderError, match="invalid_provider_config"):
        SystemOneProvider(**values)


@pytest.mark.parametrize("raw", [b'{"model":"x","model":"y"}', b'{"a":NaN}', b'{"a":1e999}', b'[]'])
async def test_native_json_parser_rejects_ambiguous_or_nonfinite_response(raw):
    active = provider(lambda request: httpx.Response(200, content=raw))
    try:
        with pytest.raises(ProviderError, match="malformed_response"):
            await choose(active)
    finally:
        await active.aclose()


async def test_privacy_and_call_quota_apply_to_every_refinement():
    questions = (QUESTIONS[0], ChoiceQuestion("target_click", "choose", tuple(str(i) for i in range(30))))
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json=reply(body, {"operation": "click", "target_click": "29"}))

    active = provider(handler, max_choices=26)
    try:
        with pytest.raises(PrivacyError, match="blocked_literal"):
            await choose(active, questions, input_text="PRIVATE", privacy=gate("PRIVATE"))
        assert not seen
        used = budget(max_provider_calls=1)
        with pytest.raises(BudgetExceeded, match="provider_calls"):
            await choose(active, questions, budget=used)
        assert len(seen) == used.usage().calls == 1
    finally:
        await active.aclose()


@pytest.mark.parametrize("secret", ['quote"secret', "slash\\secret", "line\nsecret", "tab\tsecret"])
async def test_encoded_choice_descriptions_cannot_hide_blocked_source_literals(secret):
    questions = (QUESTIONS[0], ChoiceQuestion("target_click", "choose", ("first", secret)))
    seen = []
    active = provider(lambda request: seen.append(request))
    used = budget()
    try:
        with pytest.raises(PrivacyError, match="blocked_literal"):
            await choose(active, questions, budget=used, privacy=gate(secret))
    finally:
        await active.aclose()

    target = ChoiceQuestion("target_click", "choose", ("first", "last"), (secret, None))
    active = provider(lambda request: seen.append(request))
    try:
        with pytest.raises(PrivacyError, match="blocked_literal"):
            await choose(active, (QUESTIONS[0], target), budget=used, privacy=gate(secret))
        assert not seen and used.usage().calls == 0
    finally:
        await active.aclose()
    assert not seen and used.usage().calls == 0


async def test_cancellation_during_refinement_preserves_original_deadline():
    started = asyncio.Event()
    calls = 0
    questions = (QUESTIONS[0], ChoiceQuestion("target_click", "choose", tuple(str(i) for i in range(30))))

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 2:
            started.set()
            await asyncio.Event().wait()
        return httpx.Response(200, json=reply(json.loads(request.content), {"operation": "click", "target_click": "29"}))

    active = provider(handler, max_choices=26)
    used = budget()
    task = asyncio.create_task(choose(active, questions, budget=used))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await active.aclose()
    assert calls == used.usage().calls == 2
    assert used.original_deadline == used.deadline


async def test_privacy_is_checked_again_before_refinement_socket():
    class RefinementGate(PrivacyGate):
        checks = 0

        def check_payload(self, body, capture=None, contains_image=False):
            super().check_payload(body, capture, contains_image)
            if "questions" in json.loads(body):
                self.checks += 1
                if self.checks == 2:
                    raise PrivacyError("blocked_literal")

    questions = (QUESTIONS[0], ChoiceQuestion("target_click", "choose", tuple(str(i) for i in range(30))))
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=reply(json.loads(request.content), {"operation": "click", "target_click": "29"}))

    active = provider(handler, max_choices=26)
    used = budget()
    privacy = RefinementGate(PrivacyPolicy())
    try:
        with pytest.raises(PrivacyError, match="blocked_literal"):
            await choose(active, questions, budget=used, privacy=privacy)
    finally:
        await active.aclose()
    assert privacy.checks == 2 and len(seen) == used.usage().calls == 1


async def test_timeout_during_refinement_spends_original_budget():
    calls = 0
    questions = (QUESTIONS[0], ChoiceQuestion("target_click", "choose", tuple(str(i) for i in range(30))))

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 2:
            await asyncio.Event().wait()
        return httpx.Response(200, json=reply(json.loads(request.content), {"operation": "click", "target_click": "29"}))

    active = provider(handler, max_choices=26)
    used = budget(max_seconds=0.05)
    try:
        with pytest.raises(BudgetExceeded, match="deadline_expired"):
            await choose(active, questions, budget=used)
    finally:
        await active.aclose()
    assert calls == used.usage().calls == 2
    assert used.deadline == used.original_deadline


async def test_no_helper_and_unsupported_visual_make_no_request():
    seen = []
    active = provider(lambda request: seen.append(request))
    used = budget()
    try:
        with pytest.raises(ProviderError, match="text_provider_not_configured"):
            await active.generate_text("write", budget=used, privacy=gate())
        report = await active.inspect_visual("look", b"unsafe", CaptureEvidence("wrong", False, True),
                                             budget=used, privacy=gate())
        assert (report.diagnostic, report.reason, report.findings) == (
            "incomplete", "visual_provider_not_supported", [])
    finally:
        await active.aclose()
    assert not seen and used.usage().calls == 0


async def test_optional_helper_uses_independent_endpoint_and_key_under_same_budget():
    seen = []

    def handler(request):
        seen.append(request)
        body = json.loads(request.content)
        if request.url.path == "/v1/systemone":
            assert request.headers["Authorization"] == "Bearer decision-only-key"
            return httpx.Response(200, json=reply(body))
        assert str(request.url) == "http://localhost:9001/v1/chat/completions"
        assert "Authorization" not in request.headers
        assert body["model"] == "local-helper" and body["messages"] == [{"role": "user", "content": "write value"}]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"role": "assistant", "content": "bound text"}}]})

    active = provider(handler, api_key="decision-only-key",
                      text=TextSettings("http://localhost:9001/v1/chat/completions", "local-helper", None))
    used = budget()
    try:
        await choose(active, budget=used)
        assert await active.generate_text("write value", budget=used, privacy=gate()) == "bound text"
    finally:
        await active.aclose()
    assert len(seen) == used.usage().calls == 2


async def test_redirect_not_followed_and_external_client_not_closed():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(307, headers={"Location": "https://foreign.test/"}, content=b"redirect")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    active = SystemOneProvider(endpoint=ENDPOINT, model="nimble-latest", client=client)
    try:
        with pytest.raises(ProviderError, match="http_error"):
            await choose(active)
        await active.aclose()
        await active.aclose()
        assert not client.is_closed
        assert len(seen) == 1
    finally:
        await client.aclose()


async def test_explicit_unable_target_is_preserved_for_custom_operation():
    questions = (ChoiceQuestion("operation", "choose", ("UNABLE", "DONE")),
                 ChoiceQuestion("reason", "choose reason", ("captcha", "login")))
    active = provider(lambda request: httpx.Response(200, json=reply(
        json.loads(request.content), {"operation": "UNABLE", "reason": "captcha"},
    )))
    try:
        result = await choose(active, questions, heads={"UNABLE": "reason"})
        assert result == Decision("UNABLE", "captcha", .9)
    finally:
        await active.aclose()
