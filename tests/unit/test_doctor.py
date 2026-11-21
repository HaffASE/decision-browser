"""Prerequisite-only doctor fields. No network, browser, or API calls."""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCTOR_PATH = ROOT / "skills" / "decision-browser" / "scripts" / "doctor.py"
CANARY = "CANARY-SECRET-DO-NOT-LEAK"


@pytest.fixture(autouse=True)
def clear_provider_environment(monkeypatch, isolated_provider_profile):
    for name in os.environ:
        if name == "DECISION_BROWSER_CONFIG_FILE":
            continue
        if name.startswith("DECISION_BROWSER_") or name in {"OPENAI_API_KEY", "TYPESAFE_API_KEY", "OPENROUTER_API_KEY"}:
            monkeypatch.delenv(name)


def _load():
    spec = importlib.util.spec_from_file_location("decision_browser_doctor_under_test", DOCTOR_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _block_network(monkeypatch) -> None:
    def _fail(*_args, **_kwargs):
        raise AssertionError("network or browser operation")

    monkeypatch.setattr(socket.socket, "connect", _fail)
    monkeypatch.setattr(socket, "connect", _fail, raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", _fail)


def test_readiness_scope_prerequisite_only_no_canary(monkeypatch) -> None:
    doctor = _load()
    _block_network(monkeypatch)
    monkeypatch.setattr(doctor, "_dep", lambda name: {"available": True, "version": doctor.DEPS[name]})
    monkeypatch.setattr(
        doctor,
        "_project",
        lambda _raw: {"provided": True, "exists": True, "directory": True},
    )
    monkeypatch.setenv("OPENAI_API_KEY", CANARY)
    report = doctor.build_report(CANARY)
    payload = json.dumps(report)
    assert CANARY not in payload
    assert report["readiness_scope"] == "prerequisites_only"
    assert report["api_key_present"] is True
    assert isinstance(report["api_key_present"], bool)
    assert report["local_ready"] is True
    assert report["live_ready"] is True
    assert report["live_status"] == "PREREQUISITES_PRESENT"
    for marker in (
        "NOT_RUN driver",
        "NOT_RUN entitlement",
        "NOT_RUN nativeMCP",
        "NOT_RUN admission",
        "NOT_RUN liveAPI",
    ):
        assert marker in report["notes"]
    assert "no live/native/schema/entitlement/browser proof" in report["notes"]
    assert "project" in report and "path" not in report["project"]


def test_absent_key_subprocess_boolean_only() -> None:
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env["UNRELATED_CANARY"] = CANARY
    proc = subprocess.run(
        [sys.executable, str(DOCTOR_PATH), "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert CANARY not in proc.stdout
    assert CANARY not in proc.stderr
    report = json.loads(proc.stdout)
    assert report["api_key_present"] is False
    assert isinstance(report["api_key_present"], bool)
    assert report["readiness_scope"] == "prerequisites_only"
    assert report["live_ready"] is False
    assert "NOT_RUN liveAPI" in report["notes"]


def test_cli_doctor_child_keeps_isolation_and_disables_bytecode(tmp_path, monkeypatch, capfd):
    from decision_browser import cli

    script = tmp_path / "doctor.py"
    script.write_text(
        "import json, sys\n"
        "print(json.dumps({'isolated': sys.flags.isolated, 'no_bytecode': sys.dont_write_bytecode}))\n"
    )
    monkeypatch.setattr(cli, "_skill_doctor", lambda: script)
    assert cli._doctor(None) == 0
    report = json.loads(capfd.readouterr().out)
    assert report == {"isolated": 1, "no_bytecode": True}


def test_incompatible_dependencies_cannot_report_ready(monkeypatch):
    doctor = _load()
    _block_network(monkeypatch)
    monkeypatch.setattr(doctor, "_dep", lambda name: {"available": True, "version": "1"})
    report = doctor.build_report(None)
    assert report["local_ready"] is False
    assert report["dependencies_compatible"] is False


def test_server_import_failure_cannot_report_ready(monkeypatch):
    doctor = _load()
    monkeypatch.setattr(doctor, "_server_importable", lambda: False, raising=False)
    report = doctor.build_report(None)
    assert report["local_ready"] is False
    assert report["server_importable"] is False


def test_doctor_pins_match_project_metadata():
    import tomllib

    doctor = _load()
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert doctor.DEPS == dict(dep.split("==") for dep in metadata["project"]["dependencies"])


def test_keyless_local_doctor_has_ready_prerequisites_without_network(monkeypatch):
    doctor = _load()
    _block_network(monkeypatch)
    monkeypatch.setattr(doctor, "_dep", lambda name: {"available": True, "version": doctor.DEPS[name]})
    monkeypatch.setenv("DECISION_BROWSER_PROVIDER", "local")
    monkeypatch.setenv("OPENAI_API_KEY", CANARY)
    report = doctor.build_report(None)
    assert report["provider"] == "local"
    assert report["api_key_present"] is False
    assert report["provider_ready"] is True and report["live_ready"] is True
    assert report["live_status"] == "PREREQUISITES_PRESENT"
    assert "NOT_RUN liveAPI" in report["notes"]
    assert CANARY not in json.dumps(report)


def test_doctor_selected_key_and_invalid_config_never_leak(monkeypatch):
    doctor = _load()
    _block_network(monkeypatch)
    monkeypatch.setattr(doctor, "_dep", lambda name: {"available": True, "version": doctor.DEPS[name]})
    monkeypatch.setenv("DECISION_BROWSER_PROVIDER", "typesafe")
    monkeypatch.setenv("OPENAI_API_KEY", CANARY)
    report = doctor.build_report(None)
    assert report["api_key_present"] is False and report["live_ready"] is False
    monkeypatch.setenv("TYPESAFE_API_KEY", CANARY)
    report = doctor.build_report(None)
    assert report["api_key_present"] is True and report["live_ready"] is True
    monkeypatch.setenv("DECISION_BROWSER_ENDPOINT", "https://user:" + CANARY + "@example.test/v1/systemone")
    report = doctor.build_report(None)
    assert report["provider_ready"] is False and report["live_ready"] is False
    assert report["provider_error"] == "invalid_provider_config"
    assert CANARY not in json.dumps(report)


def test_doctor_missing_config_module_is_bounded_and_blocked(monkeypatch):
    doctor = _load()
    original = doctor.importlib.import_module

    def missing(name):
        if name == "decision_browser.provider_config":
            raise ImportError(CANARY)
        return original(name)

    monkeypatch.setattr(doctor.importlib, "import_module", missing)
    report = doctor.build_report(None)
    assert report["provider_ready"] is False
    assert report["provider_error"] == "provider_config_unavailable"
    assert report["live_ready"] is False
    assert CANARY not in json.dumps(report)
