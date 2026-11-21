"""Matrix accounting and lifecycle validation without credentials or live calls."""
from __future__ import annotations

import copy
import csv
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


def command():
    path = Path(__file__).resolve().parents[2] / "scripts/run_provider_browser_matrix.py"
    spec = importlib.util.spec_from_file_location("provider_matrix_command_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(*, calls=None, attempts=1, complete=True, confirmed=True):
    return {"arm": "jev", "case": "click", "repetition": 1, "record": "runs/jev/click/1.json",
            "confirmed": confirmed, "observed": True,
            "result": {"status": "verified", "reason": None, "wall_ms": 20,
                       "usage": {"calls": calls if calls is not None else [], "attempts": attempts,
                                 "usage_complete": complete}}}


def test_complete_schedule_has_all_189_unique_cells_and_rotates_arms():
    module = command()
    suite = module.bench.load_scenarios()
    cases = module.all_cases(suite)
    plan = list(module.schedule(cases, list(module.DEFAULT_ARMS), 3))
    assert len(cases) == 21 and len(plan) == 189
    assert len({(c["arm"], c["case"], c["repetition"]) for c in plan}) == 189
    assert list(module.schedule(cases, list(module.DEFAULT_ARMS), 3)) == plan
    assert plan[:3] == [{**plan[0], "arm": arm} for arm in ("decision_api", "terra", "jev")]


def test_failed_cells_preserve_arm_case_and_original_repetition():
    module = command()
    plan = list(module.schedule(["click", "counter"], ["jev", "terra"], 3))
    failure = {"arm": "terra", "case": "counter", "repetition": 2}
    rows = [{**cell, "phase": "finished", "confirmed": cell != failure} for cell in reversed(plan)]
    baseline = {"status": "COMPLETE", "sources_unchanged": True, "runs": rows}
    assert module.failed_cells(baseline, plan) == [failure]
    with pytest.raises(ValueError, match="invalid_failure_baseline"):
        module.failed_cells({**baseline, "runs": rows[:-1] + [rows[0]]}, plan)


def test_short_and_long_requests_keep_the_shared_budgets():
    module = command()
    suite = module.bench.load_scenarios()
    baseline = module.make_request("click", "http://127.0.0.1:9/", suite)
    expanded = module.make_request("hotel", "http://127.0.0.1:9/", suite)
    long = module.make_request("rail_itinerary", "http://127.0.0.1:9/", suite)
    assert (baseline["max_steps"], expanded["max_steps"], long["max_steps"]) == (8, 24, 24)
    assert (baseline["max_calls"], expanded["max_calls"], long["max_calls"]) == (12, 12, 24)
    assert (baseline["timeout_s"], long["timeout_s"]) == (75, 180)
    assert baseline["max_cost_usd"] == expanded["max_cost_usd"] == long["max_cost_usd"] == .25


def test_unknown_tokens_do_not_become_zero_and_failed_calls_stay_in_totals(tmp_path):
    module = command()
    rows = [row(calls=[{"input_tokens": 7, "output_tokens": 0, "status": 200},
                       {"status": 429}], attempts=2, complete=False, confirmed=False)]
    totals = module.token_totals(rows)
    assert totals["input_tokens"] == {"total": None, "reported_total": 7, "missing_calls": 1, "unknown_runs": 0}
    assert totals["output_tokens"]["reported_total"] == 0
    module.write_csv(tmp_path, rows)
    with (tmp_path / "runs.csv").open() as stream:
        exported = next(csv.DictReader(stream))
    assert exported["input_tokens"] == exported["output_tokens"] == ""
    summary = module.summarize(rows, [{"arm": "jev", "case": "click", "repetition": 1}])
    assert summary["verdict"] == "HAS_FAILURES"
    assert summary["arms"]["jev"]["calls"] == 2
    assert summary["arms"]["jev"]["http_errors"] == 1


def test_lost_worker_has_unknown_spend_even_without_returned_call_rows(tmp_path):
    module = command()
    rows = [row(attempts=None, complete=False, confirmed=False)]
    assert module.token_totals(rows)["input_tokens"]["unknown_runs"] == 1
    module.write_csv(tmp_path, rows)
    with (tmp_path / "runs.csv").open() as stream:
        assert next(csv.DictReader(stream))["input_tokens"] == ""


def test_listing_does_not_read_credentials_or_create_evidence(monkeypatch, capsys, tmp_path):
    module = command()
    output = tmp_path / "absent"
    monkeypatch.setattr(module, "credentials", lambda *_: pytest.fail("listing read credentials"))
    monkeypatch.setattr("sys.argv", ["matrix", "--list", "--output", str(output)])
    assert module.main() == 0
    assert len(json.loads(capsys.readouterr().out)["cases"]) == 21
    assert not output.exists()


def test_existing_output_is_preserved_before_credential_lookup(monkeypatch, tmp_path):
    module = command()
    monkeypatch.setattr(module, "credentials", lambda *_: pytest.fail("existing output read credentials"))
    evidence = tmp_path / "prior"
    evidence.mkdir()
    (evidence / "record").write_text("preserve")
    monkeypatch.setattr("sys.argv", ["matrix", "--output", str(evidence), "--chromium", "unused", "--browsers", "unused"])
    with pytest.raises(SystemExit) as result:
        module.main()
    assert result.value.code == 2
    assert (evidence / "record").read_text() == "preserve"


def test_cell_uses_fresh_fixture_and_existing_strict_outcome(monkeypatch, tmp_path):
    module = command()
    suite = module.bench.load_scenarios()
    args = SimpleNamespace(output=tmp_path, chromium=Path("/synthetic/chromium"))

    class Fixture:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def register(self, case):
            assert case == "click"
            return "id", "http://127.0.0.1:9/"

        def verify(self, key):
            assert key == "id"
            return {"success": False, "events": []}

    monkeypatch.setattr(module.bench, "FixtureSite", Fixture)

    def execute(_args, request, key, keys):
        assert request["arm"] == "jev" and key == "synthetic-access-token"
        assert request["start_url"].startswith("http://127.0.0.1:")
        return {"returncode": 0, "status": "verified", "functional": "verified", "reason": None,
                "checks": [True] * len(request["checks"]), "wall_ms": 10,
                "cleanup": {"driver_state": "closed", "errors": []},
                "usage": {"calls": [], "attempts": 0, "usage_complete": True}}

    monkeypatch.setattr(module, "execute", execute)
    measured = module.measure(args, {"arm": "jev", "case": "click", "repetition": 1}, suite,
                              {"jev": "synthetic-access-token"})
    assert measured["phase"] == "finished"
    assert measured["functional_check_passed"] is True
    assert measured["native_success"] is True
    assert measured["confirmed"] is False  # No activation event occurred in the independent fixture.
    assert json.loads((tmp_path / measured["record"]).read_text()) == measured


def test_oracle_failure_keeps_already_billed_usage(monkeypatch, tmp_path):
    module = command()
    suite = module.bench.load_scenarios()
    args = SimpleNamespace(output=tmp_path, chromium=Path("/synthetic/chromium"))
    result = row(calls=[{"input_tokens": 41, "output_tokens": 3, "status": 200}])["result"]

    class Fixture:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def register(self, _case):
            return "id", "http://127.0.0.1:9/"

        def verify(self, _key):
            raise RuntimeError("private oracle diagnostic")

    monkeypatch.setattr(module.bench, "FixtureSite", Fixture)
    monkeypatch.setattr(module, "execute", lambda *_: result)
    measured = module.measure(args, {"arm": "jev", "case": "click", "repetition": 1}, suite,
                              {"jev": "synthetic-access-token"})
    assert measured["confirmed"] is False
    assert measured["harness_error"] == "RuntimeError"
    assert measured["result"] is result
    assert module.token_totals([measured])["input_tokens"]["total"] == 41
    assert "private oracle diagnostic" not in json.dumps(measured)


def test_child_receives_only_selected_secret(monkeypatch):
    module = command()
    for name in ("OPENAI_API_KEY", "TYPESAFE_API_KEY", "OPENCODE_API_KEY", "DECISION_BROWSER_API_KEY"):
        monkeypatch.setenv(name, "unrelated-inherited-token")
    env = module.child_environment(SimpleNamespace(browsers="/synthetic/browsers"), "jev", "selected-token")
    assert env["TYPESAFE_API_KEY"] == "selected-token"
    assert not {"OPENAI_API_KEY", "OPENCODE_API_KEY", "DECISION_BROWSER_API_KEY"} & env.keys()


def test_console_routes_use_their_own_key_and_are_opt_in(monkeypatch):
    module = command()
    monkeypatch.setenv("OPENAI_API_KEY", "decision-key")
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    services = []

    def lookup(service):
        services.append(service)
        return "console-key"

    monkeypatch.setattr(module, "keychain_key", lookup)
    console = ["opencode_jev", "opencode_gemini"]
    args = SimpleNamespace(arms=["decision_api", *console], browsers="/synthetic/browsers")
    keys = module.credentials(args)
    assert keys == {"decision_api": "decision-key", "opencode_jev": "console-key", "opencode_gemini": "console-key"}
    assert services == ["codex-opencode-console"]
    assert set(console).isdisjoint(module.DEFAULT_ARMS)
    for arm in console:
        env = module.child_environment(args, arm, keys[arm])
        assert env["OPENCODE_API_KEY"] == "console-key"
        assert "OPENAI_API_KEY" not in env


def test_secret_scan_rejects_unsafe_worker_result():
    module = command()
    with pytest.raises(RuntimeError, match="unsafe_worker_output"):
        module.safe({"message": "token-do-not-retain"}, {"jev": "token-do-not-retain"})


def test_execute_uses_environment_only_and_redacts_stderr(monkeypatch):
    module = command()
    key = "synthetic-credential-for-subprocess"

    class Child:
        returncode = 0

        def communicate(self, data, timeout):
            assert json.loads(data)["arm"] == "jev" and timeout == 120
            return json.dumps({"status": "failed", "usage": {"attempts": 0, "calls": []}}), key + " trace"

    def popen(argv, **kwargs):
        assert key not in json.dumps(argv)
        assert kwargs["env"]["TYPESAFE_API_KEY"] == key
        assert kwargs["start_new_session"] is True
        return Child()

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    result = module.execute(SimpleNamespace(python=Path("/synthetic/python"), browsers="/synthetic/browsers"),
                            {"arm": "jev", "timeout_s": 75}, key, {"jev": key})
    assert result["stderr"] == "[redacted] trace"
    assert result["returncode"] == 0 and key not in json.dumps(result)


def test_malformed_worker_output_marks_usage_unknown(monkeypatch):
    module = command()

    class Child:
        returncode = 1

        def communicate(self, *_args, **_kwargs):
            return "not JSON", ""

    monkeypatch.setattr(module.subprocess, "Popen", lambda *_a, **_k: Child())
    result = module.execute(SimpleNamespace(python=Path("/synthetic/python"), browsers="/synthetic/browsers"),
                            {"arm": "jev", "timeout_s": 75}, "synthetic-access-token",
                            {"jev": "synthetic-access-token"})
    assert result["status"] == "harness_error"
    assert result["usage"]["attempts"] is None and result["usage"]["usage_complete"] is False


def test_timeout_interrupts_only_the_owned_child_group(monkeypatch):
    module = command()
    signals = []

    class Child:
        pid = 424242
        returncode = -2
        calls = 0

        def communicate(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("synthetic-worker", 120)
            return json.dumps({"status": "failed", "usage": {"attempts": 1, "calls": [],
                                                               "usage_complete": False}}), ""

    monkeypatch.setattr(module.subprocess, "Popen", lambda *_a, **_k: Child())
    monkeypatch.setattr(module.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    result = module.execute(SimpleNamespace(python=Path("/synthetic/python"), browsers="/synthetic/browsers"),
                            {"arm": "jev", "timeout_s": 75}, "synthetic-access-token",
                            {"jev": "synthetic-access-token"})
    assert signals == [(424242, module.signal.SIGINT)]
    assert result["status"] == "harness_timeout" and result["usage_incomplete"] is True


def test_jev_env_parser_reads_only_the_named_value_without_execution(monkeypatch, tmp_path):
    module = command()
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    source = tmp_path / "connection.env"
    source.write_text("OTHER_API_KEY=unrelated\nexport TYPESAFE_API_KEY='literal-token' # comment\n")
    assert module.read_jev_key(source) == "literal-token"


def test_finished_cells_never_shrink_planned_counts_or_summary_domains(monkeypatch, tmp_path):
    module = command()
    chromium = tmp_path / "chromium"
    chromium.write_bytes(b"synthetic browser identity")
    args = SimpleNamespace(output=tmp_path / "capture", cases=["click", "counter"],
                           arms=list(module.DEFAULT_ARMS), repetitions=1, seed=20261117, jobs=1,
                           chromium=chromium, browsers=tmp_path, python=Path("/synthetic/python"))
    snapshots, summaries = [], []
    original_save = module.bench.save

    def save(path, value):
        if path.name == "results.json":
            snapshots.append(copy.deepcopy(value))
        elif path.name == "summary.json":
            summaries.append(copy.deepcopy(value))
        original_save(path, value)

    def measure(_args, cell, _suite, _keys):
        return {**row(attempts=0), **cell,
                "record": f"runs/{cell['arm']}/{cell['case']}/{cell['repetition']}.json"}

    monkeypatch.setattr(module, "credentials", lambda *_: {})
    monkeypatch.setattr(module, "source_identity", lambda: {"sha256": "synthetic-source", "files": []})
    monkeypatch.setattr(module.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout="Synthetic Chromium"))
    monkeypatch.setattr(module, "measure", measure)
    monkeypatch.setattr(module.bench, "save", save)
    assert module.run(args, SimpleNamespace()) == 0
    assert len(snapshots) == 8  # Initial, each of six cells, final.
    for snapshot in snapshots:
        assert snapshot["planned_runs"] == 6
        assert len(snapshot["runs"]) + len(snapshot["remaining"]) == 6
    for summary in summaries:
        assert summary["planned_runs"] == 6
        assert set(summary["arms"]) == set(module.DEFAULT_ARMS)
        assert set(summary["cases"]) == {"click", "counter"}
    assert summaries[-1]["completed_runs"] == 6
    assert snapshots[-1]["remaining"] == []
