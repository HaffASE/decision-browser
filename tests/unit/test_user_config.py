"""User-owned profiles resolve consistently without leaking credentials across routes."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from decision_browser import user_config
from decision_browser.provider_config import (
    ProviderConfigError,
    load_provider_settings,
    provider_names,
    provider_status,
)

CANARY = "profile-canary-secret"


@pytest.fixture
def profile(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("DECISION_BROWSER_") or name in {
            "OPENAI_API_KEY", "TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "OPENCODE_API_KEY",
        }:
            monkeypatch.delenv(name)
    path = tmp_path / "settings" / "provider.json"
    monkeypatch.setenv("DECISION_BROWSER_CONFIG_FILE", str(path))
    return path


def settings(provider="typesafe", key=CANARY):
    return load_provider_settings({"DECISION_BROWSER_PROVIDER": provider, "DECISION_BROWSER_API_KEY": key})


def test_private_atomic_profile_survives_restart(profile):
    config = settings()
    assert user_config.save_profile(config) == profile
    assert profile.stat().st_mode & 0o777 == 0o600
    assert profile.parent.stat().st_mode & 0o777 == 0o700
    assert load_provider_settings() == config
    script = (
        "import json; from decision_browser.provider_config import provider_status; "
        "print(json.dumps(provider_status()))"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    report = json.loads(result.stdout)
    assert report["provider"] == "typesafe" and report["provider_ready"] is True
    assert CANARY not in result.stdout + result.stderr + repr(config)


def test_missing_profile_and_explicit_mapping_stay_backwards_compatible(profile):
    assert user_config.load_profile() is None
    assert load_provider_settings().provider == "openai"
    user_config.save_profile(settings())
    assert load_provider_settings({}).provider == "openai"
    assert provider_status({})["provider_error"] == "missing_api_key"


@pytest.mark.parametrize("provider", provider_names())
def test_explicit_provider_bypasses_saved_route_even_same_provider(profile, monkeypatch, provider):
    user_config.save_profile(settings())
    monkeypatch.setenv("DECISION_BROWSER_PROVIDER", provider)
    actual = load_provider_settings()
    assert actual.provider == provider and actual.api_key is None
    assert provider_status()["provider"] == provider


def test_explicit_endpoint_cannot_receive_saved_key(profile, monkeypatch):
    user_config.save_profile(settings("openai"))
    monkeypatch.setenv("DECISION_BROWSER_ENDPOINT", "https://api.openai.com/v1/decisions")
    assert load_provider_settings().api_key is None


@pytest.mark.parametrize("name,value", [
    ("DECISION_BROWSER_API_KEY", "explicit"), ("DECISION_BROWSER_API_KEY", ""),
    ("TYPESAFE_API_KEY", "explicit"), ("TYPESAFE_API_KEY", ""),
])
def test_selected_explicit_credentials_override_profile_including_empty(profile, monkeypatch, name, value):
    user_config.save_profile(settings())
    monkeypatch.setenv(name, value)
    assert load_provider_settings().api_key == (value or None)


def test_generic_key_wins_over_selected_key(profile, monkeypatch):
    user_config.save_profile(settings())
    monkeypatch.setenv("DECISION_BROWSER_API_KEY", "")
    monkeypatch.setenv("TYPESAFE_API_KEY", "other")
    assert load_provider_settings().api_key is None


def test_unrelated_key_cannot_override_profile_and_local_never_borrows_it(profile, monkeypatch):
    user_config.save_profile(settings())
    monkeypatch.setenv("OPENAI_API_KEY", "other")
    assert load_provider_settings().api_key == CANARY
    user_config.save_profile(settings("local", ""))
    assert load_provider_settings().provider == "local"
    assert load_provider_settings().api_key is None


def test_model_and_choice_overrides_preserve_selected_profile(profile, monkeypatch):
    user_config.save_profile(settings())
    monkeypatch.setenv("DECISION_BROWSER_MODEL", "custom-model")
    monkeypatch.setenv("DECISION_BROWSER_MAX_CHOICES", "64")
    config = load_provider_settings()
    assert (config.provider, config.model, config.max_choices, config.api_key) == (
        "typesafe", "custom-model", 64, CANARY,
    )


def test_corrupt_profile_blocks_status_but_explicit_route_bypasses_it(profile, monkeypatch):
    user_config.save_profile(settings())
    profile.write_text(CANARY)
    with pytest.raises(ProviderConfigError):
        load_provider_settings()
    status = provider_status()
    assert status["provider_ready"] is False
    assert status["provider_error"] == "profile_unreadable"
    assert CANARY not in json.dumps(status)
    monkeypatch.setenv("DECISION_BROWSER_PROVIDER", "local")
    assert load_provider_settings().provider == "local"


@pytest.mark.parametrize("raw", [
    '[]', '{"version":true,"provider":"local"}', '{"version":2,"provider":"local"}',
    '{"version":1,"provider":"unknown"}', '{"version":1,"provider":"local","extra":1}',
    '{"version":1,"provider":"local","max_choices":true}',
    '{"version":1,"provider":"local","max_choices":"26"}',
    '{"version":1,"provider":"local","api_key":123}',
    '{"version":1,"provider":"local","provider":"typesafe"}',
    '{"version":1,"provider":"local","endpoint":"https://remote.test/v1/systemone"}',
    '[' * 1_500 + ']' * 1_500,
    'x' * 16_385,
])
def test_invalid_schema_and_oversized_profiles_fail_closed(profile, raw):
    user_config.save_profile(settings())
    profile.write_text(raw)
    with pytest.raises(ProviderConfigError) as caught:
        user_config.load_profile()
    assert str(caught.value) in {"invalid_profile", "profile_unreadable"}


@pytest.mark.parametrize("target", ["file", "directory"])
def test_broad_permissions_are_rejected_for_reads_and_writes(profile, target):
    user_config.save_profile(settings())
    (profile if target == "file" else profile.parent).chmod(0o644 if target == "file" else 0o755)
    for operation in (user_config.load_profile, lambda: user_config.save_profile(settings())):
        with pytest.raises(ProviderConfigError, match="unsafe_profile_permissions"):
            operation()


def test_symlink_file_is_never_followed(profile, tmp_path):
    user_config.save_profile(settings())
    other = tmp_path / "elsewhere.json"
    other.write_text("untouched")
    profile.unlink()
    profile.symlink_to(other)
    for operation in (user_config.load_profile, lambda: user_config.save_profile(settings())):
        with pytest.raises(ProviderConfigError, match="unsafe_profile_path"):
            operation()
    assert other.read_text() == "untouched"


def test_symlink_parent_is_never_followed(profile, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir(mode=0o700)
    profile.parent.symlink_to(other, target_is_directory=True)
    assert user_config.load_profile() is None
    with pytest.raises(ProviderConfigError, match="unsafe_profile_path"):
        user_config.save_profile(settings())
    assert not list(other.iterdir())


def test_directory_instead_of_file_rejected(profile):
    profile.mkdir(parents=True)
    with pytest.raises(ProviderConfigError, match="unsafe_profile_path"):
        user_config.load_profile()


def test_failed_replace_preserves_previous_profile_and_removes_temporary(profile, monkeypatch):
    user_config.save_profile(settings())
    before = profile.read_bytes()

    def fail(*_args):
        raise OSError(CANARY)

    monkeypatch.setattr(user_config.os, "replace", fail)
    with pytest.raises(ProviderConfigError, match="profile_write_failed") as caught:
        user_config.save_profile(settings("openrouter"))
    assert CANARY not in str(caught.value)
    assert profile.read_bytes() == before
    assert list(profile.parent.iterdir()) == [profile]


def test_profile_path_default_and_absolute_override(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert user_config.profile_path({}) == tmp_path / ".config/decision-browser/provider.json"
    for value in ("", "relative.json", str(tmp_path / "../escape.json"), "\x00"):
        with pytest.raises(ProviderConfigError, match="invalid_profile_path"):
            user_config.profile_path({"DECISION_BROWSER_CONFIG_FILE": value})


def test_unsafe_owner_is_rejected(profile, monkeypatch):
    user_config.save_profile(settings())
    monkeypatch.setattr(user_config.os, "getuid", lambda: profile.stat().st_uid + 1)
    with pytest.raises(ProviderConfigError, match="unsafe_profile_permissions"):
        user_config.load_profile()


def test_windows_credentials_fail_before_writing(profile, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(user_config.os, "name", "nt")
        with pytest.raises(ProviderConfigError, match="profile_credentials_unsupported"):
            user_config.save_profile(settings())
    assert not profile.exists()


def test_invalid_settings_cannot_destroy_valid_profile(profile):
    user_config.save_profile(settings())
    before = profile.read_bytes()
    with pytest.raises(ProviderConfigError):
        user_config.save_profile(replace(settings(), endpoint="http://unsafe.test/v1/systemone"))
    assert profile.read_bytes() == before


def test_doctor_and_factory_use_same_profile(profile, monkeypatch):
    from decision_browser import server

    user_config.save_profile(settings())
    doctor_path = Path(__file__).resolve().parents[2] / "skills/decision-browser/scripts/doctor.py"
    spec = importlib.util.spec_from_file_location("profile_doctor", doctor_path)
    assert spec is not None and spec.loader is not None
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)
    status = doctor._provider_status()
    assert status["provider"] == "typesafe" and status["api_key_present"] is True
    calls = []
    sentinel = object()
    monkeypatch.setattr(server, "SystemOneProvider", lambda **kwargs: calls.append(kwargs) or sentinel)
    assert server.production_factory() is sentinel
    assert calls[0]["api_key"] == CANARY
    assert calls[0]["endpoint"] == settings().endpoint
    assert CANARY not in json.dumps(status)


def test_corrupt_profile_can_be_replaced_explicitly(profile):
    user_config.save_profile(settings())
    profile.write_text("not valid json")
    user_config.save_profile(settings("openrouter"))
    assert load_provider_settings().provider == "openrouter"
