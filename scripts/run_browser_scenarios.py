"""Run the owned browser scenario corpus, with scripted or configured decisions.

Scripted decisions are the default and make no external model requests. Select
``--provider configured`` explicitly to exercise the production provider factory.
Only synthetic loopback pages are served; the fixture plan is never supplied to
the configured provider. Guard and unsupported-feature tests run through pytest.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT / "tests" / "integration" / "browser_scenarios.py"


def load_scenarios() -> Any:
    spec = importlib.util.spec_from_file_location("owned_browser_scenarios", SUPPORT)
    if spec is None or spec.loader is None:
        raise RuntimeError("scenario support is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def source_identity() -> str:
    digest = hashlib.sha256()
    paths = [*sorted((ROOT / "src" / "decision_browser").glob("*.py")),
             *sorted((ROOT / "src" / "decision_browser").glob("*.js")),
             *sorted((ROOT / "tests" / "fixtures" / "scenarios").rglob("*")),
             SUPPORT, Path(__file__)]
    for path in paths:
        if path.is_file():
            relative = str(path.relative_to(ROOT)).encode()
            data = path.read_bytes()
            digest.update(len(relative).to_bytes(8, "big") + relative)
            digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def save(stream: TextIO, report: dict[str, Any]) -> None:
    stream.seek(0)
    json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    stream.write("\n")
    stream.truncate()
    stream.flush()


def provider_metadata(mode: str) -> dict[str, Any]:
    if mode == "scripted":
        return {"provider": "scripted", "model": None, "endpoint": None}
    from decision_browser.provider_config import (
        ProviderConfigError,
        load_provider_settings,
    )

    try:
        settings = load_provider_settings()
    except ProviderConfigError as exc:
        return {"provider": "invalid", "error": exc.code}
    keys = [key for key in (settings.api_key, settings.text.api_key if settings.text else None) if key]

    def redacted(value: str) -> str:
        for key in keys:
            value = value.replace(key, "[redacted]")
        return value

    return {
        "provider": settings.provider,
        "model": redacted(settings.model),
        "endpoint": redacted(settings.endpoint),
        "max_choices": settings.max_choices,
        "text_helper": {"model": redacted(settings.text.model),
                        "endpoint": redacted(settings.text.endpoint)} if settings.text else None,
    }


async def run(args: argparse.Namespace, suite: Any, stream: TextIO) -> int:
    from decision_browser.contracts import Limits

    factory = None
    if args.provider == "configured":
        from decision_browser.server import production_factory

        factory = production_factory
    limits = Limits(max_steps=args.max_steps, max_seconds=args.max_seconds,
                    max_provider_calls=args.max_calls, max_input_bytes=args.max_input_bytes)
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "RUNNING",
        "started_at": datetime.now(UTC).isoformat(),
        "provider_mode": args.provider,
        "provider": provider_metadata(args.provider),
        "source_sha256": source_identity(),
        "scope": "owned loopback pages; isolated Chromium; task_scope admission",
        "cases": args.cases,
        "repetitions": args.repetitions,
        "limits_per_run": limits.model_dump(),
        "runs": [],
    }
    save(stream, report)
    try:
        for repetition in range(1, args.repetitions + 1):
            for case_id in args.cases:
                started = time.monotonic()
                try:
                    result = await suite.run_scenario(case_id, provider_factory=factory, limits=limits)
                except Exception as exc:  # noqa: BLE001 -- Persist failure without exposing response bodies.
                    # Exception text may contain a provider response or credentials.
                    result = {"case_id": case_id, "success": False,
                              "terminal": "harness_error", "error_type": type(exc).__name__}
                success = (result.get("terminal") == "verified"
                           and result.get("functional") == "verified"
                           and result.get("oracle", {}).get("success") is True)
                result.update(success=success, repetition=repetition,
                              wall_ms=round((time.monotonic() - started) * 1000))
                report["runs"].append(result)
                save(stream, report)
                print(json.dumps({"case": case_id, "repetition": repetition,
                                  "success": success, "terminal": result["terminal"]}), flush=True)
    except BaseException:
        report["status"] = "INTERRUPTED"
        save(stream, report)
        raise
    report.update(
        status="PASS" if all(row["success"] for row in report["runs"]) else "FAIL",
        passed=sum(row["success"] for row in report["runs"]),
        total=len(report["runs"]),
        finished_at=datetime.now(UTC).isoformat(),
    )
    save(stream, report)
    print(json.dumps({"status": report["status"], "passed": report["passed"],
                      "total": report["total"], "output": str(args.output)}), flush=True)
    return 0 if report["status"] == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="List cases without opening a browser or provider")
    parser.add_argument("--provider", choices=("scripted", "configured"), default="scripted")
    parser.add_argument("--cases", help="Comma-separated case IDs; defaults to the complete corpus")
    parser.add_argument("--repetitions", type=int, choices=range(1, 4), default=1)
    parser.add_argument("--output", type=Path, help="New JSON report; existing evidence is never overwritten")
    parser.add_argument("--max-steps", type=int, default=24)
    parser.add_argument("--max-calls", type=int, default=24)
    parser.add_argument("--max-seconds", type=float, default=45.0)
    parser.add_argument("--max-input-bytes", type=int, default=262144)
    args = parser.parse_args()
    suite = load_scenarios()
    if args.list:
        print(json.dumps([{"id": key, "goal": case.goal} for key, case in suite.CASES.items()],
                         ensure_ascii=False, indent=2))
        return 0
    args.cases = list(suite.CASES) if args.cases is None else args.cases.split(",")
    if not args.cases or len(set(args.cases)) != len(args.cases) or any(
        case_id not in suite.CASES for case_id in args.cases
    ):
        parser.error("--cases must name unique IDs from --list")
    if args.output is None:
        parser.error("--output is required unless --list is used")
    from decision_browser.contracts import Limits

    try:
        Limits(max_steps=args.max_steps, max_seconds=args.max_seconds,
               max_provider_calls=args.max_calls, max_input_bytes=args.max_input_bytes)
    except ValueError:
        parser.error("invalid per-run limits")
    try:
        stream = args.output.open("x", encoding="utf-8")
    except FileExistsError:
        parser.error("--output already exists; choose a new report path")
    with stream:
        return asyncio.run(run(args, suite, stream))


if __name__ == "__main__":
    raise SystemExit(main())
