"""Audit a completed browser capture offline; preserve its raw JSON and CSV."""
from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import random
import re
import statistics
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

CASES = ("click", "search", "select", "navigate", "counter", "public_about", "hotel", "profile_form",
         "autocomplete", "calendar", "styled_filter", "consent", "delayed_menu", "loading_overlay",
         "below_fold", "article", "rail_itinerary", "library_research", "equipment_pickup", "support_handoff",
         "exhibit_planner")
ARMS = {"jev": {"model": "jev-latest", "endpoint": "https://api.typesafe.ai/v1/systemone"},
        "decision_api": {"model": "gpt-6-luna", "endpoint": "https://api.openai.com/v1/decisions"},
        "terra": {"model": "gpt-6.1-terra", "endpoint": "https://api.openai.com/v1/responses",
                  "reasoning_effort": "medium"}}
TOKENS = ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "cache_write_tokens", "reasoning_tokens")
RAW_FILES = ("results.json", "summary.json", "runs.csv")
OUTPUTS = ("audited-results.json", "audited-summary.json", "audited-runs.csv", "errors.json", "report.md", "audit.json")
CORRECTION = ("The capture launcher aliased remaining to its plan. Removing completed cells therefore shrank progress "
              "denominators and summary domains. All futures had already been queued; immutable reconstruction and "
              "the per-attempt records restore reporting without changing outcomes or making paid reruns.")


class AuditError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise AuditError(code)


