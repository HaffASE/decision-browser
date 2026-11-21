"""Aggregate preserved benchmark evidence without cherry-picking failed runs."""

from __future__ import annotations

import copy
import csv
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENGINES = ("decision-browser", "jev-ultrafast", "jev-ra")


@pytest.fixture
def renderer(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "comparison_report_under_test", ROOT / "scripts" / "render_library_comparison.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def call(endpoint="decisions", tokens=100, output=0):
    return {"endpoint": endpoint, "state": "response", "status": 200,
            "input_tokens": tokens, "output_tokens": output, "cached_tokens": 0,
            "cache_write_tokens": 0, "input_bytes": 1234}


def usage(calls):
    costs = [(row["input_tokens"] * .10
              + (row["output_tokens"] * .50 if row["endpoint"] == "responses" else 0))
             / 1_000_000 for row in calls]
    return {"calls": calls, "usage_complete": True, "estimated_usd": sum(costs),
            "decisions_attempts": sum(row["endpoint"] == "decisions" for row in calls),
            "responses_attempts": sum(row["endpoint"] == "responses" for row in calls),
            "input_tokens": sum(row["input_tokens"] for row in calls),
            "input_bytes": sum(row["input_bytes"] for row in calls)}


def report():
    rows = []
    for case, group in (("click", "baseline"), ("hotel", "expanded")):
        for index, engine in enumerate(ENGINES):
            rows.append({"attempt": len(rows) + 1, "case": case, "group": group,
                         "repetition": 1, "engine": engine, "goal_sha256": "same-" + case,
                         "confirmed": True, "observed": True, "native_success": True,
                         "result": {"status": "verified" if index == 0 else "done",
                                    "reason": "goal_achieved", "wall_process_ms": 1000 + index * 100,
                                    "usage": usage([call()])}})
    return {"runs": rows, "repetitions": 1, "cases": ["click", "hotel"], "planned_runs": 6,
            "estimated_usd": sum(row["result"]["usage"]["estimated_usd"] for row in rows),
            "usage_complete": True, "status": "COMPLETE", "sources_unchanged": True,
            "model": "gpt-6-luna", "not_run": []}


def replace_calls(evidence, index, calls):
    previous = evidence["runs"][index]["result"]["usage"]["estimated_usd"]
    replacement = usage(calls)
    evidence["runs"][index]["result"]["usage"] = replacement
    evidence["estimated_usd"] += replacement["estimated_usd"] - previous


def test_native_failure_with_achieved_page_excludes_whole_triple_from_speed(renderer):
    evidence = report()
    failed = evidence["runs"][-1]
    failed.update(confirmed=False, native_success=False)
    failed["result"].update(status="blocked", reason="stuck_loop", wall_process_ms=1)
    # All three timings in that tuple are excluded, including other successes.
    evidence["runs"][3]["result"]["wall_process_ms"] = 2
    evidence["runs"][4]["result"]["wall_process_ms"] = 3
    summary = renderer.summarize(evidence)
    assert summary["matched_pairs"] == [{"case": "click", "repetition": 1}]
    assert [summary["engines"][engine]["matched_cold_wall_median_ms"] for engine in ENGINES] == [1000, 1100, 1200]
    assert summary["engines"]["jev-ra"]["confirmed"] == 1
    assert summary["engines"]["jev-ra"]["observed"] == 2
    assert summary["engines"]["jev-ra"]["failure_reasons"] == {"stuck_loop": 1}


def test_failed_fast_attempts_still_contribute_all_requests_and_cost(renderer):
    evidence = report()
    failed = evidence["runs"][-1]
    failed.update(confirmed=False, observed=False, native_success=False)
    failed["result"].update(status="error", reason="invalid_decision", wall_process_ms=1)
    replace_calls(evidence, -1, [call(tokens=100_000), call("responses", tokens=100_000, output=200)])
    summary = renderer.summarize(evidence)
    engine = summary["engines"]["jev-ra"]
    assert engine["attempted"] == 2 and engine["confirmed"] == 1
    assert engine["calls"] == 3 and engine["known_estimated_usd"] == pytest.approx(.02011)
    assert summary["total_known_estimated_usd"] == pytest.approx(.02015)
    assert engine["matched_cold_wall_median_ms"] == 1200


def test_missing_usage_keeps_known_subtotal_and_marks_incomplete(renderer):
    evidence = report()
    target = evidence["runs"][0]["result"]["usage"]
    evidence["estimated_usd"] -= target["estimated_usd"]
    target.update(usage_complete=False, estimated_usd=0)
    target["calls"][0]["input_tokens"] = None
    # Even a mistakenly true top-level flag cannot turn unknown usage complete.
    summary = renderer.summarize(evidence)
    assert summary["usage_complete"] is False
    assert summary["engines"]["decision-browser"]["unknown_usage_calls"] == 1
    assert summary["engines"]["decision-browser"]["known_estimated_usd"] == pytest.approx(.00001)
    assert summary["total_known_estimated_usd"] == pytest.approx(.00005)


@pytest.mark.parametrize("violation,error", [("duplicate", "duplicate_comparison_attempt"),
                                            ("goal", "unequal_goals")])
def test_duplicate_attempts_and_unequal_task_goals_are_rejected(renderer, violation, error):
    evidence = report()
    if violation == "duplicate":
        evidence["runs"].append(copy.deepcopy(evidence["runs"][0]))
    else:
        evidence["runs"][1]["goal_sha256"] = "different-task-facts"
    with pytest.raises(ValueError, match=error):
        renderer.summarize(evidence)


def test_wrong_reported_total_cannot_be_rendered_as_verified_cost(renderer):
    evidence = report()
    evidence["estimated_usd"] += .001
    with pytest.raises(ValueError, match="cost_total_mismatch"):
        renderer.summarize(evidence)


def test_decisions_and_text_helper_prices_and_call_counts_are_split(renderer):
    evidence = report()
    replace_calls(evidence, 1, [call(tokens=100_000), call("responses", tokens=100_000, output=200)])
    summary = renderer.summarize(evidence)
    engine = summary["engines"]["jev-ultrafast"]
    assert engine["decisions_calls"] == 2 and engine["responses_calls"] == 1
    assert engine["estimated_usd_by_endpoint"] == pytest.approx({"decisions": .01001, "responses": .0101})
    assert engine["known_estimated_usd"] == pytest.approx(.02011)
    assert engine["input_tokens"] == 200_100 and engine["request_bytes"] == 3 * 1234


def test_exported_csv_preserves_observed_failure_and_matched_sample(renderer, tmp_path):
    evidence = report()
    failed = evidence["runs"][-1]
    failed.update(confirmed=False, native_success=False)
    failed["result"].update(status="blocked", reason="stuck_loop", wall_process_ms=1)
    source = tmp_path / "results.json"
    source.write_text(json.dumps(evidence))
    destination = tmp_path / "rendered"
    summary = renderer.write_outputs(evidence, source, destination)
    with (destination / "runs.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 6 and summary["raw_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert [row["matched"] for row in rows] == ["True"] * 3 + ["False"] * 3
    assert rows[-1]["confirmed"] == "False" and rows[-1]["observed"] == "True"
    assert rows[-1]["native_success"] == "False" and rows[-1]["reason"] == "stuck_loop"
    assert rows[-1]["wall_process_ms"] == "1" and rows[-1]["decisions_calls"] == "1"
    assert float(rows[-1]["known_estimated_usd"]) == pytest.approx(.00001)
    assert rows[-1]["usage_complete"] == "True"
    assert "1 пар" in (destination / "tables.md").read_text()
    with pytest.raises(FileExistsError):
        renderer.write_outputs(evidence, source, destination)


def test_partial_triple_is_retained_but_excluded_from_paired_timing(renderer):
    evidence = report()
    missing = evidence["runs"].pop()
    evidence["estimated_usd"] -= missing["result"]["usage"]["estimated_usd"]
    evidence.update(status="STOPPED", not_run=[{"case": "hotel", "repetition": 1, "engine": "jev-ra"}])
    summary = renderer.summarize(evidence)
    assert summary["attempted_runs"] == 5 and summary["planned_runs"] == 6 and summary["not_run"] == 1
    assert summary["matched_pairs"] == [{"case": "click", "repetition": 1}]
    assert summary["per_case"]["hotel"]["jev-ra"] == {"confirmed": 0, "observed": 0, "attempted": 0, "planned": 1}
    assert summary["engines"]["jev-ultrafast"]["attempted"] == 2


def test_no_common_success_sample_has_no_speed_median(renderer):
    evidence = report()
    for row in evidence["runs"]:
        if row["engine"] == "jev-ra":
            row.update(confirmed=False, native_success=False)
    summary = renderer.summarize(evidence)
    assert summary["matched_pairs"] == []
    assert all(row["matched_cold_wall_median_ms"] is None for row in summary["engines"].values())
    assert "нет общей выборки" in renderer.render_table(summary)
