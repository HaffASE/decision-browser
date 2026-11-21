"""Run one engine/provider on the long cases; rerun a 503 in the same reset fixture."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import random
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import compare_browser_libraries as bench
import long_protocol as protocol
import long_scenarios as suite

GROUPS = {"terra": "terra", "decision-browser": "core", "jev-ra": "libraries", "jev-ultrafast": "libraries"}
HTTP_FIELDS = ("endpoint", "request_model", "model", "reasoning_effort", "status", "state",
               "input_bytes", "duration_ms", "input_tokens", "output_tokens", "cached_tokens",
               "cache_write_tokens", "reasoning_tokens", "usage_reported", "error_type")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def opencode_credential():
    if key := os.getenv("OPENCODE_API_KEY"):
        return key
    for service in ("OPENCODE_API_KEY", "codex-opencode-console"):
        if sys.platform != "darwin":
            break
        result = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                                capture_output=True, text=True, timeout=20, check=False)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    database = Path.home() / ".local/share/opencode/opencode.db"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT value FROM credential WHERE active=1 AND integration_id "
                         "IN ('opencode','opencode-go') ORDER BY integration_id LIMIT 1").fetchone()
    if not row:
        raise RuntimeError("OpenCode credential unavailable")
    return json.loads(row[0])["key"]


def credentials(args):
    values = {}
    if args.route != "opencode" or args.engine == "jev-ultrafast":
        values["OPENAI_API_KEY"] = bench.get_key(SimpleNamespace(keychain_service=args.keychain_service))
    if args.route == "opencode":
        values["OPENCODE_API_KEY"] = opencode_credential()
    return values


def run_child(args, request, keys):
    env = bench.environment(None, SimpleNamespace(playwright_browsers=args.browsers))
    env.update(keys, BENCH_ENGINE=args.engine, BENCH_ROUTE=args.route)
    start = time.monotonic()
    child = subprocess.Popen([str(args.python), "-B", str(args.worker)], env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, _ = child.communicate(json.dumps(request), timeout=protocol.TIMEOUT_S + 55)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGINT)
        try:
            child.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
        return {"status": "harness_timeout"}, round((time.monotonic() - start) * 1000)
    try:
        result = json.loads(stdout.strip().splitlines()[-1])
        if not isinstance(result, dict):
            raise TypeError("non_object_result")
    except (IndexError, TypeError, ValueError):
        result = {"status": "harness_error"}
    result["returncode"] = child.returncode
    return result, round((time.monotonic() - start) * 1000)


def measure(args, site, request, keys, base):
    result, elapsed = run_child(args, request, keys)
    oracle = site.verify(base["case"])
    score = bench.outcome(args.engine, result, oracle, expected_checks=len(request["checks"]))
    usage = result.get("usage") or {}
    http = [{k: call[k] for k in HTTP_FIELDS if k in call} for call in usage.get("calls", [])]
    statuses = [c.get("status") for c in http]
    status = result.get("status", "harness_error")
    reason = result.get("reason")
    is_infra = (result.get("returncode") != 0
                or status in {"harness_error", "harness_timeout", "runner_error"}
                or not http or any(s != 200 for s in statuses))
    if len(http) > protocol.MAX_CALLS or sum(c["input_bytes"] for c in http) > protocol.MAX_BYTES:
        raise RuntimeError("worker violated shared budget")
    endpoints = {c["endpoint"].rsplit("/", 1)[-1] for c in http}
    allowed = {"responses"} if args.engine == "terra" else {"systemone" if args.route == "opencode" else "decisions"}
    if args.engine == "jev-ultrafast" or (args.engine == "decision-browser" and args.route == "openai"):
        allowed.add("responses")
    if not endpoints <= allowed:
        raise RuntimeError("worker used unapproved endpoint")
    if args.engine == "terra" and any(c.get("request_model") != base["model"]
                                      or c.get("reasoning_effort") != "medium" for c in http):
        raise RuntimeError("Terra model/effort mismatch")
    costs = [protocol.charge(c, base["model"]) for c in http]
    row = dict(base, **score, phase="finished", wall_ms=elapsed, calls=len(http), http=http,
               status=status, reason=reason, actions=result.get("actions", []), checks=result.get("checks"),
               oracle=oracle, steps=result.get("steps"), native_evidence=result.get("native_evidence"),
               cleanup=result.get("cleanup"), returncode=result.get("returncode"),
               unfinished_speculations=result.get("unfinished_speculations", 0),
               usage_complete=usage.get("usage_complete") is True,
               is_infra=is_infra, finished=datetime.now(UTC).isoformat(),
               estimated_usd=str(sum(costs)) if http and all(c is not None for c in costs) else None)
    if any(v in json.dumps(row) for v in keys.values()):
        raise RuntimeError("unsafe output")
    return row


def execute(args, out, case, rep, keys):
    name = f"{rep}-{case}-{args.engine}-{args.route}.json"
    scored = out / "scored" / name
    with (out / "locks" / name).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if scored.exists():
            return
        with suite.ScenarioSite() as site:
            url = site.start(case)
            request = protocol.make_request(case, url)
            request.update(engine=args.engine, route=args.route, browser_executable=str(args.chromium),
                           source_root=str(args.source))
            base = {
                "case": case, "repetition": rep, "engine": args.engine, "route": args.route,
                "model": protocol.MODELS[args.route], "worker_sha256": digest(args.worker),
                "fixture_sha256": digest(Path(suite.__file__)),
                "pricing_sha256": digest(Path(protocol.__file__)),
                "terra_agent_sha256": (digest(SCRIPTS.parent / "docs/benchmarks/2026-11-03-terra-medium/agent.py")
                                       if args.engine == "terra" else None),
                "limits": {k: request[k] for k in ("max_calls", "max_steps", "max_input_bytes", "timeout_s")},
                "goal_sha256": hashlib.sha256(request["goal"].encode()).hexdigest(),
                "request_sha256": hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest(),
            }
            for n, group in enumerate(("primary", "retry503", "retry503-2"), 1):
                target = out / group / name
                if target.exists():
                    raise RuntimeError(f"unscored prior attempt requires review: {target}")
                if n > 1:
                    site.reset(case)
                base.update(attempt_no=n, phase="pending", started=datetime.now(UTC).isoformat())
                bench.save(target, base)
                row = measure(args, site, request, keys, base)
                bench.save(target, row)
                print(name, "try", n, row["status"], row["reason"], row["confirmed"], row["calls"],
                      row["wall_ms"], "infra" if row["is_infra"] else "scored", flush=True)
                if not row["is_infra"]:
                    bench.save(scored, row)
                    return
                if 503 not in [c.get("status") for c in row["http"]]:
                    raise RuntimeError(f"infrastructure error; see {target}")
            print("Persistent 503, cell excluded:", name, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=GROUPS, required=True)
    parser.add_argument("--route", choices=protocol.MODELS, required=True)
    for field in ("worker", "python", "source", "chromium", "browsers"):
        parser.add_argument("--" + field, type=Path, required=True)
    parser.add_argument("--case", choices=list(suite.CASES))
    parser.add_argument("--repetition", choices=(1, 2, 3), type=int)
    parser.add_argument("--keychain-service", default=None)
    parser.add_argument("--output", type=Path, default=SCRIPTS.parent / ".cache/long-suite")
    args = parser.parse_args()
    for field in ("worker", "source", "chromium", "browsers"):
        setattr(args, field, getattr(args, field).resolve())
    # A venv interpreter must retain its symlink path.
    args.python = args.python.absolute()
    if (args.engine == "terra") != (args.route == "responses"):
        parser.error("Terra requires responses; library agents require openai or opencode")
    if not all(p.is_file() for p in (args.python, args.worker, args.chromium)) or not args.source.is_dir():
        parser.error("worker, Python executable, browser and source checkout must exist")
    out = args.output / GROUPS[args.engine]
    for group in ("locks", "primary", "retry503", "retry503-2", "scored"):
        (out / group).mkdir(parents=True, exist_ok=True)
    keys = credentials(args)
    for rep in (args.repetition,) if args.repetition else (1, 2, 3):
        cases = [args.case] if args.case else list(suite.CASES)
        random.Random(83 + rep).shuffle(cases)
        for case in cases:
            execute(args, out, case, rep, keys)


if __name__ == "__main__":
    main()
