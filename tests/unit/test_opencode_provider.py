"""The OpenCode preset reaches the existing bounded native decisions adapter."""

from __future__ import annotations

import json
import os

import httpx
import pytest

from decision_browser import server
from decision_browser.budget import Budget
from decision_browser.contracts import Limits, PrivacyPolicy
from decision_browser.decision_codec import ChoiceQuestion, Decision
from decision_browser.privacy import PrivacyGate
from decision_browser.provider import ProviderError
from decision_browser.provider_config import load_provider_settings, provider_status
from decision_browser.runner import RunnerError
from decision_browser.systemone_provider import SystemOneProvider

ENDPOINT = "https://opencode.ai/inference/systemone/v1/systemone"
QUESTIONS = (ChoiceQuestion("operation", "Choose operation", ("click", "DONE")),
             ChoiceQuestion("target_click", "Choose button", ("activate",)))
HEADS = {"click": "target_click"}


@pytest.fixture(autouse=True)
def clean_provider_environment(monkeypatch, isolated_provider_profile):
    for name in tuple(os.environ):
        if name == "DECISION_BROWSER_CONFIG_FILE":
            continue
        if name.startswith("DECISION_BROWSER_") or name in {
            "OPENAI_API_KEY", "OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "OPENCODE_API_KEY",
        }:
            monkeypatch.delenv(name)


def test_opencode_preset_requires_only_opencode_key_and_keeps_openai_default():
    other_keys = {"OPENAI_API_KEY": "openai-secret", "TYPESAFE_API_KEY": "typesafe-secret",
                  "OPENROUTER_API_KEY": "router-secret"}
    assert load_provider_settings(other_keys).provider == "openai"
    env = {**other_keys, "DECISION_BROWSER_PROVIDER": "opencode"}
    config = load_provider_settings(env)
    assert (config.endpoint, config.model, config.max_choices, config.api_key) == (
        ENDPOINT, "jev-1.13", 255, None,
    )
    assert provider_status(env)["provider_ready"] is False
    env["OPENCODE_API_KEY"] = "opencode-secret"
    config = load_provider_settings(env)
    assert config.api_key == "opencode-secret" and "opencode-secret" not in repr(config)
    status = provider_status(env)
    assert status["provider"] == "opencode" and status["provider_ready"] is True
    assert status["text_generation"] == "unconfigured" and status["visual"] == "unsupported"
    assert not any(value in json.dumps(status) for value in [*other_keys.values(), "opencode-secret"])


def test_factory_does_not_read_other_provider_keys_when_opencode_key_absent(monkeypatch):
    monkeypatch.setenv("DECISION_BROWSER_PROVIDER", "opencode")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-key")
    with pytest.raises(RunnerError, match="missing_api_key"):
        server.production_factory()


def _factory(monkeypatch, handler):
    def native(**settings):
        return SystemOneProvider(**settings, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(server, "SystemOneProvider", native)
    monkeypatch.setenv("DECISION_BROWSER_PROVIDER", "opencode")
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-key")
    return server.production_factory()


@pytest.mark.parametrize("model", ["jev-1.13", "jev-1.13-free"])
async def test_production_factory_posts_native_opencode_request_with_explicit_model(monkeypatch, model):
    seen = []

    def handler(request):
        seen.append(request)
        body = json.loads(request.content)
        assert str(request.url) == ENDPOINT
        assert request.headers["Authorization"] == "Bearer opencode-test-key"
        assert set(body) == {"model", "state", "questions"}
        assert body["model"] == model and body["state"] == "synthetic page"
        assert not any(name.lower().startswith("x-opencode") for name in request.headers)
        answers = {}
        for name, question in body["questions"].items():
            desired = "click" if name == "operation" else "activate"
            choice = next(key for key, description in question["criteria"].items()
                          if json.loads(description) == desired)
            answers[name] = {"type": "choice", "choice": choice, "confidence": 1,
                             "probabilities": {key: int(key == choice) for key in question["criteria"]}}
        return httpx.Response(200, json={"model": "jev-1.13.0", "answers": answers,
                                        "usage": {"input_tokens": 72, "output_tokens": 4, "cost": 0},
                                        "id": "fixture-request"})

    monkeypatch.setenv("DECISION_BROWSER_MODEL", model)
    provider = _factory(monkeypatch, handler)
    budget = Budget(Limits())
    try:
        decision = await provider.decide(QUESTIONS, "synthetic page", target_heads=HEADS,
                                         budget=budget, privacy=PrivacyGate(PrivacyPolicy()))
    finally:
        await provider.aclose()
    assert decision == Decision("click", "activate", 1.0)
    assert len(seen) == budget.usage().calls == 1


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_free_model_access_or_quota_failure_never_switches_to_paid(monkeypatch, status):
    attempts = []

    def handler(request):
        attempts.append(json.loads(request.content)["model"])
        return httpx.Response(status, json={"error": {"message": "private response detail"}})

    monkeypatch.setenv("DECISION_BROWSER_MODEL", "jev-1.13-free")
    provider = _factory(monkeypatch, handler)
    budget = Budget(Limits())
    try:
        with pytest.raises(ProviderError, match="http_error") as caught:
            await provider.decide(QUESTIONS, "synthetic page", target_heads=HEADS,
                                  budget=budget, privacy=PrivacyGate(PrivacyPolicy()))
    finally:
        await provider.aclose()
    assert "private response detail" not in str(caught.value)
    assert attempts == ["jev-1.13-free"] and budget.usage().calls == 1
