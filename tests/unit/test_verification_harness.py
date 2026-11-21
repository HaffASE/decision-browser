"""The verification entrypoint must never turn missing coverage into a pass."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def verify(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "verification_under_test", ROOT / "scripts" / "verify.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Every source-loaded regression starts without the real process environment,
    # including Git identity tests and any assertion diagnostics on failing code.
    monkeypatch.setattr(module.os, "environ", {})
    return module


@pytest.fixture
def runner(verify, monkeypatch, tmp_path):
    calls = []
    failures = {}

    def command(argv, root, env, log, timeout):
        calls.append((log.stem, argv, env))
        log.write_text("fixture command result\n")
        if log.stem == "build" and not failures.get("build"):
            dist = Path(argv[argv.index("--out-dir") + 1])
            dist.mkdir()
            (dist / "package.whl").write_bytes(b"wheel fixture")
            (dist / "package.tar.gz").write_bytes(b"sdist fixture")
        for arg in argv:
            if arg.startswith("--junitxml="):
                Path(arg.split("=", 1)[1]).write_text(
                    '<testsuites><testsuite tests="1" errors="0" failures="0" skipped="0" /></testsuites>'
                )
        if isinstance(failures.get(log.stem), Exception):
            raise failures[log.stem]
        return failures.get(log.stem, 0)

    monkeypatch.setattr(verify, "run_command", command)
    # Never retain the real process environment in pytest fixture diagnostics.
    monkeypatch.setattr(
        verify, "verification_environment", lambda root: {"PATH": "/fixture/bin"}
    )
    monkeypatch.setattr(
        verify,
        "source_identity",
        lambda root: {
            "commit": "a" * 40,
            "tree_sha256": "b" * 64,
            "dirty": False,
            "unverified_untracked_count": 0,
        },
    )

    def make(profile="full"):
        output = tmp_path / profile
        output.mkdir()
        return verify.Verification(ROOT, output, profile, 30)

    return make, calls, failures


def test_offline_pass_does_not_claim_full_or_native_coverage(runner):
    make, calls, _ = runner
    run = make("offline")
    assert run.run() == 0
    report = json.loads((run.output / "result.json").read_text())
    assert report["status"] == "PASS"
    assert report["profile"] == "offline"
    assert report["full_suite_passed"] is False
    assert report["stages"]["pytest_full"]["status"] == "NOT_RUN"
    assert report["stages"]["browser_preflight"]["status"] == "NOT_RUN"
    assert {v["status"] for v in report["acceptance"].values()} == {"NOT_RUN"}
    assert all(env["UV_OFFLINE"] == "1" for _, _, env in calls)
    assert not any(name in {"pytest_full", "browser_preflight"} for name, _, _ in calls)


def test_missing_browser_is_blocked_not_false_green(runner):
    make, calls, failures = runner
    failures["browser_preflight"] = 1
    run = make()
    assert run.run() == 2
    assert run.report["status"] == "BLOCKED"
    assert run.report["full_suite_passed"] is False
    assert run.report["stages"]["pytest_unit"]["status"] == "PASS"
    assert run.report["stages"]["pytest_full"]["status"] == "NOT_RUN"
    assert not any(name == "pytest_full" for name, _, _ in calls)


def test_full_pass_requires_actual_full_pytest(runner):
    make, calls, _ = runner
    run = make()
    assert run.run() == 0
    assert run.report["full_suite_passed"] is True
    names = [name for name, _, _ in calls]
    assert names.index("browser_preflight") < names.index("pytest_full")
    command = next(command for name, command, _ in calls if name == "pytest_full")
    assert "tests" in command and "tests/unit" not in command


def test_platform_exclusions_are_explicit_and_applied_to_both_stages(verify, runner, monkeypatch):
    family = "tests/unit/test_verification_harness.py::test_timeout_cleanup_is_bounded_and_preserves_timeout"
    exclusions = [
        {"nodeid": family + "[None-None]", "reason": "POSIX process groups"},
        {"nodeid": family + "[None-signal_error1]", "reason": "POSIX process groups"},
    ]
    monkeypatch.setattr(verify, "platform_exclusions", lambda: exclusions)
    make, calls, _ = runner
    run = make()
    assert run.run() == 0
    assert run.report["platform_exclusions"] == exclusions
    for stage in ("pytest_unit", "pytest_full"):
        command = next(args for name, args, _ in calls if name == stage)
        assert command.count("--deselect") == 2
        assert {command[i + 1] for i, item in enumerate(command) if item == "--deselect"} == {x["nodeid"] for x in exclusions}


def test_unexpected_skip_still_fails_with_declared_platform_exclusions(runner, monkeypatch):
    make, _, _ = runner
    run = make("offline")
    run.report["platform_exclusions"] = [{"nodeid": "known-posix-only", "parametrized_cases": 9}]
    junit = run.output / "unexpected-skip.xml"
    junit.write_text('<testsuites><testsuite tests="3" errors="0" failures="0" skipped="1" /></testsuites>', encoding="utf-8")

    def success(name, command, **kwargs):
        run.report["stages"][name] = {"status": "PASS", "returncode": 0}

    monkeypatch.setattr(run, "stage", success)
    run.pytest_stage("pytest_unit", ["pytest"], junit)
    assert run.report["stages"]["pytest_unit"]["status"] == "FAIL"


@pytest.mark.parametrize(
    "stage",
    ["ruff", "mypy", "smoke_import", "dependency_check", "pytest_unit", "pytest_full"],
)
def test_any_failed_gate_fails_overall_report(runner, stage):
    make, _, failures = runner
    failures[stage] = 1
    run = make()
    assert run.run() == 1
    assert run.report["status"] == "FAIL"
    assert run.report["full_suite_passed"] is False


@pytest.mark.parametrize(
    "stage", ["lock_check", "frozen_sync", "build", "install_wheel"]
)
def test_setup_failure_cannot_test_a_stale_installation(runner, stage):
    make, calls, failures = runner
    failures[stage] = 1
    run = make()
    assert run.run() == 1
    assert run.report["stages"]["pytest_unit"]["status"] == "NOT_RUN"
    assert not any(
        name in {"pytest_unit", "pytest_full", "smoke_import"} for name, _, _ in calls
    )


def test_commands_freeze_dependencies_and_install_only_built_wheel(runner):
    make, calls, _ = runner
    run = make("offline")
    run.run()
    commands = {name: command for name, command, _ in calls}
    assert commands["frozen_sync"][-4:] == [
        "--frozen",
        "--group",
        "dev",
        "--no-install-project",
    ]
    assert "--build-constraints" in commands["build"]
    assert "--no-deps" in commands["install_wheel"]
    assert "--no-index" in commands["install_wheel"]
    assert commands["install_wheel"][-1].endswith("package.whl")
    assert "-I" in commands["pytest_unit"]
    assert not any(
        "playwright" in command and "install" in command
        for command in commands.values()
    )


def test_authoritative_tooling_has_explicit_static_gate_coverage(runner):
    make, calls, _ = runner
    assert make("offline").run() == 0
    commands = {name: command for name, command, _ in calls}
    assert "hatch_build.py" in commands["ruff"]
    assert "skills/decision-browser/scripts/doctor.py" in commands["ruff"]
    assert "scripts/build_metadata.py" in commands["mypy"]
    assert "skills/decision-browser/scripts/doctor.py" in commands["mypy"]


def test_environment_removes_test_filtering_and_provider_key(verify, monkeypatch):
    monkeypatch.setattr(verify.os, "environ", {})
    monkeypatch.setattr(verify, "git", lambda *args: "123")
    for name in (
        "OPENAI_API_KEY",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
    ):
        monkeypatch.setenv(name, "canary")
    env = verify.verification_environment(ROOT)
    assert "canary" not in env.values()
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert env["UV_PYTHON_DOWNLOADS"] == "never"


def test_source_drift_fails_a_successful_run(verify, runner, monkeypatch):
    make, _, _ = runner
    run = make("offline")
    monkeypatch.setattr(verify, "source_identity", lambda root: {"commit": "changed"})
    assert run.run() == 1
    assert run.report["stages"]["source_unchanged"]["status"] == "FAIL"


def test_timeout_is_not_a_success(runner):
    make, _, failures = runner
    failures["pytest_unit"] = subprocess.TimeoutExpired("pytest", 30)
    run = make("offline")
    assert run.run() == 1
    assert run.report["stages"]["pytest_unit"]["reason"] == "TimeoutExpired"


def test_browser_preflight_does_not_disable_sandbox(verify):
    assert "chromium_sandbox=True" in verify.BROWSER_CODE
    assert 'channel="chromium"' in verify.BROWSER_CODE
    assert "screenshot(timeout=5000)" in verify.BROWSER_CODE
    assert "--no-sandbox" not in verify.BROWSER_CODE
    assert "executable_path=" not in verify.BROWSER_CODE


@pytest.mark.parametrize("path", ["inside", "existing"])
def test_output_directory_cannot_overwrite_source_or_previous_evidence(
    verify, monkeypatch, tmp_path, path
):
    target = ROOT / "evidence" if path == "inside" else tmp_path
    monkeypatch.setattr("sys.argv", ["verify.py", "--output-dir", str(target)])
    with pytest.raises(SystemExit) as exc:
        verify.main()
    assert exc.value.code == 3


def test_sha256_closes_file_and_matches_known_value(verify, tmp_path):
    target = tmp_path / "empty"
    target.write_bytes(b"")
    assert (
        verify.sha256(target)
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_untracked_private_contents_are_never_read(verify, monkeypatch, tmp_path):
    (tmp_path / "README.md").write_text("public source")
    (tmp_path / "private-config").write_text("PRIVATE-CANARY")
    monkeypatch.setattr(
        verify.subprocess,
        "check_output",
        lambda command, **kwargs: (
            b"README.md\0" if "--cached" in command else b"private-config\0"
        ),
    )
    monkeypatch.setattr(verify, "git", lambda *args: "test")
    original = verify.sha256

    def checked_hash(path):
        assert path.name != "private-config", "must not open untracked private data"
        return original(path)

    monkeypatch.setattr(verify, "sha256", checked_hash)
    identity = verify.source_identity(tmp_path)
    assert identity["unverified_untracked_count"] == 1
    assert [file["path"] for file in identity["files"]] == ["README.md"]
    assert "PRIVATE-CANARY" not in json.dumps(identity)
    assert "private-config" not in json.dumps(identity)


def test_unverified_untracked_files_block_all_commands(runner):
    make, calls, _ = runner
    run = make()
    run.report["source_before"]["unverified_untracked_count"] = 1
    assert run.run() == 1
    assert run.report["stages"]["source_scope"]["status"] == "FAIL"
    assert calls == []


def test_missing_node_cannot_silently_skip_javascript_coverage(runner):
    make, calls, failures = runner
    failures["node_version"] = FileNotFoundError("node")
    run = make("offline")
    assert run.run() == 2
    assert run.report["stages"]["node_version"]["status"] == "BLOCKED"
    assert run.report["stages"]["pytest_unit"]["status"] == "NOT_RUN"
    assert not any(name.startswith("pytest") for name, _, _ in calls)


@pytest.mark.parametrize(
    "xml",
    [
        None,
        "invalid xml",
        '<testsuites><testsuite tests="0" errors="0" failures="0" skipped="0" /></testsuites>',
        '<testsuites><testsuite tests="3" errors="0" failures="0" skipped="1" /></testsuites>',
    ],
)
def test_missing_empty_or_skipped_junit_is_not_a_pass(runner, monkeypatch, xml):
    make, _, _ = runner
    run = make("offline")
    junit = run.output / "pytest-unit.xml"
    if xml is not None:
        junit.write_text(xml)

    def fake_stage(name, command, **kwargs):
        run.report["stages"][name] = {"status": "PASS", "returncode": 0}

    monkeypatch.setattr(run, "stage", fake_stage)
    run.pytest_stage("pytest_unit", ["pytest"], junit)
    assert run.report["stages"]["pytest_unit"]["status"] == "FAIL"


def test_uv_environment_cannot_redirect_verification(verify, monkeypatch):
    monkeypatch.setattr(verify.os, "environ", {})
    monkeypatch.setattr(verify, "git", lambda *args: "123")
    for name in (
        "UV_PROJECT",
        "UV_WORKING_DIR",
        "UV_CONFIG_FILE",
        "UV_INDEX",
        "UV_NO_SYNC",
        "PIP_INDEX_URL",
        "GIT_DIR",
    ):
        monkeypatch.setenv(name, "REDIRECT-CANARY")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/selected/environment")
    monkeypatch.setenv("UV_CACHE_DIR", "/selected/cache")
    env = verify.verification_environment(ROOT)
    assert "REDIRECT-CANARY" not in env.values()
    assert env["UV_PROJECT_ENVIRONMENT"] == "/selected/environment"
    assert env["UV_CACHE_DIR"] == "/selected/cache"
    assert "GIT_DIR" not in verify.git_environment()


def test_uv_commands_are_anchored_and_disable_ambient_config(runner):
    make, calls, _ = runner
    run = make("offline")
    assert run.run() == 0
    for name, command, _ in calls:
        if command[0] == "uv" and name != "uv_version":
            assert command[command.index("--directory") + 1] == str(ROOT)
            assert command[command.index("--project") + 1] == str(ROOT)
            assert "--no-config" in command


def test_tracked_private_roots_are_not_hashed_or_exported(
    verify, monkeypatch, tmp_path
):
    (tmp_path / "README.md").write_text("public")
    monkeypatch.setattr(
        verify.subprocess,
        "check_output",
        lambda command, **kwargs: (
            b"README.md\0.omx/private.md\0.codex/config.toml\0"
            if "--cached" in command
            else b""
        ),
    )
    monkeypatch.setattr(verify, "git", lambda *args: "test")
    identity = verify.source_identity(tmp_path)
    assert identity["excluded_tracked_count"] == 2
    assert [item["path"] for item in identity["files"]] == ["README.md"]
    assert ".omx" not in json.dumps(identity)
    assert ".codex" not in json.dumps(identity)


@pytest.mark.parametrize("name", [
    "LICENSE", "SUPPORT.md", "scripts/build_npm.py", "npm/package.json",
    "npm/README.md", "npm/bin/decision-browser.cjs", "npm/test/launcher.test.cjs",
])
def test_release_inputs_participate_in_source_identity(
    verify, monkeypatch, tmp_path, name
):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("original release document")
    monkeypatch.setattr(
        verify.subprocess, "check_output",
        lambda command, **kwargs: (name + "\0").encode()
        if "--cached" in command else b"",
    )
    monkeypatch.setattr(verify, "git", lambda *args: "test")
    before = verify.source_identity(tmp_path)
    assert [item["path"] for item in before["files"]] == [name]
    path.write_text("changed release document")
    after = verify.source_identity(tmp_path)
    assert before["tree_sha256"] != after["tree_sha256"]


def test_every_git_identity_command_ignores_ambient_selectors(verify, monkeypatch):
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT"):
        monkeypatch.setenv(name, "REDIRECT-CANARY")
    calls = []

    def output(command, **kwargs):
        calls.append(command)
        assert not any(name.startswith("GIT_") for name in kwargs["env"])
        return "commit" if kwargs.get("text") else b""

    monkeypatch.setattr(verify.subprocess, "check_output", output)
    verify.source_identity(ROOT)
    assert len(calls) == 4


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--profile", "invalid"],
        ["--unknown"],
        ["--timeout", "0"],
        ["--timeout", "-1"],
        ["--timeout", "nan"],
        ["--timeout", "inf"],
        ["--timeout", "not-a-number"],
    ],
)
def test_usage_errors_never_return_blocked(verify, monkeypatch, tmp_path, arguments):
    output = tmp_path / "new-evidence"
    argv = ["verify.py", "--output-dir", str(output), *arguments]
    if not arguments:
        argv = ["verify.py"]
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr(
        verify,
        "Verification",
        lambda *args: pytest.fail("invalid CLI started verification"),
    )
    with pytest.raises(SystemExit) as exc:
        verify.main()
    assert exc.value.code == 3
    assert not output.exists()


def test_child_environment_is_an_explicit_allowlist(verify, monkeypatch, tmp_path):
    safe = {
        "PATH": "/synthetic/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "LANG": "C.UTF-8",
        "UV_PROJECT_ENVIRONMENT": "/selected/environment",
        "UV_CACHE_DIR": "/selected/cache",
        "PLAYWRIGHT_BROWSERS_PATH": "/selected/browsers",
    }
    private = {
        name: "SYNTHETIC-PRIVATE-CANARY"
        for name in (
            "AZURE_OPENAI_API_KEY",
            "GITHUB_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "UNRECOGNIZED_CREDENTIAL",
            "OPENAI_API_KEY",
            "NODE_OPTIONS",
            "PLAYWRIGHT_CHROMIUM_SANDBOX",
            "LD_PRELOAD",
            "PYTHONPATH",
        )
    }
    monkeypatch.setattr(verify.os, "environ", safe | private)
    monkeypatch.setattr(verify, "git", lambda *args: "123")
    env = verify.verification_environment(ROOT)
    assert not (private.keys() & env.keys())
    assert safe.items() <= env.items()
    assert not (private.keys() & verify.git_environment().keys())

    # Exercise the actual log path with a wholly synthetic parent environment.
    log = tmp_path / "environment.log"
    assert (
        verify.run_command(
            [sys.executable, "-I", "-c", "import os; print(dict(os.environ))"],
            ROOT,
            env,
            log,
            5,
        )
        == 0
    )
    assert "SYNTHETIC-PRIVATE-CANARY" not in log.read_text()


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("pytest", 30), OSError()])
@pytest.mark.parametrize(
    "xml",
    [
        None,
        "<truncated",
        '<testsuites><testsuite tests="0" errors="0" failures="0" skipped="0" /></testsuites>',
    ],
)
def test_junit_problem_preserves_primary_command_failure(
    verify, runner, monkeypatch, error, xml
):
    make, _, _ = runner
    run = make("offline")
    junit = run.output / "pytest-unit.xml"
    if xml is not None:
        junit.write_text(xml)

    def fail(*args):
        raise error

    monkeypatch.setattr(verify, "run_command", fail)
    run.pytest_stage("pytest_unit", ["pytest"], junit)
    result = run.report["stages"]["pytest_unit"]
    assert result["status"] == "FAIL"
    assert result["reason"] == type(error).__name__
    assert result["junit_reason"] in {
        "missing_or_invalid_junit_report",
        "incomplete_or_failing_test_coverage",
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
@pytest.mark.parametrize(
    "signal_error", [None, ProcessLookupError(), PermissionError()]
)
@pytest.mark.parametrize(
    "wait_error", [None, subprocess.TimeoutExpired("child", 5), OSError()]
)
def test_timeout_cleanup_is_bounded_and_preserves_timeout(
    verify, monkeypatch, tmp_path, signal_error, wait_error
):
    waits = []

    class Process:
        pid = 12345

        def __enter__(self):
            return self

        def __exit__(self, *args):
            # Popen.__exit__ has an unconditional wait. Fail instead of hanging.
            self.wait()

        def wait(self, timeout=None):
            assert timeout is not None, "cleanup must never wait without a deadline"
            waits.append(timeout)
            if len(waits) == 1:
                raise subprocess.TimeoutExpired("child", timeout)
            if wait_error is not None:
                raise wait_error
            return -9

    def killpg(pid, sig):
        assert pid == 12345 and sig == verify.signal.SIGKILL
        if signal_error is not None:
            raise signal_error

    monkeypatch.setattr(verify.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(verify.os, "killpg", killpg)
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        verify.run_command(["synthetic-child"], ROOT, {}, tmp_path / "child.log", 0.01)
    assert len(waits) == 2
    assert 0 < waits[1] <= 5
    cleanup = exc.value.cleanup
    assert cleanup["leader_reaped"] is (wait_error is None)
    assert cleanup["status"] == (
        "ERROR"
        if isinstance(signal_error, PermissionError) or isinstance(wait_error, OSError)
        else "UNKNOWN"
        if wait_error is not None
        else "LEADER_REAPED"
    )


def test_timeout_cleanup_evidence_survives_missing_junit(verify, runner, monkeypatch):
    make, _, _ = runner
    run = make("offline")
    cleanup = {"status": "UNKNOWN", "leader_reaped": False}

    def fail(*args):
        raise verify.CommandTimeout(subprocess.TimeoutExpired("pytest", 30), cleanup)

    monkeypatch.setattr(verify, "run_command", fail)
    run.pytest_stage("pytest_unit", ["pytest"], run.output / "missing.xml")
    result = run.report["stages"]["pytest_unit"]
    assert result["reason"] == "TimeoutExpired"
    assert result["cleanup"] == cleanup
    assert result["junit_reason"] == "missing_or_invalid_junit_report"


@pytest.mark.parametrize("cleanup_status", ["UNKNOWN", "ERROR", "LEADER_REAPED"])
def test_unresolved_cleanup_is_failure_even_for_prerequisite(
    verify, runner, cleanup_status
):
    make, _, failures = runner
    cleanup = {
        "status": cleanup_status,
        "leader_reaped": cleanup_status == "LEADER_REAPED",
    }
    failures["browser_preflight"] = verify.CommandTimeout(
        subprocess.TimeoutExpired("browser", 30), cleanup
    )
    run = make("full")
    assert run.run() == (2 if cleanup_status == "LEADER_REAPED" else 1)
    result = run.report["stages"]["browser_preflight"]
    assert result["reason"] == "TimeoutExpired"
    assert result["cleanup"] == cleanup
    assert run.report["stages"]["pytest_full"]["status"] == "NOT_RUN"


def test_real_timed_out_child_is_reaped(verify, tmp_path):
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        verify.run_command(
            [sys.executable, "-I", "-c", "import time; time.sleep(30)"],
            ROOT,
            {},
            tmp_path / "timed-out-child.log",
            0.02,
        )
    assert exc.value.cleanup["status"] == "LEADER_REAPED"
    assert exc.value.cleanup["leader_reaped"] is True
