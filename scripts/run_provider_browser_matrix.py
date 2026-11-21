"""Run browser cases against selected direct or OpenCode Console routes.

Only --list is offline. Each selected cell is attempted once, without retries;
COMPLETE means all cells finished, not that every task passed. Secrets are read
only by this parent and passed to workers through their environment.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import shlex
import signal
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts/benchmarks"))
import compare_browser_libraries as bench
import long_protocol
import long_scenarios
from provider_matrix_worker import CONSOLE_SETTINGS, ROUTES

WORKER = ROOT / "scripts/benchmarks/provider_matrix_worker.py"
ARMS = {"jev": {"model": "jev-latest", "endpoint": "https://api.typesafe.ai/v1/systemone"},
        "decision_api": {"model": "gpt-6-luna", "endpoint": "https://api.openai.com/v1/decisions"},
        "terra": {"model": "gpt-6.1-terra", "endpoint": "https://api.openai.com/v1/responses",
                  "reasoning_effort": "medium"}}
DEFAULT_ARMS = tuple(ARMS)
ARMS.update({
    arm: {"model": settings.model, "endpoint": settings.endpoint}
    for arm, settings in CONSOLE_SETTINGS.items()
})
TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cached_tokens",
                "cache_write_tokens", "reasoning_tokens")


def now():
    return datetime.now(UTC).isoformat()


def schedule(cases, arms, repetitions, seed=20261117):
    for repetition in range(1, repetitions + 1):
        shuffled = list(cases)
        random.Random(seed + repetition).shuffle(shuffled)
        for position, case in enumerate(shuffled):
            offset = (repetition + position) % len(arms)
            for arm in arms[offset:] + arms[:offset]:
                yield {"arm": arm, "case": case, "repetition": repetition}


def all_cases(suite):
    return [*bench.BASELINE, "public_about", *suite.CASES, *long_scenarios.CASES]


def failed_cells(baseline, full_plan):
    def cell_id(row):
        return row["arm"], row["case"], row["repetition"]

    rows = baseline["runs"]
    recorded = {cell_id(row): row for row in rows}
    if (
        baseline["status"] != "COMPLETE"
        or baseline["sources_unchanged"] is not True
        or len(rows) != len(recorded)
        or set(recorded) != {cell_id(cell) for cell in full_plan}
        or any(row["phase"] != "finished" or type(row["confirmed"]) is not bool for row in rows)
    ):
        raise ValueError("invalid_failure_baseline")
    return [cell for cell in full_plan if not recorded[cell_id(cell)]["confirmed"]]


def source_identity():
    files = [*ROOT.glob("src/decision_browser/*.py"), *ROOT.glob("src/decision_browser/*.js"),
             *ROOT.glob("scripts/benchmarks/*.py"), *ROOT.glob("tests/fixtures/scenarios/**/*"),
             ROOT / "tests/integration/browser_scenarios.py", ROOT / "scripts/compare_browser_libraries.py",
             ROOT / "docs/benchmarks/2026-11-03-terra-medium/agent.py", ROOT / "pyproject.toml",
             Path(__file__)]
    return bench.digest_files(ROOT, files)


def read_jev_key(path):
    if key := os.environ.get("TYPESAFE_API_KEY", "").strip():
        return key
    if path is not None:
        for line in path.read_text().splitlines():
            parts = line.strip().removeprefix("export ").split("=", 1)
            if len(parts) == 2 and parts[0].strip() == "TYPESAFE_API_KEY":
                values = shlex.split(parts[1], comments=True)
                if values and values[0]:
                    return values[0]
    raise RuntimeError("jev_key_unavailable")


def keychain_key(service):
    if sys.platform != "darwin":
        return ""
    result = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
                            capture_output=True, text=True, timeout=20, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def credentials(args):
    keys = {}
    if "jev" in args.arms:
        keys["jev"] = read_jev_key(args.jev_env_file)
    if openai_arms := set(args.arms) & {"decision_api", "terra"}:
        key = os.environ.get("OPENAI_API_KEY", "").strip() or keychain_key("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("openai_key_unavailable")
        keys.update(dict.fromkeys(openai_arms, key))
    if console_arms := set(args.arms) & CONSOLE_SETTINGS.keys():
        key = os.environ.get("OPENCODE_API_KEY", "").strip() or keychain_key("codex-opencode-console")
        if not key:
            raise RuntimeError("opencode_console_key_unavailable")
        keys.update(dict.fromkeys(console_arms, key))
    if any(not key.isascii() or any(c.isspace() for c in key) for key in keys.values()):
        raise RuntimeError("invalid_credential")
    return keys


def safe(value, keys):
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    for key in keys.values():
        if key in encoded or json.dumps(key, ensure_ascii=False)[1:-1] in encoded:
            raise RuntimeError("unsafe_worker_output")
    return value


def child_environment(args, arm, key):
    env = bench.environment(None, SimpleNamespace(playwright_browsers=args.browsers))
    env["PYTHONPATH"] = str(ROOT / "src")
    env[ROUTES[arm][0]] = key
    return env


def terminate(child):
    try:
        os.killpg(child.pid, signal.SIGINT)
    except ProcessLookupError:
        pass
    try:
        return child.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return child.communicate(timeout=8)


def execute(args, request, key, keys):
    started = time.monotonic()
    child = subprocess.Popen([str(args.python), "-B", str(WORKER)],
                             env=child_environment(args, request["arm"], key), stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    timed_out = False
    try:
        stdout, stderr = child.communicate(json.dumps(request), timeout=request["timeout_s"] + 45)
    except subprocess.TimeoutExpired:
        timed_out = True
        stdout, stderr = terminate(child)
    try:
        result = json.loads(stdout.strip().splitlines()[-1])
        if not isinstance(result, dict):
            raise TypeError("non_object_output")
    except (ValueError, IndexError, TypeError):
        result = {"status": "harness_error", "reason": "invalid_worker_output", "checks": [],
                  "usage": {"calls": [], "usage_complete": False, "attempts": None}}
    if timed_out:
        result.update(status="harness_timeout", reason="outer_deadline", usage_incomplete=True)
    for secret in keys.values():
        stderr = stderr.replace(secret, "[redacted]")
    result.update(returncode=child.returncode, wall_ms=round((time.monotonic() - started) * 1000),
                  stderr=stderr.encode()[:32768].decode(errors="replace"), stderr_bytes=len(stderr.encode()))
    return safe(result, keys)


def make_request(case, url, suite):
    request = (long_protocol.make_request(case, url) if case in long_scenarios.CASES
               else bench.make_request(case, url, suite))
    request.update(max_calls=request["max_provider_calls"], max_cost_usd=.25)
    return request


def measure(args, cell, suite, keys):
    relative = Path("runs") / cell["arm"] / cell["case"] / f"{cell['repetition']}.json"
    target = args.output / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    row = {**cell, "phase": "pending", "started_at": now(), "record": str(relative)}
    bench.save(target, row)
    result = None
    try:
        with ExitStack() as stack:
            case = cell["case"]
            if case in bench.BASELINE:
                site = stack.enter_context(bench.FixtureSite())
                oracle_id, url = site.register(case)
            elif case == "public_about":
                site, oracle_id, url = None, None, bench.PUBLIC_URL
            else:
                module = long_scenarios if case in long_scenarios.CASES else suite
                site = stack.enter_context(module.ScenarioSite())
                oracle_id, url = case, site.start(case)
            request = make_request(case, url, suite)
            request.update(arm=cell["arm"], source_root=str(ROOT), browser_executable=str(args.chromium))
            row.update(group="long" if case in long_scenarios.CASES else "short",
                       goal_sha256=hashlib.sha256(request["goal"].encode()).hexdigest(),
                       limits={k: request[k] for k in ("max_steps", "max_calls", "timeout_s", "max_input_bytes")})
            result = execute(args, request, keys[cell["arm"]], keys)
            oracle = site.verify(oracle_id) if site else {
                "success": result.get("final_url") == bench.PUBLIC_EXPECTED,
                "expected_url": bench.PUBLIC_EXPECTED, "observed_url": result.get("final_url")}
            engine = "terra" if cell["arm"] == "terra" else "decision-browser"
            row.update(**bench.outcome(engine, result, oracle, expected_checks=len(request["checks"])),
                       result=result, oracle=oracle)
    except Exception as exc:  # noqa: BLE001 - Retain coded diagnostics and paid usage, never exception text.
        row.update(confirmed=False, native_success=False, observed=False, functional_check_passed=False,
                   lifecycle_success=False, harness_error=type(exc).__name__,
                   result=result if result is not None else {
                       "status": "harness_error", "reason": type(exc).__name__, "checks": [],
                       "usage": {"calls": [], "usage_complete": False, "attempts": None}})
    row.update(phase="finished", finished_at=now())
    bench.save(target, safe(row, keys))
    return row


def token_totals(rows):
    calls = [call for row in rows for call in (row["result"].get("usage") or {}).get("calls", [])]
    unknown_runs = sum((row["result"].get("usage") or {}).get("attempts") is None
                       or row["result"].get("usage_incomplete") is True for row in rows)
    totals = {}
    for field in TOKEN_FIELDS:
        known = sum(c[field] for c in calls if type(c.get(field)) is int and c[field] >= 0)
        missing = sum(type(c.get(field)) is not int or c[field] < 0 for c in calls)
        totals[field] = {"total": None if missing or unknown_runs else known,
                         "reported_total": known, "missing_calls": missing, "unknown_runs": unknown_runs}
    return totals


def summarize(rows, planned):
    def aggregate(selected):
        durations = [r["result"]["wall_ms"] for r in selected if type(r["result"].get("wall_ms")) is int]
        errors = {}
        for row in selected:
            if not row["confirmed"]:
                reason = row.get("harness_error") or row["result"].get("reason") or row["result"].get("status", "unknown")
                errors[reason] = errors.get(reason, 0) + 1
        calls = [c for r in selected for c in (r["result"].get("usage") or {}).get("calls", [])]
        return {"attempts": len(selected), "confirmed": sum(r["confirmed"] for r in selected),
                "observed": sum(r["observed"] for r in selected),
                "usage_complete_runs": sum((r["result"].get("usage") or {}).get("usage_complete") is True for r in selected),
                "calls": len(calls), "http_errors": sum(type(c.get("status")) is int and c["status"] >= 300 for c in calls),
                "median_wall_ms": statistics.median(durations) if durations else None,
                "tokens": token_totals(selected), "errors": errors,
                "unknown_usage_runs": sum((r["result"].get("usage") or {}).get("attempts") is None
                                          or r["result"].get("usage_incomplete") is True for r in selected)}
    arms = list(dict.fromkeys(c["arm"] for c in planned))
    cases = list(dict.fromkeys(c["case"] for c in planned))
    return {"planned_runs": len(planned), "completed_runs": len(rows),
            "verdict": "PASS" if len(rows) == len(planned) and all(r["confirmed"] for r in rows) else "HAS_FAILURES",
            "arms": {arm: aggregate([r for r in rows if r["arm"] == arm]) for arm in arms},
            "cases": {case: {arm: aggregate([r for r in rows if r["case"] == case and r["arm"] == arm])
                             for arm in arms} for case in cases}}


def write_csv(output, rows):
    fields = ["arm", "case", "repetition", "confirmed", "observed", "status", "reason", "harness_error", "wall_ms", "calls",
              *TOKEN_FIELDS, "usage_complete", "record"]
    with (output / "runs.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            result, usage = row["result"], row["result"].get("usage") or {}
            totals = token_totals([row])
            writer.writerow({**{k: row[k] for k in ("arm", "case", "repetition", "confirmed", "observed", "record")},
                             **{k: result.get(k) for k in ("status", "reason", "wall_ms")},
                             "harness_error": row.get("harness_error"),
                             "calls": usage.get("attempts"), "usage_complete": usage.get("usage_complete") is True,
                             **{k: totals[k]["total"] for k in TOKEN_FIELDS}})


def run(args, suite):
    plan = list(schedule(args.cases, args.arms, args.repetitions, args.seed))
    baseline_sha256 = None
    if baseline_path := getattr(args, "failures_from", None):
        baseline = baseline_path.read_bytes()
        plan = failed_cells(json.loads(baseline), plan)
        if not plan:
            raise ValueError("empty_failure_cohort")
        baseline_sha256 = hashlib.sha256(baseline).hexdigest()
    keys = credentials(args)
    args.output.mkdir(parents=True, exist_ok=False)
    identity = source_identity()
    env = bench.environment(None, SimpleNamespace(playwright_browsers=args.browsers))
    browser = subprocess.run([str(args.chromium), "--version"], env=env, capture_output=True,
                             text=True, timeout=15, check=True).stdout.strip()
    report = {"schema_version": 1, "status": "RUNNING", "started_at": now(), "planned_runs": len(plan),
              "cases": args.cases, "arms": {arm: ARMS[arm] for arm in args.arms}, "repetitions": args.repetitions,
              "jobs": args.jobs, "seed": args.seed, "retries": 0, "fallbacks": 0,
              "scope": "isolated Chromium; task_scope development admission; synthetic fixtures plus public Python.org About",
              "runtime": {"parent_python": sys.version, "worker_python_executable": str(args.python),
                          "playwright": version("playwright"), "chromium": browser,
                          "chromium_sha256": hashlib.sha256(args.chromium.read_bytes()).hexdigest()},
              "source_sha256": identity["sha256"], "runs": [], "remaining": list(plan)}
    if baseline_sha256 is not None:
        report.update(failure_baseline_sha256=baseline_sha256, selected_cells=plan)
    bench.save(args.output / "source-manifest.json", identity)
    bench.save(args.output / "results.json", report)
    pool = ThreadPoolExecutor(max_workers=args.jobs)
    try:
        futures = {pool.submit(measure, args, cell, suite, keys): cell for cell in plan}
        for future in as_completed(futures):
            cell, row = futures[future], future.result()
            report["runs"].append(row)
            report["remaining"].remove(cell)
            bench.save(args.output / "results.json", report)
            bench.save(args.output / "summary.json", summarize(report["runs"], plan))
            write_csv(args.output, report["runs"])
            print(json.dumps({**cell, "completed": len(report["runs"]), "planned": len(plan),
                              "confirmed": row["confirmed"], "status": row["result"].get("status"),
                              "reason": row["result"].get("reason")}), flush=True)
    except BaseException as exc:
        pool.shutdown(wait=True, cancel_futures=True)
        report.update(status="INTERRUPTED", interruption_type=type(exc).__name__)
        bench.save(args.output / "results.json", report)
        raise
    else:
        pool.shutdown(wait=True)
    report.update(status="COMPLETE", finished_at=now(), sources_unchanged=source_identity() == identity)
    if not report["sources_unchanged"]:
        report["status"] = "INVALID_SOURCE_CHANGED"
    summary = summarize(report["runs"], plan)
    report["verdict"] = summary["verdict"]
    bench.save(args.output / "results.json", report)
    bench.save(args.output / "summary.json", summary)
    return 0 if report["status"] == "COMPLETE" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--arms", nargs="+", choices=ARMS, default=list(DEFAULT_ARMS))
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--repetitions", type=int, choices=range(1, 4), default=3)
    parser.add_argument("--jobs", type=int, choices=range(1, 4), default=3)
    parser.add_argument("--seed", type=int, default=20261117)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--chromium", type=Path)
    parser.add_argument("--browsers", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--jev-env-file", type=Path)
    parser.add_argument("--failures-from", type=Path,
                        help="Repeat only the unconfirmed cells of a matching complete matrix results JSON.")
    args = parser.parse_args()
    suite = bench.load_scenarios()
    available = all_cases(suite)
    if args.list:
        print(json.dumps({"cases": available, "arms": ARMS, "default_arms": DEFAULT_ARMS,
                          "default_planned_runs": len(available) * len(DEFAULT_ARMS) * 3}, indent=2))
        return 0
    if not all((args.chromium, args.browsers, args.output)):
        parser.error("--chromium, --browsers and --output are required unless --list is used")
    args.cases = args.cases or available
    if set(args.cases) - set(available) or len(set(args.cases)) != len(args.cases):
        parser.error("cases must be unique published case IDs")
    if len(set(args.arms)) != len(args.arms):
        parser.error("arms must be unique")
    if args.output.exists():
        parser.error("output must be a new directory; existing evidence is preserved")
    args.chromium, args.browsers, args.output = args.chromium.resolve(), args.browsers.resolve(), args.output.absolute()
    args.python = args.python.absolute()  # Preserve a virtualenv interpreter's symlink.
    try:
        return run(args, suite)
    except Exception as exc:  # noqa: BLE001 - Never emit credentials or arbitrary request/response text.
        print(json.dumps({"status": "harness_error", "reason": type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
