"""Recover a transient Decisions service failure within the original budget."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from test_provider import _Q, _gate, _ok
from test_wave2_provider_transport import Stream

from decision_browser.budget import Budget, BudgetExceeded
from decision_browser.contracts import Limits
from decision_browser.provider import OpenAIProvider, ProviderError


async def test_service_503_repeats_exact_request_and_accounts_both_attempts():
    requests = []
    streams = []

    def handler(request):
        requests.append(request)
        stream = Stream(b"unavailable" if len(requests) == 1 else _ok())
        streams.append(stream)
        return httpx.Response(503 if len(requests) == 1 else 200, stream=stream)

    provider = OpenAIProvider("test-key", transport=httpx.MockTransport(handler))
    budget = Budget(Limits())
    try:
        result = await provider.decide(_Q, "ship", budget=budget, privacy=_gate())
    finally:
        await provider.aclose()
    assert result.operation == "DONE"
    assert len(requests) == budget.usage().calls == 2
    assert requests[0].url == requests[1].url
    assert requests[0].content == requests[1].content
    assert requests[0].headers == requests[1].headers
    assert budget.usage().input_bytes == 2 * len(requests[0].content)
    assert budget.usage().output_bytes == len(b"unavailable") + len(_ok())
    assert [s.closes for s in streams] == [1, 1]


@pytest.mark.parametrize("status,expected", [(400, 1), (401, 1), (429, 1), (500, 1), (502, 1), (503, 2)])
async def test_only_503_retries_at_most_once(status, expected):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, content=b"private response")

    provider = OpenAIProvider("test-key", transport=httpx.MockTransport(handler))
    budget = Budget(Limits())
    try:
        with pytest.raises(ProviderError, match="^http_error$") as caught:
            await provider.decide(_Q, "ship", budget=budget, privacy=_gate())
    finally:
        await provider.aclose()
    assert caught.value.http_status == status
    assert len(requests) == budget.usage().calls == expected
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("limit", ["max_provider_calls", "max_input_bytes", "max_output_bytes", "max_seconds"])
async def test_retry_cannot_exceed_original_budget(limit):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503, content=b"unavailable")

    provider = OpenAIProvider("test-key", transport=httpx.MockTransport(handler))
    limits = {"max_provider_calls": 1} if limit == "max_provider_calls" else (
        {"max_input_bytes": 256} if limit == "max_input_bytes" else (
            {"max_output_bytes": 5} if limit == "max_output_bytes" else {"max_seconds": 0.01}))
    budget = Budget(Limits(**limits))
    try:
        with pytest.raises(BudgetExceeded):
            await provider.decide(_Q, "ship", budget=budget, privacy=_gate())
    finally:
        await provider.aclose()
    assert len(requests) == 1
    if limit == "max_input_bytes":
        assert len(requests[0].content) <= 256 < 2 * len(requests[0].content)
    assert budget.usage().calls == len(requests)


async def test_close_failure_prevents_retry():
    requests = []
    stream = Stream(b"unavailable", close_error=RuntimeError("private"))

    def handler(request):
        requests.append(request)
        return httpx.Response(503, stream=stream)

    provider = OpenAIProvider("test-key", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.decide(_Q, "ship", budget=Budget(Limits()), privacy=_gate())
    finally:
        await provider.aclose()
    assert "provider:response_close_failed" in caught.value.diagnostics
    assert len(requests) == 1


async def test_responses_route_retains_single_attempt():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503, content=b"unavailable")

    provider = OpenAIProvider("test-key", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError):
            await provider._post("/v1/responses", {}, budget=Budget(Limits()), privacy=_gate())
    finally:
        await provider.aclose()
    assert len(requests) == 1


async def test_cancellation_during_backoff_prevents_second_request():
    seen = asyncio.Event()
    requests = []

    def handler(request):
        requests.append(request)
        seen.set()
        return httpx.Response(503, content=b"unavailable")

    provider = OpenAIProvider("test-key", transport=httpx.MockTransport(handler))
    job = asyncio.create_task(provider.decide(_Q, "ship", budget=Budget(Limits()), privacy=_gate()))
    try:
        await seen.wait()
        await asyncio.sleep(0.01)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
    finally:
        await provider.aclose()
    assert len(requests) == 1


async def test_only_one_recovery_across_decisions_and_providers_with_shared_budget():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=_ok()) if len(requests) == 2 else httpx.Response(503)

    transport = httpx.MockTransport(handler)
    first = OpenAIProvider("test-key", transport=transport)
    second = OpenAIProvider("test-key", transport=transport)
    budget = Budget(Limits())
    try:
        result = await first.decide(_Q, "first", budget=budget, privacy=_gate())
        assert result.operation == "DONE"
        with pytest.raises(ProviderError, match="^http_error$"):
            await second.decide(_Q, "second", budget=budget, privacy=_gate())
    finally:
        await first.aclose()
        await second.aclose()
    assert len(requests) == budget.usage().calls == 3


async def test_independent_tasks_can_each_recover_once():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=_ok()) if len(requests) % 2 == 0 else httpx.Response(503)

    provider = OpenAIProvider("test-key", transport=httpx.MockTransport(handler))
    budgets = [Budget(Limits()), Budget(Limits())]
    try:
        for budget in budgets:
            result = await provider.decide(_Q, "ship", budget=budget, privacy=_gate())
            assert result.operation == "DONE"
    finally:
        await provider.aclose()
    assert len(requests) == 4
    assert [budget.usage().calls for budget in budgets] == [2, 2]


async def test_residual_cleanup_blocks_retry_even_with_clean_status(monkeypatch):
    calls = []
    error = ProviderError("http_error")
    error.http_status = 503
    error.diagnostics = ("http_status:server_error",)
    error.__dict__["residual_tasks"] = (object(),)

    async def failed_post(*args, **kwargs):
        calls.append(1)
        raise error

    monkeypatch.setattr("decision_browser.provider.post_json", failed_post)
    provider = OpenAIProvider("test-key")
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.decide(_Q, "ship", budget=Budget(Limits()), privacy=_gate())
    finally:
        await provider.aclose()
    assert caught.value is error
    assert len(calls) == 1
