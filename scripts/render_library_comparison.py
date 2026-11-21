"""Render existing sanitized comparison evidence; never open a browser or API."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any

from benchmarks.common import estimate

ENGINES = ("decision-browser", "jev-ultrafast", "jev-ra")


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    rows = report["runs"]
    repetitions = report["repetitions"]
    cases = report["cases"]
    identities = [(row["case"], row["repetition"], row["engine"]) for row in rows]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate_comparison_attempt")
    if any(case not in cases or not 1 <= rep <= repetitions or engine not in ENGINES
           for case, rep, engine in identities):
        raise ValueError("invalid_comparison_attempt")
    if report["planned_runs"] != len(cases) * repetitions * len(ENGINES):
        raise ValueError("invalid_comparison_plan")
    triples = defaultdict(dict)
    for row in rows:
        triples[row["case"], row["repetition"]][row["engine"]] = row
    for triple in triples.values():
        if len({row["goal_sha256"] for row in triple.values()}) != 1:
            raise ValueError("unequal_goals")
    matched = {key for key, triple in triples.items()
               if set(triple) == set(ENGINES)
               and all(row["confirmed"] is True for row in triple.values())}
    engines = {}
    per_case = {}
    complete = True
    for engine in ENGINES:
        selected = [row for row in rows if row["engine"] == engine]
        calls = [call for row in selected for call in (row["result"].get("usage") or {}).get("calls", [])]
        costs = [estimate(call) for call in calls]
        known = sum(cost for cost in costs if cost is not None)
        complete &= (all(cost is not None for cost in costs)
                     and all((row["result"].get("usage") or {}).get("usage_complete") is True
                             for row in selected))
        matched_times = [row["result"]["wall_process_ms"] for row in selected
                         if (row["case"], row["repetition"]) in matched]
        engine_summary = {
            "attempted": len(selected), "planned": len(cases) * repetitions,
            "confirmed": sum(row["confirmed"] is True for row in selected),
            "observed": sum(row["observed"] is True for row in selected),
            "matched_triples": len(matched),
            "matched_cold_wall_median_ms": median(matched_times) if matched_times else None,
            "calls": len(calls),
            "decisions_calls": sum(call["endpoint"] == "decisions" for call in calls),
            "responses_calls": sum(call["endpoint"] == "responses" for call in calls),
            "input_tokens": sum(call.get("input_tokens") or 0 for call in calls),
            "output_tokens": sum(call.get("output_tokens") or 0 for call in calls),
            "request_bytes": sum(call["input_bytes"] for call in calls),
            "known_estimated_usd": known, "unknown_usage_calls": sum(cost is None for cost in costs),
            "estimated_usd_by_endpoint": {
                endpoint: sum(cost for call, cost in zip(calls, costs, strict=True)
                              if call["endpoint"] == endpoint and cost is not None)
                for endpoint in ("decisions", "responses")},
            "http_statuses": dict(Counter(str(call.get("status", call["state"])) for call in calls)),
            "failure_reasons": dict(Counter(
                row["result"].get("reason") or row["result"].get("error_type") or row["result"]["status"]
                for row in selected if not row["confirmed"])),
        }
        for group in ("baseline", "expanded"):
            subset = [row for row in selected if row["group"] == group]
            engine_summary[group] = {"confirmed": sum(row["confirmed"] is True for row in subset),
                                     "observed": sum(row["observed"] is True for row in subset),
                                     "attempted": len(subset)}
        engines[engine] = engine_summary
    for case in cases:
        per_case[case] = {}
        for engine in ENGINES:
            selected = [row for row in rows if row["case"] == case and row["engine"] == engine]
            per_case[case][engine] = {
                "confirmed": sum(row["confirmed"] is True for row in selected),
                "observed": sum(row["observed"] is True for row in selected),
                "attempted": len(selected), "planned": repetitions,
            }
    total = sum(row["known_estimated_usd"] for row in engines.values())
    if abs(total - report["estimated_usd"]) > 1e-10:
        raise ValueError("cost_total_mismatch")
    return {"schema_version": 1, "status": report["status"],
            "sources_unchanged": report.get("sources_unchanged"),
            "usage_complete": bool(complete and report["usage_complete"]),
            "planned_runs": report["planned_runs"], "attempted_runs": len(rows),
            "not_run": len(report.get("not_run", [])), "model": report["model"],
            "total_known_estimated_usd": total, "engines": engines, "per_case": per_case,
            "matched_pairs": [{"case": case, "repetition": rep} for case, rep in sorted(matched)]}


def render_table(summary: dict) -> str:
    lines = ["| Библиотека | Подтверждено | Страница достигнута | Decisions / Responses | Расчётный расход | Медиана общего процесса* |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for engine, row in summary["engines"].items():
        timing = row["matched_cold_wall_median_ms"]
        display = f"{timing / 1000:.3f} с" if timing is not None else "нет общей выборки"
        lines.append(f"| {engine} | {row['confirmed']}/{row['planned']} | {row['observed']}/{row['planned']} "
                     f"| {row['decisions_calls']} / {row['responses_calls']} "
                     f"| ${row['known_estimated_usd']:.8f} | {display} |")
    n = len(summary["matched_pairs"])
    lines += ["", (f"*Одинаковые {n} пар «кейс × повтор», где все три библиотеки подтвердили успех. "
              "Время включает запуск Python, браузер, выполнение, проверку и очистку. "
              "Расход включает все попытки, в том числе неудачные."), "",
              "| Кейс | decision-browser | jev-ultrafast | jev-ra |", "| --- | ---: | ---: | ---: |"]
    for case, engines in summary["per_case"].items():
        cells = [f"{engines[engine]['confirmed']}/{engines[engine]['planned']} "
                 f"({engines[engine]['observed']}/{engines[engine]['planned']})" for engine in ENGINES]
        lines.append(f"| {case} | " + " | ".join(cells) + " |")
    lines += ["", "В скобках — достигнутое состояние страницы по точным DOM-проверкам и событиям сервера.", ""]
    return "\n".join(lines)


def write_outputs(report: dict, source: Path, directory: Path):
    summary = summarize(report)
    directory.mkdir(parents=True, exist_ok=False)
    summary["raw_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    (directory / "tables.md").write_text(render_table(summary))
    matched = {(row["case"], row["repetition"]) for row in summary["matched_pairs"]}
    columns = ("attempt", "case", "repetition", "engine", "group", "confirmed", "observed", "native_success",
               "status", "reason", "wall_process_ms", "matched", "decisions_calls", "responses_calls",
               "input_tokens", "request_bytes", "known_estimated_usd", "usage_complete")
    with (directory / "runs.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in report["runs"]:
            result, usage = row["result"], row["result"].get("usage") or {}
            writer.writerow({**{key: row[key] for key in columns[:8]},
                             "status": result["status"], "reason": result.get("reason"),
                             "wall_process_ms": result["wall_process_ms"],
                             "matched": (row["case"], row["repetition"]) in matched,
                             "decisions_calls": usage.get("decisions_attempts"),
                             "responses_calls": usage.get("responses_attempts"),
                             "input_tokens": usage.get("input_tokens"),
                             "request_bytes": usage.get("input_bytes"),
                             "known_estimated_usd": usage.get("estimated_usd"),
                             "usage_complete": usage.get("usage_complete")})
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="New directory")
    args = parser.parse_args()
    report = json.loads(args.input.read_text())
    summary = write_outputs(report, args.input, args.output)
    print(json.dumps({"attempts": summary["attempted_runs"], "estimated_usd": summary["total_known_estimated_usd"],
                      "output": str(args.output)}))


if __name__ == "__main__":
    main()
