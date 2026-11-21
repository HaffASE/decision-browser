"""Terminal-only provider setup. Never contacts a model or edits client settings."""

from __future__ import annotations

import getpass
import os
import sys
import warnings

from decision_browser.provider_config import (
    ProviderConfigError,
    ProviderSettings,
    load_provider_settings,
    preset_key_matches_endpoint,
    provider_key_name,
    provider_names,
)
from decision_browser.user_config import (
    load_profile,
    prepare_profile_destination,
    save_profile,
)

_LABELS = {
    "openai": "OpenAI — Decisions",
    "typesafe": "TypeSafe — Jev",
    "openrouter": "OpenRouter — Jev",
    "opencode": "OpenCode Console — Jev (usage-based billing)",
    "gemini": "Gemini Decision 1 — OpenCode Console (usage-based billing)",
    "local": "Local — an existing System One service",
}
_CONFIG_ERRORS = {
    "invalid_provider_config": "Invalid provider settings or API key. Nothing was saved.",
    "invalid_profile_path": "The profile location must be an absolute path without parent traversal.",
    "unsafe_profile_path": "Use a regular profile file and directory, without symbolic links.",
    "unsafe_profile_permissions": "The profile directory and file must be private to your user (0700/0600).",
    "credential_route_mismatch": (
        "A custom host needs an explicit DECISION_BROWSER_API_KEY for that route. Nothing was saved."
    ),
    "profile_credentials_unsupported": (
        "Saving API keys on Windows is not supported yet. Use provider environment variables."
    ),
}


def _select_provider(default: str) -> str:
    names = provider_names()
    print("Choose a provider:")
    for index, name in enumerate(names, 1):
        print(f"  {index}. {_LABELS[name]} ({name})")
    value = input(f"Provider [{default}]: ").strip() or default
    if value.isascii() and value.isdecimal() and 1 <= int(value) <= len(names):
        return names[int(value) - 1]
    # The existing resolver validates canonical names and aliases without I/O.
    return load_provider_settings({"DECISION_BROWSER_PROVIDER": value}).provider


def _existing_key(
    settings: ProviderSettings, saved: dict[str, str] | None,
) -> tuple[str | None, str]:
    """Only reuse a credential explicitly associated with this exact route."""
    key_name = provider_key_name(settings.provider)
    env = os.environ
    # A generic environment key has no identity unless its route is explicit.
    if "DECISION_BROWSER_PROVIDER" in env:
        try:
            route = load_provider_settings({
                name: value for name, value in env.items()
                if name in {"DECISION_BROWSER_PROVIDER", "DECISION_BROWSER_ENDPOINT"}
            })
        except ProviderConfigError:
            route = None
        if (
            route is not None and route.provider == settings.provider
            and route.endpoint == settings.endpoint and "DECISION_BROWSER_API_KEY" in env
        ):
            return env["DECISION_BROWSER_API_KEY"] or None, "the selected key from this shell"
    if (
        key_name is not None and key_name in env
        and preset_key_matches_endpoint(settings.provider, settings.endpoint)
    ):
        return env[key_name] or None, key_name + " from this shell"
    if saved is not None:
        previous = load_provider_settings(saved)
        if previous.provider == settings.provider and previous.endpoint == settings.endpoint:
            return previous.api_key, "the saved key"
    return None, ""


def _read_key(existing: str | None, source: str) -> str:
    prompt = f"API key (Enter keeps {source}): " if existing else "API key: "
    # getpass otherwise falls back to echoing input on unsupported terminals.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        value = getpass.getpass(prompt)
    return value if value else existing or ""


def configure(provider: str | None = None) -> int:
    """Persist a selected preset after all input validates; cancellation is inert."""
    if not sys.stdin.isatty():
        print("Provider setup needs an interactive terminal. Run decision-browser configure there.",
              file=sys.stderr)
        return 1
    try:
        try:
            saved = load_profile()
        except ProviderConfigError:
            saved = None
            print("The saved profile could not be read. Setup will try to replace it safely.")
        default = saved["DECISION_BROWSER_PROVIDER"] if saved else "openai"
        selected = provider or _select_provider(default)
        settings = load_provider_settings({"DECISION_BROWSER_PROVIDER": selected})
        previous = load_provider_settings(saved) if saved else None
        if previous is not None and previous.provider == settings.provider:
            settings = previous
        prepare_profile_destination(credentials=settings.provider != "local")
        print(f"Selected: {_LABELS[settings.provider]}")
        values = {
            "DECISION_BROWSER_PROVIDER": settings.provider,
            "DECISION_BROWSER_ENDPOINT": settings.endpoint,
            "DECISION_BROWSER_MODEL": settings.model,
            "DECISION_BROWSER_MAX_CHOICES": str(settings.max_choices),
        }
        if settings.provider == "local":
            print("Start your System One service first. Setup does not install or start a model.")
            print("For Ollama use http://127.0.0.1:11434/v1/systemone and a downloaded Nimble model.")
            defaults = settings
            values["DECISION_BROWSER_ENDPOINT"] = (
                input(f"Service endpoint [{defaults.endpoint}]: ").strip() or defaults.endpoint
            )
            values["DECISION_BROWSER_MODEL"] = (
                input(f"Model [{defaults.model}]: ").strip() or defaults.model
            )
            values["DECISION_BROWSER_MAX_CHOICES"] = str(defaults.max_choices)
            if values["DECISION_BROWSER_ENDPOINT"] == settings.endpoint and settings.api_key is not None:
                values["DECISION_BROWSER_API_KEY"] = settings.api_key
        else:
            print("Your key is saved outside projects in a private local file; it is not encrypted.")
            print("Setup makes no API call. Browser tasks use your provider's account and quota.")
            key, source = _existing_key(settings, saved)
            values["DECISION_BROWSER_API_KEY"] = _read_key(key, source)
        configured = load_provider_settings(values)
        if configured.provider != "local" and configured.api_key is None:
            print("An API key is required for this provider. Nothing was saved.", file=sys.stderr)
            return 1
        save_profile(configured)
        print(f"Saved provider: {configured.provider}.")
        if any(name.startswith("DECISION_BROWSER_") and name not in {
            "DECISION_BROWSER_CONFIG_FILE",
        } for name in os.environ) or (
            (key_name := provider_key_name(configured.provider)) is not None
            and key_name in os.environ
        ):
            print("Provider environment overrides may still take precedence over this profile.")
            print("Remove those overrides from the client environment to use the saved settings.")
        print("Restart your coding client, connect decision_browser and ask it to call capabilities.")
        print("Provider access and a real browser task have not been tested by setup.")
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\nProvider setup cancelled. The previous profile is unchanged.", file=sys.stderr)
        return 130
    except getpass.GetPassWarning:
        print("Hidden key input is unavailable. Use an interactive terminal; nothing was saved.",
              file=sys.stderr)
        return 1
    except ProviderConfigError as exc:
        print(_CONFIG_ERRORS.get(exc.code,
              "Cannot save the provider profile. Check its location and private file permissions."),
              file=sys.stderr)
        return 1
    except UnicodeError:
        print("Provider settings contain invalid text. Nothing was saved.", file=sys.stderr)
        return 1
    except OSError:
        print("Cannot access the provider profile. Check the user configuration directory.",
              file=sys.stderr)
        return 1
