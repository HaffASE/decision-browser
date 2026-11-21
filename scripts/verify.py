"""Reproduce local verification with locked packages and machine-readable evidence.

Requires Python 3.11+, git, and uv. No browser download, credential lookup,
provider request, native-host interaction, or manual-admission claim is made.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[1]
CONSTRAINTS = ROOT / "scripts" / "build-constraints.txt"
PUBLIC_ADDITIONS = {
    "hatch_build.py",
    "LICENSE",
    "SUPPORT.md",
    "scripts/install.py",
    "scripts/build_installer.py",
    "scripts/build_npm.py",
    "scripts/build_metadata.py",
    "docs/installation.md",
    "THIRD_PARTY_NOTICES.md",
    "tests/unit/test_installer.py",
    "CHANGELOG.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "docs/public-release-checklist.md",
    "scripts/check_release.py",
    "tests/unit/test_release.py",
    "docs/releasing.md",
    ".github/workflows/verify.yml",
    "scripts/run_counter_acceptance.py",
    "src/decision_browser/counter_acceptance.py",
    "tests/unit/test_counter_acceptance.py",
    "scripts/verify.py",
    "scripts/compare_browser_libraries.py",
    "scripts/render_library_comparison.py",
    "scripts/build-constraints.txt",
    "docs/verification.md",
    "tests/unit/test_verification_harness.py",
    "docs/public-release-plan.md",
    "src/decision_browser/onboarding.py",
    "src/decision_browser/user_config.py",
    "tests/unit/test_onboarding.py",
    "tests/unit/test_user_config.py",
    "tests/unit/test_gemini_provider.py",
    "tests/unit/test_setup_installer.py",
    "tests/unit/test_doctor_installation.py",
    "tests/unit/test_installer_network.py",
    "tests/unit/test_installer_provider_preservation.py",
    "npm/bin/uv-bootstrap.cjs",
    "npm/bin/uv-download.cjs",
    "npm/bin/uv-manifest.json",
    "npm/test/uv-bootstrap.test.cjs",
    "npm/test/uv-download.test.cjs",
    "scripts/prepare_tool_runtime.py",
    "tests/unit/test_tool_runtime.py",
}
NOT_RUN = {"status": "NOT_RUN", "reason": "outside_requested_profile"}
# Explicit runtime paths and locale settings only. Do not inherit credentials,
# proxy URLs, tool configuration, or arbitrary provider/browser overrides.
RUNTIME_ENVIRONMENT = {
    "PATH",
    "HOME",
    "USERPROFILE",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "XDG_CACHE_HOME",
}
VERIFICATION_ENVIRONMENT = {
    "UV_PROJECT_ENVIRONMENT",
    "UV_CACHE_DIR",
    "PLAYWRIGHT_BROWSERS_PATH",
}
CLEANUP_TIMEOUT = 5.0
ENVIRONMENT_CODE = """
import importlib.metadata as m, json, platform, sys
print(json.dumps({
    'python': sys.version,
    'implementation': platform.python_implementation(),
    'executable': sys.executable,
    'packages': sorted(
        ({'name': d.metadata['Name'], 'version': d.version} for d in m.distributions()),
        key=lambda d: d['name'].lower(),
    ),
}, sort_keys=True))
"""
SMOKE_CODE = """
import importlib, json, pathlib, sys
names = ['decision_browser', 'decision_browser.browser', 'decision_browser.cli',
         'decision_browser.server', 'mcp', 'playwright', 'httpx', 'pydantic']
modules = {name: importlib.import_module(name) for name in names}
location = pathlib.Path(modules['decision_browser'].__file__).resolve()
assert location.is_relative_to(pathlib.Path(sys.prefix).resolve()), location
print(json.dumps({'imports': names, 'package_file': str(location)}, sort_keys=True))
"""
BROWSER_CODE = """
import asyncio, json
from playwright.async_api import async_playwright
async def check():
    async with async_playwright() as p:
        # Keep Chromium's sandbox enabled. Never substitute a system browser or
        # downgrade launch security to make this prerequisite pass.
        browser = await p.chromium.launch(chromium_sandbox=True, channel="chromium", timeout=20000)
        try:
            page = await browser.new_page()
            await page.set_content('<title>verification-prerequisite</title><button>Check</button>')
            assert await page.title() == 'verification-prerequisite'
            await page.get_by_role('button').click(timeout=5000)
            assert (await page.screenshot(timeout=5000)).startswith(b'\\x89PNG')
            print(json.dumps({'browser': 'chromium', 'version': browser.version,
                              'sandbox_requested': True}, sort_keys=True))
        finally:
            await browser.close()
