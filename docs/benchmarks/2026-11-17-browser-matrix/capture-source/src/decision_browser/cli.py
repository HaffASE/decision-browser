"""decision-browser CLI. Serve speaks MCP on stdout. Diagnostics stay on stderr."""

from __future__ import annotations

import argparse
import os
import runpy
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, Literal


class ProjectPolicyError(ValueError):
    """Fixed local-policy diagnostics, without configuration contents."""


def _policy_info(info: os.stat_result, *, directory: bool = False) -> bool:
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    return (
        correct_type and (directory or info.st_nlink == 1)
        and (os.name == "nt" or info.st_uid == os.getuid() and not info.st_mode & 0o022)
    )


def _read_project_policy(root: Path) -> dict[str, Any] | None:
    """Bounded descriptor-first parsing; no symlink or writable policy boundary."""
    directory = root / ".codex"
    config = directory / "config.toml"
    try:
        try:
            parent = directory.lstat()
        except FileNotFoundError:
            return None
        if not _policy_info(parent, directory=True):
            raise ProjectPolicyError("project_policy_unreadable")
        try:
            fd = os.open(config, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not _policy_info(opened):
                raise ProjectPolicyError("project_policy_unreadable")
            raw = handle.read(65_537)
            current, current_parent = config.lstat(), directory.lstat()
            if (
                not _policy_info(current) or not _policy_info(current_parent, directory=True)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                or (parent.st_dev, parent.st_ino) != (current_parent.st_dev, current_parent.st_ino)
                or len(raw) > 65_536
            ):
                raise ProjectPolicyError("project_policy_unreadable")
        return tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError):
        raise ProjectPolicyError("project_policy_unreadable") from None
    except (tomllib.TOMLDecodeError, RecursionError):
        raise ProjectPolicyError("project_policy_invalid") from None


def _resolved_admission(
    mode: Literal["standard_form", "task_scope"], source: str,
) -> Literal["standard_form", "task_scope"]:
    print(f"admission_policy_source: {source}", file=sys.stderr)
    return mode


def _project_admission_mode() -> Literal["standard_form", "task_scope"]:
    """Resolve this exact project's saved launcher policy when host argv is cached."""
    root = Path.cwd().resolve()
    raw = _read_project_policy(root)
    if raw is None:
        return _resolved_admission("standard_form", "default_standard_form")
    servers = raw.get("mcp_servers", {})
    server = servers.get("decision_browser", {}) if isinstance(servers, dict) else {}
    if not isinstance(server, dict):
        return _resolved_admission("standard_form", "default_standard_form")
    command, cwd, args = server.get("command"), server.get("cwd"), server.get("args")
    # Accept isolated launchers while preserving recognition of saved legacy ones.
    if type(args) is list and args[:1] == ["-I"]:
        args = args[1:]
    try:
        if (
            type(command) is not str or type(cwd) is not str or type(args) is not list
            or Path(command).resolve() != Path(sys.executable).resolve()
            or Path(cwd).resolve() != root
            or args[:2] != ["-m", "decision_browser.keychain_entry"]
        ):
            return _resolved_admission("standard_form", "default_standard_form")
    except (OSError, ValueError, RuntimeError):
        raise ProjectPolicyError("project_policy_invalid") from None
    if len(args) == 2:
        return _resolved_admission("standard_form", "project_launcher")
    if (
        len(args) != 4 or args[2] != "--admission"
        or type(args[3]) is not str or args[3] not in {"standard_form", "task_scope"}
    ):
        raise ProjectPolicyError("project_policy_invalid")
    return _resolved_admission(
        "task_scope" if args[3] == "task_scope" else "standard_form", "project_launcher",
    )


def _skill_doctor() -> Path:
    checkout = (
        Path(__file__).resolve().parents[2]
        / "skills/decision-browser/scripts/doctor.py"
    )
    if checkout.is_file():
        return checkout
    return Path(__file__).resolve().parent / "skill/scripts/doctor.py"


def _serve(
    manager: Any | None, admission_mode: Literal["standard_form", "task_scope"] = "standard_form"
) -> int:
    if manager is None:
        from decision_browser.server import create_server

        manager = create_server(admission_mode=admission_mode)
    manager.run(transport="stdio")
    return 0


DOCTOR_TIMEOUT = 30


def _doctor(project: str | None, *, json_output: bool = False) -> int:
    script = _skill_doctor()
    if not script.is_file():
        print("doctor unavailable: use packaged skill doctor", file=sys.stderr)
        return 1
    cmd = [sys.executable, "-I", "-B", str(script)]
    if json_output:
        cmd.append("--json")
    if project is not None:
        cmd.extend(["--project", project])
    try:
        return subprocess.run(cmd, check=False, timeout=DOCTOR_TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        print("doctor timed out", file=sys.stderr)
        return 1


def _install(
    client: str | None, project: str, *, upgrade: bool, uninstall: bool, setup: bool = False,
) -> int:
    """Use the same installer from a wheel or a development checkout."""
    script = Path(__file__).resolve().with_name("install.py")
    if not script.is_file():
        script = Path(__file__).resolve().parents[2] / "scripts/install.py"
    if not script.is_file():
        print("installer unavailable: obtain the complete Decision Browser package", file=sys.stderr)
        return 1
    args = ["--project", project] if client is None else ["--client", client, "--project", project]
    if setup:
        args.append("--setup")
    if upgrade:
        args.append("--upgrade")
    if uninstall:
        args.append("--uninstall")
    return int(runpy.run_path(str(script))["main"](args))


def main(argv: list[str] | None = None, manager: Any | None = None) -> int:
    parser = argparse.ArgumentParser(prog="decision-browser")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--admission", choices=("standard_form", "task_scope"), default=None)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--project", default=None)
    install = sub.add_parser("install", help="install MCP and skill in the current project")
    install.add_argument("client", choices=("codex", "claude", "both"))
    install.add_argument("--project", default=".", help="existing project (default: current directory)")
    action = install.add_mutually_exclusive_group()
    action.add_argument("--upgrade", action="store_true", help="update all installed clients")
    action.add_argument("--uninstall", action="store_true", help="remove unchanged managed registrations")
    setup = sub.add_parser("setup", help="choose a client, install, and configure a provider")
    setup.add_argument("client", nargs="?", choices=("codex", "claude", "both"))
    setup.add_argument("--project", default=".", help="existing project (default: current directory)")
    setup.add_argument("--upgrade", action="store_true", help="update all installed clients")
    configure = sub.add_parser("configure", help="choose or change the saved provider interactively")
    configure.add_argument("provider", nargs="?", help="provider preset (omit to choose from a menu)")
    try:
        args = parser.parse_args(argv)
        if args.cmd == "serve":
            mode = (
                _resolved_admission(args.admission, "explicit_cli")
                if args.admission is not None else _project_admission_mode()
            )
            return _serve(manager, mode)
        if args.cmd == "install":
            return _install(args.client, args.project, upgrade=args.upgrade, uninstall=args.uninstall)
        if args.cmd == "setup":
            return _install(args.client, args.project, upgrade=args.upgrade, uninstall=False, setup=True)
        if args.cmd == "configure":
            from decision_browser.onboarding import configure as configure_provider

            return configure_provider(args.provider)
        return _doctor(args.project, json_output=True) if args.json else _doctor(args.project)
    except ProjectPolicyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - sanitized CLI boundary
        print(f"error: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
