"""F11 file and platform boundaries; fixtures never inspect personal profiles."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from decision_browser import browser, user_config
from decision_browser.provider_config import ProviderConfigError


def test_wave3_fifo_port_rejected_without_writer(tmp_path: Path) -> None:
    fifo = tmp_path / "DevToolsActivePort"
    os.mkfifo(fifo)
    code = (
        "from pathlib import Path; from decision_browser.browser import "
        "read_devtools_active_port, BrowserBlocked; "
        f"\ntry: read_devtools_active_port(Path({str(fifo)!r}))"
        "\nexcept BrowserBlocked as e: assert e.code == 'devtools_port_unreadable'"
        "\nelse: raise AssertionError('accepted FIFO')"
    )
    subprocess.run([sys.executable, "-I", "-c", code], check=True, timeout=2)


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
def test_wave3_writable_port_rejected(tmp_path: Path, mode: int) -> None:
    port = tmp_path / "DevToolsActivePort"
    port.write_text("9222\n/devtools/browser/test\n")
    port.chmod(mode)
    with pytest.raises(browser.BrowserBlocked, match="devtools_port_unreadable"):
        browser.read_devtools_active_port(port)


def test_wave3_regular_readable_port_and_symlink_control(tmp_path: Path) -> None:
    port = tmp_path / "DevToolsActivePort"
    port.write_text("9222\n/devtools/browser/test\n")
    port.chmod(0o644)
    assert browser.read_devtools_active_port(port).port == 9222
    link = tmp_path / "port-link"
    link.symlink_to(port)
    with pytest.raises(browser.BrowserBlocked, match="devtools_port_unreadable"):
        browser.read_devtools_active_port(link)


def test_wave3_missing_profile_through_alias_falls_back(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    env = {"DECISION_BROWSER_CONFIG_FILE": str(alias / "missing.json")}
    assert user_config.load_profile(env) is None
    (directory / "missing.json").write_text('{"version":1,"provider":"local"}')
    (directory / "missing.json").chmod(0o600)
    with pytest.raises(ProviderConfigError, match="unsafe_profile_path"):
        user_config.load_profile(env)


@pytest.mark.parametrize("platform,expected", [
    ("darwin", "Library/Application Support/Google/Chrome/DevToolsActivePort"),
    ("linux", ".config/google-chrome/DevToolsActivePort"),
    ("win32", "AppData/Local/Google/Chrome/User Data/DevToolsActivePort"),
])
def test_wave3_default_profile_for_platform(monkeypatch, tmp_path, platform, expected):
    monkeypatch.setattr(browser.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CHROME_CONFIG_HOME", raising=False)
    assert browser.default_devtools_active_port() == tmp_path / expected


def test_wave3_unsupported_default_blocks_before_runtime(monkeypatch):
    monkeypatch.setattr(browser.sys, "platform", "unsupported")
    with pytest.raises(browser.BrowserBlocked, match="existing_chrome_unsupported"):
        browser.BrowserDriver("existing_chrome")
    assert browser.BrowserDriver("isolated").mode is browser.BrowserMode.isolated


def test_wave3_profile_replaced_between_check_and_open_is_rejected(tmp_path, monkeypatch):
    profile = tmp_path / "profile.json"
    profile.write_text('{"version":1,"provider":"local"}')
    profile.chmod(0o600)
    replacement = tmp_path / "other.json"
    replacement.write_text('{"version":1,"provider":"local"}')
    replacement.chmod(0o600)
    real_open = os.open

    def replace_before_open(path, flags):
        replacement.replace(profile)
        return real_open(path, flags)

    monkeypatch.setattr(user_config.os, "open", replace_before_open)
    with pytest.raises(ProviderConfigError, match="unsafe_profile_path"):
        user_config.load_profile({"DECISION_BROWSER_CONFIG_FILE": str(profile)})


def test_wave3_port_descriptor_owner_and_hardlink_boundaries(tmp_path, monkeypatch):
    port = tmp_path / "port"
    port.write_text("9222\n/devtools/browser/test\n")
    other = tmp_path / "linked-port"
    os.link(port, other)
    with pytest.raises(browser.BrowserBlocked, match="devtools_port_unreadable"):
        browser.read_devtools_active_port(port)
    other.unlink()
    real_fstat = os.fstat

    def foreign_owner(fd):
        fields = list(real_fstat(fd))
        fields[4] = os.getuid() + 1
        return os.stat_result(fields)

    monkeypatch.setattr(browser.os, "fstat", foreign_owner)
    with pytest.raises(browser.BrowserBlocked, match="devtools_port_unreadable"):
        browser.read_devtools_active_port(port)


def test_wave3_configured_os_profile_roots(monkeypatch, tmp_path):
    monkeypatch.setattr(browser.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert browser.default_devtools_active_port() == tmp_path / "xdg/google-chrome/DevToolsActivePort"
    monkeypatch.setenv("CHROME_CONFIG_HOME", str(tmp_path / "chrome-config"))
    assert browser.default_devtools_active_port() == tmp_path / "chrome-config/google-chrome/DevToolsActivePort"
    monkeypatch.setattr(browser.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert browser.default_devtools_active_port() == tmp_path / "local/Google/Chrome/User Data/DevToolsActivePort"


def test_wave3_doctor_reports_path_support_without_opening_profile(monkeypatch):
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "skills/decision-browser/scripts/doctor.py"
    spec = importlib.util.spec_from_file_location("wave3_doctor", path)
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)
    monkeypatch.setattr(browser.sys, "platform", "unsupported")
    assert doctor._existing_chrome_status() == {
        "default_profile_supported": False, "readiness": "UNSUPPORTED", "native_qualification": "NOT_RUN",
    }
    monkeypatch.setattr(browser.sys, "platform", "linux")
    assert doctor._existing_chrome_status() == {
        "default_profile_supported": True, "readiness": "NOT_RUN", "native_qualification": "NOT_RUN",
    }
