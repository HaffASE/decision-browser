"""The offline auditor preserves raw evidence and rejects incomplete matrices."""
from __future__ import annotations

import copy
import csv
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


def module():
    path = Path(__file__).resolve().parents[2] / "scripts/render_provider_browser_matrix.py"
    spec = importlib.util.spec_from_file_location("provider_matrix_report_test", path)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def capture(root, mod):
    files, combined = [], hashlib.sha256()
    for name in sorted(("scripts/run_provider_browser_matrix.py", "scripts/benchmarks/provider_matrix_worker.py")):
        data = b"# Synthetic source capture; never executed.\n"
        path = root / "capture-source" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        files.append({"path": name, "sha256": hashlib.sha256(data).hexdigest()})
        encoded = name.encode()
        combined.update(len(encoded).to_bytes(8, "big") + encoded)
        combined.update(len(data).to_bytes(8, "big") + data)
    save(root / "source-manifest.json", {"sha256": combined.hexdigest(), "files": files})
    raw = {"status": "COMPLETE", "sources_unchanged": True, "retries": 0, "fallbacks": 0,
           "repetitions": 3, "cases": list(mod.CASES), "arms": copy.deepcopy(mod.ARMS), "seed": 20261117,
           "source_sha256": combined.hexdigest(), "planned_runs": 189, "remaining": [], "runs": [],
           "runtime": {"chromium": "synthetic-version"}}
    for cell in mod.expected_plan(raw):
        long = cell["case"] in mod.CASES[-5:]
        call = {"input_tokens": 10, "output_tokens": 0, "total_tokens": 10,
                "cached_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0,
                "input_bytes": 100, "status": 200, "usage_reported": True,
                "request_model": mod.ARMS[cell["arm"]]["model"], "model": mod.ARMS[cell["arm"]]["model"],
                "endpoint": mod.ARMS[cell["arm"]]["endpoint"].rsplit("/", 1)[-1]}
        terra = cell["arm"] == "terra"
        if terra:
            call["reasoning_effort"] = "medium"
        check_count = 1 if cell["case"] == "public_about" else 3 if cell["case"] in {"autocomplete", "calendar"} else 2
        result = {"status": "done" if terra else "verified", "functional": "verified", "reason": "goal_achieved" if terra else None,
                  "returncode": 0, "checks": [True] * check_count,
                  "cleanup": {"browser": "closed"} if terra else {"driver_state": "closed", "errors": []},
                  "wall_ms": 100, "usage": {"calls": [call], "attempts": 1, "usage_complete": True},
                  "model": mod.ARMS[cell["arm"]]["model"], "native_evidence": []}
        if terra:
            result["reasoning_effort"] = "medium"
        row = {**cell, "phase": "finished", "confirmed": True, "observed": True,
               "goal_sha256": hashlib.sha256(cell["case"].encode()).hexdigest(),
               "oracle": {"success": True}, "result": result,
               "limits": {"max_steps": 24 if long or cell["case"] in mod.CASES[6:] else 8,
                          "max_calls": 24 if long else 12, "timeout_s": 180 if long else 75,
                          "max_input_bytes": 524288 if long else 131072},
               "record": f"runs/{cell['arm']}/{cell['case']}/{cell['repetition']}.json"}
        raw["runs"].append(row)
        save(root / row["record"], row)
    save(root / "results.json", raw)
    save(root / "summary.json", {"arms": {}, "cases": {}, "planned_runs": 0})  # The mutable-plan bug.
    (root / "runs.csv").write_text("raw capture CSV remains unchanged\n")
    return raw


def persist(root, raw, row):
    save(root / row["record"], row)
    save(root / "results.json", raw)


def test_all_cells_are_restored_without_overwriting_any_raw_capture(tmp_path):
    mod = module()
    capture(tmp_path, mod)
    before = {name: (tmp_path / name).read_bytes() for name in mod.RAW_FILES}
    audit = mod.generate(tmp_path)
    assert audit["status"] == "PASS" and audit["verified_attempt_records"] == 189 and audit["paid_reruns"] == 0
    assert before == {name: (tmp_path / name).read_bytes() for name in mod.RAW_FILES}
    summary = mod.load(tmp_path / "audited-summary.json")
    assert summary["planned_runs"] == summary["completed_runs"] == 189
    assert len(summary["cases"]) == 21 and all(data["attempts"] == data["confirmed"] == 63 for data in summary["arms"].values())
    assert all(data["tokens"]["input_tokens"]["total"] == 630 for data in summary["arms"].values())
    with pytest.raises(mod.AuditError, match="prior_audit_outputs_exist"):
        mod.generate(tmp_path)


def test_running_capture_is_refused_without_creating_outputs(tmp_path):
    mod = module()
    raw = capture(tmp_path, mod)
    raw["status"] = "RUNNING"
    save(tmp_path / "results.json", raw)
    with pytest.raises(mod.AuditError, match="capture_not_complete"):
        mod.generate(tmp_path)
    assert not any((tmp_path / name).exists() for name in mod.OUTPUTS)


