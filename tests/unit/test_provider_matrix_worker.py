"""Offline accounting contracts for the real-provider browser matrix worker."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from decision_browser.budget import Budget, BudgetExceeded
from decision_browser.contracts import Limits, PrivacyPolicy
from decision_browser.privacy import PrivacyGate
from decision_browser.provider_http import ProviderError


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/benchmarks"))
    import provider_matrix_worker
    return provider_matrix_worker


def limits(**updates):
    return {"max_calls": 2, "max_input_bytes": 4096, **updates}


def test_escaping_worker_failure_reports_unknown_usage(worker, monkeypatch, capsys):
    def fail():
        raise RuntimeError("An exception message must stay out of the record")

    monkeypatch.setattr(worker, "main", fail)
    worker.entry()
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "status": "runner_error",
        "reason": "RuntimeError",
        "checks": [],
        "usage_incomplete": True,
        "usage": {"calls": [], "attempts": None, "usage_complete": False},
    }


def test_missing_usage_is_unknown_and_zero_stays_zero(worker):
    row = {"endpoint": "systemone", "state": "parsed", "status": 200}
    worker.read_usage(row, {"input_tokens": 0, "output_tokens": 0})
    result = worker.summarize([row])
    assert result["usage_complete"] is True
    assert result["input_tokens"] == result["output_tokens"] == 0
    assert result["cached_tokens"] is None
    assert result["token_fields_missing"]["cached_tokens"] == 1
    assert result["calls"][0]["reasoning_tokens"] is None


def test_partial_totals_are_not_published_as_complete(worker):
    rows = []
    for usage in ({"input_tokens": 11, "output_tokens": 2}, {"input_tokens": 5}):
        row = {"endpoint": "decisions", "status": 200, "state": "parsed"}
        worker.read_usage(row, usage)
        rows.append(row)
    result = worker.summarize(rows)
    assert result["usage_complete"] is False
    assert result["input_tokens"] == 16 and result["output_tokens"] is None
    assert result["reported_tokens"]["output_tokens"] == 2


def test_nested_details_and_invalid_numbers(worker):
    row = {}
    worker.read_usage(row, {"input_tokens": True, "output_tokens": -1,
                            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 3},
                            "output_tokens_details": {"reasoning_tokens": 4}})
    assert row["input_tokens"] is None and row["output_tokens"] is None
    assert row["cached_tokens"] == 0 and row["cache_write_tokens"] == 3
    assert row["reasoning_tokens"] == 4


@pytest.mark.asyncio
async def test_route_guard_rejects_wrong_origin_auth_model_without_attempt(worker):
    meter = worker.Meter("jev", "TEST_KEY", limits())
    for endpoint, key, model in [
        ("https://evil.example/v1/systemone", "TEST_KEY", "jev-latest"),
        ("https://api.typesafe.ai/v1/systemone", "OTHER", "jev-latest"),
        ("https://api.typesafe.ai/v1/systemone", "TEST_KEY", "other"),
    ]:
        request = httpx.Request("POST", endpoint, headers={"Authorization": "Bearer " + key},
                                json={"model": model})
        with pytest.raises(ProviderError, match="matrix_unexpected_route"):
            await meter.request(request)
    assert meter.rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("arm", "model"), [
    ("opencode_jev", "jev-1.13"),
    ("opencode_gemini", "gemini-decision-1"),
])
async def test_console_meter_records_the_selected_model_on_systemone(worker, arm, model):
    meter = worker.Meter(arm, "TEST_KEY", limits())
    endpoint = "https://opencode.ai/inference/systemone/v1/systemone"

    async def handle(request):
        assert str(request.url) == endpoint and json.loads(request.content)["model"] == model
        assert request.headers["Authorization"] == "Bearer TEST_KEY"
        return httpx.Response(200, json={"model": model, "usage": {"input_tokens": 7, "output_tokens": 0}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle),
                                event_hooks={"request": [meter.request], "response": [meter.response]}) as client:
        await meter.post(client, endpoint, "TEST_KEY", {"model": model},
                         budget=Budget(Limits()), privacy=PrivacyGate(PrivacyPolicy()))
    call = worker.summarize(meter.rows)["calls"][0]
    assert (call["request_model"], call["model"], call["input_tokens"]) == (model, model, 7)


@pytest.mark.asyncio
async def test_post_records_alias_after_native_bounded_read(worker):
    meter = worker.Meter("jev", "TEST_KEY", limits())
    seen = []

    async def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"model": "jev-1.13", "usage": {"input_tokens": 7, "output_tokens": 0},
                                         "answers": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle),
                                event_hooks={"request": [meter.request], "response": [meter.response]}) as client:
        reply = await meter.post(client, "https://api.typesafe.ai/v1/systemone", "TEST_KEY",
                                 {"model": "jev-latest"}, budget=Budget(Limits(max_provider_calls=2)),
                                 privacy=PrivacyGate(PrivacyPolicy()))
    assert reply["model"] == "jev-1.13" and len(seen) == 1
    row = worker.summarize(meter.rows)
    assert row["usage_complete"] is True
    assert row["calls"][0]["model"] == "jev-1.13"
    assert row["calls"][0]["request_model"] == "jev-latest"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["http", "transport", "malformed"])
async def test_failed_physical_attempts_remain_in_usage(worker, mode):
    meter = worker.Meter("decision_api", "TEST_KEY", limits())

    async def handle(request):
        if mode == "transport":
            raise httpx.ConnectError("secret must not escape", request=request)
        if mode == "http":
            return httpx.Response(429, text="secret must not escape")
        return httpx.Response(200, content=b"invalid JSON secret")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle),
                                event_hooks={"request": [meter.request], "response": [meter.response]}) as client:
        with pytest.raises(ProviderError):
            await meter.post(client, "https://api.openai.com/v1/decisions", "TEST_KEY",
                             {"model": "gpt-6-luna"}, budget=Budget(Limits()),
                             privacy=PrivacyGate(PrivacyPolicy()))
    result = worker.summarize(meter.rows)
    assert result["attempts"] == 1 and result["usage_complete"] is False
    assert result["input_tokens"] is None
    assert result["calls"][0]["state"] == {"http": "http_error", "transport": "transport_error",
                                          "malformed": "read_error"}[mode]
    assert "secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_meter_preserves_native_output_limit(worker):
    meter = worker.Meter("jev", "TEST_KEY", limits())

    async def handle(_request):
        return httpx.Response(200, content=b" " * 2000)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle),
                                event_hooks={"request": [meter.request], "response": [meter.response]}) as client:
        with pytest.raises(BudgetExceeded, match="output_bytes"):
            await meter.post(client, "https://api.typesafe.ai/v1/systemone", "TEST_KEY",
                             {"model": "jev-latest"}, budget=Budget(Limits(max_output_bytes=1024)),
                             privacy=PrivacyGate(PrivacyPolicy()))
    assert len(meter.rows) == 1 and worker.summarize(meter.rows)["usage_complete"] is False


def test_unsafe_model_and_arbitrary_fields_are_not_retained(worker):
    rows = worker.safe_calls([{"model": "sk-never-save", "request_model": "jev-latest",
                               "endpoint": "systemone", "headers": "SECRET", "provider_error": "SECRET"}])
    encoded = json.dumps(rows)
    assert "sk-never-save" not in encoded and "SECRET" not in encoded


@pytest.mark.asyncio
@pytest.mark.parametrize("exception,status,state", [
    (httpx.ReadTimeout("secret"), None, "transport_error"),
    (asyncio.CancelledError(), None, "cancelled"),
    (ValueError("secret"), 200, "read_error"),
    (RuntimeError("secret"), 429, "http_error"),
])
async def test_terra_failure_annotates_native_attempt_and_preserves_exception(worker, monkeypatch,
                                                                            exception, status, state):
    class Terra:
        def __init__(self):
            self.rows = []

        async def choose(self, _observation):
            row = {"endpoint": "responses", "state": "pending", "usage_reported": False}
            if status is not None:
                row["status"] = status
            self.rows.append(row)
            raise exception

    loaded = SimpleNamespace(Terra=Terra)

    async def run(_data, _key):
        native = loaded.Terra()
        with pytest.raises(type(exception)) as caught:
            await native.choose({})
        assert caught.value is exception
        return {"status": "failed", "usage": {"calls": native.rows}}

    loaded.run = run
    monkeypatch.setattr(worker, "load_agent", lambda: loaded)
    answer = await worker.run_terra({}, "TEST_KEY")
    row = answer["usage"]["calls"][0]
    assert row["state"] == state and row["error_type"] == type(exception).__name__
    assert answer["usage"]["usage_complete"] is False
    assert "secret" not in json.dumps(answer)


def test_only_fixed_api_error_codes_are_retained(worker):
    allowed, unsafe = worker.safe_calls([{"provider_error": "invalid_api_key"},
                                         {"provider_error": "sk-secret"}])
    assert allowed["provider_error"] == "invalid_api_key"
    assert "provider_error" not in unsafe
