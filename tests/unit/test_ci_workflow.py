"""Narrow guards for this workflow; not a general GitHub Actions YAML validator."""

from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/verify.yml"


def _step(workflow: str, name: str) -> str:
    marker = f"      - name: {name}\n"
    assert marker in workflow
    return workflow.split(marker, 1)[1].split("      - name:", 1)[0]


def test_runner_context_is_not_used_before_steps():
    # GitHub disallows runner in job-level env, although ordinary YAML parsers
    # accept it. This repository has no pre-step runner-context use case.
    prefix, separator, _ = WORKFLOW.read_text().partition("    steps:\n")
    assert separator
    assert re.search(r"\$\{\{\s*runner(?:\.|\[)", prefix) is None


def test_verifier_requires_successful_runtime_path_initialization():
    body = _step(WORKFLOW.read_text(), "Run full verification and preserve prerequisite failures")
    condition = next(line.strip() for line in body.splitlines() if line.strip().startswith("if:"))
    for step in ("paths", "checkout", "python", "uv"):
        assert f"steps.{step}.outcome == 'success'" in condition
    assert "!cancelled()" in condition


@pytest.mark.parametrize("missing", [None, "RUNNER_TEMP", "GITHUB_ENV"])
def test_runtime_path_initialization_is_quoted_and_fail_closed(tmp_path, missing):
    body = _step(WORKFLOW.read_text(), "Initialize runner-local paths")
    assert "        id: paths\n" in body
    script = textwrap.dedent(body.split("        run: |\n", 1)[1])
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    target = tmp_path / "environment file"
    runner_temp = tmp_path / "runner temp"
    env = {"RUNNER_TEMP": str(runner_temp), "GITHUB_ENV": str(target)}
    if missing:
        env.pop(missing)
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=False
    )
    if missing:
        assert result.returncode != 0
        assert not target.exists() or target.read_text() == ""
        return
    assert result.returncode == 0, result.stderr
    assert dict(line.split("=", 1) for line in target.read_text().splitlines()) == {
        "UV_PROJECT_ENVIRONMENT": f"{runner_temp}/decision-browser-venv",
        "UV_CACHE_DIR": f"{runner_temp}/decision-browser-uv-cache",
        "PLAYWRIGHT_BROWSERS_PATH": f"{runner_temp}/decision-browser-browsers",
        "CI_EVIDENCE_ROOT": f"{runner_temp}/decision-browser-evidence",
    }