@pytest.mark.parametrize("mutation,reason", [
    ("duplicate", "duplicate_or_missing_cells"), ("record", "per_attempt_record_mismatch"),
    ("extra", "extra_or_pending_attempt_records"), ("source", "captured_source_hash_mismatch"),
])
def test_inconsistent_or_changed_evidence_is_rejected(tmp_path, mutation, reason):
    mod = module()
    raw = capture(tmp_path, mod)
    if mutation == "duplicate":
        raw["runs"][-1] = raw["runs"][0]
        save(tmp_path / "results.json", raw)
    elif mutation == "record":
        save(tmp_path / raw["runs"][0]["record"], {})
    elif mutation == "extra":
        save(tmp_path / "runs/extra/pending.json", {"phase": "pending"})
    else:
        (tmp_path / "capture-source/scripts/benchmarks/provider_matrix_worker.py").write_text("changed")
    with pytest.raises(mod.AuditError, match=reason):
        mod.generate(tmp_path)


@pytest.mark.parametrize("mutation,reason", [
    ("effort", "terra_effort_mismatch"), ("model", "unexpected_api_route_or_model"),
    ("bytes", "request_byte_budget_violation"), ("confirmation", "false_confirmation"),
])
def test_model_budgets_and_confirmation_are_checked(tmp_path, mutation, reason):
    mod = module()
    raw = capture(tmp_path, mod)
    row = next(row for row in raw["runs"] if row["arm"] == "terra")
    call = row["result"]["usage"]["calls"][0]
    if mutation == "effort":
        call["reasoning_effort"] = "high"
    elif mutation == "model":
        call["request_model"] = "gpt-6-luna"
    elif mutation == "bytes":
        call["input_bytes"] = row["limits"]["max_input_bytes"] + 1
    else:
        row["oracle"]["success"] = False
    persist(tmp_path, raw, row)
    with pytest.raises(mod.AuditError, match=reason):
        mod.generate(tmp_path)


def test_unknown_usage_is_nullable_while_failed_attempt_tokens_are_kept(tmp_path):
    mod = module()
    raw = capture(tmp_path, mod)
    row = next(row for row in raw["runs"] if row["arm"] == "jev")
    row["confirmed"] = False
    row["result"].update(status="failed", reason="failed", native_evidence=["provider:rejected_sentinel"])
    row["result"]["usage"]["calls"][0]["input_tokens"] = None
    row["result"]["usage"]["usage_complete"] = False
    persist(tmp_path, raw, row)
    mod.generate(tmp_path)
    summary = mod.load(tmp_path / "audited-summary.json")["arms"]["jev"]
    assert summary["tokens"]["input_tokens"] == {"total": None, "reported_total": 620, "missing_calls": 1, "unknown_runs": 0}
    assert summary["tokens"]["output_tokens"]["total"] == 0
    assert summary["diagnostic_codes"] == {"provider:rejected_sentinel": 1}
    with (tmp_path / "audited-runs.csv").open() as stream:
        exported = next(r for r in csv.DictReader(stream) if r["arm"] == row["arm"] and r["case"] == row["case"] and r["repetition"] == str(row["repetition"]))
    assert exported["input_tokens"] == "" and exported["input_tokens_missing_calls"] == "1"


def test_native_done_with_failed_oracle_is_categorized_without_invented_cause(tmp_path):
    mod = module()
    raw = capture(tmp_path, mod)
    row = next(row for row in raw["runs"] if row["arm"] == "terra")
    row.update(confirmed=False, observed=False, oracle={"success": False, "observed": "wrong synthetic result"})
    persist(tmp_path, raw, row)
    mod.generate(tmp_path)
    error = mod.load(tmp_path / "errors.json")[0]
    assert error["status"] == "done" and error["reason"] == "goal_achieved"
    assert error["categories"] == ["oracle_mismatch"] and error["http"][0]["input_tokens"] == 10


def test_503_csv_distinguishes_known_call_count_from_missing_token_usage(tmp_path):
    mod = module()
    raw = capture(tmp_path, mod)
    row = next(row for row in raw["runs"] if row["arm"] == "decision_api")
    row["confirmed"] = False
    row["result"].update(status="failed", reason="failed", native_evidence=["provider:http_error"])
    usage = row["result"]["usage"]
    failed_call = {**usage["calls"][0], "status": 503, "usage_reported": False}
    failed_call.update({name: None for name in mod.TOKENS})
    failed_call.pop("model")
    usage.update(calls=[usage["calls"][0], failed_call], attempts=2, usage_complete=False)
    persist(tmp_path, raw, row)
    mod.generate(tmp_path)
    with (tmp_path / "audited-runs.csv").open() as stream:
        reader = csv.DictReader(stream)
        assert "unknown_usage" not in reader.fieldnames
        exported = next(r for r in reader if r["arm"] == row["arm"] and r["case"] == row["case"]
                        and r["repetition"] == str(row["repetition"]))
    assert exported["calls"] == "2" and exported["unknown_call_count"] == "False"
    assert exported["usage_complete"] == "False"
    assert exported["input_tokens"] == exported["output_tokens"] == ""
    assert exported["input_tokens_reported"] == "10" and exported["output_tokens_reported"] == "0"
    assert exported["input_tokens_missing_calls"] == exported["output_tokens_missing_calls"] == "1"