asyncio.run(check())
"""


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def git_environment() -> dict[str, str]:
    return {
        name: os.environ[name] for name in RUNTIME_ENVIRONMENT if name in os.environ
    }


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, env=git_environment()
    ).strip()


def public_source(name: str) -> bool:
    return name in PUBLIC_ADDITIONS | {
        "README.md",
        "pyproject.toml",
        "uv.lock",
        ".gitignore",
        "scripts/probe_browser.py",
    } or name.startswith(
        ("src/", "tests/", "skills/", "examples/", "docs/", ".github/workflows/",
         "scripts/", "npm/")
    )


def source_identity(root: Path) -> dict[str, Any]:
    # Do not open arbitrary untracked files: local configuration or credentials
    # may exist even when they are not covered by .gitignore.
    tracked = set(
        subprocess.check_output(
            ["git", "-C", str(root), "ls-files", "-z", "--cached"],
            env=git_environment(),
        )
        .decode()
        .split("\0")
    ) - {""}
    untracked = set(
        subprocess.check_output(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--others",
                "--exclude-standard",
            ],
            env=git_environment(),
        )
        .decode()
        .split("\0")
    ) - {""}
    public_tracked = {name for name in tracked if public_source(name)}
    paths = public_tracked | (untracked & PUBLIC_ADDITIONS)
    files = []
    for name in sorted(set(paths) - {""}):
        path = root / name
        digest: str | None
        if path.is_symlink():
            digest = hashlib.sha256(os.readlink(path).encode()).hexdigest()
        else:
            digest = sha256(path) if path.is_file() else None
        files.append(
            {
                "path": name,
                "sha256": digest,
                "mode": path.lstat().st_mode if path.exists() else None,
            }
        )
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return {
        "commit": git(root, "rev-parse", "HEAD"),
        "dirty": bool(git(root, "status", "--porcelain", "--untracked-files=all")),
        "tree_sha256": hashlib.sha256(encoded).hexdigest(),
        "files": files,
        "unverified_untracked_count": len(untracked - PUBLIC_ADDITIONS),
        "excluded_tracked_count": len(tracked - public_tracked),
    }


WINDOWS_ADDITIONAL_POSIX_EXCLUSIONS = (
    ('tests/unit/test_wave4_installer_lifecycle.py::test_root_crash_during_owned_stage_refuses_new_installer', 'POSIX installer SIGKILL/process-group ownership proof'),
    ('tests/unit/test_wave4_provider_routes.py::test_same_provider_rotation_keeps_bound_custom_route[typesafe]', 'POSIX private credential profile fixture'),
    ('tests/unit/test_wave4_provider_routes.py::test_same_provider_rotation_keeps_bound_custom_route[openrouter]', 'POSIX private credential profile fixture'),
    ('tests/unit/test_wave4_provider_routes.py::test_same_provider_rotation_keeps_bound_custom_route[opencode]', 'POSIX private credential profile fixture'),
    ('tests/unit/test_wave4_provider_routes.py::test_same_provider_rotation_keeps_bound_custom_route[gemini]', 'POSIX private credential profile fixture'),
    ('tests/unit/test_wave4_provider_routes.py::test_custom_profile_does_not_silently_receive_named_environment_key', 'POSIX private credential profile fixture'),
    ('tests/unit/test_wave4_provider_routes.py::test_custom_rotation_does_not_offer_unbound_named_environment_key', 'POSIX private credential profile fixture'),
    ('tests/unit/test_wave4_provider_routes.py::test_explicit_authenticated_local_route_is_preserved', 'POSIX private credential profile fixture'),
    ('tests/unit/test_wave4_provider_routes.py::test_local_configuration_reuses_key_only_for_exact_saved_endpoint[same-endpoint-keeps-key]', 'POSIX private credential profile fixture'),
    ('tests/unit/test_wave4_provider_routes.py::test_local_configuration_reuses_key_only_for_exact_saved_endpoint[changed-endpoint-clears-key]', 'POSIX private credential profile fixture'),
    ('tests/unit/test_wave4_provider_routes.py::test_unsafe_destination_rejected_before_secret_prompt[symlink_file]', 'POSIX symlink and private permission fixtures'),
    ('tests/unit/test_wave4_provider_routes.py::test_unsafe_destination_rejected_before_secret_prompt[symlink_parent]', 'POSIX symlink and private permission fixtures'),
    ('tests/unit/test_wave4_provider_routes.py::test_unsafe_destination_rejected_before_secret_prompt[public_directory]', 'POSIX symlink and private permission fixtures'),
    ('tests/unit/test_wave4_provider_routes.py::test_unsafe_destination_rejected_before_secret_prompt[public_file]', 'POSIX symlink and private permission fixtures'),
    ('tests/unit/test_wave4_provider_routes.py::test_unsafe_destination_rejected_before_secret_prompt[relative]', 'POSIX symlink and private permission fixtures'),
    ('tests/unit/test_wave4_provider_routes.py::test_save_destination_revalidated_after_prompt', 'POSIX private credential profile and symlink fixture'),
    ('tests/unit/test_wave4_provider_routes.py::test_implicit_policy_rejects_untrusted_file_boundary[file_link]', 'POSIX symlink and write-permission policy boundary'),
    ('tests/unit/test_wave4_provider_routes.py::test_implicit_policy_rejects_untrusted_file_boundary[parent_link]', 'POSIX symlink and write-permission policy boundary'),
    ('tests/unit/test_wave4_provider_routes.py::test_implicit_policy_rejects_untrusted_file_boundary[writable]', 'POSIX symlink and write-permission policy boundary'),
    ('tests/unit/test_wave4_installer.py::test_kernel_lease_releases_on_crash_without_unlink', 'POSIX native process/kernel/permission/dir_fd fixtures; portable Windows branches remain enabled'),
    ('tests/unit/test_wave4_installer.py::test_unsafe_lease_is_not_opened[symlink]', 'POSIX native process/kernel/permission/dir_fd fixtures; portable Windows branches remain enabled'),
    ('tests/unit/test_wave4_installer.py::test_unsafe_lease_is_not_opened[fifo]', 'POSIX native process/kernel/permission/dir_fd fixtures; portable Windows branches remain enabled'),
    ('tests/unit/test_wave4_installer.py::test_unsafe_lease_is_not_opened[hardlink]', 'POSIX native process/kernel/permission/dir_fd fixtures; portable Windows branches remain enabled'),
    ('tests/unit/test_wave4_installer.py::test_unsafe_lease_is_not_opened[public]', 'POSIX native process/kernel/permission/dir_fd fixtures; portable Windows branches remain enabled'),
    ('tests/unit/test_wave4_installer.py::test_stage_timeout_stops_owned_descendants_and_reports_fixed_hint', 'POSIX native process/kernel/permission/dir_fd fixtures; portable Windows branches remain enabled'),
    ('tests/unit/test_wave4_installer.py::test_child_umask_is_restrictive_without_parent_change', 'POSIX native process/kernel/permission/dir_fd fixtures; portable Windows branches remain enabled'),
    ('tests/unit/test_wave4_installer.py::test_atomic_write_rejects_symlink_and_preserves_target', 'POSIX native process/kernel/permission/dir_fd fixtures; portable Windows branches remain enabled'),
    ('tests/unit/test_wave4_installer.py::test_atomic_write_parent_swap_stays_on_held_directory', 'POSIX native process/kernel/permission/dir_fd fixtures; portable Windows branches remain enabled'),
    ('tests/unit/test_wave4_installer.py::test_actual_uv_runtime_created_under_permissive_parent_umask_is_valid', 'POSIX native process/kernel/permission/dir_fd fixtures; portable Windows branches remain enabled'),
    ('tests/unit/test_wave4_installer.py::test_concurrent_installer_refuses_before_mutable_plan', 'POSIX native process/kernel/permission/dir_fd fixtures; portable Windows branches remain enabled'),
)


def platform_exclusions() -> list[dict[str, Any]]:
    # These exact cases exercise killpg, which Windows does not provide. Never
    # deselect a whole family: a newly added case must receive explicit review.
    if sys.platform == "win32":
        family = "tests/unit/test_verification_harness.py::test_timeout_cleanup_is_bounded_and_preserves_timeout"
        return [
            {"nodeid": f"{family}[{wait}-{signal}]", "reason": "POSIX process groups"}
            for wait in ("None", "wait_error1", "wait_error2")
            for signal in ("None", "signal_error1", "signal_error2")
        ] + [{"nodeid": nodeid, "reason": reason}
             for nodeid, reason in WINDOWS_ADDITIONAL_POSIX_EXCLUSIONS]
    return []


def verification_environment(root: Path) -> dict[str, str]:
    env = {
        name: os.environ[name]
        for name in RUNTIME_ENVIRONMENT | VERIFICATION_ENVIRONMENT
        if name in os.environ
    }
    env.update(
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTHONHASHSEED="0",
        UV_PYTHON_DOWNLOADS="never",
        SOURCE_DATE_EPOCH=git(root, "show", "-s", "--format=%ct", "HEAD"),
    )
    return env


class CommandTimeout(subprocess.TimeoutExpired):
    """Keep the primary failure and bounded cleanup evidence together."""

    def __init__(
        self, cause: subprocess.TimeoutExpired, cleanup: dict[str, Any]
    ) -> None:
        super().__init__(cause.cmd, cause.timeout, cause.output, cause.stderr)
        self.cleanup = cleanup


def run_command(
    command: list[str], root: Path, env: dict[str, str], log: Path, timeout: float
) -> int:
    with log.open("w", encoding="utf-8") as stream:
        # Popen's context manager waits without a timeout on exit. Own this
        # lifecycle explicitly so an unreapable child cannot hang the verifier.
        process = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            cleanup: dict[str, Any] = {"status": "UNKNOWN", "leader_reaped": False}
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                cleanup["termination"] = "kill_sent"
            except ProcessLookupError:
                cleanup["termination"] = "not_found"
            except OSError as error:
                cleanup.update(status="ERROR", termination_error=type(error).__name__)
            try:
                process.wait(timeout=CLEANUP_TIMEOUT)
                cleanup["leader_reaped"] = True
                if cleanup["status"] != "ERROR":
                    cleanup["status"] = "LEADER_REAPED"
            except subprocess.TimeoutExpired:
                cleanup["wait_error"] = "TimeoutExpired"
            except OSError as error:
                cleanup.update(status="ERROR", wait_error=type(error).__name__)
            # Reaping the leader does not attest that all descendants exited.
            raise CommandTimeout(exc, cleanup) from exc


def aggregate(stages: dict[str, dict[str, Any]]) -> tuple[str, int]:
    statuses = {stage["status"] for stage in stages.values()}
    if "FAIL" in statuses:
        return "FAIL", 1
    if "BLOCKED" in statuses:
        return "BLOCKED", 2
    return "PASS", 0


class Verification:
    def __init__(self, root: Path, output: Path, profile: str, timeout: float) -> None:
        self.root = root
        self.output = output
        self.profile = profile
        self.timeout = timeout
        self.env = verification_environment(root)
        self.env["UV_OFFLINE"] = "1" if profile == "offline" else "0"
        environment = Path(self.env.get("UV_PROJECT_ENVIRONMENT", root / ".venv"))
        if not environment.is_absolute():
            environment = root / environment
        self.python = str(
            environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        self.report: dict[str, Any] = {
            "schema_version": 1,
            "profile": profile,
            "status": "RUNNING",
            "full_suite_passed": False,
            "started_at": datetime.now(UTC).isoformat(),
            "platform": platform.platform(),
            "platform_exclusions": platform_exclusions(),
            "source_before": source_identity(root),
            "input_sha256": {
                name: sha256(root / name)
                for name in (
                    "uv.lock",
                    "pyproject.toml",
                    "scripts/build-constraints.txt",
                    "scripts/verify.py",
                )
            },
            "stages": {},
            "acceptance": {
                name: dict(NOT_RUN)
                for name in (
                    "native_host",
                    "human_admission",
                    "live_provider",
                    "existing_browser",
                )
            },
        }
        self.save()

    def save(self) -> None:
        temporary = self.output / "result.json.tmp"
        temporary.write_text(
            json.dumps(self.report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(self.output / "result.json")

    def skip(self, name: str, reason: str) -> None:
        self.report["stages"][name] = {"status": "NOT_RUN", "reason": reason}
        self.save()

    def stage(
        self,
        name: str,
        command: list[str],
        *,
        blocked: bool = False,
        announce: bool = True,
    ) -> bool:
        log = self.output / f"{name}.log"
        started = time.monotonic()
        result: dict[str, Any] = {"command": command, "log": log.name}
        try:
            code = run_command(command, self.root, self.env, log, self.timeout)
            result.update(
                returncode=code,
                status="PASS" if code == 0 else "BLOCKED" if blocked else "FAIL",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            result.update(
                returncode=None,
                status="BLOCKED" if blocked else "FAIL",
                reason="TimeoutExpired"
                if isinstance(exc, subprocess.TimeoutExpired)
                else type(exc).__name__,
            )
            if isinstance(exc, CommandTimeout):
                result["cleanup"] = exc.cleanup
                if exc.cleanup["status"] in {"UNKNOWN", "ERROR"}:
                    result["status"] = "FAIL"
            if not log.exists():
                log.write_text(type(exc).__name__ + "\n", encoding="utf-8")
        result.update(
            seconds=round(time.monotonic() - started, 3), log_sha256=sha256(log)
        )
        self.report["stages"][name] = result
        self.save()
        if announce:
            print(f"{name}: {result['status']}", flush=True)
        return result["status"] == "PASS"

    def pytest_stage(self, name: str, command: list[str], junit: Path) -> None:
        self.stage(name, [*command, f"--junitxml={junit}"], announce=False)
        result = self.report["stages"][name]
        junit_reason = None
        try:
            suites = ET.parse(junit).getroot().findall(".//testsuite")
            counts = {
                key: sum(int(suite.attrib[key]) for suite in suites)
                for key in ("tests", "errors", "failures", "skipped")
            }
            result.update(junit=junit.name, junit_sha256=sha256(junit), counts=counts)
            if counts["tests"] == 0 or any(
                counts[key] for key in ("errors", "failures", "skipped")
            ):
                junit_reason = "incomplete_or_failing_test_coverage"
        except (OSError, ET.ParseError, KeyError, ValueError):
            junit_reason = "missing_or_invalid_junit_report"
        if junit_reason is not None:
            result["status"] = "FAIL"
            result["junit_reason"] = junit_reason
            result.setdefault("reason", junit_reason)
        self.save()
        print(f"{name}: {result['status']}", flush=True)

    def finish(self) -> int:
        after = source_identity(self.root)
        self.report["source_after"] = after
        unchanged = after == self.report["source_before"]
        self.report["stages"]["source_unchanged"] = {
            "status": "PASS" if unchanged else "FAIL"
        }
        status, code = aggregate(self.report["stages"])
        self.report.update(
            status=status,
            exit_code=code,
            full_suite_passed=status == "PASS"
            and self.profile == "full"
            and self.report["stages"].get("pytest_full", {}).get("status") == "PASS",
            finished_at=datetime.now(UTC).isoformat(),
        )
        self.save()
        print(
            json.dumps(
                {
                    "profile": self.profile,
                    "status": status,
                    "result": str(self.output / "result.json"),
                }
            )
        )
        return code

    def run(self) -> int:
        uv = [
            "uv",
            "--directory",
            str(self.root),
            "--project",
            str(self.root),
            "--no-config",
            "--no-python-downloads",
        ]
        if self.report["source_before"]["unverified_untracked_count"]:
            self.report["stages"]["source_scope"] = {
                "status": "FAIL",
                "reason": "unverified_untracked_files; commit intended source before verification",
            }
            return self.finish()
        bootstrapped = self.stage("uv_version", ["uv", "--version"])
        if bootstrapped:
            bootstrapped = self.stage("lock_check", [*uv, "lock", "--check"])
        if bootstrapped:
            # Avoid resolving unpinned editable-build dependencies during sync.
            bootstrapped = self.stage(
                "frozen_sync",
                [*uv, "sync", "--frozen", "--group", "dev", "--no-install-project"],
            )
        if not bootstrapped:
            for name in (
                "build",
                "install_wheel",
                "environment",
                "dependency_check",
                "ruff",
                "mypy",
                "smoke_import",
                "pytest_unit",
                "node_version",
                "browser_preflight",
                "pytest_full",
            ):
                self.skip(name, "environment_setup_failed")
            return self.finish()

        artifacts = self.output / "dist"
        built = self.stage(
            "build",
            [
                *uv,
                "build",
                "--python",
                self.python,
                "--build-constraints",
                str(CONSTRAINTS),
                "--out-dir",
                str(artifacts),
            ],
        )
        wheels = list(artifacts.glob("*.whl"))
        sdists = list(artifacts.glob("*.tar.gz"))
        if built and (len(wheels) != 1 or len(sdists) != 1):
            self.report["stages"]["build"].update(
                status="FAIL", reason="expected_one_wheel_and_one_sdist"
            )
            built = False
        self.report["artifacts"] = [
            {
                "file": str(p.relative_to(self.output)),
                "sha256": sha256(p),
                "bytes": p.stat().st_size,
            }
            for p in sorted(wheels + sdists)
        ]
        installed = built and self.stage(
            "install_wheel",
            [
                *uv,
                "pip",
                "install",
                "--python",
                self.python,
                "--no-deps",
                "--no-index",
                "--reinstall",
                str(wheels[0]),
            ],
        )
        if not built:
            self.skip("install_wheel", "build_failed")

        self.stage(
            "ruff",
            [self.python, "-I", "-m", "ruff", "check", "src", "tests", "scripts",
             "hatch_build.py", "skills/decision-browser/scripts/doctor.py"],
        )
        self.stage(
            "mypy",
            [
                self.python,
                "-I",
                "-m",
                "mypy",
                "src/decision_browser",
                "scripts/verify.py",
                "scripts/check_release.py",
                "scripts/install.py",
                "scripts/build_installer.py",
                "scripts/build_npm.py",
                "scripts/prepare_tool_runtime.py",
                "scripts/build_metadata.py",
                "skills/decision-browser/scripts/doctor.py",
            ],
        )
        if installed:
            self.stage("environment", [self.python, "-I", "-c", ENVIRONMENT_CODE])
            self.stage(
                "dependency_check", [*uv, "pip", "check", "--python", self.python]
            )
            self.stage("smoke_import", [self.python, "-I", "-c", SMOKE_CODE])
            pytest = [
                self.python,
                "-I",
                "-m",
                "pytest",
                "-p",
                "pytest_asyncio.plugin",
                "-o",
                "addopts=",
                "-q",
            ]
            for exclusion in self.report["platform_exclusions"]:
                pytest.extend(["--deselect", exclusion["nodeid"]])
            node_ready = self.stage("node_version", ["node", "--version"], blocked=True)
            if node_ready:
                self.pytest_stage(
                    "pytest_unit",
                    [*pytest, "tests/unit"],
                    self.output / "pytest-unit.xml",
                )
            else:
                self.skip("pytest_unit", "node_prerequisite_blocked")
            if self.profile == "full" and node_ready:
                ready = self.stage(
                    "browser_preflight",
                    [self.python, "-I", "-c", BROWSER_CODE],
                    blocked=True,
                )
                if ready:
                    self.pytest_stage(
                        "pytest_full",
                        [*pytest, "-vv", "-o", "faulthandler_timeout=60", "tests"],
                        self.output / "pytest-full.xml",
                    )
                else:
                    self.skip(
                        "pytest_full",
                        "browser_prerequisite_blocked; see browser_preflight.log",
                    )
            elif self.profile == "offline":
                self.skip("browser_preflight", "offline_profile_excludes_browser")
                self.skip("pytest_full", "offline_profile_is_unit_only")
            else:
                self.skip("browser_preflight", "node_prerequisite_blocked")
                self.skip("pytest_full", "node_prerequisite_blocked")
        else:
            for name in (
                "environment",
                "dependency_check",
                "smoke_import",
                "pytest_unit",
                "node_version",
                "browser_preflight",
                "pytest_full",
            ):
                self.skip(name, "built_wheel_not_installed")
        return self.finish()


class VerificationArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(3, f"{self.prog}: error: {message}\n")


def main() -> int:
    parser = VerificationArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("offline", "full"), default="full")
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="New evidence directory outside the checkout",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=900,
        help="Per-stage timeout in seconds (default: 900)",
    )
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if output.is_relative_to(ROOT) or output.exists():
        parser.error("--output-dir must be a new directory outside the checkout")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    output.mkdir(parents=True)
    return Verification(ROOT, output, args.profile, args.timeout).run()


if __name__ == "__main__":
    raise SystemExit(main())
