"""The first-run journey never uses a real home, key, client or provider."""

from __future__ import annotations

import getpass
import os
import socket
import warnings

import pytest

from decision_browser import cli, onboarding
from decision_browser.provider_config import load_provider_settings, provider_names
from decision_browser.user_config import load_profile, profile_path, save_profile

CANARY = "onboarding-placeholder-secret"


@pytest.fixture(autouse=True)
def isolated_setup(tmp_path, monkeypatch, capsys):
    for name in list(os.environ):
        if name.startswith("DECISION_BROWSER_") or name.endswith("API_KEY"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("DECISION_BROWSER_CONFIG_FILE", str(tmp_path / "config/provider.json"))
    monkeypatch.setattr(onboarding.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(onboarding.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("network during setup"))


@pytest.mark.parametrize("provider", ["openai", "typesafe", "openrouter", "opencode", "gemini"])
def test_hosted_provider_persists_without_exposing_key(provider, monkeypatch, capsys):
    monkeypatch.setattr(getpass, "getpass", lambda prompt: CANARY)
    assert onboarding.configure(provider) == 0
    settings = load_provider_settings()
    assert settings.provider == provider
    assert settings.api_key == CANARY
    output = capsys.readouterr()
    assert CANARY not in output.out + output.err
    assert "not encrypted" in output.out
    assert "have not been tested" in output.out


def test_menu_selection_and_alias(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    monkeypatch.setattr(getpass, "getpass", lambda prompt: CANARY)
    assert cli.main(["configure"]) == 0
    assert load_provider_settings().provider == "typesafe"
    assert cli.main(["configure", "jev"]) == 0


def test_gemini_menu_selection_keeps_selected_opencode_key(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: str(provider_names().index("gemini") + 1))
    monkeypatch.setenv("OPENCODE_API_KEY", CANARY)
    prompts = []
    monkeypatch.setattr(getpass, "getpass", lambda prompt: prompts.append(prompt) or "")
    assert cli.main(["configure"]) == 0
    monkeypatch.delenv("OPENCODE_API_KEY")
    settings = load_provider_settings()
    assert (settings.provider, settings.model, settings.api_key) == ("gemini", "gemini-decision-1", CANARY)
    assert "OPENCODE_API_KEY" in prompts[0]
    output = capsys.readouterr()
    assert "Gemini Decision 1" in output.out
    assert CANARY not in output.out + output.err


def test_existing_profile_key_can_be_kept(monkeypatch):
    save_profile(load_provider_settings({
        "DECISION_BROWSER_PROVIDER": "typesafe", "TYPESAFE_API_KEY": CANARY,
    }))
    prompts = []
    monkeypatch.setattr(getpass, "getpass", lambda prompt: prompts.append(prompt) or "")
    assert onboarding.configure("typesafe") == 0
    assert load_provider_settings().api_key == CANARY
    assert "saved key" in prompts[0] and CANARY not in prompts[0]


def test_switch_does_not_reuse_other_provider_key(monkeypatch):
    save_profile(load_provider_settings({
        "DECISION_BROWSER_PROVIDER": "typesafe", "TYPESAFE_API_KEY": CANARY,
    }))
    before = profile_path().read_bytes()
    monkeypatch.setenv("OPENAI_API_KEY", "other-placeholder-key")
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "")
    assert onboarding.configure("openrouter") == 1
    assert profile_path().read_bytes() == before


def test_same_provider_keeps_its_exact_saved_custom_route(monkeypatch):
    save_profile(load_provider_settings({
        "DECISION_BROWSER_PROVIDER": "typesafe", "DECISION_BROWSER_API_KEY": CANARY,
        "DECISION_BROWSER_ENDPOINT": "https://example.test/v1/systemone",
    }))
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "")
    assert onboarding.configure("typesafe") == 0
    settings = load_provider_settings()
    assert settings.endpoint == "https://example.test/v1/systemone"
    assert settings.api_key == CANARY


def test_selected_environment_key_can_be_saved(monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", CANARY)
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "")
    assert onboarding.configure("openrouter") == 0
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert load_provider_settings().api_key == CANARY
    output = capsys.readouterr()
    assert CANARY not in output.out + output.err
    assert "overrides" in output.out


@pytest.mark.parametrize("route", [None, "openai"])
def test_generic_environment_key_is_not_borrowed_for_another_route(route, monkeypatch):
    monkeypatch.setenv("DECISION_BROWSER_API_KEY", CANARY)
    if route:
        monkeypatch.setenv("DECISION_BROWSER_PROVIDER", route)
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "")
    assert onboarding.configure("typesafe") == 1
    assert not profile_path().exists()


