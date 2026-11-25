"""No-network launcher/package regressions; all setup children are mocked."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import tomllib
from pathlib import Path
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("npm_builder", ROOT / "scripts/build_npm.py")
assert spec and spec.loader
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def fixture_wheel(tmp_path, version=None):
    if version is None:
        version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    wheel = tmp_path / "fixture.whl"
    files = {
        "pyproject.toml": f'[project]\nname="decision-browser"\nversion="{version}"\n',
        "uv.lock": "", "LICENSE": "fixture license", "THIRD_PARTY_NOTICES.md": "fixture notices",
        "skills/decision-browser/SKILL.md": "skill", "src/decision_browser/cli.py": "",
        "scripts/install.py": (ROOT / "scripts/install.py").read_text(),
        "scripts/prepare_tool_runtime.py": (ROOT / "scripts/prepare_tool_runtime.py").read_text(),
    }
    for name in ("README.md", "hatch_build.py", "scripts/build-constraints.txt", "scripts/build_metadata.py"):
        files[name] = (ROOT / name).read_text(encoding="utf-8")
    with ZipFile(wheel, "w") as archive:
        for name, text in files.items():
            archive.writestr("decision_browser/bootstrap/" + name, text)
    return wheel


def test_node_launcher_regressions():
    node = shutil.which("node")
    assert node, "Node.js is a required verification prerequisite"
    suites = sorted((ROOT / "npm/test").glob("*.test.cjs"))
    assert suites, "Node launcher regression tests are required"
    subprocess.run([node, "--test", *(str(path) for path in suites)], check=True, timeout=60)


def test_npm_bundle_is_self_contained_and_never_overwritten(tmp_path):
    output = tmp_path / "output with spaces"
    wheel = fixture_wheel(tmp_path)
    builder.build(wheel, output)
    for helper in ("install-recovery.py", "install_recovery_evidence.py"):
        assert (output / "bin" / helper).read_bytes() == (ROOT / "npm/bin" / helper).read_bytes()
    assert not list((output / "bin").rglob("*.py[co]"))
    manifest = json.loads((output / "installer/manifest.json").read_text())
    assert manifest["sha256"] == hashlib.sha256((output / "installer/decision-browser.pyz").read_bytes()).hexdigest()
    assert (output / "LICENSE").read_text() == "fixture license"
    package = json.loads((output / "package.json").read_text())
    assert not package.get("dependencies")
    assert not set(package["scripts"]) & {"preinstall", "install", "postinstall", "prepare"}
    assert package["version"] == manifest["version"]
    with pytest.raises(FileExistsError):
        builder.build(wheel, output)


def test_npm_bundle_rejects_other_wheel_version_before_writing(tmp_path):
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="versions must match"):
        builder.build(fixture_wheel(tmp_path, "9.9.9"), output)
    assert not output.exists()


def test_npm_version_matches_project():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    package = json.loads((ROOT / "npm/package.json").read_text())
    assert package["name"] == "@haffa/decision-browser"
    assert project["version"] == package["version"]
