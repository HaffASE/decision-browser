"""Release metadata must fail closed on stale locks or premature tags."""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import pathspec
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "check_release", ROOT / "scripts/check_release.py"
)
assert spec is not None and spec.loader is not None
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="decision-browser"\nversion="0.2.0"\nlicense="MIT"\n'
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname="decision-browser"\nversion="0.2.0"\n'
    )
    (tmp_path / "LICENSE").write_text("Fixture license text, not a project license.\n")
    (tmp_path / "CHANGELOG.md").write_text(
        "## [0.2.0] - 2026-11-04\n\nFixture changes.\n"
    )
    (tmp_path / "npm").mkdir()
    (tmp_path / "npm/package.json").write_text('{"version":"0.2.0"}', encoding="utf-8")
    return tmp_path


def test_repository_version_matches_lock():
    assert release.check(ROOT)


def test_generated_benchmark_output_is_excluded_from_source_distribution():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    excludes = project["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    source_excludes = pathspec.GitIgnoreSpec.from_lines(excludes)
    for name in (
        "docs/benchmarks/output/result.json",
        "docs/benchmarks/2026-11-17-browser-matrix/output/result.json",
        "docs/benchmarks/2026-11-17-browser-matrix/output/nested/screenshot.png",
    ):
        assert source_excludes.match_file(name), name
    for name in (
        "docs/benchmarks/2026-11-17-browser-matrix/report.md",
        "docs/benchmarks/2026-11-17-browser-matrix/audited-runs.csv",
        "docs/assets/readme-short-tasks.svg",
    ):
        assert not source_excludes.match_file(name), name


def test_valid_release(project):
    assert release.check(project, "v0.2.0") == "0.2.0"


def test_mismatched_lock(project):
    (project / "uv.lock").write_text(
        '[[package]]\nname="decision-browser"\nversion="0.1.0"\n'
    )
    with pytest.raises(ValueError, match="disagree"):
        release.check(project)


@pytest.mark.parametrize("tag", ["0.2.0", "v0.3.0", "v0.2.0/unsafe", "v0.2.0\n"])
def test_mismatched_tag(project, tag):
    with pytest.raises(ValueError, match="Tag must"):
        release.check(project, tag)


def test_missing_license_blocks_only_release(project):
    (project / "LICENSE").unlink()
    assert release.check(project) == "0.2.0"
    with pytest.raises(ValueError, match="owner-approved"):
        release.check(project, "v0.2.0")


def test_missing_license_metadata(project):
    p = project / "pyproject.toml"
    p.write_text(p.read_text().replace('license="MIT"', ""))
    with pytest.raises(ValueError, match="owner-approved"):
        release.check(project, "v0.2.0")


@pytest.mark.parametrize(
    "heading", ["## [Unreleased]", "## [0.2.0] - Unreleased", "## [0.2.0] - 2026-02-30"]
)
def test_undated_or_invalid_release(project, heading):
    (project / "CHANGELOG.md").write_text(heading)
    with pytest.raises(ValueError):
        release.check(project, "v0.2.0")


@pytest.mark.parametrize("version", ["0.2.0rc1", "0.2.0a1", "1.0.0", "12.10.20b2"])
def test_canonical_versions(version):
    assert release.VERSION.fullmatch(version)


@pytest.mark.parametrize(
    "version", ["01.2.0", "v1.0.0", "1.2", "1.2.3+local", "1.0.0-rc.1"]
)
def test_noncanonical_versions(version):
    assert not release.VERSION.fullmatch(version)
