"""Validate and publish long browser runs. With no --raw-dir, rebuild from the published CSV."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from decimal import Decimal
from pathlib import Path
from statistics import median

from render_long_comparison import (
    BENCH,
    ENGINES,
    LABELS,
    LONG_LABELS,
    ROOT,
    ROUTES,
    display_cost,
    indexed,
    read_rows,
    render,
    seconds,
    totals,
)

sys.path.insert(0, str(ROOT / "src"))
import long_protocol as protocol
import long_scenarios as suite

PUBLIC = BENCH / "2026-11-03-long-suite"
GIF = ROOT / "docs/assets/browser-four-agents.gif"
LIMITS = {"max_calls": protocol.MAX_CALLS, "max_steps": protocol.MAX_STEPS,
          "max_input_bytes": protocol.MAX_BYTES, "timeout_s": protocol.TIMEOUT_S}
COLUMNS = (
    "case", "repetition", "engine", "route", "model", "confirmed", "observed", "native_success",
    "page_checks_passed", "fixture_passed", "correct_prefix_sections", "submitted_sections", "total_sections",
    "wall_ms", "calls", "helper_calls", "request_bytes", "estimated_usd", "known_estimated_usd", "unpriced_calls",
    "status", "reason", "attempt_no",
    "goal_sha256", "request_sha256", "fixture_sha256", "worker_sha256",
)


def serial(data):
    return json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def arm_rows(rows, engine, route):
    return [r for r in rows if r["engine"] == engine and r["route"] == route]


def configurations():
    return [(engine, route) for engine in ENGINES for route in (("responses",) if engine == "terra" else ROUTES)]


def stop_reason(row):
    # Ultrafast converts the meter's denied 25th request to a connection error
    # before sending it. The CSV retains that native reason verbatim.
    if (row["engine"] == "jev-ultrafast" and row["reason"] == "connection_failed"
            and int(row["calls"]) == protocol.MAX_CALLS):
        return "model_call_limit"
    if row["native_success"] == "True":
        return "native_done_checks_failed"
    return row["reason"] or row["status"]


def csv_row(row):
    if row["limits"] != LIMITS or row["is_infra"] or row["phase"] != "finished":
        raise ValueError("Scored row failed common protocol")
    if not 1 <= len(row["http"]) == row["calls"] <= protocol.MAX_CALLS:
        raise ValueError("Invalid physical model request count")
    if any(c["status"] != 200 for c in row["http"]):
        raise ValueError("Unexpected transport failure among scored requests")
    request_bytes = sum(c["input_bytes"] for c in row["http"])
    if not 0 < request_bytes <= protocol.MAX_BYTES:
        raise ValueError("Cumulative request size outside limits")
    charges = [protocol.charge(c, row["model"]) for c in row["http"]]
    cost = str(sum(charges)) if all(c is not None for c in charges) else None
    if row["estimated_usd"] != cost:
        raise ValueError("Published cost differs from scored usage")
    oracle = row["oracle"]
    expected = [section.expected_record() for section in suite.CASES[row["case"]].sections]
    observed = oracle["observed"]
    if expected != oracle["expected"] or (observed == expected) != oracle["success"]:
        raise ValueError("Fixture records differ from scenario specification")
    prefix = 0
    for actual, required in zip(observed, expected):
        if actual != required:
            break
        prefix += 1
    page_ok = row["checks"] == [True, True]
    if row["observed"] != (page_ok and oracle["success"]) or row["confirmed"] != (row["observed"] and row["native_success"]):
        raise ValueError("Inconsistent completion or independent checks")
    helper_calls = sum(c["endpoint"].rsplit("/", 1)[-1] == "responses" for c in row["http"]) if row["engine"] == "jev-ultrafast" else 0
    scalars = dict(row, request_bytes=request_bytes, helper_calls=helper_calls,
                   known_estimated_usd=sum(c for c in charges if c is not None), unpriced_calls=charges.count(None),
                   page_checks_passed=page_ok,
                   fixture_passed=oracle["success"], correct_prefix_sections=prefix, submitted_sections=len(observed),
                   total_sections=len(expected))
    return {field: "" if scalars[field] is None else str(scalars[field]) for field in COLUMNS}


def evidence_row(row, *, include_core_diagnostics=False):
    calls = []
    for call in row["http"]:
        fields = ("request_model", "status", "usage_reported", "input_bytes", "input_tokens",
                  "output_tokens", "cached_tokens", "cache_write_tokens", "reasoning_tokens")
        calls.append({"endpoint": call["endpoint"].rsplit("/", 1)[-1],
                      **{key: call[key] for key in fields if key in call}})
    detail = {key: row[key] for key in ("engine", "route", "case", "repetition", "actions", "checks")} | {
        "saved_sections": row["oracle"]["observed"], "http": calls}
    if include_core_diagnostics:
        from decision_browser.runner import _PROVIDER_FAILURE_CODES

        allowed = {f"provider:{code}" for code in _PROVIDER_FAILURE_CODES}
        allowed.update(f"{check}:{code}" for check in ("route", "text") for code in ("ok", "mismatch", "playwright_error"))
        detail["native_steps"] = row["steps"]
        detail["native_evidence"] = [code for code in row["native_evidence"] if code in allowed]
    return detail


def from_raw(raw, lanes=("terra", "core", "libraries")):
    trials, excluded = [], []
    for lane in lanes:
        for path in sorted((raw / lane / "scored").glob("*.json")):
            row = json.loads(path.read_bytes())
            n = row["attempt_no"]
            for attempt in range(1, n + 1):
                group = "primary" if attempt == 1 else ("retry503" if attempt == 2 else f"retry503-{attempt-1}")
                physical = json.loads((raw / lane / group / path.name).read_bytes())
                if physical["request_sha256"] != row["request_sha256"] or physical["attempt_no"] != attempt:
                    raise ValueError(f"Retry request identity mismatch for {path.name}")
                if attempt == n:
                    if physical != row:
                        raise ValueError(f"Scored row differs from final attempt for {path.name}")
                    continue
                statuses = [h.get("status") for h in physical["http"]]
                if not physical["is_infra"] or 503 not in statuses:
                    raise ValueError(f"Excluded attempt without 503 for {path.name}")
                costs = [protocol.charge(call, row["model"]) for call in physical["http"]]
                excluded.append({"case": row["case"], "engine": row["engine"], "route": row["route"],
                                 "repetition": row["repetition"], "attempt_no": attempt,
                                 "request_sha256": row["request_sha256"], "http_statuses": statuses,
                                 "known_estimated_usd": str(sum(c for c in costs if c is not None)),
                                 "unpriced_calls": costs.count(None)})
            trials.append((csv_row(row), evidence_row(row, include_core_diagnostics=lanes == ("core",))))
    trials.sort(key=lambda trial: (list(LONG_LABELS).index(trial[0]["case"]), int(trial[0]["repetition"]),
                                  ENGINES.index(trial[0]["engine"]), trial[0]["route"]))
    return [r for r, _ in trials], excluded, [r for _, r in trials]


def validate_evidence(rows, evidence):
    for row, detail in zip(rows, evidence, strict=True):
        if any(str(detail[k]) != row[k] for k in ("engine", "route", "case", "repetition")):
            raise ValueError("Per-attempt evidence and CSV order differ")
        expected = [section.expected_record() for section in suite.CASES[row["case"]].sections]
        saved = detail["saved_sections"]
        correct = next((i for i, (x, y) in enumerate(zip(expected, saved)) if x != y), min(len(expected), len(saved)))
        checks = detail["checks"] == [True, True]
        observed = checks and saved == expected
        if (str(observed) != row["observed"] or str(checks) != row["page_checks_passed"]
                or str(saved == expected) != row["fixture_passed"] or int(row["submitted_sections"]) != len(saved)
                or correct != int(row["correct_prefix_sections"])):
            raise ValueError("Per-attempt evidence and CSV browser outcome differ")
        costs = [protocol.charge(c, row["model"]) for c in detail["http"]]
        if (len(costs) != int(row["calls"]) or costs.count(None) != int(row["unpriced_calls"])
                or sum(c for c in costs if c is not None) != Decimal(row["known_estimated_usd"])
                or sum(c["input_bytes"] for c in detail["http"]) != int(row["request_bytes"])):
            raise ValueError("Per-attempt evidence and CSV usage differ")


def git_revision(revision):
    return subprocess.check_output(
        ["git", "-C", ROOT, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"], text=True,
    ).strip()


def git_file(commit, path):
    return subprocess.check_output(["git", "-C", ROOT, "show", f"{commit}:{path}"])


def provenance(raw, revision):
    commit = git_revision(revision)

    def source_hash(path):
        return hashlib.sha256(git_file(commit, path)).hexdigest()

    hashes = {}
    for name in ("long_protocol.py", "long_run.py", "long_scenarios.py", "long_core.py", "long_external.py", "long_terra.py", "dbc_runner.py"):
        hashes[name] = source_hash(f"scripts/benchmarks/{name}")
    hashes["compare_browser_libraries.py"] = source_hash("scripts/compare_browser_libraries.py")
    hashes["terra_base_agent.py"] = source_hash("docs/benchmarks/2026-11-03-terra-medium/agent.py")
    external = json.loads((raw / "libraries/source-identity.json").read_bytes())
    for name, expected in external["worker_dependencies"].items():
        if source_hash(f"scripts/benchmarks/{name}") != expected:
            raise ValueError(f"External worker dependency differs from its manifest: {name}")
    return {"measurement_commit": commit, "harness_sha256": hashes, "external_libraries": external,
            "core": json.loads((raw / "core/source-identity.json").read_bytes())}


def archive_core_series(source, publication, arms):
    return {
        "measurement_commit": source, "publication": publication,
        "arms": {route: {field: arms[f"decision-browser/{route}"][field] for field in
                         ("confirmed", "attempts", "median_wall_ms", "estimated_usd", "known_estimated_usd")}
                 for route in ROUTES},
    }


def core_rerun(raw, revision, previous_publication):
    previous = git_revision(previous_publication)
    commit = git_revision(revision)
    published = PUBLIC.relative_to(ROOT)
    old_rows = list(csv.DictReader(git_file(previous, published / "runs.csv").decode().splitlines()))
    old_evidence = [json.loads(line) for line in git_file(previous, published / "evidence.jsonl").splitlines()]
    audit = json.loads(git_file(previous, published / "audit.json"))
    summary = json.loads(git_file(previous, published / "summary.json"))
    indexed(old_rows, LONG_LABELS)
    validate_evidence(old_rows, old_evidence)
    fresh, excluded, evidence = from_raw(raw, ("core",))
    if len(fresh) != 30 or any(r["engine"] != "decision-browser" for r in fresh):
        raise ValueError("Expected exactly 30 fresh Core observations")
    identity = json.loads((raw / "core/source-identity.json").read_bytes())
    names = ("long_scenarios.py", "long_core.py", "long_run.py", "long_protocol.py", "dbc_runner.py")
    hashes = {name: hashlib.sha256(git_file(commit, f"scripts/benchmarks/{name}")).hexdigest() for name in names}
    root = "src/decision_browser"
    files = subprocess.check_output(["git", "-C", ROOT, "ls-tree", "-r", "--name-only", commit, root], text=True)
    digest = hashlib.sha256()
    for line in files.splitlines():
        path = Path(line)
        if str(path.parent) == root and path.suffix in {".py", ".js"}:
            name, content = line.encode(), git_file(commit, line)
            digest.update(len(name).to_bytes(8, "big") + name)
            digest.update(len(content).to_bytes(8, "big") + content)
    if (identity["revision"] != commit or identity["source_root"] != root
            or identity["source_sha256"] != digest.hexdigest()
            or identity["worker_sha256"] != hashes["long_core.py"]):
        raise ValueError("Core source identity differs from the pinned Git revision")
    if any(hashes[k] != audit["sources"]["harness_sha256"][k] for k in ("long_scenarios.py", "long_protocol.py")):
        raise ValueError("Fixture or budgets changed between the two Core runs")
    old_core = [r for r in old_rows if r["engine"] == "decision-browser"]

    def key(r):
        return r["engine"], r["route"], r["case"], str(r["repetition"])

    replacement = {key(r): (r, e) for r, e in zip(fresh, evidence, strict=True)}
    if set(replacement) != {key(r) for r in old_core}:
        raise ValueError("Fresh Core attempts differ from the previous case/repetition matrix")
    merged = [replacement[key(r)] if r["engine"] == "decision-browser" else (r, e)
              for r, e in zip(old_rows, old_evidence, strict=True)]
    prior = audit.get("core_rerun")
    if prior is None:
        history = []
        prior_source = audit["sources"]["measurement_commit"]
    else:
        prior_source = prior["measurement_commit"]
        if "core_history" in audit:
            # Earlier unpublished runs can now be located in this previous publication.
            history = [{**series, "publication": series["publication"] or previous} for series in audit["core_history"]]
        else:
            # First-generation rerun reports store their baseline and intermediate run separately.
            history = [archive_core_series(audit["sources"]["measurement_commit"],
                                           prior["previous_publication"], prior["previous_arms"])]
            intermediate = audit.get("intermediate_core_run")
            if intermediate:
                history.append(archive_core_series(intermediate["core"]["revision"], previous, intermediate["arms"]))
    audit["core_history"] = [*history, archive_core_series(prior_source, previous, summary["arms"])]
    audit["core_rerun"] = {
        "measurement_commit": commit, "previous_publication": previous,
        "harness_sha256": hashes, "core": identity,
        "previous_arms": {key: value for key, value in summary["arms"].items() if key.startswith("decision-browser/")},
        "unconfirmed": [
            {"route": r["route"], "case": r["case"], "repetition": r["repetition"], "reason": stop_reason(r),
             "correct_sections": f"{r['correct_prefix_sections']}/{r['total_sections']}", "calls": r["calls"],
             "native_evidence": detail["native_evidence"]}
            for r, detail in zip(fresh, evidence, strict=True) if r["confirmed"] == "False"
        ],
    }
    audit["retries"] = [r for r in audit["retries"] if r["engine"] != "decision-browser"] + excluded
    return [r for r, _ in merged], [e for _, e in merged], audit


def intermediate_core_run(raw, previous_publication):
    identity = json.loads((raw / "core/source-identity.json").read_bytes())
    rows, evidence, audit = core_rerun(raw, identity["revision"], previous_publication)
    validate_evidence(rows, evidence)
    summary = summarize(rows, audit)
    fields = ("route", "case", "repetition", "confirmed", "observed", "calls", "wall_ms", "estimated_usd",
              "status", "reason", "attempt_no", "request_sha256")
    return {
        "core": identity, "scored": 30,
        "arms": {k: v for k, v in summary["arms"].items() if k.startswith("decision-browser/")},
        "attempts": [{k: r[k] for k in fields} for r in rows if r["engine"] == "decision-browser"],
        "retries": [r for r in audit["retries"] if r["engine"] == "decision-browser"],
    }


def include_intermediate(audit, intermediate):
    revision = intermediate["core"]["revision"]
    if revision == audit["core_rerun"]["measurement_commit"]:
        raise ValueError("Intermediate and final Core runs must use distinct revisions")
    if any(series["measurement_commit"] == revision for series in audit["core_history"]):
        raise ValueError("Intermediate Core series already appears in the published history")
    if "intermediate_core_run" in audit:
        audit.setdefault("additional_intermediate_core_runs", []).append(intermediate)
    else:
        audit["intermediate_core_run"] = intermediate
    audit["core_history"].append(archive_core_series(revision, None, intermediate["arms"]))


def summarize(rows, audit):
    indexed(rows, LONG_LABELS)
    fixture_hash = audit["sources"]["harness_sha256"]["long_scenarios.py"]
    if any(r["fixture_sha256"] != fixture_hash for r in rows):
        raise ValueError("Fixture used for measured trials differs from checked-in source")
    cases = {}
    for case, spec in suite.CASES.items():
        selected = [r for r in rows if r["case"] == case]
        goal_hashes = {r["goal_sha256"] for r in selected}
        goal = spec.goal
        if spec.values:
            goal += "\n\nProvided field values: " + json.dumps(spec.values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if goal_hashes != {hashlib.sha256(goal.encode()).hexdigest()}:
            raise ValueError(f"Different goals for {case}")
        cases[case] = {"title": spec.title, "goal": spec.goal, "min_actions": spec.minimum_actions,
                       "sections": len(spec.sections), "goal_sha256": selected[0]["goal_sha256"]}
    arms = {}
    for engine, route in configurations():
        selected = arm_rows(rows, engine, route)
        result = totals(selected)
        confirmed = [r for r in selected if r["confirmed"] == "True"]
        hashes = {r["worker_sha256"] for r in selected}
        filename = {"terra": "long_terra.py", "decision-browser": "long_core.py"}.get(engine, "long_external.py")
        source = audit["core_rerun"] if engine == "decision-browser" and "core_rerun" in audit else audit["sources"]
        if hashes != {source["harness_sha256"][filename]} or any(r["model"] != protocol.MODELS[route] for r in selected):
            raise ValueError(f"Worker or model changed within {engine}/{route}")
        result.update(model=protocol.MODELS[route], worker_sha256=hashes.pop(),
                      observed=sum(r["observed"] == "True" for r in selected),
                      calls=sum(int(r["calls"]) for r in selected),
                      unpriced_calls=sum(int(r["unpriced_calls"]) for r in selected),
                      helper_calls=sum(int(r["helper_calls"]) for r in selected),
                      median_confirmed_wall_ms=median(int(r["wall_ms"]) for r in confirmed) if confirmed else None,
                      per_case={c: sum(r["case"] == c for r in confirmed) for c in cases},
                      stop_reasons=dict(sorted(Counter(stop_reason(r)
                                       for r in selected if r["confirmed"] == "False").items())))
        arms[f"{engine}/{route}"] = result
    preflight = audit["preflight"]["arms"].values()
    result = {"cohort": "long", "scored": len(rows), "limits": LIMITS, "fixture_sha256": fixture_hash,
            "measurement_commit": audit["sources"]["measurement_commit"],
            "excluded_503_attempts": len(audit["retries"]),
            "preflight": {"issued": sum(a["issued"] for a in preflight),
                          "recorded_requests": sum(a["recorded_requests"] for a in preflight),
                          "known_estimated_usd": str(sum(Decimal(a["known_estimated_usd"]) for a in preflight)),
                          "billing_complete": audit["preflight"]["billing_complete"]},
            "cases": cases, "arms": arms}
    if "core_rerun" in audit:
        rerun = audit["core_rerun"]
        result["core_rerun"] = {k: rerun[k] for k in ("measurement_commit", "previous_publication", "previous_arms", "unconfirmed")}
        retries = [r for r in audit["retries"] if r["engine"] == "decision-browser"]
        if retries and all("known_estimated_usd" in r for r in retries):
            known = sum(Decimal(r["known_estimated_usd"]) for r in retries)
            scored = [arm for key, arm in arms.items() if key.startswith("decision-browser/")]
            result["core_rerun"]["retry_usage"] = {
                "requests": sum(len(r["http_statuses"]) for r in retries),
                "unpriced_calls": sum(r["unpriced_calls"] for r in retries),
                "known_estimated_usd": str(known),
                "known_physical_estimated_usd": str(known + sum(Decimal(arm["known_estimated_usd"]) for arm in scored)),
            }
    if "core_history" in audit:
        result["core_history"] = audit["core_history"]
    if "intermediate_core_run" in audit:
        intermediate = audit["intermediate_core_run"]
        result["intermediate_core_run"] = {
            "measurement_commit": intermediate["core"]["revision"], "scored": intermediate["scored"],
            "arms": intermediate["arms"],
        }
    return result


def label(key):
    engine, route = key.split("/")
    return LABELS[ENGINES.index(engine)], protocol.MODELS[route]


def report(summary):
    arms = summary["arms"]
    preflight = summary["preflight"]
    lines = [
        "# Five multi-step browser workflows",
        "",
        "Each agent ran the five workflows three times in a fresh browser. Terra used ordinary Playwright actions and GPT-6.1 Terra at medium reasoning. The other three libraries each ran on GPT-6 Luna through OpenAI Decisions and Jev 1.13 through OpenCode. These are 105 scored attempts across seven configurations.",
        "",
        "| Agent | Model | Confirmed | Independently observed | Median per attempt | Model calls | Estimated cost for 15 attempts |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for key, arm in arms.items():
        agent, model = label(key)
        lines.append(f"| {agent} | {model} | {arm['confirmed']}/15 | {arm['observed']}/15 | "
                     f"{seconds(arm['median_wall_ms'])} | {arm['calls']} | {display_cost(arm)} |")
    lines += [
        "", "The median includes unsuccessful attempts, even if the agent stopped on an early page. The timer covers worker and Chromium startup, agent work, independent page checks and cleanup. The parent process checks the saved fixture records immediately after the timed worker exits. Configurations ran concurrently on the same host, so timings include shared-host and provider variability. Estimated cost uses reported tokens for scored calls. Ultrafast used its GPT-6 Luna text helper on both decision-provider routes. Its requests and estimated charges are included. A ≥ sign marks the known subtotal when requests lack complete usage records.",
    ]
    if "core_rerun" in summary:
        rerun = summary["core_rerun"]
        source = f"https://github.com/HaffASE/decision-browser/tree/{rerun['measurement_commit']}"
        previous = (f"https://github.com/HaffASE/decision-browser/blob/{rerun['previous_publication']}/"
                    "docs/benchmarks/2026-11-03-long-suite/report.md")
        lines += [
            "", "## Core rerun", "",
            ("Core ran all 30 attempts again with the same goals, fixtures, scoring and budgets. "
             "The other 75 measurements come from the original series. The two series ran at different times. "
             f"[Measured Core source]({source}) · [Previous report]({previous})"), "",
            "| Model | Previous confirmations | New confirmations | Previous median | New median | Previous cost | New cost |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for key, old in rerun["previous_arms"].items():
            new = arms[key]
            lines.append(f"| {new['model']} | {old['confirmed']}/15 | {new['confirmed']}/15 | "
                         f"{seconds(old['median_wall_ms'])} | {seconds(new['median_wall_ms'])} | "
                         f"{display_cost(old)} | {display_cost(new)} |")
        if usage := rerun.get("retry_usage"):
            priced = usage["requests"] - usage["unpriced_calls"]
            lines += ["", (f"Excluded HTTP 503 attempts added {usage['requests']} requests. "
                           f"Usage was available for {priced} of them, worth an estimated ${usage['known_estimated_usd']}. "
                           f"Across both Core models, known estimated spend including retries is "
                           f"${usage['known_physical_estimated_usd']}. The tables use scored costs only.")]

        if rerun["unconfirmed"]:
            lines += ["", "Unconfirmed attempts in the final Core run:", "",
                      "| Model | Task | Repetition | Stop | Correct sections | Model calls | Provider diagnostic |",
                      "| --- | --- | ---: | --- | ---: | ---: | --- |"]
            for stop in rerun["unconfirmed"]:
                codes = ", ".join(f"`{c}`" for c in stop["native_evidence"] if c.startswith("provider:")) or "—"
                lines.append(f"| {protocol.MODELS[stop['route']]} | {stop['case']} | {stop['repetition']} | "
                             f"`{stop['reason']}` | {stop['correct_sections']} | {stop['calls']} | {codes} |")
    if "core_history" in summary:
        rerun = summary["core_rerun"]
        current = archive_core_series(rerun["measurement_commit"], None, arms)
        lines += [
            "", "## Core measurement history", "",
            ("Each row represents a separate run of the same 15 attempts per model. "
             "The chart uses the latest Core row. [Luna regression diagnosis](luna-diagnosis.md)"), "",
            "| Measured source | Luna confirmed | Luna median | Luna cost | Jev confirmed | Jev median | Jev cost |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for series in [*summary["core_history"], current]:
            src = series["measurement_commit"]
            cells = [f"[{src[:8]}](https://github.com/HaffASE/decision-browser/tree/{src})"]
            for route in ROUTES:
                arm = series["arms"][route]
                cells.extend([f"{arm['confirmed']}/{arm['attempts']}", seconds(arm["median_wall_ms"]), display_cost(arm)])
            lines.append("| " + " | ".join(cells) + " |")
        lines += ["", ("The audit includes source identities, links to earlier publications and "
                       "per-attempt records for the intermediate runs. The 75 measurements from the other agents contribute once.")]
    elif "intermediate_core_run" in summary:
        intermediate = summary["intermediate_core_run"]
        revision = intermediate["measurement_commit"]
        lines += [
            "",
            (f"A separate intermediate Core revision also ran all 30 attempts under the same limits. "
             f"Its observations are recorded separately in the audit. Only the final Core rerun contributes "
             f"to the 105-attempt chart. [Intermediate source](https://github.com/HaffASE/decision-browser/tree/{revision})"),
            "",
            "| Intermediate model | Confirmed | Median | Cost for 15 |",
            "| --- | ---: | ---: | ---: |",
        ]
        for old in intermediate["arms"].values():
            lines.append(f"| {old['model']} | {old['confirmed']}/15 | {seconds(old['median_wall_ms'])} | {display_cost(old)} |")
    lines += ["", "## Workflows", "",
              "| Task | Minimum browser actions | Sections | Goal |", "| --- | ---: | ---: | --- |"]
    for case, spec in summary["cases"].items():
        lines.append(f"| `{case}` | {spec['min_actions']} | {spec['sections']} | {spec['goal']} |")
    lines += ["", "A minimum action is a field entry, selection, checkbox change or section submission on the successful path. Initial navigation and a terminal completion signal are outside that count. The fixture records submitted choices and values on every section. Confirmation requires the agent’s native completion signal, the independent final page checks and an exact match to the saved sequence.",
              "", "## Confirmations by workflow", "",
              "| Agent | Model | " + " | ".join(LONG_LABELS.values()) + " |",
              "| --- | --- |" + " ---: |" * len(LONG_LABELS)]
    for key, arm in arms.items():
        lines.append("| " + " | ".join([*label(key), *[f"{arm['per_case'][c]}/3" for c in LONG_LABELS]]) + " |")
    lines += ["", "## Time and completion", "",
              "| Agent | Model | Median among confirmed attempts | Recorded stops on unconfirmed attempts |",
              "| --- | --- | ---: | --- |"]
    for key, arm in arms.items():
        duration = arm["median_confirmed_wall_ms"]
        reasons = ", ".join(f"`{reason}` × {n}" for reason, n in arm["stop_reasons"].items()) or "—"
        lines.append("| " + " | ".join([*label(key), seconds(duration) if duration is not None else "—", reasons]) + " |")
    lines += [
        "", "## Protocol and reproduction", "",
        "All agents used a 1280 × 720 Chromium viewport with up to 24 native steps, 24 physical model requests, 180 seconds and 512 KiB of cumulative request data per attempt. A text-helper request counts toward the same 24-call limit. Reaching a local request limit is a scored stop. The native stop reason can reflect the agent’s browser controls, guardrails or model output. The CSV reports the length of the correct prefix of submitted sections separately from final completion.",
        "", f"The protocol retries HTTP 503 on the same request after resetting the fixture. This run excluded {summary['excluded_503_attempts']} such attempts from scored timing and cost.",
        "", f"An earlier preflight issued {preflight['issued']} attempts. Forms redirected away from their declared destinations, and the initial Terra adapter rejected valid Playwright references after navigation. The fixture now submits to its declared destination, and Terra accepts document-scoped references present in its current snapshot. All seven configurations ran again. The preflight recorded {preflight['recorded_requests']} requests and at least ${preflight['known_estimated_usd']} in estimated usage. Billing for the preflight is incomplete. The full comparison excludes those trials.",
        "", "The combined GIF opens with the two long-workflow provider views, then shows the historical 16-task suite. Each cohort has a separate time scale and a separate cost denominator. The same Terra measurements appear once in each provider view of their cohort.",
        "", f"The original fixture, common runner and benchmark workers used source revision [`{summary['measurement_commit'][:12]}`](https://github.com/HaffASE/decision-browser/tree/{summary['measurement_commit']}). The audit includes the exact worker hashes and library source identities.",
        "", "[Combined GIF](../../assets/browser-four-agents.gif) · [Per-attempt CSV](runs.csv) · [Actions, page checks, saved sections and request usage](evidence.jsonl) · [Summary](summary.json) · [Sources, preflight and retries](audit.json)",
        "", "[Fixture definitions](../../../scripts/benchmarks/long_scenarios.py) · [Shared limits and pricing](../../../scripts/benchmarks/long_protocol.py) · [Live runner](../../../scripts/benchmarks/long_run.py)",
        "", "To exercise the five reference paths and their independent checks with local Chromium, install the locked development environment and run the fixture test. It covers the required action counts, wrong submitted values and direct attempts to open a receipt before finishing.",
        "", "```sh", "uv sync --frozen --group dev", ". .venv/bin/activate",
        "python -m playwright install chromium",
        "python -m pytest -q tests/integration/test_long_benchmark_scenarios.py", "```",
        "", "To run one live Terra attempt, set `CHROMIUM` to the Chromium executable, `PLAYWRIGHT_BROWSERS_PATH` to its browser installation directory and `OPENAI_API_KEY` to a valid Responses credential. On macOS, `--keychain-service SERVICE` can supply the credential instead.",
        "", "```sh", 'run_dir=$(mktemp -d)',
        "python scripts/benchmarks/long_run.py \\",
        "  --engine terra --route responses --worker scripts/benchmarks/long_terra.py \\",
        '  --source "$PWD" --python "$VIRTUAL_ENV/bin/python" \\',
        '  --chromium "$CHROMIUM" --browsers "$PLAYWRIGHT_BROWSERS_PATH" \\',
        '  --case rail_itinerary --repetition 1 --output "$run_dir"', "```",
        "", "Omit `--case` and `--repetition` to run all 15 attempts in that configuration. Core uses `--engine decision-browser`, `--worker scripts/benchmarks/long_core.py` and the project checkout for `--source`. Jev-ra and Ultrafast use `--engine jev-ra` or `--engine jev-ultrafast` with `--worker scripts/benchmarks/long_external.py`. For each external engine, point `--source` to its prepared source checkout and `--python` to its environment. Both library routes accept `--route openai` or `--route opencode`. OpenCode uses its stored Console credential or `OPENCODE_API_KEY`; Ultrafast also needs the OpenAI credential for its text helper.",
        "", "To rebuild the report and GIF from the published CSV, run the following command in the project environment with `ffmpeg`, `ffprobe` and `rsvg-convert` on PATH.",
        "", "```sh", "python scripts/benchmarks/publish_long_suite.py", "```",
    ]
    if "core_history" in summary:
        rerun = summary["core_rerun"]
        n = sum(series["publication"] is None for series in summary["core_history"])
        intermediate_flags = [f'  --intermediate-core-run "$INTERMEDIATE_RUN_{i+1}" \\' for i in range(n)]
        lines += [
            "", "To import this series, supply the raw Core run directories and pinned revisions.", "",
            "```sh",
            "python scripts/benchmarks/publish_long_suite.py \\",
            '  --raw-dir "$FINAL_RUN" \\', *intermediate_flags,
            f"  --source-revision {rerun['measurement_commit']} \\",
            f"  --previous-publication {rerun['previous_publication']}", "```",
        ]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, help="Import one complete live matrix with primary and retry records")
    parser.add_argument("--source-revision", help="Git revision of the benchmark files used for the live matrix")
    parser.add_argument("--previous-publication", help="For a Core-only rerun, Git revision of the previous published series")
    parser.add_argument("--intermediate-core-run", action="append", type=Path, default=[],
                        help="Archive a 30-attempt Core series; repeat in chronological order")
    args = parser.parse_args()
    if args.intermediate_core_run and not args.previous_publication:
        parser.error("--intermediate-core-run requires --previous-publication")
    if args.previous_publication and not args.raw_dir:
        parser.error("--previous-publication requires a Core --raw-dir")
    if args.raw_dir:
        if not args.source_revision:
            parser.error("--raw-dir requires --source-revision")
        if args.previous_publication:
            rows, evidence, audit = core_rerun(args.raw_dir, args.source_revision, args.previous_publication)
            for raw_intermediate in args.intermediate_core_run:
                intermediate = intermediate_core_run(raw_intermediate, args.previous_publication)
                include_intermediate(audit, intermediate)
        else:
            rows, excluded, evidence = from_raw(args.raw_dir)
            audit = {"sources": provenance(args.raw_dir, args.source_revision),
                     "preflight": json.loads((args.raw_dir / "preflight-audit.json").read_bytes()), "retries": excluded}
    else:
        rows = read_rows(PUBLIC / "runs.csv")
        audit = json.loads((PUBLIC / "audit.json").read_bytes())
        evidence = [json.loads(line) for line in (PUBLIC / "evidence.jsonl").read_text().splitlines()]
    validate_evidence(rows, evidence)
    summary = summarize(rows, audit)
    PUBLIC.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PUBLIC) as directory:
        stage = Path(directory)
        with (stage / "runs.csv").open("w", newline="") as source:
            writer = csv.DictWriter(source, fieldnames=COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        (stage / "summary.json").write_text(serial(summary))
        (stage / "audit.json").write_text(serial(audit))
        (stage / "evidence.jsonl").write_text("".join(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n"
                                                     for row in evidence))
        (stage / "report.md").write_text(report(summary))
        render(rows, stage / GIF.name)
        for name in ("runs.csv", "audit.json", "evidence.jsonl", "summary.json", "report.md"):
            os.replace(stage / name, PUBLIC / name)
        os.replace(stage / GIF.name, GIF)
    print(f"Published {len(rows)} long attempts and rendered both cohorts in {GIF}")


if __name__ == "__main__":
    main()
