#!/usr/bin/env python3
"""Observation-only prerequisite doctor. No network, browser launch, or secret values.

local_ready and live_ready are prerequisite observations only. Both booleans
reflect Python, pinned package compatibility, server importability, an optional project path, and the selected
provider configuration. They are never live, native, browser, entitlement, or human
evidence. No live check has run. api_key_present is a boolean only.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import importlib.util
import json
import platform
import sys
from pathlib import Path
from types import ModuleType

tomllib: ModuleType | None
try:
    tomllib = importlib.import_module("tomllib")
except ModuleNotFoundError:
    tomllib = None  # Report unsupported Python instead of failing before diagnostics.

# Bundled doctor also runs from a wheel, where pyproject.toml is unavailable.
# A regression checks these exact pins against the project metadata.
DEPS = {"playwright": "1.64.1", "mcp": "2.3.0", "httpx": "0.28.1", "certifi": "2026.10.23", "pydantic": "2.14.2"}
CAP = 128
NOT_RUN = (
    "NOT_RUN driver",
    "NOT_RUN entitlement",
    "NOT_RUN nativeMCP",
    "NOT_RUN admission",
    "NOT_RUN liveAPI",
)


def _clip(text: str, cap: int = CAP) -> str:
    text = " ".join(text.split())
    return text if len(text) <= cap else text[: cap - 3] + "..."


def _dep(name: str) -> dict:
    available = False
    version = None
    try:
        available = importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        available = False
    try:
        version = _clip(importlib.metadata.version(name))
    except (importlib.metadata.PackageNotFoundError, ImportError, ValueError):
        version = None
    return {"available": bool(available), "version": version}


def _server_importable() -> bool:
    try:
        importlib.import_module("decision_browser.server")
    except Exception:  # noqa: BLE001 - report only a boolean, never exception payloads
        return False
    return True


def _provider_status() -> dict:
    try:
        module = importlib.import_module("decision_browser.provider_config")
        return module.provider_status()
    except Exception:  # noqa: BLE001 - dependency/config import errors stay secret-free
        return {
            "provider": "unavailable", "api_key_present": False,
            "provider_ready": False, "provider_error": "provider_config_unavailable",
            "text_generation": "unconfigured", "visual": "unsupported",
        }


def _existing_chrome_status() -> dict:
    """Path selection only: no profile file, endpoint, or browser is opened."""
    try:
        module = importlib.import_module("decision_browser.browser")
    except Exception:  # noqa: BLE001 - dependency errors stay bounded and secret-free
        return {"default_profile_supported": False, "readiness": "UNAVAILABLE", "native_qualification": "NOT_RUN"}
    try:
        module.default_devtools_active_port()
    except module.BrowserBlocked:
        return {"default_profile_supported": False, "readiness": "UNSUPPORTED", "native_qualification": "NOT_RUN"}
    return {"default_profile_supported": True, "readiness": "NOT_RUN", "native_qualification": "NOT_RUN"}


def _project(raw: str | None) -> dict | None:
    if raw is None:
        return None
    path = Path(raw)
    try:
        exists = path.exists()
        directory = path.is_dir() if exists else False
    except OSError:
        exists, directory = False, False
    return {"provided": True, "exists": bool(exists), "directory": bool(directory)}


def _installation(raw: str | None) -> dict:
    """Inspect only local registration; never claim trust or a live MCP connection."""
    result: dict = {"client_connection": "NOT_RUN", "next_steps": []}
    if raw is None:
        result["next_steps"] = ["Pass --project to inspect client registration."]
        return result
    root = Path(raw)
    for client, filename, key, skill in (
        ("codex", ".codex/config.toml", "mcp_servers", ".agents"),
        ("claude", ".mcp.json", "mcpServers", ".claude"),
    ):
        item = {"registration": "missing", "runtime_exists": False,
                "skill_present": (root / skill / "skills/decision-browser/SKILL.md").is_file()}
        try:
            config = root / filename
            if config.is_file():
                with config.open("rb") as handle:
                    data = handle.read(1_048_577)
                if len(data) > 1_048_576:
                    raise ValueError("size")
                if client == "codex":
                    if tomllib is None:
                        item["registration"] = "unavailable_python"
                        result[client] = item
                        continue
                    parsed = tomllib.loads(data.decode())
                else:
                    parsed = json.loads(data)
                server = parsed.get(key, {}).get("decision_browser")
                if server is not None:
                    if not isinstance(server, dict) or not isinstance(server.get("command"), str):
                        raise ValueError("server")
                    item["registration"] = "present"
                    command = Path(server["command"])
                    item["runtime_exists"] = command.is_absolute() and command.is_file()
        except (OSError, ValueError, TypeError, AttributeError):
            item["registration"] = "invalid"
        result[client] = item
    result["next_steps"] = [
        "If registration/runtime/skill is missing, rerun install for that client.",
        "Restart the client in this project and review its trust/MCP prompts.",
        "Confirm decision_browser connects in the client and call capabilities.",
        "Run decision-browser configure to save a provider, or use explicit environment settings; then run an approved task.",
    ]
    return result


def build_report(project_path: str | None) -> dict:
    supported = sys.version_info >= (3, 11)
    deps = {name: _dep(name) for name in DEPS}
    project = _project(project_path)
    project_ok = project is None or (project["exists"] and project["directory"])
    for name, item in deps.items():
        item["required"] = DEPS[name]
        item["compatible"] = bool(item["available"] and item["version"] == DEPS[name])
    deps_ok = all(item["compatible"] for item in deps.values())
    server_importable = _server_importable()
    provider = _provider_status()
    local_ready = bool(supported and deps_ok and server_importable and project_ok)
    live_ready = bool(local_ready and provider["provider_ready"])
    notes = list(NOT_RUN)
    if not supported:
        notes.append("Python >= 3.11 required")
    missing = [name for name, item in deps.items() if not item["available"]]
    if missing:
        notes.append("missing: " + ",".join(missing))
    incompatible = [name for name, item in deps.items() if not item["compatible"]]
    if incompatible:
        notes.append("incompatible: " + ",".join(incompatible))
    if not server_importable:
        notes.append("server import failed")
    if project is not None and not project_ok:
        notes.append("project path is not an existing directory")
    if provider["provider"] == "openai" and provider["provider_error"] == "missing_api_key":
        notes.append("OPENAI_API_KEY absent; live_status BLOCKED")
    elif not provider["provider_ready"]:
        notes.append("selected provider prerequisites absent; live_status BLOCKED")
    notes.append("no live/native/schema/entitlement/browser proof")
    return {
        "python": {"version": _clip(platform.python_version()), "supported": supported},
        "platform": _clip(platform.platform()),
        "dependencies": deps,
        "dependencies_compatible": deps_ok,
        "server_importable": server_importable,
        **provider,
        "project": project,
        "local_ready": local_ready,
        "live_ready": live_ready,
        "installation": _installation(project_path),
        "existing_chrome": _existing_chrome_status(),
        "readiness_scope": "prerequisites_only",
        "local_status": "READY" if local_ready else "BLOCKED",
        "live_status": "PREREQUISITES_PRESENT" if live_ready else "BLOCKED",
        "notes": [_clip(n, 160) for n in notes[:12]],
    }


def _installation_summary(installation: dict) -> dict:
    summary: dict = {"client_connection": "NOT_RUN", "next_steps": ["Rerun doctor --project for registration details."]}
    states = {"present", "missing", "invalid", "unavailable_python"}
    for client in ("codex", "claude"):
        item = installation.get(client)
        if isinstance(item, dict):
            summary[client] = {
                "registration": item.get("registration") if item.get("registration") in states else "invalid",
                "runtime_exists": item.get("runtime_exists") is True,
                "skill_present": item.get("skill_present") is True,
            }
    return summary


def _emit(report: dict, *, json_output: bool = True) -> None:
    payload = json.dumps(report, separators=(",", ":"), ensure_ascii=True)
    if len(payload.encode("utf-8")) > 8192:
        report = {
            "python": report["python"],
            "platform": _clip(str(report.get("platform", "")), 32),
            "dependencies": {
                name: {"available": item["available"], "version": None}
                for name, item in report["dependencies"].items()
            },
            "dependencies_compatible": report["dependencies_compatible"],
            "server_importable": report["server_importable"],
            "api_key_present": report["api_key_present"],
            "provider": report["provider"],
            "provider_ready": report["provider_ready"],
            "provider_error": report["provider_error"],
            "text_generation": report["text_generation"],
            "visual": report["visual"],
            "project": report["project"],
            "local_ready": report["local_ready"],
            "live_ready": report["live_ready"],
            "installation": _installation_summary(report["installation"]),
            "existing_chrome": report["existing_chrome"],
            "readiness_scope": "prerequisites_only",
            "local_status": report["local_status"],
            "live_status": report["live_status"],
            "notes": ["truncated to bound"],
        }
        payload = json.dumps(report, separators=(",", ":"), ensure_ascii=True)
    if not json_output:
        payload = "\n".join(
            f"{name}: {json.dumps(value, separators=(',', ':'), ensure_ascii=True)}"
            for name, value in report.items()
        )
    sys.stdout.write(payload + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Observation-only prerequisite doctor")
    parser.add_argument("--json", action="store_true", help="Print JSON report")
    parser.add_argument(
        "--project", default=None, help="Optional project directory path"
    )
    args = parser.parse_args(argv)
    try:
        report = build_report(args.project)
    except (OSError, ValueError, TypeError, ImportError, RuntimeError):
        sys.stderr.write("doctor failed\n")
        return 1
    _emit(report, json_output=args.json)
    return 0 if report["local_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
