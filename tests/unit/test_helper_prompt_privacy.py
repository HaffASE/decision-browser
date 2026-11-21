"""Known-schema free-fill values are checked before nested provider serialization."""
from __future__ import annotations

import json

import httpx
import pytest

from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.decision_codec import Decision
from decision_browser.dom import Action, Candidate, GuardResult, Observation
from decision_browser.privacy import PrivacyError
from decision_browser.provider import OpenAIProvider
from decision_browser.provider_config import TextSettings
from decision_browser.runner import RunManager
from decision_browser.systemone_provider import SystemOneProvider

CANARIES = ('canary"quoted', 'canary\\slash', 'canary\nline', 'canary\tcontrol', 'canary\x01control')


class _FillProvider:
    """Script only the decision; use a real HTTP provider for the text helper."""

    def __init__(self, route, helper):
        self.route = route
        self.helper = helper
        self.helper_calls = 0

    async def decide(self, questions, input_text, *, target_heads, **kwargs):
        head = target_heads["fill"]
        target = next(q for q in questions if q.name == head).choices[0]
        return Decision("fill", target, 1.0)

    async def generate_text(self, prompt, **kwargs):
        self.helper_calls += 1
        return await self.helper.generate_text(prompt, **kwargs)

    async def aclose(self):
        await self.helper.aclose()


def _provider(route, seen):
    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if route == "openai":
            return httpx.Response(200, json={
                "status": "completed", "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": json.dumps({"text": "Ada"})}
                ]}],
            })
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "Ada",
        }}]})

    transport = httpx.MockTransport(handler)
    helper = OpenAIProvider("synthetic-key", transport=transport) if route == "openai" else SystemOneProvider(
        endpoint="http://localhost:9000/v1/systemone", model="test-decisions",
        text=TextSettings("http://localhost:9001/v1/chat/completions", "test-helper", None),
        transport=transport,
    )
    return _FillProvider(route, helper)


def _setup(provider, blocked, source="goal", value="ordinary text"):
    node = {"name": value if source == "field" else "Name"}
    semantic = {"origin": "https://example.test", "url": "https://example.test/", "node": node}
    candidate = Candidate("name", ("fill",), {"fill": semantic}, None)
    obs = Observation("obs", "https://example.test", "doc",
                      value if source == "page_text" else "An ordinary page", {"name": candidate}, ())
    host = RunManager(lambda: provider)
    run = host._open(TaskRequest(
        goal=value if source == "goal" else "Enter a name", start_url="https://example.test/",
        values={}, checks=[], privacy=PrivacyPolicy(blocked_literals=[blocked]), limits=Limits(max_seconds=10),
    ))

    class DOM:
        async def prepare(self, operation, token, *, value, destination=None):
            return Action(operation, token, value, destination, "obs", "{}")

        async def guard(self, action):
            return GuardResult("ready", "ready")

    run.dom, run.provider = DOM(), provider
    return host, run, obs


def _prompt(route, payload):
    if route == "openai":
        return json.loads(payload["input"][0]["content"][0]["text"])
    return json.loads(payload["messages"][0]["content"])


@pytest.mark.parametrize("route", ["openai", "chat"])
@pytest.mark.parametrize("source", ["goal", "field", "page_text"])
@pytest.mark.parametrize("secret", CANARIES)
async def test_helper_source_literal_blocks_before_provider_call(route, source, secret):
    seen = []
    provider = _provider(route, seen)
    host, run, obs = _setup(provider, secret, source, secret)
    try:
        with pytest.raises(PrivacyError, match="blocked_literal"):
            await host._choose(run, obs)
    finally:
        await provider.aclose()
    assert not seen and provider.helper_calls == run.budget.usage().calls == 0


@pytest.mark.parametrize("route", ["openai", "chat"])
async def test_helper_benign_escaped_values_survive_schema_decode(route):
    text = 'ordinary "quoted" \\ path\nnew line\ttext'
    seen = []
    provider = _provider(route, seen)
    host, run, obs = _setup(provider, "absent-secret", value=text)
    try:
        action = await host._choose(run, obs)
    finally:
        await provider.aclose()
    assert action.operation == "fill" and action.value == "Ada"
    assert provider.helper_calls == run.budget.usage().calls == len(seen) == 1
    assert _prompt(route, seen[0])["goal"] == text


@pytest.mark.parametrize("route,blocked", [("openai", "gpt-6-luna"), ("chat", "test-helper")])
async def test_helper_exact_wire_gate_still_blocks_model_literal(route, blocked):
    seen = []
    provider = _provider(route, seen)
    host, run, obs = _setup(provider, blocked)
    try:
        with pytest.raises(PrivacyError, match="blocked_literal"):
            await host._choose(run, obs)
    finally:
        await provider.aclose()
    assert not seen and run.budget.usage().calls == 0
    assert provider.helper_calls == 1


@pytest.mark.parametrize("route", ["openai", "chat"])
async def test_helper_does_not_recursively_decode_opaque_source_string(route):
    secret = 'opaque"literal'
    opaque = json.dumps({"quoted": secret})
    assert secret not in opaque
    seen = []
    provider = _provider(route, seen)
    host, run, obs = _setup(provider, secret, value=opaque)
    try:
        action = await host._choose(run, obs)
    finally:
        await provider.aclose()
    assert action.value == "Ada" and len(seen) == 1
    assert _prompt(route, seen[0])["goal"] == opaque
