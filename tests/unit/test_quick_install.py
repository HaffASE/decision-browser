"""Quick-install CLI dispatch without downloads or real client settings."""
from __future__ import annotations

from pathlib import Path

import pytest

from decision_browser import cli


@pytest.mark.parametrize("client", ["codex", "claude", "both"])
def test_install_defaults_to_current_project(monkeypatch, client):
    calls = []
    monkeypatch.setattr(cli, "_install", lambda *args, **kwargs: calls.append((args, kwargs)) or 0)
    assert cli.main(["install", client]) == 0
    assert calls == [((client, "."), {"upgrade": False, "uninstall": False})]


@pytest.mark.parametrize("option", ["--upgrade", "--uninstall"])
def test_install_options_and_exit_code(monkeypatch, option):
    calls = []
    monkeypatch.setattr(cli, "_install", lambda *args, **kwargs: calls.append((args, kwargs)) or 7)
    assert cli.main(["install", "both", "--project", '/project with spaces/é', option]) == 7
    assert calls == [(("both", '/project with spaces/é'), {
        "upgrade": option == "--upgrade", "uninstall": option == "--uninstall",
    })]


@pytest.mark.parametrize("args", [
    ["install"], ["install", "unknown"],
    ["install", "codex", "--upgrade", "--uninstall"],
    ["install", "codex", "--scope", "user"],
])
def test_invalid_options_do_not_start_setup(monkeypatch, args):
    monkeypatch.setattr(cli, "_install", lambda *a, **k: pytest.fail("setup started"))
    with pytest.raises(SystemExit) as caught:
        cli.main(args)
    assert caught.value.code == 2


def test_install_help_is_side_effect_free(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_install", lambda *a, **k: pytest.fail("setup started"))
    with pytest.raises(SystemExit) as caught:
        cli.main(["install", "--help"])
    assert caught.value.code == 0
    assert "current directory" in capsys.readouterr().out


@pytest.mark.parametrize("packaged", [True, False])
def test_dispatch_uses_same_installer_for_wheel_and_checkout(tmp_path, monkeypatch, packaged):
    module = tmp_path / "src/decision_browser/cli.py"
    module.parent.mkdir(parents=True)
    module.write_text("")
    monkeypatch.setattr(cli, "__file__", str(module))
    script = module.with_name("install.py") if packaged else tmp_path / "scripts/install.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("")
    calls = []

    def load(path):
        assert Path(path) == script
        return {"main": lambda args: calls.append(args) or 9}

    monkeypatch.setattr(cli.runpy, "run_path", load)
    assert cli._install("both", "/project with spaces", upgrade=True, uninstall=False) == 9
    assert calls == [["--client", "both", "--project", "/project with spaces", "--upgrade"]]
    calls.clear()
    assert cli._install("codex", ".", upgrade=False, uninstall=True) == 9
    assert calls == [["--client", "codex", "--project", ".", "--uninstall"]]


def test_missing_installer_fails_without_running_anything(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "src/decision_browser/cli.py"))
    monkeypatch.setattr(cli.runpy, "run_path", lambda *a: pytest.fail("script started"))
    assert cli._install("codex", ".", upgrade=False, uninstall=False) == 1
    assert "installer unavailable" in capsys.readouterr().err


def test_existing_serve_and_doctor_commands(monkeypatch):
    monkeypatch.setattr(cli, "_serve", lambda manager, mode: 3 if mode == "standard_form" else 4)
    monkeypatch.setattr(cli, "_doctor", lambda project: 5 if project == "/project" else 6)
    assert cli.main(["serve", "--admission", "standard_form"]) == 3
    assert cli.main(["doctor", "--project", "/project"]) == 5
