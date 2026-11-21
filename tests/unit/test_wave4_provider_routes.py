"""Route-bound credentials and local configuration fail closed without network."""

from __future__ import annotations

import getpass
import json
import os
import socket
import sys

import pytest

from decision_browser import cli, onboarding, user_config
from decision_browser.provider_config import (
    ProviderConfigError,
    load_provider_settings,
    provider_status,
)

SECRET = "wave4-synthetic-route-secret"
CUSTOM = "https://custom.example.test/v1/systemone"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("DECISION_BROWSER_") or name.endswith("API_KEY"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("DECISION_BROWSER_CONFIG_FILE", str(tmp_path / "private/provider.json"))
    monkeypatch.setattr(onboarding.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(socket.socket, "connect", lambda *_a, **_k: pytest.fail("configuration network"))


@pytest.mark.parametrize("provider,key_name", [
    ("typesafe", "TYPESAFE_API_KEY"), ("openrouter", "OPENROUTER_API_KEY"),
    ("opencode", "OPENCODE_API_KEY"), ("gemini", "OPENCODE_API_KEY"),
])
def test_named_credential_cannot_follow_custom_host(provider, key_name):
    env = {"DECISION_BROWSER_PROVIDER": provider, "DECISION_BROWSER_ENDPOINT": CUSTOM,
           key_name: SECRET}
    with pytest.raises(ProviderConfigError, match="credential_route_mismatch"):
        load_provider_settings(env)
    status = provider_status(env)
    assert status["provider_ready"] is False
    assert status["provider_error"] == "credential_route_mismatch"
    assert SECRET not in json.dumps(status) and CUSTOM not in json.dumps(status)


@pytest.mark.parametrize("provider,key_name", [
    ("typesafe", "TYPESAFE_API_KEY"), ("openrouter", "OPENROUTER_API_KEY"),
    ("opencode", "OPENCODE_API_KEY"), ("gemini", "OPENCODE_API_KEY"),
])
def test_production_factory_blocks_unbound_key_before_client_creation(provider, key_name, monkeypatch):
    from decision_browser import server
    from decision_browser.runner import RunnerError

    calls = []
    sentinel = object()
    monkeypatch.setattr(server, "SystemOneProvider", lambda **kw: calls.append(kw) or sentinel)
    monkeypatch.setenv("DECISION_BROWSER_PROVIDER", provider)
    monkeypatch.setenv("DECISION_BROWSER_ENDPOINT", CUSTOM)
    monkeypatch.setenv(key_name, SECRET)
    with pytest.raises(RunnerError, match="credential_route_mismatch"):
        server.production_factory()
    assert calls == []
    monkeypatch.setenv("DECISION_BROWSER_API_KEY", "explicit-route-key")
    assert server.production_factory() is sentinel
    assert calls[0]["endpoint"] == CUSTOM and calls[0]["api_key"] == "explicit-route-key"


@pytest.mark.parametrize("key", [SECRET, ""], ids=["explicit-key", "explicit-empty"])
def test_explicit_generic_custom_host_choice_preserves_empty_key_rule(key):
    settings = load_provider_settings({
        "DECISION_BROWSER_PROVIDER": "typesafe", "DECISION_BROWSER_ENDPOINT": CUSTOM,
        "TYPESAFE_API_KEY": "unbound-preset-secret", "DECISION_BROWSER_API_KEY": key,
    })
    assert settings.endpoint == CUSTOM and settings.api_key == (key or None)


@pytest.mark.parametrize("endpoint", [
    "https://api.typesafe.ai:443/custom/systemone", "https://API.TYPESAFE.AI/custom/systemone",
])
def test_same_default_origin_keeps_named_credential(endpoint):
    settings = load_provider_settings({"DECISION_BROWSER_PROVIDER": "typesafe",
                                       "DECISION_BROWSER_ENDPOINT": endpoint,
                                       "TYPESAFE_API_KEY": SECRET})
    assert settings.api_key == SECRET and settings.endpoint == endpoint


@pytest.mark.parametrize("provider", ["typesafe", "openrouter", "opencode", "gemini"])
@pytest.mark.skipif(os.name == "nt", reason="POSIX private credential profile fixture")
def test_same_provider_rotation_keeps_bound_custom_route(provider, monkeypatch, capsys):
    before = load_provider_settings({
        "DECISION_BROWSER_PROVIDER": provider, "DECISION_BROWSER_ENDPOINT": CUSTOM,
        "DECISION_BROWSER_MODEL": "custom-model", "DECISION_BROWSER_MAX_CHOICES": "63",
        "DECISION_BROWSER_API_KEY": SECRET,
    })
    user_config.save_profile(before)
    prompts = []
    monkeypatch.setattr(getpass, "getpass", lambda prompt: prompts.append(prompt) or "replacement-key")
    assert onboarding.configure(provider) == 0
    after = load_provider_settings()
    assert (after.provider, after.endpoint, after.model, after.max_choices) == (
        before.provider, before.endpoint, before.model, before.max_choices,
    )
    assert after.api_key == "replacement-key"
    assert "saved key" in prompts[0]
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err and "replacement-key" not in output.out + output.err


@pytest.mark.skipif(os.name == "nt", reason="POSIX private credential profile fixture")
def test_custom_profile_does_not_silently_receive_named_environment_key(monkeypatch):
    user_config.save_profile(load_provider_settings({
        "DECISION_BROWSER_PROVIDER": "typesafe", "DECISION_BROWSER_ENDPOINT": CUSTOM,
        "DECISION_BROWSER_API_KEY": SECRET,
    }))
    monkeypatch.setenv("TYPESAFE_API_KEY", "new-unbound-key")
    with pytest.raises(ProviderConfigError, match="credential_route_mismatch"):
        load_provider_settings()


@pytest.mark.skipif(os.name == "nt", reason="POSIX private credential profile fixture")
def test_custom_rotation_does_not_offer_unbound_named_environment_key(monkeypatch):
    user_config.save_profile(load_provider_settings({
        "DECISION_BROWSER_PROVIDER": "typesafe", "DECISION_BROWSER_ENDPOINT": CUSTOM,
        "DECISION_BROWSER_API_KEY": SECRET,
    }))
    monkeypatch.setenv("TYPESAFE_API_KEY", "new-unbound-key")
    prompts = []
    monkeypatch.setattr(getpass, "getpass", lambda prompt: prompts.append(prompt) or "")
    assert onboarding.configure("typesafe") == 0
    monkeypatch.delenv("TYPESAFE_API_KEY")
    assert load_provider_settings().api_key == SECRET
    assert "saved key" in prompts[0] and "TYPESAFE_API_KEY" not in prompts[0]


@pytest.mark.skipif(os.name == "nt", reason="POSIX private credential profile fixture")
def test_explicit_authenticated_local_route_is_preserved():
    settings = load_provider_settings({"DECISION_BROWSER_PROVIDER": "local",
                                       "DECISION_BROWSER_ENDPOINT": "http://127.0.0.1:9123/v1/systemone",
                                       "DECISION_BROWSER_API_KEY": SECRET})
    assert settings.api_key == SECRET
    user_config.save_profile(settings)
    assert load_provider_settings() == settings


@pytest.mark.parametrize("endpoint,expected_key", [
    ("http://127.0.0.1:9123/v1/systemone", SECRET),
    ("http://127.0.0.1:9124/v1/systemone", None),
], ids=["same-endpoint-keeps-key", "changed-endpoint-clears-key"])
@pytest.mark.skipif(os.name == "nt", reason="POSIX private credential profile fixture")
def test_local_configuration_reuses_key_only_for_exact_saved_endpoint(endpoint, expected_key, monkeypatch):
    user_config.save_profile(load_provider_settings({
        "DECISION_BROWSER_PROVIDER": "local",
        "DECISION_BROWSER_ENDPOINT": "http://127.0.0.1:9123/v1/systemone",
        "DECISION_BROWSER_API_KEY": SECRET,
    }))
    inputs = iter([endpoint, ""])
    monkeypatch.setattr("builtins.input", lambda _p: next(inputs))
    monkeypatch.setattr(getpass, "getpass", lambda _p: pytest.fail("local secret prompt"))
    assert onboarding.configure("local") == 0
    after = load_provider_settings()
    assert after.endpoint == endpoint and after.api_key == expected_key


def test_minimal_legacy_profile_can_use_default_origin_named_key(monkeypatch):
    path = user_config.profile_path()
    path.parent.mkdir(mode=0o700)
    path.write_text('{"version":1,"provider":"typesafe"}')
    path.chmod(0o600)
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)
    assert load_provider_settings().api_key == SECRET


@pytest.mark.parametrize("unsafe", ["symlink_file", "symlink_parent", "public_directory", "public_file", "relative"])
@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink and private permission fixtures")
def test_unsafe_destination_rejected_before_secret_prompt(unsafe, tmp_path, monkeypatch, capsys):
    path = user_config.profile_path()
    if unsafe == "relative":
        monkeypatch.setenv("DECISION_BROWSER_CONFIG_FILE", "relative/profile.json")
    elif unsafe == "symlink_parent":
        other = tmp_path / "other"
        other.mkdir(mode=0o700)
        path.parent.symlink_to(other, target_is_directory=True)
    else:
        path.parent.mkdir(mode=0o700)
        if unsafe == "public_directory":
            path.parent.chmod(0o755)
        elif unsafe == "public_file":
            path.write_text("previous")
            path.chmod(0o644)
        else:
            other = tmp_path / "other.json"
            other.write_text("untouched")
            path.symlink_to(other)
    prompts = []
    monkeypatch.setattr(getpass, "getpass", lambda p: prompts.append(p) or SECRET)
    assert onboarding.configure("typesafe") == 1
    assert prompts == []
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err


@pytest.mark.skipif(os.name == "nt", reason="POSIX private credential profile and symlink fixture")
def test_save_destination_revalidated_after_prompt(tmp_path, monkeypatch):
    path = user_config.profile_path()
    user_config.save_profile(load_provider_settings({"DECISION_BROWSER_PROVIDER": "typesafe",
                                                    "DECISION_BROWSER_API_KEY": SECRET}))
    original = path.read_bytes()
    other = tmp_path / "foreign.json"
    other.write_bytes(b"foreign")

    def replace_destination(_prompt):
        path.unlink()
        path.symlink_to(other)
        return "replacement-key"

    monkeypatch.setattr(getpass, "getpass", replace_destination)
    assert onboarding.configure("typesafe") == 1
    assert path.is_symlink() and other.read_bytes() == b"foreign"
    assert SECRET.encode() in original


def test_unicode_error_direct_configure_is_sanitized(monkeypatch, capsys):
    def malformed(_prompt):
        raise UnicodeError(SECRET)

    monkeypatch.setattr(getpass, "getpass", malformed)
    assert onboarding.configure("typesafe") == 1
    assert SECRET not in capsys.readouterr().err
    assert not user_config.profile_path().exists()


def test_unwritable_destination_is_probed_before_secret_prompt(monkeypatch, capsys):
    def denied(*_args, **_kwargs):
        raise PermissionError(SECRET)

    monkeypatch.setattr(user_config.tempfile, "mkstemp", denied)
    monkeypatch.setattr(getpass, "getpass", lambda _p: pytest.fail("secret before write probe"))
    assert onboarding.configure("typesafe") == 1
    assert SECRET not in capsys.readouterr().err


def test_windows_credential_preflight_rejects_before_secret_prompt(monkeypatch, capsys):
    monkeypatch.setattr(onboarding, "load_profile", lambda: None)
    monkeypatch.setattr(getpass, "getpass", lambda _p: pytest.fail("unsupported secret prompt"))
    with monkeypatch.context() as patch:
        patch.setattr(user_config.os, "name", "nt")
        assert onboarding.configure("typesafe") == 1
    assert "not supported" in capsys.readouterr().err


def _project_config(tmp_path, monkeypatch, raw):
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / ".codex"
    directory.mkdir(mode=0o700)
    config = directory / "config.toml"
    config.write_bytes(raw)
    config.chmod(0o600)
    return config


@pytest.mark.parametrize("raw", [
    b"bad = [", b"\xff", b"x" * 65_537, b"a = " + b"[" * 1_500 + b"]" * 1_500,
], ids=["malformed", "non-utf8", "oversized", "too-deep"])
def test_implicit_policy_parse_failure_has_bounded_fixed_reason(raw, tmp_path, monkeypatch):
    _project_config(tmp_path, monkeypatch, raw)
    with pytest.raises(ValueError) as caught:
        cli._project_admission_mode()
    assert str(caught.value) in {"project_policy_invalid", "project_policy_unreadable"}
    assert len(str(caught.value)) < 64


@pytest.mark.parametrize("kind", ["file_link", "parent_link", "writable"])
@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink and write-permission policy boundary")
def test_implicit_policy_rejects_untrusted_file_boundary(kind, tmp_path, monkeypatch):
    config = _project_config(tmp_path, monkeypatch, b"unrelated = 1")
    if kind == "file_link":
        other = tmp_path / "foreign.toml"
        other.write_bytes(config.read_bytes())
        config.unlink()
        config.symlink_to(other)
    elif kind == "parent_link":
        old = tmp_path / "real-codex"
        config.parent.rename(old)
        (tmp_path / ".codex").symlink_to(old, target_is_directory=True)
    else:
        config.chmod(0o666)
    with pytest.raises(ValueError, match="project_policy_unreadable"):
        cli._project_admission_mode()


@pytest.mark.parametrize("admission", ["standard_form", "task_scope"])
def test_trusted_exact_launcher_reports_policy_source(admission, tmp_path, monkeypatch, capsys):
    raw = ('[mcp_servers.decision_browser]\ncommand = ' + json.dumps(sys.executable)
           + '\ncwd = ' + json.dumps(str(tmp_path)) + '\nargs = ["-I", "-m", '
           '"decision_browser.keychain_entry", "--admission", ' + json.dumps(admission) + ']\n')
    _project_config(tmp_path, monkeypatch, raw.encode())
    assert cli._project_admission_mode() == admission
    assert "admission_policy_source: project_launcher" in capsys.readouterr().err


def test_explicit_admission_bypasses_malformed_project_config(tmp_path, monkeypatch, capsys):
    _project_config(tmp_path, monkeypatch, b"malformed = [")
    calls = []
    monkeypatch.setattr(cli, "_serve", lambda manager, mode: calls.append(mode) or 0)
    assert cli.main(["serve", "--admission", "standard_form"]) == 0
    assert calls == ["standard_form"]
    assert "admission_policy_source: explicit_cli" in capsys.readouterr().err


def test_missing_policy_is_explicit_default_source(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli._project_admission_mode() == "standard_form"
    assert "admission_policy_source: default_standard_form" in capsys.readouterr().err


def test_cli_policy_error_is_fixed_and_never_starts_server(tmp_path, monkeypatch, capsys):
    _project_config(tmp_path, monkeypatch, ("broken = [" + SECRET).encode())
    monkeypatch.setattr(cli, "_serve", lambda *_a: pytest.fail("untrusted policy started server"))
    assert cli.main(["serve"]) == 1
    output = capsys.readouterr()
    assert "error: project_policy_invalid" in output.err
    assert SECRET not in output.out + output.err
