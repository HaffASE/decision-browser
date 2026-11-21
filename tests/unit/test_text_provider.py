"""An independently configured chat helper shares the task's privacy and budget."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from decision_browser.budget import Budget, BudgetExceeded
from decision_browser.contracts import Limits, PrivacyPolicy
from decision_browser.privacy import PrivacyError, PrivacyGate
from decision_browser.provider_config import TextSettings
from decision_browser.provider_http import ProviderError
from decision_browser.text_provider import generate_chat_text

LOCAL = TextSettings("http://127.0.0.1:11434/v1/chat/completions", "local-text", None)
GATE = PrivacyGate(PrivacyPolicy())


def _reply(content="orbital mechanics", **message):
    return {"choices": [{"index": 0, "finish_reason": "stop", "message": {
        "role": "assistant", "content": content, **message,
    }}]}


async def _generate(response, *, settings=LOCAL, prompt="Write one search query.",
                    privacy=GATE, budget=None, **kwargs):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=response)

    budget = budget or Budget(Limits())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        text = await generate_chat_text(client, settings, prompt, privacy=privacy,
                                        budget=budget, **kwargs)
    return text, seen, budget


async def test_local_chat_posts_only_explicit_helper_route_model_and_no_key():
    value, seen, budget = await _generate(_reply())
    assert value == "orbital mechanics" and len(seen) == 1
    request = seen[0]
    assert str(request.url) == LOCAL.endpoint
    assert "authorization" not in request.headers
    body = json.loads(request.content)
    assert body == {"model": "local-text", "stream": False, "messages": [
        {"role": "user", "content": "Write one search query."},
    ]}
    assert budget.usage().calls == 1
    assert budget.usage().input_bytes == len(request.content)


async def test_remote_helper_uses_only_own_explicit_credentials():
    settings = TextSettings("https://helper.example/v1/chat/completions", "text-model", "helper-key")
    value, seen, _ = await _generate(_reply("short"), settings=settings)
    assert value == "short"
    assert seen[0].headers["authorization"] == "Bearer helper-key"


@pytest.mark.parametrize("helper_key", [None, "explicit-helper-key"])
@pytest.mark.parametrize("client_auth", [None, ("decision-user", "decision-password")])
async def test_helper_never_inherits_client_authorization_or_query_credentials(helper_key, client_auth):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=_reply())

    settings = TextSettings(LOCAL.endpoint, LOCAL.model, helper_key)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer decision-client-key", "X-API-Key": "decision-header-key"},
        params={"api_key": "decision-query-key"}, cookies={"session": "decision-cookie"}, auth=client_auth,
    ) as client:
        assert await generate_chat_text(client, settings, "write", budget=Budget(Limits()), privacy=GATE)
    assert len(seen) == 1 and str(seen[0].url) == settings.endpoint
    assert seen[0].headers.get("authorization") == ("Bearer " + helper_key if helper_key else None)
    assert "x-api-key" not in seen[0].headers
    assert "cookie" not in seen[0].headers


async def test_client_auth_challenge_cannot_retry_outside_original_call_budget():
    attempts = []

    def challenge(request):
        attempts.append(request)
        return httpx.Response(401, headers={"WWW-Authenticate": 'Digest realm="fixture", nonce="abc"'},
                              content=b"unauthorized")

    budget = Budget(Limits(max_provider_calls=1))
    async with httpx.AsyncClient(transport=httpx.MockTransport(challenge),
                                auth=httpx.DigestAuth("decision-user", "decision-secret")) as client:
        with pytest.raises(ProviderError, match="http_error"):
            await generate_chat_text(client, LOCAL, "write", budget=budget, privacy=GATE)
    assert len(attempts) == budget.usage().calls == 1
    assert "authorization" not in attempts[0].headers


@pytest.mark.parametrize("response,code", [
    ({"choices": []}, "malformed"),
    ({"choices": [_reply()["choices"][0]] * 2}, "malformed"),
    ({"choices": "raw"}, "malformed"),
    ({"choices": [{"finish_reason": "length", "message": {}}]}, "incomplete"),
    ({"choices": [{"finish_reason": "content_filter", "message": {}}]}, "refusal"),
    ({"choices": [{"finish_reason": "tool_calls", "message": {}}]}, "malformed"),
    ({"choices": [{"finish_reason": None, "message": {}}]}, "incomplete"),
    (_reply(None), "malformed"),
    (_reply([{"type": "text", "text": "x"}]), "malformed"),
    (_reply("x", role="user"), "malformed"),
    (_reply("x", refusal="refused"), "refusal"),
    (_reply("x", tool_calls=[{"function": {"name": "send"}}]), "malformed"),
    (_reply("x", function_call={"name": "send"}), "malformed"),
    ({**_reply(), "error": {"message": "SECRET"}}, "malformed"),
])
async def test_truncated_refused_or_ambiguous_chat_never_becomes_field_text(response, code):
    with pytest.raises(ProviderError) as caught:
        await _generate(response)
    assert caught.value.code == code
    assert "SECRET" not in str(caught.value)


async def test_character_limit_accepts_exact_boundary_and_rejects_overflow():
    assert (await _generate(_reply("абв"), max_text_chars=3))[0] == "абв"
    with pytest.raises(ProviderError, match="max_text_chars"):
        await _generate(_reply("абвг"), max_text_chars=3)


async def test_privacy_and_remaining_call_quota_block_before_socket():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=_reply())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        blocked = PrivacyGate(PrivacyPolicy(blocked_literals=["SECRET"]))
        budget = Budget(Limits())
        with pytest.raises(PrivacyError, match="blocked_literal"):
            await generate_chat_text(client, LOCAL, "SECRET", budget=budget, privacy=blocked)
        assert budget.usage().calls == 0
        assert not requests
        exhausted = Budget(Limits(max_provider_calls=1))
        exhausted.reserve_provider(10)  # The decision already spent this call.
        with pytest.raises(BudgetExceeded, match="provider_calls"):
            await generate_chat_text(client, LOCAL, "write", budget=exhausted, privacy=GATE)
        assert not requests


@pytest.mark.parametrize("max_chars", [True, 0, 65537, "3"])
async def test_invalid_limit_is_rejected_before_socket(max_chars):
    def forbidden(_request):
        raise AssertionError("network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(ProviderError, match="max_text_chars"):
            await generate_chat_text(client, LOCAL, "write", budget=Budget(Limits()),
                                     privacy=GATE, max_text_chars=max_chars)


async def test_cancelled_helper_preserves_cancellation_and_original_deadline():
    entered = asyncio.Event()

    async def hanging(_request):
        entered.set()
        await asyncio.Event().wait()

    budget = Budget(Limits(max_seconds=30))
    deadline = budget.deadline
    async with httpx.AsyncClient(transport=httpx.MockTransport(hanging)) as client:
        task = asyncio.create_task(generate_chat_text(client, LOCAL, "write", budget=budget, privacy=GATE))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert budget.deadline == budget.original_deadline == deadline
    assert budget.usage().calls == 1
