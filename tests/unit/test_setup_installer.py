"""Interactive onboarding uses disposable projects and never real credentials."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("setup_installer", ROOT / "scripts/install.py")
assert spec and spec.loader
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    for name in installer.ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def project_setup(tmp_path, monkeypatch):
    source = tmp_path / "source"
    skill = source / "skills/decision-browser/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("fixture skill")
    project = tmp_path / "project with spaces"
    project.mkdir()
    monkeypatch.setattr(installer, "source_id", lambda source: "f" * 20)
    monkeypatch.setattr(installer, "check_uv", lambda uv: None)
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(installer.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    prepared = []

    def prepare(source, project, runtime, uv):
        runtime.mkdir(parents=True)
        (runtime / "retained").write_text("runtime")
        prepared.append(runtime)

    monkeypatch.setattr(installer, "prepare", prepare)
    return source, project, prepared


@pytest.mark.parametrize("client", [None, "codex"])
def test_setup_without_terminal_stops_before_work(project_setup, monkeypatch, capsys, client):
    source, project, prepared = project_setup
    monkeypatch.setattr(installer.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    args = ["--setup", "--source", str(source), "--project", str(project)]
    if client:
        args += ["--client", client]
    assert installer.main(args) == 1
    assert "requires a terminal" in capsys.readouterr().err
    assert not prepared and not list(project.iterdir())


def test_direct_setup_checks_terminal_before_changes(project_setup, monkeypatch):
    source, project, prepared = project_setup
    monkeypatch.setattr(installer.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    with pytest.raises(installer.InstallError, match="requires a terminal"):
        installer.install(source, project, ["codex"], setup=True)
    assert not prepared and not list(project.iterdir())


@pytest.mark.parametrize("answer,clients", [("1", ["codex"]), ("2", ["claude"]),
                                            ("3", ["codex", "claude"]),
                                            (" Claude ", ["claude"])])
def test_setup_selects_client_then_configures_committed_runtime(
    project_setup, monkeypatch, capsys, answer, clients,
):
    source, project, prepared = project_setup
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    monkeypatch.setenv("OPENAI_API_KEY", "CANARY-KEY")
    calls = []

    def run(command, **kwargs):
        assert (project / installer.STATE / "manifest.json").is_file()
        assert not (project / installer.STATE / "install.lock").exists()
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(installer.subprocess, "run", run)
    assert installer.main(["--setup", "--source", str(source), "--project", str(project)]) == 0
    assert len(prepared) == len(calls) == 1
    command, kwargs = calls[0]
    suffix = "Scripts/python.exe" if installer.os.name == "nt" else "bin/python"
    assert command == [str(prepared[0] / "venv" / suffix), "-I", "-m",
                       "decision_browser.cli", "configure"]
    assert kwargs == {"cwd": project, "check": False, "shell": False}
    assert "CANARY-KEY" not in repr(calls)
    state = json.loads((project / installer.STATE / "manifest.json").read_text())
    assert set(state["clients"]) == set(clients)
    captured = capsys.readouterr()
    assert "CANARY-KEY" not in captured.out + captured.err
    assert "entitlement" in captured.out and "NOT_RUN" in captured.out


def test_explicit_client_skips_client_prompt(project_setup, monkeypatch):
    source, project, _ = project_setup
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("client prompt"))
    monkeypatch.setattr(installer, "configure_runtime", lambda *args: 0)
    assert installer.main(["--setup", "codex", "--source", str(source),
                           "--project", str(project)]) == 0


@pytest.mark.parametrize("answer", ["", "4", "CANARY-INVALID"])
def test_invalid_selection_never_prepares_or_echoes_input(
    project_setup, monkeypatch, capsys, answer,
):
    source, project, prepared = project_setup
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    assert installer.main(["--setup", "--source", str(source), "--project", str(project)]) == 1
    assert not prepared and not list(project.iterdir())
    assert "CANARY-INVALID" not in capsys.readouterr().err


@pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt])
def test_cancelled_selection_leaves_project_untouched(project_setup, monkeypatch, error):
    source, project, prepared = project_setup

    def cancel(prompt):
        raise error

    monkeypatch.setattr("builtins.input", cancel)
    assert installer.main(["--setup", "--source", str(source), "--project", str(project)]) == 130
    assert not prepared and not list(project.iterdir())


@pytest.mark.parametrize("status,expected", [(1, 1), (130, 130), (-2, 130), (-15, 1)])
def test_unfinished_provider_keeps_installation_and_reports_resume(
    project_setup, monkeypatch, capsys, status, expected,
):
    source, project, prepared = project_setup
    monkeypatch.setattr(installer.subprocess, "run",
                        lambda command, **kwargs: subprocess.CompletedProcess(command, status))
    assert installer.main(["--setup", "codex", "--source", str(source),
                           "--project", str(project)]) == expected
    assert (prepared[0] / "retained").is_file()
    assert (project / ".codex/config.toml").is_file()
    assert not (project / installer.STATE / "install.lock").exists()
    error = capsys.readouterr().err
    assert "Installation retained" in error
    assert "decision_browser.cli configure" in error


@pytest.mark.parametrize("error,expected", [(OSError("CANARY-PRIVATE"), 1), (KeyboardInterrupt(), 130)])
def test_configure_launch_failure_is_resumable_and_sanitized(tmp_path, monkeypatch, capsys, error, expected):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(installer.subprocess, "run", fail)
    assert installer.configure_runtime(tmp_path, tmp_path) == expected
    captured = capsys.readouterr()
    assert "Resume with" in captured.err
    assert "CANARY-PRIVATE" not in captured.out + captured.err


def test_setup_cannot_uninstall(project_setup):
    source, project, prepared = project_setup
    with pytest.raises(SystemExit) as caught:
        installer.main(["--setup", "codex", "--uninstall", "--source", str(source),
                        "--project", str(project)])
    assert caught.value.code == 2
    assert not prepared and not list(project.iterdir())


def test_plain_install_stays_unattended(project_setup, monkeypatch):
    source, project, prepared = project_setup
    monkeypatch.setattr(installer.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(installer, "configure_runtime", lambda *args: pytest.fail("configure called"))
    assert installer.main(["codex", "--source", str(source), "--project", str(project)]) == 0
    assert len(prepared) == 1


def client_environment(project, client):
    if client == "codex":
        return tomllib.loads((project / ".codex/config.toml").read_text())["mcp_servers"][installer.SERVER]
    return json.loads((project / ".mcp.json").read_text())["mcpServers"][installer.SERVER]


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_explicit_profile_path_survives_client_restart_and_upgrade(project_setup, monkeypatch, client):
    source, project, _ = project_setup
    profile = str(project.parent / "private profiles/API_KEY_profile.json")
    monkeypatch.setenv("DECISION_BROWSER_CONFIG_FILE", profile)
    assert "DECISION_BROWSER_CONFIG_FILE" not in installer.clean_environment()
    installer.install(source, project, [client])
    configured = client_environment(project, client)
    assert configured["env"]["DECISION_BROWSER_CONFIG_FILE"] == profile
    assert "DECISION_BROWSER_CONFIG_FILE" in installer.PROVIDER_SETTINGS
    if client == "codex":
        assert "DECISION_BROWSER_CONFIG_FILE" in configured["env_vars"]
    monkeypatch.delenv("DECISION_BROWSER_CONFIG_FILE")
    installer.install(source, project, [client], upgrade=True)
    assert client_environment(project, client)["env"]["DECISION_BROWSER_CONFIG_FILE"] == profile
    installer.install(source, project, [client], uninstall=True)


@pytest.mark.parametrize("value", ["", "relative.json", "/tmp/../CANARY", "/tmp/CANARY\nprivate"])
def test_invalid_profile_path_rejected_before_preparation(project_setup, monkeypatch, capsys, value):
    source, project, prepared = project_setup
    monkeypatch.setenv("DECISION_BROWSER_CONFIG_FILE", value)
    assert installer.main(["codex", "--source", str(source), "--project", str(project)]) == 1
    assert not prepared and not list(project.iterdir())
    error = capsys.readouterr().err
    assert "Invalid provider profile path" in error and "CANARY" not in error


def write_client_environment(project, client, env):
    if client == "codex":
        target = project / ".codex/config.toml"
        target.parent.mkdir(exist_ok=True)
        target.write_text(installer.codex_block({"env": env}))
    else:
        (project / ".mcp.json").write_text(json.dumps({"mcpServers": {installer.SERVER: {"env": env}}}))


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_project_override_warning_names_effective_fields_without_values(tmp_path, monkeypatch, capsys, client):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"provider": "typesafe", "api_key": "CANARY-PROFILE-KEY"}))
    monkeypatch.setenv("DECISION_BROWSER_CONFIG_FILE", str(profile))
    env = {"DECISION_BROWSER_PROVIDER": "typesafe", "DECISION_BROWSER_MODEL": "CANARY-MODEL",
           "DECISION_BROWSER_ENDPOINT": "https://CANARY-ENDPOINT.example/v1/systemone",
           "DECISION_BROWSER_MAX_CHOICES": "27", "DECISION_BROWSER_API_KEY": "CANARY-GENERIC-KEY",
           "TYPESAFE_API_KEY": "CANARY-SELECTED-KEY", "OPENAI_API_KEY": "CANARY-UNRELATED-KEY",
           "DECISION_BROWSER_CONFIG_FILE": str(profile)}
    write_client_environment(tmp_path, client, env)
    installer.warn_project_overrides(tmp_path, [client])
    warning = capsys.readouterr().err
    assert f"{client} project settings" in warning
    for name in set(env) - {"OPENAI_API_KEY", "DECISION_BROWSER_CONFIG_FILE"}:
        assert name in warning
    assert "OPENAI_API_KEY" not in warning and "DECISION_BROWSER_CONFIG_FILE" not in warning
    assert "CANARY" not in warning and str(profile) not in warning
    assert "Remove those entries" in warning and "will not replace" in warning


@pytest.mark.parametrize("provider,key_name", [("typesafe", "TYPESAFE_API_KEY"), ("gemini", "OPENCODE_API_KEY")])
def test_profile_selection_determines_the_only_relevant_cloud_key(tmp_path, monkeypatch, capsys, provider, key_name):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"provider": provider}))
    monkeypatch.setenv("DECISION_BROWSER_CONFIG_FILE", str(profile))
    write_client_environment(tmp_path, "claude", {key_name: "CANARY", "OPENAI_API_KEY": "CANARY"})
    installer.warn_project_overrides(tmp_path, ["claude"])
    warning = capsys.readouterr().err
    assert key_name in warning and "OPENAI_API_KEY" not in warning
    assert "CANARY" not in warning


def test_matching_profile_without_overrides_is_silent_and_other_clients_are_ignored(tmp_path, monkeypatch, capsys):
    profile = tmp_path / "profile.json"
    monkeypatch.setenv("DECISION_BROWSER_CONFIG_FILE", str(profile))
    write_client_environment(tmp_path, "codex", {"DECISION_BROWSER_CONFIG_FILE": str(profile)})
    write_client_environment(tmp_path, "claude", {"DECISION_BROWSER_PROVIDER": "local"})
    installer.warn_project_overrides(tmp_path, ["codex"])
    assert capsys.readouterr().err == ""


def test_different_persisted_profile_has_actionable_name_only_warning(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DECISION_BROWSER_CONFIG_FILE", str(tmp_path / "wizard.json"))
    write_client_environment(tmp_path, "codex", {"DECISION_BROWSER_CONFIG_FILE": str(tmp_path / "CANARY-old.json")})
    installer.warn_project_overrides(tmp_path, ["codex"])
    warning = capsys.readouterr().err
    assert "DECISION_BROWSER_CONFIG_FILE" in warning and "CANARY" not in warning


@pytest.mark.parametrize("body", ["CANARY-invalid", '{"mcpServers": []}', "x" * 1_048_577])
def test_malformed_or_oversized_project_settings_get_bounded_warning(tmp_path, capsys, body):
    (tmp_path / ".mcp.json").write_text(body)
    installer.warn_project_overrides(tmp_path, ["claude"])
    warning = capsys.readouterr().err
    assert "could not inspect claude" in warning
    assert len(warning) < 200 and "CANARY" not in warning


def test_symlinked_project_settings_are_not_read(tmp_path, capsys):
    target = tmp_path / "private"
    target.write_text("CANARY-private")
    (tmp_path / ".mcp.json").symlink_to(target)
    installer.warn_project_overrides(tmp_path, ["claude"])
    warning = capsys.readouterr().err
    assert "could not inspect claude" in warning and "CANARY" not in warning


def test_configure_warns_after_saving_for_selected_clients(tmp_path, monkeypatch, capsys):
    def run(command, **kwargs):
        write_client_environment(tmp_path, "claude", {"DECISION_BROWSER_PROVIDER": "local"})
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(installer.subprocess, "run", run)
    assert installer.configure_runtime(tmp_path, tmp_path, ["claude"]) == 0
    assert "claude project settings" in capsys.readouterr().err
