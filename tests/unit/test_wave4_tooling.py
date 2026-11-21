"""Boundary regressions for doctor, reviewed build inputs, and verifier tooling."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from decision_browser import cli

ROOT = Path(__file__).resolve().parents[2]


def load(path: str):
    spec = importlib.util.spec_from_file_location("wave4_" + Path(path).stem, ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("json_output", [False, True])
def test_doctor_cli_output_choice_and_lifetime(monkeypatch, tmp_path, json_output):
    script = tmp_path / "doctor.py"
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(cli, "_skill_doctor", lambda: script)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(cli.subprocess, "run", run)
    args = ["doctor", "--project", "/owned project"] + (["--json"] if json_output else [])
    assert cli.main(args) == 7
    command, kwargs = calls[0]
    assert ("--json" in command) is json_output
    assert command[:3] == [sys.executable, "-I", "-B"]
    assert command[-2:] == ["--project", "/owned project"]
    assert 0 < kwargs["timeout"] <= 30


def test_doctor_timeout_is_sanitized(monkeypatch, tmp_path, capsys):
    script = tmp_path / "doctor.py"
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(cli, "_skill_doctor", lambda: script)

    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired("SECRET-CHILD-COMMAND", 0.01)

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli.main(["doctor", "--json"]) == 1
    error = capsys.readouterr().err
    assert "doctor timed out" in error
    assert "SECRET" not in error


def test_real_doctor_timeout_reaps_owned_child(monkeypatch, tmp_path, capsys):
    script = tmp_path / "doctor.py"
    script.write_text("import time; time.sleep(30)\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_skill_doctor", lambda: script)
    monkeypatch.setattr(cli, "DOCTOR_TIMEOUT", 0.1)
    real_popen = subprocess.Popen
    children = []

    def start(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(cli.subprocess, "Popen", start)
    started = time.monotonic()
    assert cli._doctor(None) == 1
    assert time.monotonic() - started < 2
    assert children and children[0].returncode is not None
    assert children[0].poll() is not None
    assert capsys.readouterr().err == "doctor timed out\n"


def test_large_doctor_keeps_bounded_installation_summary(capsys):
    doctor = load("skills/decision-browser/scripts/doctor.py")
    report = doctor.build_report(None)
    report["installation"] = {
        "client_connection": "NOT_RUN",
        "codex": {"registration": "present", "runtime_exists": True, "skill_present": False},
        "claude": {"registration": "invalid", "runtime_exists": False, "skill_present": True},
        "next_steps": ["SECRET-INSTALLATION-DETAIL" * 1000],
    }
    doctor._emit(report)
    encoded = capsys.readouterr().out
    assert len(encoded.encode("utf-8")) <= 8193
    result = json.loads(encoded)
    assert result["installation"]["client_connection"] == "NOT_RUN"
    assert result["installation"]["codex"] == report["installation"]["codex"]
    assert result["installation"]["claude"] == report["installation"]["claude"]
    assert "SECRET-INSTALLATION-DETAIL" not in encoded


@pytest.mark.parametrize("json_output", [False, True])
def test_doctor_script_honors_json_choice(monkeypatch, capsys, json_output):
    doctor = load("skills/decision-browser/scripts/doctor.py")
    report = doctor.build_report(None)
    monkeypatch.setattr(doctor, "build_report", lambda _: report)
    doctor.main(["--json"] if json_output else [])
    output = capsys.readouterr().out
    if json_output:
        assert json.loads(output)["local_status"] == report["local_status"]
    else:
        assert output.startswith("python: ")
        assert "installation: " in output and "local_status: " in output


@pytest.mark.parametrize("python,npm", [
    ("0.1.0", "0.1.0"), ("0.2.0a1", "0.2.0-alpha.1"),
    ("0.2.0b2", "0.2.0-beta.2"), ("0.2.0rc0", "0.2.0-rc.0"),
    ("12.10.20rc9", "12.10.20-rc.9"),
])
def test_release_version_contract(python, npm):
    metadata = load("scripts/build_metadata.py")
    assert metadata.npm_version(python) == npm


@pytest.mark.parametrize("version", [
    "01.2.0", "1.2", "v1.2.3", "1.2.3-rc.1", "1.2.3rc01", "1.2.3.post1",
    "1.2.3.dev1", "1.2.3+local", "1!1.2.3", "1.2.3\n", "1.2.3rc1٢", None, True,
])
def test_release_version_contract_rejects_unsupported(version):
    metadata = load("scripts/build_metadata.py")
    with pytest.raises(ValueError):
        metadata.npm_version(version)


def test_release_rejects_launcher_mismatch(tmp_path):
    release = load("scripts/check_release.py")
    (tmp_path / "pyproject.toml").write_text('[project]\nname="decision-browser"\nversion="0.2.0rc1"\n', encoding="utf-8")
    (tmp_path / "uv.lock").write_text('[[package]]\nname="decision-browser"\nversion="0.2.0rc1"\n', encoding="utf-8")
    (tmp_path / "npm").mkdir()
    (tmp_path / "npm/package.json").write_text('{"version":"0.2.0rc1"}', encoding="utf-8")
    with pytest.raises(ValueError, match="npm"):
        release.check(tmp_path)
    (tmp_path / "npm/package.json").write_text('{"version":"0.2.0-rc.1","description":"é"}', encoding="utf-8")
    assert release.check(tmp_path) == "0.2.0rc1"


def test_release_reads_unicode_metadata_under_non_utf8_mode(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="decision-browser"\nversion="0.2.0rc1"\ndescription="é"\n', encoding="utf-8")
    (tmp_path / "uv.lock").write_text('[[package]]\nname="decision-browser"\nversion="0.2.0rc1"\n', encoding="utf-8")
    (tmp_path / "npm").mkdir()
    (tmp_path / "npm/package.json").write_text('{"version":"0.2.0-rc.1","description":"é"}', encoding="utf-8")
    code = "import runpy,sys; from pathlib import Path; print(runpy.run_path(sys.argv[1])['check'](Path(sys.argv[2])))"
    result = subprocess.run(
        [sys.executable, "-I", "-X", "utf8=0", "-c", code, str(ROOT / "scripts/check_release.py"), str(tmp_path)],
        env={**os.environ, "LC_ALL": "C"}, capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.2.0rc1"


@pytest.mark.parametrize("version,npm", [("0.2.0a1", "0.2.0-alpha.1"), ("0.2.0b2", "0.2.0-beta.2"), ("0.2.0rc1", "0.2.0-rc.1")])
def test_npm_prerelease_build_keeps_mapped_version_and_original_wheel(tmp_path, version, npm):
    metadata = load("scripts/build_metadata.py")
    builder = load("scripts/build_npm.py")
    root = tmp_path / "root"
    shutil.copytree(ROOT / "npm", root / "npm")
    (root / "scripts").mkdir()
    for name in ("build_installer.py", "build_metadata.py"):
        shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
    package = json.loads((root / "npm/package.json").read_text(encoding="utf-8"))
    package.update(version=npm, description="Unicode metadata é")
    (root / "npm/package.json").write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
    wheel = tmp_path / "fixture.whl"
    with ZipFile(wheel, "w") as archive:
        for name in metadata.REQUIRED_BOOTSTRAP_FILES:
            value = f'[project]\nname="decision-browser"\nversion="{version}"\n' if name == "pyproject.toml" else "fixture"
            archive.writestr("decision_browser/bootstrap/" + name, value)
    output = tmp_path / "candidate"
    builder.build(wheel, output, root)
    assert json.loads((output / "package.json").read_text(encoding="utf-8"))["version"] == npm
    assert json.loads((output / "installer/manifest.json").read_text(encoding="utf-8"))["version"] == npm
    with ZipFile(output / "installer/decision-browser.pyz") as archive:
        assert archive.read("payload/prebuilt/fixture.whl") == wheel.read_bytes()


def test_npm_missing_bootstrap_input_rejects_before_candidate_creation(tmp_path):
    metadata = load("scripts/build_metadata.py")
    builder = load("scripts/build_npm.py")
    wheel = tmp_path / "fixture.whl"
    with ZipFile(wheel, "w") as archive:
        for name in metadata.REQUIRED_BOOTSTRAP_FILES:
            if name == "scripts/build-constraints.txt":
                continue
            value = '[project]\nname="decision-browser"\nversion="0.1.0"\n' if name == "pyproject.toml" else "fixture"
            archive.writestr("decision_browser/bootstrap/" + name, value)
    output = tmp_path / "candidate"
    with pytest.raises(ValueError, match="complete bootstrap"):
        builder.build(wheel, output)
    assert not output.exists()


@pytest.mark.parametrize("name", ["README.md", "THIRD_PARTY_NOTICES.md", "scripts/build-constraints.txt", "hatch_build.py", "scripts/build_metadata.py"])
def test_installer_rejects_missing_consumed_inputs_before_output(tmp_path, name):
    metadata = load("scripts/build_metadata.py")
    builder = load("scripts/build_installer.py")
    wheel = tmp_path / "fixture.whl"
    with ZipFile(wheel, "w") as archive:
        for required in metadata.REQUIRED_BOOTSTRAP_FILES:
            if required != name:
                archive.writestr("decision_browser/bootstrap/" + required, b"fixture")
    output = tmp_path / "output.pyz"
    with pytest.raises(ValueError, match="complete bootstrap"):
        builder.build(wheel, output)
    assert not output.exists()


@pytest.mark.parametrize("script", ["scripts/run_browser_scenarios.py", "scripts/probe_flights_state.py", "scripts/build_metadata.py"])
def test_shipped_scripts_participate_in_source_identity(tmp_path, monkeypatch, script):
    verify = load("scripts/verify.py")
    target = tmp_path / script
    target.parent.mkdir(parents=True)
    target.write_text("before", encoding="utf-8")
    monkeypatch.setattr(verify.subprocess, "check_output", lambda command, **kw: (script.encode() + b"\0") if "--cached" in command else b"")
    monkeypatch.setattr(verify, "git", lambda *args: "fixture")
    before = verify.source_identity(tmp_path)
    target.write_text("after", encoding="utf-8")
    after = verify.source_identity(tmp_path)
    assert before["tree_sha256"] != after["tree_sha256"]
    assert [x["path"] for x in after["files"]] == [script]


def test_windows_excludes_only_reviewed_exact_posix_cases(monkeypatch):
    verify = load("scripts/verify.py")
    monkeypatch.setattr(verify.sys, "platform", "win32")
    exclusions = verify.platform_exclusions()
    assert len(exclusions) == 39
    assert len({x["nodeid"] for x in exclusions}) == 39
    assert sum(x["nodeid"].startswith("tests/unit/test_verification_harness.py::test_timeout_cleanup_is_bounded_and_preserves_timeout[") for x in exclusions) == 9
    assert sum(x["nodeid"].startswith("tests/unit/test_wave4_provider_routes.py::") for x in exclusions) == 18
    assert sum(x["nodeid"].startswith("tests/unit/test_wave4_installer.py::") for x in exclusions) == 11
    assert sum(x["nodeid"].startswith("tests/unit/test_wave4_installer_lifecycle.py::") for x in exclusions) == 1
    assert all(x["reason"] and "::" in x["nodeid"] for x in exclusions)
    monkeypatch.setattr(verify.sys, "platform", "darwin")
    assert verify.platform_exclusions() == []


def test_platform_exclusions_match_exact_collected_nodeids(monkeypatch):
    verify = load("scripts/verify.py")
    family = "tests/unit/test_verification_harness.py::test_timeout_cleanup_is_bounded_and_preserves_timeout"
    collected = subprocess.run(
        [sys.executable, "-I", "-m", "pytest", "-p", "pytest_asyncio.plugin", "-o", "addopts=", "--collect-only", "-q", "tests/unit"],
        cwd=ROOT, env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert collected.returncode == 0, collected.stderr
    ids = {line for line in collected.stdout.splitlines() if line.startswith("tests/unit/") and "::" in line}
    monkeypatch.setattr(verify.sys, "platform", "win32")
    excluded = {x["nodeid"] for x in verify.platform_exclusions()}
    assert excluded <= ids
    assert {x for x in excluded if x.startswith(family + "[")} == {x for x in ids if x.startswith(family + "[")}
    provider_cases = {x for x in ids if x.startswith("tests/unit/test_wave4_provider_routes.py::")}
    assert len(provider_cases - excluded) == 25


def test_optional_extra_and_dev_group_stay_synchronized():
    import tomllib

    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["optional-dependencies"]["dev"] == metadata["dependency-groups"]["dev"]
