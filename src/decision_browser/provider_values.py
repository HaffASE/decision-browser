"""Pure provider values and explicit-mapping validation; no profile I/O."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

_DEFAULTS = {
    "openai": ("https://api.openai.com/v1/decisions", "gpt-6-luna", "OPENAI_API_KEY", 255),
    "typesafe": ("https://api.typesafe.ai/v1/systemone", "jev-latest", "TYPESAFE_API_KEY", 255),
    "openrouter": ("https://openrouter.ai/api/alpha/decisions", "typesafe/jev-1.13", "OPENROUTER_API_KEY", 255),
    "opencode": ("https://opencode.ai/inference/systemone/v1/systemone", "jev-1.13", "OPENCODE_API_KEY", 255),
    "gemini": ("https://opencode.ai/inference/systemone/v1/systemone", "gemini-decision-1", "OPENCODE_API_KEY", 255),
    "local": ("http://127.0.0.1:8000/v1/systemone", "nimble-latest", None, 26),
}
_ALIASES = {"jev": "typesafe", "nimble": "local"}


class ProviderConfigError(Exception):
    """A stable code, never an environment value, credential, or endpoint."""

    def __init__(self, code: str = "invalid_provider_config") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TextSettings:
    endpoint: str
    model: str
    api_key: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    provider: str
    endpoint: str
    model: str
    api_key: str | None = field(repr=False)
    max_choices: int
    text: TextSettings | None = None


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_endpoint(endpoint: str, *, local_only: bool = False) -> str:
    """Validate one complete URL without resolving names or connecting anywhere.

    Plain HTTP is allowed only on literal loopback addresses or localhost.
    local_only additionally excludes every non-loopback HTTPS host.
    """
    if (
        type(endpoint) is not str or not endpoint or len(endpoint) > 2048
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127
               for character in endpoint)
        or "\\" in endpoint
    ):
        raise ProviderConfigError()
    try:
        parsed = urlsplit(endpoint)
        host = parsed.hostname
        port = parsed.port
        if (
            parsed.scheme not in {"http", "https"} or not host
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or "?" in endpoint or "#" in endpoint
            or not parsed.path.startswith("/") or parsed.path == "/"
            or parsed.netloc.endswith(":")
            or port is not None and not 1 <= port <= 65535
            or parsed.scheme == "http" and not _loopback(host)
            or local_only and not _loopback(host)
        ):
            raise ProviderConfigError()
    except (ValueError, UnicodeError):
        raise ProviderConfigError() from None
    return endpoint


def validate_model(value: str) -> str:
    if (
        type(value) is not str or not value or len(value) > 256
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127
               for character in value)
    ):
        raise ProviderConfigError()
    return value


def validate_api_key(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if (
        type(value) is not str or not value.isascii() or len(value) > 8192
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127
               for character in value)
    ):
        raise ProviderConfigError()
    return value


def validate_text_settings(settings: TextSettings) -> TextSettings:
    """Apply the same endpoint/key rules to direct Python helper callers."""
    if type(settings) is not TextSettings:
        raise ProviderConfigError()
    endpoint = validate_endpoint(settings.endpoint)
    model = validate_model(settings.model)
    key = validate_api_key(settings.api_key)
    if not _loopback(urlsplit(endpoint).hostname or "") and key is None:
        raise ProviderConfigError()
    return TextSettings(endpoint, model, key)


def _provider(env: Mapping[str, str]) -> str:
    name = env.get("DECISION_BROWSER_PROVIDER", "openai")
    name = _ALIASES.get(name, name)
    if name not in _DEFAULTS:
        raise ProviderConfigError()
    return name


def provider_names() -> tuple[str, ...]:
    """Canonical presets shared by configuration and interactive setup."""
    return tuple(_DEFAULTS)


def provider_key_name(provider: str) -> str | None:
    """Return the selected preset's credential variable, including known aliases."""
    return _DEFAULTS[_provider({"DECISION_BROWSER_PROVIDER": provider})][2]


def preset_key_matches_endpoint(provider: str, endpoint: str) -> bool:
    """A preset-named key is bound to its default HTTPS origin, without DNS."""
    default = _DEFAULTS[_provider({"DECISION_BROWSER_PROVIDER": provider})][0]
    route, preset = urlsplit(endpoint), urlsplit(default)
    return (
        route.scheme == preset.scheme
        and route.hostname == preset.hostname
        and (route.port or 443) == (preset.port or 443)
    )


def _text_settings(env: Mapping[str, str], provider: str) -> TextSettings | None:
    names = ("DECISION_BROWSER_TEXT_ENDPOINT", "DECISION_BROWSER_TEXT_MODEL", "DECISION_BROWSER_TEXT_API_KEY")
    if not any(name in env for name in names):
        return None
    if provider == "openai" or not env.get(names[0]) or not env.get(names[1]):
        raise ProviderConfigError()
    return validate_text_settings(TextSettings(env[names[0]], env[names[1]], env.get(names[2])))


def settings_from_environment(env: Mapping[str, str]) -> ProviderSettings:
    """Validate one explicit environment mapping without profile or environment reads."""
    provider = _provider(env)
    default_endpoint, default_model, key_name, default_choices = _DEFAULTS[provider]
    endpoint = validate_endpoint(
        env.get("DECISION_BROWSER_ENDPOINT", default_endpoint), local_only=provider == "local",
    )
    if provider != "local" and urlsplit(endpoint).scheme != "https":
        raise ProviderConfigError()
    model = validate_model(env.get("DECISION_BROWSER_MODEL", default_model))
    raw_choices = env.get("DECISION_BROWSER_MAX_CHOICES", str(default_choices))
    if not raw_choices.isascii() or not raw_choices.isdecimal():
        raise ProviderConfigError()
    try:
        max_choices = int(raw_choices)
    except ValueError:
        raise ProviderConfigError() from None
    if not 2 <= max_choices <= 255:
        raise ProviderConfigError()
    if provider == "openai" and (endpoint, model, max_choices) != (default_endpoint, default_model, default_choices):
        raise ProviderConfigError()
    if (
        "DECISION_BROWSER_API_KEY" not in env and key_name is not None and key_name in env
        and not preset_key_matches_endpoint(provider, endpoint)
    ):
        raise ProviderConfigError("credential_route_mismatch")
    api_key = validate_api_key(env.get("DECISION_BROWSER_API_KEY", env.get(key_name) if key_name else None))
    return ProviderSettings(provider, endpoint, model, api_key, max_choices, _text_settings(env, provider))


# Preserve the established public pickle/import identity after moving values.
ProviderConfigError.__module__ = "decision_browser.provider_config"
TextSettings.__module__ = "decision_browser.provider_config"
ProviderSettings.__module__ = "decision_browser.provider_config"
