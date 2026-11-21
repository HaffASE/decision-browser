"""Offline transport contracts; never call paid services."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

BENCH = Path(__file__).resolve().parents[2] / "scripts" / "benchmarks"
_spec = importlib.util.spec_from_file_location("benchmark_transport_under_test", BENCH / "openai_transport.py")
assert _spec is not None and _spec.loader is not None
_transport = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_transport)
MODEL, TransportError, make_post_json = _transport.MODEL, _transport.TransportError, _transport.make_post_json

DECISION_SOURCE = "https://api.typesafe.ai/v1/systemone"
TEXT_SOURCE = "https://api.openai.com/v1/chat/completions"


def question(choices, instructions="Original instructions"):
    return {"type": "choice", "criteria": dict.fromkeys(choices, "description"), "instructions": instructions}


def answer(name, choices, selected=None):
    selected = selected or choices[0]
    return {
        "name": name, "type": "choice", "choice": selected, "confidence": 1.0,
        "probabilities": [{"value": choice, "probability": float(choice == selected)} for choice in choices],
    }


def body():
    return {
        "model": "jev-latest", "state": {"page": "Owned fixture"},
        "questions": {
            "operation": question(["CLICK", "DONE"], {"goal": "Original goal", "rules": ["Unchanged"]}),
            "click_target": question(["1", "2"]),
            "type_text_target": question(["3"]),
        },
    }


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False)


def completed(text='{"text":"orbital mechanics"}'):
    return {
        "status": "completed", "model": MODEL, "usage": {"input_tokens": 5},
        "output": [{"type": "reasoning"}, {"type": "message", "role": "assistant", "status": "completed",
                   "content": [{"type": "output_text", "text": text}]}],
    }


def text_body():
    return {
        "model": "other-provider-model", "max_tokens": 1024,
        "messages": [{"role": "system", "content": "Generate the requested field text from the user goal."},
                     {"role": "user", "content": '{"goal":"Search orbital mechanics"}'}],
    }


def test_multhead_request_preserves_instructions_state_and_all_choices():
    request = body()
    calls = []

    def handle(wire):
        calls.append(wire)
        assert str(wire.url) == "https://api.openai.com/v1/decisions"
        assert wire.headers["Authorization"] == "Bearer TEST_ONLY_KEY"
        payload = json.loads(wire.content)
        assert payload["model"] == MODEL
        assert json.loads(payload["input"]) == request["state"]
        assert [q["name"] for q in payload["questions"]] == ["operation", "click_target"]
        assert json.loads(payload["questions"][0]["instructions"]) == request["questions"]["operation"]["instructions"]
        for q in payload["questions"]:
            assert [c["value"] for c in q["choices"]] == list(request["questions"][q["name"]]["criteria"])
        return httpx.Response(200, json={"model": MODEL, "answers": [
            answer("operation", ["CLICK", "DONE"]), answer("click_target", ["1", "2"], "2")
        ], "usage": {"input_tokens": 7}})

    with client_for(handle) as client:
        result = make_post_json("TEST_ONLY_KEY", client)(DECISION_SOURCE, "ignored", request)
    assert len(calls) == 1
    assert result["answers"]["click_target"]["choice"] == "2"
    assert result["answers"]["type_text_target"] == {"choice": "3", "confidence": 1.0, "probabilities": {"3": 1.0}}
    assert result["usage"] == {"input_tokens": 7}


def test_all_singletons_are_resolved_without_http_or_extra_candidates():
    def forbidden(_request):
        raise AssertionError("Singleton heads need no model request")

    request = {"state": {}, "questions": {"operation": question(["DONE"])}}
    with client_for(forbidden) as client:
        result = make_post_json("TEST_ONLY_KEY", client)(DECISION_SOURCE, "ignored", request)
    assert result == {"model": MODEL, "answers": {
        "operation": {"choice": "DONE", "confidence": 1.0, "probabilities": {"DONE": 1.0}}
    }, "usage": {}}


@pytest.mark.parametrize("size", [2, 26, 255])
def test_full_choice_domains_are_retained(size):
    choices = [str(i) for i in range(size)]
    request = {"state": {}, "questions": {"operation": question(choices)}}

    def handle(wire):
        offered = json.loads(wire.content)["questions"][0]["choices"]
        assert [c["value"] for c in offered] == choices
        return httpx.Response(200, json={"model": MODEL, "answers": [answer("operation", choices)]})

    with client_for(handle) as client:
        result = make_post_json("TEST_ONLY_KEY", client)(DECISION_SOURCE, "ignored", request)
    assert set(result["answers"]["operation"]["probabilities"]) == set(choices)


def test_oversized_domain_is_rejected_before_http():
    def forbidden(_request):
        raise AssertionError("An oversized domain must not be truncated or sent")

    request = {"state": {}, "questions": {"operation": question([str(i) for i in range(256)])}}
    with client_for(forbidden) as client, pytest.raises(TransportError, match="invalid_choice_domain"):
        make_post_json("TEST_ONLY_KEY", client)(DECISION_SOURCE, "ignored", request)


@pytest.mark.parametrize("kind", ["refusal", "malformed", "missing"])
def test_unused_target_head_remains_invalid_for_native_selected_head_validation(kind):
    rows = [answer("operation", ["CLICK", "DONE"]), answer("click_target", ["1", "2"])]
    request = body()
    request["questions"]["type_text_target"] = question(["3", "4"])
    if kind != "missing":
        rows.append({"name": "type_text_target", "type": "refusal"} if kind == "refusal"
                    else {"name": "type_text_target", "type": "choice", "probabilities": "broken"})
    with client_for(lambda _: httpx.Response(200, json={"model": MODEL, "answers": rows})) as client:
        result = make_post_json("TEST_ONLY_KEY", client)(DECISION_SOURCE, "ignored", request)
    assert result["answers"]["click_target"]["choice"] == "1"
    if kind == "missing":
        assert "type_text_target" not in result["answers"]
    else:
        assert result["answers"]["type_text_target"] == {}


@pytest.mark.parametrize("kind", ["unknown", "duplicate", "missing_operation"])
def test_unknown_duplicate_or_missing_operation_fails_safely(kind):
    rows = [answer("operation", ["CLICK", "DONE"])]
    if kind == "unknown":
        rows.append(answer("unknown", ["1", "2"]))
    elif kind == "duplicate":
        rows *= 2
    else:
        rows = []
    with client_for(lambda _: httpx.Response(200, json={"model": MODEL, "answers": rows})) as client, \
            pytest.raises(TransportError):
        make_post_json("TEST_ONLY_KEY", client)(DECISION_SOURCE, "ignored", body())


def test_text_helper_messages_and_schema_are_translated_without_value_injection(monkeypatch):
    request = text_body()

    def handle(wire):
        assert str(wire.url) == "https://api.openai.com/v1/responses"
        payload = json.loads(wire.content)
        assert payload["model"] == MODEL and payload["max_output_tokens"] == 1024
        messages = [{"role": m["role"], "content": m["content"][0]["text"]} for m in payload["input"]]
        assert messages == request["messages"]
        assert payload["text"]["format"]["schema"]["properties"]["text"] == {"type": ["string", "null"]}
        return httpx.Response(200, json=completed())

    with client_for(handle) as client:
        result = make_post_json("TEST_ONLY_KEY", client)(TEXT_SOURCE, "ignored", request)
    assert json.loads(result["choices"][0]["message"]["content"]) == {"text": "orbital mechanics"}


@pytest.mark.parametrize("change,code", [
    ("incomplete", "text_incomplete"), ("refusal", "text_refusal"), ("missing_message", "text_malformed"),
    ("extra_message", "text_malformed"), ("bad_json", "text_schema"), ("extra_key", "text_schema"),
    ("duplicate_key", "text_schema"), ("wrong_type", "text_schema"), ("message_failed", "text_malformed"),
])
def test_text_helper_refusal_incomplete_and_malformed_outputs_are_safe(change, code):
    raw = completed()
    if change == "incomplete":
        raw["status"] = "incomplete"
    elif change == "refusal":
        raw["output"][1]["content"] = [{"type": "refusal", "refusal": "PRIVATE_CANARY"}]
    elif change == "missing_message":
        raw["output"] = []
    elif change == "extra_message":
        raw["output"].append(raw["output"][1])
    elif change == "message_failed":
        raw["output"][1]["status"] = "failed"
    else:
        raw["output"][1]["content"][0]["text"] = {
            "bad_json": "PRIVATE_CANARY", "extra_key": '{"text":"PRIVATE_CANARY","extra":1}',
            "duplicate_key": '{"text":"PRIVATE_CANARY","text":"other"}', "wrong_type": '{"text":42}',
        }[change]
    with client_for(lambda _: httpx.Response(200, json=raw)) as client, pytest.raises(TransportError) as caught:
        make_post_json("TEST_ONLY_KEY", client)(TEXT_SOURCE, "ignored", text_body())
    assert str(caught.value) == code and "PRIVATE_CANARY" not in str(caught.value)


def test_null_is_preserved_for_native_text_helper_validation():
    with client_for(lambda _: httpx.Response(200, json=completed('{"text":null}'))) as client:
        result = make_post_json("TEST_ONLY_KEY", client)(TEXT_SOURCE, "ignored", text_body())
    assert json.loads(result["choices"][0]["message"]["content"]) == {"text": None}


@pytest.mark.parametrize("status", [302, 401])
def test_http_errors_do_not_expose_response_or_follow_other_destinations(status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, text="PRIVATE_CANARY", headers={"Location": "https://elsewhere.test/"})

    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client, \
            pytest.raises(TransportError) as caught:
        make_post_json("TEST_ONLY_KEY", client)(DECISION_SOURCE, "ignored", body())
    assert str(caught.value) == f"http_{status}" and len(calls) == 1


def test_unknown_source_url_is_rejected_without_any_http():
    with client_for(lambda _: pytest.fail("unexpected network call")) as client, \
            pytest.raises(TransportError, match="unsupported_source_endpoint"):
        make_post_json("TEST_ONLY_KEY", client)("https://elsewhere.test/", "ignored", body())


@pytest.mark.parametrize("status", [429, 503, 529])
def test_transient_failure_is_one_physical_attempt_without_hidden_retry(status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, text="PRIVATE_CANARY")

    with client_for(handle) as client, pytest.raises(TransportError, match=f"http_{status}"):
        make_post_json("TEST_ONLY_KEY", client)(DECISION_SOURCE, "ignored", body())
    assert len(calls) == 1


def load_runner(name, monkeypatch):
    monkeypatch.syspath_prepend(str(BENCH))
    spec = importlib.util.spec_from_file_location("benchmark_" + name + "_under_test", BENCH / (name + ".py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def valid_request(tmp_path):
    executable = tmp_path / "chrome"
    executable.write_text("fixture")
    executable.chmod(0o700)
    return {"start_url": "http://127.0.0.1/fixture/", "goal": "Complete the owned fixture.",
            "source_root": str(tmp_path), "browser_executable": str(executable),
            "checks": [{"type": "value_equals", "target": "#field", "value": "exact"}]}


@pytest.mark.parametrize("change", ["unknown_check", "empty_target", "relative_source", "missing_browser",
                                     "conflicting_calls", "nan_timeout", "invalid_value", "nan_cost"])
def test_external_request_rejects_invalid_contract_before_launch(change, tmp_path, monkeypatch):
    module = load_runner("jev_runner", monkeypatch)
    request = valid_request(tmp_path)
    if change == "unknown_check":
        request["checks"][0]["type"] = "text_contains"
    elif change == "empty_target":
        request["checks"][0]["target"] = ""
    elif change == "relative_source":
        request["source_root"] = "relative"
    elif change == "missing_browser":
        del request["browser_executable"]
    elif change == "conflicting_calls":
        request.update(max_calls=3, max_provider_calls=4)
    elif change == "nan_timeout":
        request["timeout_s"] = float("nan")
    elif change == "invalid_value":
        request["values"] = {"Field": 42}
    else:
        request["max_cost_usd"] = float("nan")
    with pytest.raises((ValueError, TypeError)):
        module.checked_input(request)


@pytest.mark.parametrize("runner_name", ["jev_runner", "ultrafast_runner"])
def test_exact_field_value_and_text_checks_use_distinct_dom_properties(runner_name, monkeypatch):
    module = load_runner(runner_name, monkeypatch)
    expressions = []

    def evaluate(expression):
        expressions.append(expression)
        if expression == "location.href":
            return "http://127.0.0.1/done"
        if "innerWidth" in expression:
            return {"width": 1280, "height": 720}
        if "el.value" in expression:
            return "2026-09-20"
        if "el.innerText.trim()" in expression:
            return "Date confirmed"
        raise AssertionError(expression)

    session = SimpleNamespace(evaluate=evaluate, observe=lambda: {"url": "http://127.0.0.1/done"})
    agent = SimpleNamespace(browser=session, session=session)
    checks, url, viewport = module.verify(agent, [
        {"type": "value_equals", "target": "#departure", "value": "2026-09-20"},
        {"type": "text_equals", "target": "#status", "value": "Date confirmed"},
        {"type": "url_equals", "value": "http://127.0.0.1/done"},
    ])
    assert checks == [True, True, True] and url == "http://127.0.0.1/done"
    assert viewport == {"width": 1280, "height": 720}
    with pytest.raises(ValueError, match="unsupported_check"):
        module.verify(agent, [{"type": "text_contains", "value": "Date"}])
