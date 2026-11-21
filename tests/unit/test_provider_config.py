"""Provider settings are explicit, secret-free observations until a run starts."""

from __future__ import annotations

import json
import socket

import pytest

from decision_browser.provider_config import (
    ProviderConfigError,
    TextSettings,
    load_provider_settings,
    provider_status,
    validate_endpoint,
    validate_text_settings,
)

CANARY = "CANARY-provider-secret"


def test_default_openai_contract_and_generic_key_override():
    config = load_provider_settings({"OPENAI_API_KEY": CANARY})
    assert (config.provider, config.endpoint, config.model, config.max_choices) == (
        "openai", "https://api.openai.com/v1/decisions", "gpt-6-luna", 255,
    )
    assert config.api_key == CANARY and config.text is None
    assert CANARY not in repr(config)
    config = load_provider_settings({"OPENAI_API_KEY": CANARY, "DECISION_BROWSER_API_KEY": "explicit"})
    assert config.api_key == "explicit"
    config = load_provider_settings({"OPENAI_API_KEY": CANARY, "DECISION_BROWSER_API_KEY": ""})
    assert config.api_key is None


@pytest.mark.parametrize("provider,canonical,model,key_name,endpoint", [
    ("typesafe", "typesafe", "jev-latest", "TYPESAFE_API_KEY", "https://api.typesafe.ai/v1/systemone"),
    ("jev", "typesafe", "jev-latest", "TYPESAFE_API_KEY", "https://api.typesafe.ai/v1/systemone"),
    ("openrouter", "openrouter", "typesafe/jev-1.13", "OPENROUTER_API_KEY", "https://openrouter.ai/api/alpha/decisions"),
    ("gemini", "gemini", "gemini-decision-1", "OPENCODE_API_KEY", "https://opencode.ai/inference/systemone/v1/systemone"),
])
def test_selected_remote_route_uses_only_its_own_key(provider, canonical, model, key_name, endpoint):
    env = {"DECISION_BROWSER_PROVIDER": provider, "OPENAI_API_KEY": "wrong-openai"}
    config = load_provider_settings(env)
    assert config.api_key is None
    env[key_name] = CANARY
    config = load_provider_settings(env)
    assert (config.provider, config.model, config.endpoint) == (canonical, model, endpoint)
    assert config.api_key == CANARY and CANARY not in repr(config)
    assert provider_status(env)["provider_ready"] is True


@pytest.mark.parametrize("provider", ["local", "nimble"])
def test_keyless_local_never_inherits_cloud_credentials(provider):
    env = {
        "DECISION_BROWSER_PROVIDER": provider,
        "OPENAI_API_KEY": CANARY,
        "TYPESAFE_API_KEY": CANARY,
        "OPENROUTER_API_KEY": CANARY,
        "OPENCODE_API_KEY": CANARY,
    }
    config = load_provider_settings(env)
    assert (config.provider, config.endpoint, config.model, config.max_choices) == (
        "local", "http://127.0.0.1:8000/v1/systemone", "nimble-latest", 26,
    )
    assert config.api_key is None
    status = provider_status(env)
    assert status["provider_ready"] is True and status["api_key_present"] is False
    assert status["text_generation"] == "unconfigured"
    assert status["visual"] == "unsupported"
    assert CANARY not in json.dumps(status)


def test_alternate_overrides_and_separate_text_helper():
    config = load_provider_settings({
        "DECISION_BROWSER_PROVIDER": "local",
        "DECISION_BROWSER_ENDPOINT": "http://[::1]:8001/v1/systemone",
        "DECISION_BROWSER_MODEL": "local-model",
        "DECISION_BROWSER_API_KEY": "decision-secret",
        "DECISION_BROWSER_MAX_CHOICES": "64",
        "DECISION_BROWSER_TEXT_ENDPOINT": "http://localhost:11434/v1/chat/completions",
        "DECISION_BROWSER_TEXT_MODEL": "local-text",
        "DECISION_BROWSER_TEXT_API_KEY": CANARY,
    })
    assert (config.endpoint, config.model, config.max_choices) == (
        "http://[::1]:8001/v1/systemone", "local-model", 64,
    )
    assert config.text is not None and config.text.api_key == CANARY
    assert CANARY not in repr(config) and "decision-secret" not in repr(config)


@pytest.mark.parametrize("endpoint", [
    "http://127.0.0.1:9000/v1/systemone", "http://[::1]:9000/v1/systemone",
    "https://localhost:9000/v1/systemone", "http://127.1.2.3/v1/systemone",
])
def test_loopback_endpoints_without_dns(endpoint):
    assert validate_endpoint(endpoint, local_only=True) == endpoint


