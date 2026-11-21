"""Gemini Decision 1 uses the bounded OpenCode System One route."""

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
from decision_browser.user_config import save_profile

ENDPOINT = "https://opencode.ai/inference/systemone/v1/systemone"
MODEL = "gemini-decision-1"
KEY = "gemini-opencode-placeholder-key"
QUESTIONS = (
    ChoiceQuestion("operation", "Choose operation", ("click", "DONE")),
    ChoiceQuestion("target_click", "Choose button", ("activate",)),
)
HEADS = {"click": "target_click"}


@pytest.fixture(autouse=True)
def clean_provider_environment(monkeypatch, isolated_provider_profile):
    for name in tuple(os.environ):
        if name == "DECISION_BROWSER_CONFIG_FILE":
            continue
        if name.startswith("DECISION_BROWSER_") or name.endswith("API_KEY"):
            monkeypatch.delenv(name)


def test_gemini_preset_uses_opencode_key_and_does_not_infer_a_provider():
    unrelated_keys = {
        "OPENAI_API_KEY": "unrelated-openai-key",
        "TYPESAFE_API_KEY": "unrelated-typesafe-key",
        "OPENROUTER_API_KEY": "unrelated-openrouter-key",
        "GOOGLE_API_KEY": "unrelated-google-key",
        "GEMINI_API_KEY": "unrelated-gemini-key",
    }
    assert load_provider_settings(unrelated_keys).provider == "openai"
    env = {**unrelated_keys, "DECISION_BROWSER_PROVIDER": "gemini"}
    config = load_provider_settings(env)
    assert (config.provider, config.endpoint, config.model, config.max_choices, config.api_key) == (
        "gemini", ENDPOINT, MODEL, 255, None,
    )
    assert provider_status(env)["provider_error"] == "missing_api_key"

    env["OPENCODE_API_KEY"] = KEY
    config = load_provider_settings(env)
    assert config.api_key == KEY and KEY not in repr(config)
    status = provider_status(env)
    assert status == {
        "provider": "gemini", "api_key_present": True, "provider_ready": True,
        "provider_error": None, "text_generation": "unconfigured", "visual": "unsupported",
    }
    assert not any(value in json.dumps(status) for value in [*unrelated_keys.values(), KEY])
    assert load_provider_settings({"DECISION_BROWSER_PROVIDER": "opencode"}).model == "jev-1.13"


def test_explicit_empty_key_does_not_fall_back_to_opencode_key():
    env = {"DECISION_BROWSER_PROVIDER": "gemini", "OPENCODE_API_KEY": KEY,
           "DECISION_BROWSER_API_KEY": ""}
    assert load_provider_settings(env).api_key is None
    assert provider_status(env)["provider_error"] == "missing_api_key"


def test_gemini_factory_refuses_unrelated_credentials(monkeypatch):
    monkeypatch.setenv("DECISION_BROWSER_PROVIDER", "gemini")
    for name in ("OPENAI_API_KEY", "TYPESAFE_API_KEY", "OPENROUTER_API_KEY",
                 "GOOGLE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(name, "unrelated-placeholder-key")
    monkeypatch.setattr(server, "SystemOneProvider", lambda **kwargs: pytest.fail("client without key"))
    with pytest.raises(RunnerError, match="^missing_api_key$"):
        server.production_factory()


def _transport_factory(monkeypatch, handler):
    def native(**settings):
        return SystemOneProvider(**settings, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(server, "SystemOneProvider", native)


def _successful_response(request):
    body = json.loads(request.content)
    assert request.method == "POST" and str(request.url) == ENDPOINT
    assert request.headers["Authorization"] == "Bearer " + KEY
    assert not any(name.lower().startswith("x-opencode") for name in request.headers)
    assert set(body) == {"model", "state", "questions"}
    assert body["model"] == MODEL and body["state"] == "synthetic page"
    assert set(body["questions"]) == {"operation", "target_click"}
    answers = {}
    for name, question in body["questions"].items():
        assert question["type"] == "choice"
        desired = "click" if name == "operation" else "activate"
        choice = next(key for key, description in question["criteria"].items()
                      if json.loads(description) == desired)
        answers[name] = {
            "type": "choice", "choice": choice, "confidence": 1,
            "probabilities": {key: int(key == choice) for key in question["criteria"]},
        }
    return httpx.Response(200, json={
        "model": MODEL, "answers": answers,
        "usage": {"input_tokens": 72, "output_tokens": 4, "cost": 0},
    })


@pytest.mark.parametrize("route", ["gemini", "opencode"])
async def test_gemini_native_request_decodes_to_original_action(monkeypatch, route):
    seen = []

    def handler(request):
        seen.append(request)
        return _successful_response(request)

    monkeypatch.setenv("DECISION_BROWSER_PROVIDER", route)
    monkeypatch.setenv("OPENCODE_API_KEY", KEY)
    if route == "opencode":
        monkeypatch.setenv("DECISION_BROWSER_MODEL", MODEL)
    _transport_factory(monkeypatch, handler)
    provider = server.production_factory()
    budget = Budget(Limits())
    try:
        decision = await provider.decide(
            QUESTIONS, "synthetic page", target_heads=HEADS,
            budget=budget, privacy=PrivacyGate(PrivacyPolicy()),
        )
    finally:
        await provider.aclose()
    assert decision == Decision("click", "activate", 1.0)
    assert len(seen) == budget.usage().calls == 1


@pytest.mark.parametrize("route", ["gemini", "opencode"])
@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_gemini_access_quota_or_server_error_never_retries_or_switches_model(monkeypatch, route, status):
    attempts = []

    def handler(request):
        attempts.append((str(request.url), json.loads(request.content)["model"]))
        return httpx.Response(status, json={"error": {"message": KEY + " private provider detail"}})

    monkeypatch.setenv("DECISION_BROWSER_PROVIDER", route)
    monkeypatch.setenv("OPENCODE_API_KEY", KEY)
    if route == "opencode":
        monkeypatch.setenv("DECISION_BROWSER_MODEL", MODEL)
    _transport_factory(monkeypatch, handler)
    provider = server.production_factory()
    budget = Budget(Limits())
    try:
        with pytest.raises(ProviderError, match="^http_error$") as caught:
            await provider.decide(
                QUESTIONS, "synthetic page", target_heads=HEADS,
                budget=budget, privacy=PrivacyGate(PrivacyPolicy()),
            )
    finally:
        await provider.aclose()
    assert KEY not in str(caught.value) and "private provider detail" not in str(caught.value)
    assert attempts == [(ENDPOINT, MODEL)] and budget.usage().calls == 1


async def test_saved_gemini_profile_reaches_production_transport(monkeypatch):
    save_profile(load_provider_settings({"DECISION_BROWSER_PROVIDER": "gemini", "OPENCODE_API_KEY": KEY}))
    monkeypatch.setenv("GEMINI_API_KEY", "unrelated-google-direct-key")
    assert load_provider_settings().model == MODEL
    assert provider_status()["provider"] == "gemini"
    _transport_factory(monkeypatch, _successful_response)
    provider = server.production_factory()
    budget = Budget(Limits())
    try:
        decision = await provider.decide(
            QUESTIONS, "synthetic page", target_heads=HEADS,
            budget=budget, privacy=PrivacyGate(PrivacyPolicy()),
        )
    finally:
        await provider.aclose()
    assert decision == Decision("click", "activate", 1.0)
    assert budget.usage().calls == 1
