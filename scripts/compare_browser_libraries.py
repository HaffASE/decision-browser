"""Compare native browser libraries using one Decisions model and current cases.

No paid request is made by --list or --preflight. An actual run requires an
explicit output directory plus an environment key or named macOS Keychain item.
Source trees and Python environments for external libraries are caller-owned.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import random
import signal
import subprocess
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT / "scripts" / "benchmarks"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(SUPPORT))

from baseline_site import CASES as BASELINE
from baseline_site import FixtureSite

ENGINES = ("decision-browser", "jev-ultrafast", "jev-ra")
PUBLIC_URL = "https://www.python.org/"
PUBLIC_EXPECTED = "https://www.python.org/about/"
MODEL = "gpt-6-luna"


def load_scenarios():
    path = ROOT / "tests" / "integration" / "browser_scenarios.py"
    spec = importlib.util.spec_from_file_location("comparison_scenarios", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("scenario_module_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def canonical_goal(goal: str, values: dict[str, str]) -> str:
    """Identical goal bytes for all engines, including the goal-only library."""
    if not values:
        return goal
    return goal + "\n\nProvided field values: " + json.dumps(
        values, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def make_request(case: str, url: str, suite) -> dict[str, Any]:
    if case == "public_about":
        goal, values = "Open the About page on Python.org.", {}
        checks = [{"type": "url_equals", "value": PUBLIC_EXPECTED}]
    elif case in BASELINE:
        goal, values = BASELINE[case]["goal"], BASELINE[case]["values"]
        checks = [{"type": "text_equals", "target": "#status", "value": BASELINE[case]["expected"]},
                  {"type": "url_equals", "value": url + ("details" if case == "navigate" else "")}]
    else:
        spec = suite.CASES[case]
        goal, values = spec.goal, spec.values
        kinds = {"text": "text_equals", "route": "url_equals", "value": "value_equals"}
        checks = [{"type": kinds[c.kind], "target": c.target, "value": c.expected}
                  for c in spec.checks(url)]
    return {"start_url": url, "goal": canonical_goal(goal, values), "values": dict(values),
            "checks": checks, "max_steps": 24 if case in suite.CASES else 8,
            "max_provider_calls": 12, "timeout_s": 75., "max_input_bytes": 131072}


def schedule(cases: list[str], repetitions: int, seed: int = 83):
    for repetition in range(1, repetitions + 1):
        shuffled = list(cases)
        random.Random(seed + repetition).shuffle(shuffled)
        for position, case in enumerate(shuffled):
            rotation = (repetition + position) % len(ENGINES)
            engines = ENGINES[rotation:] + ENGINES[:rotation]
            if (repetition + position) % 2:
                engines = engines[::-1]
            for engine in engines:
                yield repetition, case, engine


def lifecycle_success(engine: str, result: dict) -> bool:
    """Page achievement alone does not prove a completed benchmark attempt."""
    usage = result.get("usage")
    cleanup = result.get("cleanup")
    if (type(result.get("returncode")) is not int or result["returncode"] != 0
            or not isinstance(usage, dict) or usage.get("usage_complete") is not True
            or result.get("usage_incomplete", False) is not False
            or type(result.get("unfinished_speculations", 0)) is not int
            or result.get("unfinished_speculations", 0) != 0
            or not isinstance(cleanup, dict)):
        return False
    if engine == "decision-browser":
        return cleanup == {"driver_state": "closed", "errors": []}
    if engine == "terra":
        return cleanup == {"browser": "closed"}
    if engine not in {"jev-ra", "jev-ultrafast"}:
        return False
    allowed = {"agent": {"closed"}, "chrome": {"already_exited", "terminated", "killed_after_timeout"},
               "daemon": {"already_reaped", "reaped", "terminated"}, "temporary_profile": {"removed"}}
    required = {"agent", "chrome", "temporary_profile"}
    return (required <= cleanup.keys() <= allowed.keys()
            and all(isinstance(value, str) and value in allowed[key] for key, value in cleanup.items()))


def outcome(engine: str, result: dict, oracle: dict,
            expected_checks: int | None = None) -> dict[str, bool]:
    if engine == "decision-browser":
        native = result.get("status") == "verified" and result.get("functional") == "verified"
    else:
        native = result.get("status") == "done" and result.get("reason") == "goal_achieved"
    checks = result.get("checks")
    functional = (isinstance(checks, list) and bool(checks)
                  and (expected_checks is None or len(checks) == expected_checks)
                  and all(value is True for value in checks))
    observed = functional and oracle.get("success") is True
    lifecycle = lifecycle_success(engine, result)
    return {"confirmed": bool(native and observed and lifecycle), "observed": bool(observed),
            "lifecycle_success": lifecycle,
            "native_success": bool(native), "functional_check_passed": functional}


def digest_files(root: Path, paths: list[Path]) -> dict:
    rows = []
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        if not path.is_file():
            continue
        relative = str(path.relative_to(root))
        content = path.read_bytes()
        name = relative.encode()
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(len(content).to_bytes(8, "big") + content)
        rows.append({"path": relative, "sha256": hashlib.sha256(content).hexdigest()})
    return {"sha256": digest.hexdigest(), "files": rows}


def source_identity(args) -> dict:
    core = ROOT / "src" / "decision_browser"
    corpus = ROOT / "tests" / "fixtures" / "scenarios"
    identities = {
        "decision-browser": digest_files(ROOT, [*core.glob("*.py"), *core.glob("*.js")]),
        "corpus": digest_files(ROOT, [*corpus.rglob("*"),
                   ROOT / "tests" / "integration" / "browser_scenarios.py", SUPPORT / "baseline_site.py"]),
        "harness": digest_files(ROOT, [Path(__file__), *SUPPORT.glob("*.py")]),
    }
    for engine, root, package in (
        ("jev-ultrafast", args.source_ultrafast, "jev_ultrafast"),
        ("jev-ra", args.source_jev_ra, "jev_ra"),
    ):
        paths = [p for p in (root / package).rglob("*")
                 if "__pycache__" not in p.parts and p.suffix not in {".pyc", ".pyo"}]
        paths += [root / name for name in ("pyproject.toml", "uv.lock")]
        identities[engine] = digest_files(root, paths)
        if (root / ".git").exists():
            commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                    capture_output=True, text=True, check=True, timeout=10).stdout.strip()
            dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                                   capture_output=True, text=True, check=True, timeout=10).stdout.strip()
            identities[engine].update(git_commit=commit, git_dirty=bool(dirty))
        else:
            identities[engine].update(git_commit=None, provenance="local adapted source; no Git metadata")
    return identities


def environment(key: str | None, args) -> dict[str, str]:
    allowed = ("PATH", "HOME", "TMPDIR", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ",
               "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
               "http_proxy", "https_proxy", "all_proxy")
    env = {name: os.environ[name] for name in allowed if name in os.environ}
    if key:
        env["OPENAI_API_KEY"] = key
    env.update(PLAYWRIGHT_BROWSERS_PATH=str(args.playwright_browsers),
               PYTHONDONTWRITEBYTECODE="1", NO_PROXY="127.0.0.1,localhost,::1",
               no_proxy="127.0.0.1,localhost,::1")
    return env


def preflight(args) -> dict:
    runtimes = {}
    code = ("import sys,json,importlib.metadata as m; "
            "print(json.dumps({'python':sys.version.split()[0], "
            "'packages':{p:m.version(p) for p in "
            "['httpx','browser-harness'] if m.packages_distributions().get(p.replace('-','_'))}}))")
    for engine, python in python_paths(args).items():
        result = subprocess.run([str(python), "-c", code], env=environment(None, args),
                                capture_output=True, text=True, timeout=20, check=True)
        runtimes[engine] = json.loads(result.stdout)
    version = subprocess.run([str(args.chromium), "--version"],
                             env=environment(None, args), capture_output=True, text=True,
                             timeout=10, check=True).stdout.strip()
    return {"sources": source_identity(args), "runtimes": runtimes, "browser_version": version,
            "browser_sha256": hashlib.sha256(args.chromium.read_bytes()).hexdigest()}


def python_paths(args):
    return {"decision-browser": args.python_core, "jev-ultrafast": args.python_ultrafast,
            "jev-ra": args.python_jev_ra}


def get_key(args) -> str:
    key = os.environ.get("OPENAI_API_KEY", "")
    if args.keychain_service:
        result = subprocess.run(["/usr/bin/security", "find-generic-password", "-s",
                                 args.keychain_service, "-w"], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30, check=False)
        key = result.stdout.strip() if result.returncode == 0 else ""
    if not key.startswith("sk-") or key.startswith("sk-or-") or "\n" in key:
        raise ValueError("OpenAI Platform key unavailable in the explicitly selected credential source")
    return key


def execute(engine: str, request: dict, args, key: str) -> dict:
    filename = {"decision-browser": "dbc_runner.py", "jev-ultrafast": "ultrafast_runner.py",
                "jev-ra": "jev_runner.py"}[engine]
    started = time.monotonic()
    child = subprocess.Popen([str(python_paths(args)[engine]), str(SUPPORT / filename)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=environment(key, args), start_new_session=True)
    try:
        stdout, stderr = child.communicate(json.dumps(request), timeout=request["timeout_s"] + 55)
    except subprocess.TimeoutExpired:
        # SIGINT unwinds Python finally blocks (including owned Chrome cleanup).
        # External native speculation can take up to 27 seconds to settle.
        os.killpg(child.pid, signal.SIGINT)
        try:
            child.communicate(timeout=45)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
        return {"status": "harness_timeout", "usage_incomplete": True,
                "wall_process_ms": round((time.monotonic() - started) * 1000)}
    try:
        result = json.loads(stdout.strip().splitlines()[-1])
        if not isinstance(result, dict):
            raise TypeError("non_object_result")
    except (TypeError, ValueError, IndexError):
        result = {"status": "harness_error", "usage_incomplete": True}
    # Arbitrary subprocess logs and exception messages never enter the evidence.
    safe = json.dumps(result, ensure_ascii=False).replace(key, "[redacted]")
    result = json.loads(safe)
    result.update(wall_process_ms=round((time.monotonic() - started) * 1000),
                  returncode=child.returncode, stderr_bytes=len(stderr.encode()))
    return result


def save(path: Path, value: dict):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def run(args, suite, identity: dict) -> int:
    key = get_key(args)
    args.output.mkdir(parents=True, exist_ok=False)
    target = args.output / "results.json"
    plan = list(schedule(args.cases, args.repetitions, args.seed))
    report = {"schema_version": 1, "status": "RUNNING", "started_at": datetime.now(UTC).isoformat(),
              "model": MODEL, "decision_endpoint": "https://api.openai.com/v1/decisions",
              "text_helper_endpoint": "https://api.openai.com/v1/responses",
              "identity": identity, "seed": args.seed, "cases": args.cases,
              "repetitions": args.repetitions, "planned_runs": len(plan),
              "estimated_spend_stop_usd": args.max_cost_usd,
              "input_protocol": "same goal plus canonical supplied-values JSON; native structured values retained",
              "limits": {"baseline_steps": 8, "expanded_steps": 24, "physical_calls": 12,
                         "input_bytes": 131072, "task_seconds": 75, "outer_seconds": 130,
                         "outer_interrupt_cleanup_grace_seconds": 45,
                         "viewport": {"width": 1280, "height": 720}},
              "runs": [], "not_run": [], "estimated_usd": 0., "usage_complete": True}
    save(target, report)
    try:
        with ExitStack() as stack:
            baseline = stack.enter_context(FixtureSite())
            # One fresh scenario origin per run prevents any cross-agent state.
            for index, (repetition, case, engine) in enumerate(plan):
                if not report["usage_complete"] or report["estimated_usd"] >= args.max_cost_usd:
                    report["stop_reason"] = "cost_threshold_or_incomplete_usage"
                    report["not_run"] = [{"repetition": rep, "case": name, "engine": library}
                                         for rep, name, library in plan[index:]]
                    break
                with ExitStack() as run_stack:
                    if case in suite.CASES:
                        site = run_stack.enter_context(suite.ScenarioSite())
                        url, oracle_id = site.start(case), case
                    elif case in BASELINE:
                        site = baseline
                        oracle_id, url = baseline.register(case)
                    else:
                        site, oracle_id, url = None, None, PUBLIC_URL
                    request = make_request(case, url, suite)
                    source = {"decision-browser": ROOT, "jev-ultrafast": args.source_ultrafast,
                              "jev-ra": args.source_jev_ra}[engine]
                    request.update(source_root=str(source), browser_executable=str(args.chromium),
                                   max_cost_usd=args.max_cost_usd - report["estimated_usd"])
                    result = execute(engine, request, args, key)
                    oracle = site.verify(oracle_id) if site else {
                        "success": result.get("final_url") == PUBLIC_EXPECTED,
                        "expected_url": PUBLIC_EXPECTED, "observed_url": result.get("final_url")}
                    score = outcome(engine, result, oracle, expected_checks=len(request["checks"]))
                usage = result.get("usage")
                complete = (isinstance(usage, dict) and usage.get("usage_complete") is True
                            and result.get("usage_incomplete") is not True)
                if isinstance(usage, dict):
                    report["estimated_usd"] += usage.get("estimated_usd", 0.)
                report["usage_complete"] &= complete
                row = {"attempt": index + 1, "repetition": repetition, "case": case, "engine": engine,
                       "group": "expanded" if case in suite.CASES else "baseline",
                       "goal_sha256": hashlib.sha256(request["goal"].encode()).hexdigest(),
                       **score, "oracle": oracle, "result": result}
                report["runs"].append(row)
                save(target, report)
                print(json.dumps({"attempt": index + 1, "of": len(plan), "engine": engine,
                                  "case": case, "repetition": repetition, **score,
                                  "status": result.get("status"), "reason": result.get("reason"),
                                  "calls": usage.get("attempts") if isinstance(usage, dict) else None,
                                  "total_estimated_usd": round(report["estimated_usd"], 8)},
                                 ensure_ascii=False), flush=True)
    except BaseException as exc:
        report.update(status="INTERRUPTED", interruption_type=type(exc).__name__)
        save(target, report)
        raise
    after = source_identity(args)
    report.update(status="COMPLETE" if not report["not_run"] else "STOPPED",
                  sources_unchanged=after == identity["sources"],
                  finished_at=datetime.now(UTC).isoformat())
    if not report["sources_unchanged"]:
        report["status"] = "INVALID_SOURCE_CHANGED"
    save(target, report)
    return 0 if report["status"] == "COMPLETE" and report["usage_complete"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--preflight", action="store_true", help="Validate sources and environments, without API/browser runs")
    parser.add_argument("--python-core", type=Path, default=Path(sys.executable))
    parser.add_argument("--python-ultrafast", type=Path)
    parser.add_argument("--python-jev-ra", type=Path)
    parser.add_argument("--source-ultrafast", type=Path)
    parser.add_argument("--source-jev-ra", type=Path)
    parser.add_argument("--chromium", type=Path)
    parser.add_argument("--playwright-browsers", type=Path)
    parser.add_argument("--keychain-service", help="Explicit macOS Keychain service; otherwise use OPENAI_API_KEY")
    parser.add_argument("--output", type=Path, help="New directory, existing evidence is never overwritten")
    parser.add_argument("--cases", help="Comma-separated IDs; default all current cases")
    parser.add_argument("--repetitions", type=int, choices=range(1, 4), default=3)
    parser.add_argument("--seed", type=int, default=83)
    parser.add_argument("--max-cost-usd", type=float, default=.25)
    args = parser.parse_args()
    suite = load_scenarios()
    allowed = [*BASELINE, "public_about", *suite.CASES]
    if args.list:
        for case in allowed:
            print(case)
        return 0
    args.cases = args.cases.split(",") if args.cases else allowed
    if (not args.cases or len(args.cases) != len(set(args.cases))
            or any(case not in allowed for case in args.cases)):
        parser.error("cases must be unique known scenario IDs")
    if not math.isfinite(args.max_cost_usd) or not 0 < args.max_cost_usd <= .25:
        parser.error("max-cost-usd must be finite, positive and at most 0.25")
    for name in ("python_core", "python_ultrafast", "python_jev_ra", "source_ultrafast",
                 "source_jev_ra", "chromium", "playwright_browsers"):
        path = getattr(args, name)
        if path is None or not path.exists():
            parser.error(f"--{name.replace('_', '-')} must identify an existing path")
        setattr(args, name, path.absolute())
    if not args.preflight and (args.output is None or args.output.exists()):
        parser.error("--output must be a new directory")
    identity = preflight(args)
    if args.preflight:
        print(json.dumps(identity, ensure_ascii=False, indent=2))
        return 0
    return run(args, suite, identity)


if __name__ == "__main__":
    raise SystemExit(main())