@pytest.mark.parametrize("endpoint", [
    "http://example.test/v1/systemone", "https://user:password@example.test/v1/systemone",
    "https://example.test/v1/systemone?key=secret", "https://example.test/v1/systemone#fragment",
    "https://example.test", "file:///v1/systemone", "https://example.test:0/v1/systemone",
    "https://example.test:65536/v1/systemone", "https://example.test:/v1/systemone",
    "https://example.test/v1/systemone\n", " https://example.test/v1/systemone",
    "https://example.test/v1/my endpoint", "https:///v1/systemone",
])
def test_invalid_endpoint_is_sanitized(endpoint):
    with pytest.raises(ProviderConfigError) as caught:
        validate_endpoint(endpoint)
    assert caught.value.code == "invalid_provider_config"
    assert endpoint not in str(caught.value)


@pytest.mark.parametrize("endpoint", [
    "https://example.test/v1/systemone", "https://localhost.evil.test/v1/systemone",
    "http://0.0.0.0:8000/v1/systemone", "http://[::]:8000/v1/systemone",
    "http://192.168.0.1:8000/v1/systemone", "http://2130706433/v1/systemone",
])
def test_local_mode_cannot_route_to_other_hosts(endpoint):
    with pytest.raises(ProviderConfigError):
        load_provider_settings({"DECISION_BROWSER_PROVIDER": "local", "DECISION_BROWSER_ENDPOINT": endpoint})


@pytest.mark.parametrize("overrides", [
    {"DECISION_BROWSER_PROVIDER": CANARY},
    {"DECISION_BROWSER_MODEL": CANARY},
    {"DECISION_BROWSER_ENDPOINT": "https://example.test/v1/decisions"},
    {"DECISION_BROWSER_MAX_CHOICES": "26"},
    {"DECISION_BROWSER_PROVIDER": "local", "DECISION_BROWSER_MODEL": ""},
    {"DECISION_BROWSER_PROVIDER": "local", "DECISION_BROWSER_MODEL": "model\nsecret"},
    {"DECISION_BROWSER_PROVIDER": "local", "DECISION_BROWSER_MAX_CHOICES": "1"},
    {"DECISION_BROWSER_PROVIDER": "local", "DECISION_BROWSER_MAX_CHOICES": "256"},
    {"DECISION_BROWSER_PROVIDER": "local", "DECISION_BROWSER_MAX_CHOICES": "2.0"},
    {"DECISION_BROWSER_PROVIDER": "typesafe", "DECISION_BROWSER_ENDPOINT": "http://localhost:8000/v1/systemone"},
    {"DECISION_BROWSER_PROVIDER": "local", "DECISION_BROWSER_TEXT_MODEL": "local"},
    {"DECISION_BROWSER_PROVIDER": "local", "DECISION_BROWSER_TEXT_API_KEY": CANARY},
    {"DECISION_BROWSER_PROVIDER": "local", "DECISION_BROWSER_TEXT_ENDPOINT": "https://example.test/v1/chat/completions", "DECISION_BROWSER_TEXT_MODEL": "text", "OPENAI_API_KEY": CANARY},
    {"DECISION_BROWSER_TEXT_MODEL": "text", "DECISION_BROWSER_TEXT_ENDPOINT": "http://localhost:8000/v1/chat/completions"},
    {"DECISION_BROWSER_API_KEY": "key\r\nInjected: secret"},
    {"DECISION_BROWSER_API_KEY": "ключ"},
])
def test_invalid_configuration_blocks_readiness_without_leaking(overrides):
    with pytest.raises(ProviderConfigError):
        load_provider_settings(overrides)
    status = provider_status(overrides)
    assert status["provider_ready"] is False
    assert status["provider_error"] == "invalid_provider_config"
    assert CANARY not in json.dumps(status)


def test_missing_remote_key_and_no_network_in_status(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("network in provider status")

    monkeypatch.setattr(socket.socket, "connect", fail)
    for provider in ("openai", "typesafe", "openrouter", "opencode", "gemini"):
        status = provider_status({"DECISION_BROWSER_PROVIDER": provider})
        assert status["provider_ready"] is False
        assert status["api_key_present"] is False
        assert status["provider_error"] == "missing_api_key"


def test_remote_text_helper_needs_its_explicit_key():
    env = {
        "DECISION_BROWSER_PROVIDER": "typesafe", "TYPESAFE_API_KEY": "decision-key",
        "DECISION_BROWSER_TEXT_MODEL": "text-model",
        "DECISION_BROWSER_TEXT_ENDPOINT": "https://example.test/v1/chat/completions",
        "DECISION_BROWSER_TEXT_API_KEY": CANARY,
    }
    config = load_provider_settings(env)
    assert config.text is not None and config.text.api_key == CANARY
    assert provider_status(env)["text_generation"] == "configured"
    assert CANARY not in json.dumps(provider_status(env))


@pytest.mark.parametrize("settings", [
    TextSettings("https://example.test/v1/chat/completions", "model", None),
    TextSettings("http://example.test/v1/chat/completions", "model", CANARY),
    TextSettings("http://localhost:11434/v1/chat/completions", "bad model", None),
    TextSettings("http://localhost:11434/v1/chat/completions", "model", "ключ"),
])
def test_direct_text_settings_have_same_validation(settings):
    with pytest.raises(ProviderConfigError) as caught:
        validate_text_settings(settings)
    assert CANARY not in str(caught.value)
