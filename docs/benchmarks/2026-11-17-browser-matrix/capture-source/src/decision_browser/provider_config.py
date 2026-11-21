"""Explicit operator-selected model routes; no network or credential discovery."""

from __future__ import annotations

import os
from collections.abc import Mapping

from decision_browser.provider_values import (
    _DEFAULTS,
    ProviderConfigError,
    ProviderSettings,
    TextSettings,
    _provider,
    preset_key_matches_endpoint,
    provider_key_name,
    provider_names,
    settings_from_environment,
    validate_api_key,
    validate_endpoint,
    validate_model,
    validate_text_settings,
)

__all__ = (
    "ProviderConfigError",
    "ProviderSettings",
    "TextSettings",
    "load_provider_settings",
    "preset_key_matches_endpoint",
    "provider_key_name",
    "provider_names",
    "provider_status",
    "validate_api_key",
    "validate_endpoint",
    "validate_model",
    "validate_text_settings",
)


def _effective_environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    if environ is not None:
        return environ
    # An explicit route must never inherit a saved route's endpoint or key.
    if "DECISION_BROWSER_PROVIDER" in os.environ or "DECISION_BROWSER_ENDPOINT" in os.environ:
        return os.environ
    from decision_browser.user_config import load_profile

    profile = load_profile()
    if profile is None:
        return os.environ
    result = {**profile, **os.environ}
    key_name = _DEFAULTS[_provider(profile)][2]
    if "DECISION_BROWSER_API_KEY" not in os.environ and key_name and key_name in os.environ:
        if not preset_key_matches_endpoint(
            _provider(profile), profile.get("DECISION_BROWSER_ENDPOINT", _DEFAULTS[_provider(profile)][0]),
        ):
            raise ProviderConfigError("credential_route_mismatch")
        result["DECISION_BROWSER_API_KEY"] = os.environ[key_name]
    return result


def load_provider_settings(environ: Mapping[str, str] | None = None) -> ProviderSettings:
    """Resolve the saved profile plus environment, or an isolated explicit mapping."""
    return settings_from_environment(_effective_environment(environ))


def provider_status(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """Prerequisites only: never construct a client or expose operator values."""
    name = "invalid"
    try:
        env = _effective_environment(environ)
        name = _provider(env)
        config = load_provider_settings(env)
    except ProviderConfigError as exc:
        return {
            "provider": name, "api_key_present": False, "provider_ready": False,
            "provider_error": exc.code, "text_generation": "unconfigured", "visual": "unsupported",
        }
    ready = config.api_key is not None or name == "local"
    return {
        "provider": name, "api_key_present": config.api_key is not None, "provider_ready": ready,
        "provider_error": None if ready else "missing_api_key",
        "text_generation": "native" if name == "openai" else "configured" if config.text is not None else "unconfigured",
        "visual": "native" if name == "openai" else "unsupported",
    }