def load(path):
    return json.loads(path.read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identity(row):
    return row["arm"], row["case"], row["repetition"]


def expected_plan(raw):
    require(raw["repetitions"] == 3 and set(raw["cases"]) == set(CASES)
            and len(raw["cases"]) == 21 and raw["arms"] == ARMS, "unexpected_matrix_contract")
    arms, plan = list(raw["arms"]), []
    for repetition in range(1, 4):
        cases = list(raw["cases"])
        random.Random(raw["seed"] + repetition).shuffle(cases)
        for position, case in enumerate(cases):
            offset = (repetition + position) % len(arms)
            plan.extend({"arm": arm, "case": case, "repetition": repetition}
                        for arm in arms[offset:] + arms[:offset])
    return plan


def audit_sources(root, raw):
    manifest = load(root / "source-manifest.json")
    capture, combined, seen = root / "capture-source", hashlib.sha256(), set()
    for row in sorted(manifest["files"], key=lambda row: row["path"]):
        relative = Path(row["path"])
        require(not relative.is_absolute() and ".." not in relative.parts and row["path"] not in seen,
                "invalid_source_manifest_path")
        seen.add(row["path"])
        path = capture / relative
        require(path.resolve().is_relative_to(capture.resolve()) and path.is_file() and not path.is_symlink(),
                "missing_or_unsafe_captured_source")
        data, name = path.read_bytes(), row["path"].encode()
        require(hashlib.sha256(data).hexdigest() == row["sha256"], "captured_source_hash_mismatch")
        combined.update(len(name).to_bytes(8, "big") + name)
        combined.update(len(data).to_bytes(8, "big") + data)
    require({"scripts/run_provider_browser_matrix.py", "scripts/benchmarks/provider_matrix_worker.py"} <= seen,
            "missing_captured_launcher_or_worker")
    require(combined.hexdigest() == manifest["sha256"] == raw["source_sha256"], "source_identity_mismatch")
    return {"files": len(seen), "sha256": manifest["sha256"],
            "launcher_sha256": digest(capture / "scripts/run_provider_browser_matrix.py"),
            "worker_sha256": digest(capture / "scripts/benchmarks/provider_matrix_worker.py")}


def numeric(value):
    return type(value) is int and value >= 0


def unknown_usage(row):
    return (row["result"].get("usage") or {}).get("attempts") is None or row["result"].get("usage_incomplete") is True


def calls(row):
    return (row["result"].get("usage") or {}).get("calls", [])


def native_flags(row):
    result, usage = row["result"], row["result"].get("usage") or {}
    native = (result.get("status") == "done" and result.get("reason") == "goal_achieved" if row["arm"] == "terra"
              else result.get("status") == result.get("functional") == "verified")
    checks = result.get("checks")
    count = 1 if row["case"] == "public_about" else 3 if row["case"] in {"autocomplete", "calendar"} else 2
    page = isinstance(checks, list) and len(checks) == count and all(check is True for check in checks)
    cleanup = {"browser": "closed"} if row["arm"] == "terra" else {"driver_state": "closed", "errors": []}
    lifecycle = (type(result.get("returncode")) is int and result["returncode"] == 0
                 and usage.get("usage_complete") is True and result.get("usage_incomplete", False) is False
                 and type(result.get("unfinished_speculations", 0)) is int
                 and result.get("unfinished_speculations", 0) == 0 and result.get("cleanup") == cleanup)
    return native, page, row.get("oracle", {}).get("success") is True, lifecycle


def audit_row(row):
    result, arm = row["result"], row["arm"]
    require(row["phase"] == "finished" and type(row["confirmed"]) is bool, "unfinished_or_invalid_cell")
    long = row["case"] in CASES[-5:]
    expected = {"max_steps": 24 if long or row["case"] in CASES[6:] else 8,
                "max_calls": 24 if long else 12, "timeout_s": 180 if long else 75,
                "max_input_bytes": 524288 if long else 131072}
    require(row.get("limits") == expected, "case_budget_mismatch")
    physical, usage = calls(row), result.get("usage") or {}
    require(isinstance(physical, list) and len(physical) <= expected["max_calls"], "physical_call_budget_violation")
    require(usage.get("attempts") is None or usage["attempts"] == len(physical), "physical_call_count_mismatch")
    require(all(numeric(call.get("input_bytes")) for call in physical)
            and sum(call["input_bytes"] for call in physical) <= expected["max_input_bytes"], "request_byte_budget_violation")
    endpoints = {"systemone"} if arm == "jev" else {"responses"} if arm == "terra" else {"decisions", "responses"}
    model_pattern = (r"jev-(?:latest|\d+(?:\.\d+)*)" if arm == "jev" else
                     re.escape(ARMS[arm]["model"]) + r"(?:-\d{4}-\d{2}-\d{2})?")
    for call in physical:
        require(call.get("endpoint") in endpoints and call.get("request_model") == ARMS[arm]["model"],
                "unexpected_api_route_or_model")
        require("model" not in call or re.fullmatch(model_pattern, call["model"]), "unexpected_resolved_model")
        require(arm != "terra" or call.get("reasoning_effort") == "medium", "terra_effort_mismatch")
        require(all(call.get(name) is None or numeric(call[name]) for name in TOKENS), "invalid_token_usage")
    if arm == "terra" and physical:
        require(result.get("model") == "gpt-6.1-terra" and result.get("reasoning_effort") == "medium",
                "terra_result_identity_mismatch")
    if numeric(result.get("steps")):
        require(result["steps"] <= expected["max_steps"], "step_budget_violation")
    if usage.get("usage_complete") is True:
        require(bool(physical) and all(call.get("status") == 200 and call.get("usage_reported") is True
                                     and numeric(call.get("input_tokens")) and numeric(call.get("output_tokens"))
                                     for call in physical), "false_complete_usage")
    if row["confirmed"]:
        require(all(native_flags(row)) and not row.get("harness_error"), "false_confirmation")


def audit_capture(root):
    raw = load(root / "results.json")
    require(raw.get("status") == "COMPLETE", "capture_not_complete")
    require(raw.get("sources_unchanged") is True and raw.get("retries") == raw.get("fallbacks") == 0,
            "changed_sources_or_retry_contract")
    plan, rows = expected_plan(raw), raw["runs"]
    expected = set(itertools.product(ARMS, CASES, range(1, 4)))
    require(len(plan) == len(rows) == raw.get("planned_runs") == 189, "incomplete_matrix")
    require(len({identity(row) for row in rows}) == 189 and {identity(row) for row in rows} == expected,
            "duplicate_or_missing_cells")
    records = set()
    for row in rows:
        relative = Path("runs") / row["arm"] / row["case"] / f"{row['repetition']}.json"
        require(row.get("record") == str(relative) and load(root / relative) == row, "per_attempt_record_mismatch")
        records.add(relative)
        audit_row(row)
    require(all(len({row.get("goal_sha256") for row in rows if row["case"] == case}) == 1
                and all(re.fullmatch(r"[a-f0-9]{64}", row.get("goal_sha256", ""))
                        for row in rows if row["case"] == case) for case in CASES), "case_goal_identity_mismatch")
    require({p.relative_to(root) for p in (root / "runs").rglob("*.json")} == records,
            "extra_or_pending_attempt_records")
    return raw, plan, audit_sources(root, raw)


def diagnostic_codes(row):
    evidence = row["result"].get("native_evidence") or []
    return [value for value in evidence if isinstance(value, str) and re.fullmatch(r"[a-z_]+:[a-z0-9_:-]+", value)]


def token_totals(rows):
    physical = [call for row in rows for call in calls(row)]
    unknown = sum(unknown_usage(row) for row in rows)
    result = {}
    for name in TOKENS:
        known = sum(call[name] for call in physical if numeric(call.get(name)))
        missing = sum(not numeric(call.get(name)) for call in physical)
        result[name] = {"total": None if missing or unknown else known, "reported_total": known,
                        "missing_calls": missing, "unknown_runs": unknown}
    return result


def aggregate(rows):
    durations = [row["result"]["wall_ms"] for row in rows if numeric(row["result"].get("wall_ms"))]
    physical = [call for row in rows for call in calls(row)]
    return {"attempts": len(rows), "confirmed": sum(row["confirmed"] for row in rows),
            "observed": sum(row.get("observed") is True for row in rows), "calls": len(physical),
            "usage_complete_runs": sum((row["result"].get("usage") or {}).get("usage_complete") is True for row in rows),
            "unknown_usage_runs": sum(unknown_usage(row) for row in rows), "tokens": token_totals(rows),
            "median_wall_ms": statistics.median(durations) if durations else None, "timed_runs": len(durations),
            "errors": dict(Counter(row.get("harness_error") or row["result"].get("reason")
                                   or row["result"].get("status", "unknown") for row in rows if not row["confirmed"])),
            "diagnostic_codes": dict(Counter(code for row in rows if not row["confirmed"] for code in diagnostic_codes(row))),
            "requested_models": sorted({call["request_model"] for call in physical}),
            "resolved_models": dict(Counter(call["model"] for call in physical if "model" in call)),
            "http_statuses": dict(Counter(str(call.get("status", "missing")) for call in physical))}


def summarize(raw):
    rows = raw["runs"]
    return {"planned_runs": 189, "completed_runs": len(rows),
            "verdict": "PASS" if all(row["confirmed"] for row in rows) else "HAS_FAILURES",
            "arms": {arm: aggregate([row for row in rows if row["arm"] == arm]) for arm in ARMS},
            "cases": {case: {arm: aggregate([row for row in rows if row["arm"] == arm and row["case"] == case])
                             for arm in ARMS} for case in raw["cases"]}}


def error_records(rows):
    errors = []
    for row in rows:
        if row["confirmed"]:
            continue
        result = row["result"]
        native, page, oracle, lifecycle = native_flags(row)
        categories = [name for name, failed in (("harness_error", bool(row.get("harness_error"))),
                      ("native_failure", not native), ("page_check_failure", not page),
                      ("oracle_mismatch", not oracle), ("lifecycle_failure", not lifecycle)) if failed]
        errors.append({**{key: row.get(key) for key in ("arm", "case", "repetition", "record", "harness_error")},
                       "categories": categories, "status": result.get("status"), "reason": result.get("reason"),
                       "native_evidence": result.get("native_evidence"), "diagnostic_codes": diagnostic_codes(row),
                       "checks": result.get("checks"), "oracle": row.get("oracle"), "cleanup": result.get("cleanup"),
                       "http": calls(row), "usage_complete": (result.get("usage") or {}).get("usage_complete") is True})
    return errors


def write_csv(path, rows):
    fields = ["arm", "case", "repetition", "confirmed", "observed", "status", "reason", "harness_error", "diagnostic_codes",
              "categories", "wall_ms", "calls", "unknown_call_count", "usage_complete", "record", *TOKENS,
              *[name + suffix for name in TOKENS for suffix in ("_reported", "_missing_calls")]]
    errors = {identity(row): row for row in error_records(rows)}
    with path.open("x", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            totals, result = token_totals([row]), row["result"]
            data = {key: row.get(key) for key in ("arm", "case", "repetition", "confirmed", "observed", "harness_error", "record")}
            data.update({key: result.get(key) for key in ("status", "reason", "wall_ms")})
            data.update(calls=(result.get("usage") or {}).get("attempts"), unknown_call_count=unknown_usage(row),
                        usage_complete=(result.get("usage") or {}).get("usage_complete") is True,
                        diagnostic_codes=";".join(diagnostic_codes(row)), categories=";".join(errors.get(identity(row), {}).get("categories", [])))
            for name in TOKENS:
                data.update({name: totals[name]["total"], name + "_reported": totals[name]["reported_total"],
                             name + "_missing_calls": totals[name]["missing_calls"]})
            writer.writerow(data)


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def token_text(value):
    return str(value["total"]) if value["total"] is not None else (
        f"unknown (reported {value['reported_total']}; {value['missing_calls']} calls missing; {value['unknown_runs']} runs unknown)")


def render(raw, summary, errors, source):
    lines = ["# Browser matrix: Jev, Decision API and Terra 6.1 medium", "",
             "Fresh completed capture: 21 cases × 3 repetitions × 3 configurations = 189 attempts. Every failure and early stop remains in the denominator.", "",
             "## Configuration and totals", "",
             "| Configuration | Confirmed | Page + oracle | Calls | Input tokens | Output tokens | Cached | Cache write | Reasoning | Median process seconds |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for arm, data in summary["arms"].items():
        tokens = [token_text(data["tokens"][name]) for name in ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens", "reasoning_tokens")]
        median = f"{data['median_wall_ms']/1000:.3f}" if data["median_wall_ms"] is not None else "unknown"
        lines.append("| " + " | ".join(map(cell, [arm, f"{data['confirmed']}/63", f"{data['observed']}/63",
                     str(data["calls"]) + (" + unknown" if data["unknown_usage_runs"] else ""), *tokens, median])) + " |")
    lines += ["", "Jev uses direct TypeSafe System One (`jev-latest`); it is a different route and model series from the archived OpenCode Jev 1.13 results. Decision Browser uses its frozen current Core providers and runner. Standalone Terra uses `gpt-6.1-terra`, `reasoning.effort=medium`, and the Responses API.", "",
              "Requested and observed model identities:", ""]
    for arm, data in summary["arms"].items():
        lines.append(f"- **{arm}** requested `{ARMS[arm]['model']}`; observed `{cell(json.dumps(data['resolved_models'], sort_keys=True))}`.")
    lines += ["", "## Confirmed repetitions by scenario", "", "| Scenario | Jev | Decision API | Terra medium |", "| --- | ---: | ---: | ---: |"]
    for case, data in summary["cases"].items():
        lines.append(f"| `{case}` | " + " | ".join(f"{data[arm]['confirmed']}/3" for arm in ARMS) + " |")
    lines += ["", "## Scenario token usage", "", "Each cell is input / output tokens across all three attempts, including failed attempts.", "",
              "| Scenario | Jev input / output | Decision API input / output | Terra input / output |", "| --- | ---: | ---: | ---: |"]
    for case, data in summary["cases"].items():
        lines.append(f"| `{case}` | " + " | ".join(" / ".join(token_text(data[arm]["tokens"][name])
                     for name in ("input_tokens", "output_tokens")) for arm in ARMS) + " |")
    lines += ["", "## Unconfirmed attempts", "", "Generic native reasons and exception classes are retained as observed; they do not establish a root cause. A native completion with an unsuccessful oracle is an oracle mismatch, not a confirmed task.", "",
              "| Arm | Scenario · repetition | Native status / reason | Diagnostics / failed dimensions | Attempt |", "| --- | --- | --- | --- | --- |"]
    for error in errors:
        diagnostics = "; ".join(error["diagnostic_codes"] + error["categories"])
        lines.append("| " + " | ".join(map(cell, [error["arm"], f"{error['case']} · {error['repetition']}",
                     f"{error['status']} / {error['reason']}", diagnostics, f"[JSON]({error['record']})"])) + " |")
    lines += ["", "## Method and evidence", "",
              "All configurations receive the same case goals, supplied values, independent page checks, fixture oracles and case budgets. There are three repetitions, no retries and no fallback routes. Sixteen short cases use 8 baseline / 24 expanded steps, 12 API calls, 75 seconds and 128 KiB. Five long cases use 24 steps, 24 calls, 180 seconds and 512 KiB. The standalone Terra agent retains its historical estimated spend guard of $0.25 per attempt; this is not an invoice or a new cost estimate.", "",
              "Twenty scenarios use synthetic owned fixtures; `public_about` is a read-only visit to Python.org. Browsers use fresh isolated Chromium contexts. Core uses task_scope development admission; these results do not prove native human admission or general-site acceptance. Confirmation requires native completion, independent page and oracle success, complete reported input/output usage and successful cleanup. Medians include unsuccessful attempts and startup/cleanup, with missing timing explicitly retained in JSON.", "",
              "Tokens come from API usage for each physical request. Missing values remain unknown; partial reported sums and missing counts remain in JSON/CSV. Cache and reasoning are token subsets and must not be added to input/output totals. These are API-reported tokens, not a USD invoice.", "",
              f"Capture source: `{source['sha256']}` ({source['files']} verified files). Browser/runtime: `{cell(json.dumps(raw['runtime'], sort_keys=True))}`.", "",
              "### Reporting correction", "", CORRECTION, "",
              "Raw [results](results.json), [summary](summary.json) and [CSV](runs.csv) remain byte-for-byte unchanged. Use the [audited results](audited-results.json), [audited summary](audited-summary.json), [all-attempt CSV](audited-runs.csv), [full error evidence](errors.json), and [audit manifest](audit.json)."]
    return "\n".join(lines) + "\n"


def generate(root):
    require(not any((root / name).exists() for name in OUTPUTS), "prior_audit_outputs_exist")
    raw_hashes = {name: digest(root / name) for name in RAW_FILES}
    raw, plan, sources = audit_capture(root)
    summary, errors = summarize(raw), error_records(raw["runs"])
    audited = {**raw, "planned_runs": 189, "remaining": [], "immutable_plan": plan,
               "verdict": summary["verdict"], "reporting_correction": CORRECTION}
    for name, value in (("audited-results.json", audited), ("audited-summary.json", summary), ("errors.json", errors)):
        with (root / name).open("x") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
    write_csv(root / "audited-runs.csv", raw["runs"])
    with (root / "report.md").open("x") as stream:
        stream.write(render(raw, summary, errors, sources))
    require(raw_hashes == {name: digest(root / name) for name in RAW_FILES}, "raw_capture_modified")
    audit = {"status": "PASS", "created_at": datetime.now(UTC).isoformat(), "raw_file_hashes": raw_hashes,
             "output_file_hashes": {name: digest(root / name) for name in OUTPUTS if name != "audit.json"},
             "source_capture": sources, "reporting_correction": CORRECTION, "paid_reruns": 0,
             "expected_cells": 189, "verified_attempt_records": len(raw["runs"]), "unconfirmed_attempts": len(errors),
             "generator_sha256": digest(Path(__file__))}
    with (root / "audit.json").open("x") as stream:
        json.dump(audit, stream, indent=2)
        stream.write("\n")
    return audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    try:
        audit = generate(args.input)
    except (AuditError, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "FAIL", "reason": str(exc) if isinstance(exc, AuditError) else type(exc).__name__}))
        return 1
    print(json.dumps({"status": audit["status"], "verified_attempt_records": audit["verified_attempt_records"],
                      "unconfirmed_attempts": audit["unconfirmed_attempts"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