def test_empty_selected_environment_key_blocks_saved_key_reuse(monkeypatch):
    save_profile(load_provider_settings({
        "DECISION_BROWSER_PROVIDER": "typesafe", "TYPESAFE_API_KEY": CANARY,
    }))
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "")
    assert onboarding.configure("typesafe") == 1


def test_local_service_never_asks_for_a_key(monkeypatch):
    values = iter(["http://127.0.0.1:11434/v1/systemone", "nimble:latest"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(values))
    monkeypatch.setattr(getpass, "getpass", lambda *a: pytest.fail("local key prompt"))
    monkeypatch.setenv("OPENAI_API_KEY", CANARY)
    assert onboarding.configure("local") == 0
    settings = load_provider_settings()
    assert settings.endpoint == "http://127.0.0.1:11434/v1/systemone"
    assert settings.model == "nimble:latest" and settings.api_key is None
    assert CANARY not in profile_path().read_text()


@pytest.mark.parametrize("failure", [EOFError, KeyboardInterrupt])
def test_cancel_preserves_previous_profile(failure, monkeypatch, capsys):
    save_profile(load_provider_settings({"DECISION_BROWSER_PROVIDER": "local"}))
    before = profile_path().read_bytes()

    def cancel(prompt):
        raise failure

    monkeypatch.setattr(getpass, "getpass", cancel)
    assert onboarding.configure("openai") == 130
    assert profile_path().read_bytes() == before
    assert "unchanged" in capsys.readouterr().err


def test_noninteractive_does_not_read_or_write_profile(monkeypatch, capsys):
    monkeypatch.setattr(onboarding.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(onboarding, "load_profile", lambda: pytest.fail("read without terminal"))
    assert cli.main(["configure", "openai"]) == 1
    assert "interactive terminal" in capsys.readouterr().err
    assert not profile_path().exists()


def test_hidden_input_never_falls_back_to_echo(monkeypatch, capsys):
    def unavailable(prompt):
        warnings.warn("placeholder-warning", getpass.GetPassWarning)
        pytest.fail("must not fall back to echoed input")

    monkeypatch.setattr(getpass, "getpass", unavailable)
    assert onboarding.configure("openai") == 1
    assert "Hidden key input" in capsys.readouterr().err
    assert not profile_path().exists()


def test_invalid_key_is_sanitized_and_not_saved(monkeypatch, capsys):
    monkeypatch.setattr(getpass, "getpass", lambda prompt: CANARY + "\ninvalid")
    assert onboarding.configure("openai") == 1
    output = capsys.readouterr()
    assert CANARY not in output.out + output.err
    assert not profile_path().exists()


def test_invalid_selection_is_sanitized(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: CANARY)
    assert onboarding.configure() == 1
    output = capsys.readouterr()
    assert CANARY not in output.out + output.err
    assert load_profile() is None


def test_setup_dispatch_and_uninstall_rejection(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_install", lambda *args, **kw: calls.append((args, kw)) or 7)
    assert cli.main(["setup"]) == 7
    assert calls == [((None, "."), {"upgrade": False, "uninstall": False, "setup": True})]
    assert cli.main(["setup", "both", "--upgrade", "--project", "/project with spaces"]) == 7
    assert calls[-1] == (("both", "/project with spaces"), {
        "upgrade": True, "uninstall": False, "setup": True,
    })
    with pytest.raises(SystemExit) as caught:
        cli.main(["setup", "--uninstall"])
    assert caught.value.code == 2
