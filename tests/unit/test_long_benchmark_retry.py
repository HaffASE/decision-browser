"""Transient provider failures must reuse the same task on a clean fixture."""
import json
from pathlib import Path
from types import SimpleNamespace


def test_503_is_retried_at_same_url_and_only_the_new_result_is_scored(tmp_path, monkeypatch):
    directory = Path(__file__).resolve().parents[2] / "scripts/benchmarks"
    monkeypatch.syspath_prepend(str(directory))
    import long_run

    events = []

    class Site:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def start(self, case):
            return f"http://127.0.0.1:8000/{case}/"

        def reset(self, case):
            events.append(("reset", case))

        def verify(self, case):
            return {"success": True, "observed": [], "expected": []}

    def run_child(args, request, keys):
        events.append(("run", request["start_url"]))
        status = 503 if len(events) == 1 else 200
        call = {"status": status, "endpoint": "decisions", "input_bytes": 100, "usage_reported": False}
        return {"status": "verified", "functional": "verified", "reason": "verified",
                "checks": [True, True], "returncode": 0,
                "cleanup": {"driver_state": "closed", "errors": []},
                "usage": {"calls": [call], "usage_complete": status == 200}}, 100

    monkeypatch.setattr(long_run.suite, "ScenarioSite", Site)
    monkeypatch.setattr(long_run, "run_child", run_child)
    for folder in ("primary", "scored", "retry503", "locks"):
        (tmp_path / folder).mkdir()
    args = SimpleNamespace(engine="decision-browser", route="openai", source=directory,
                           worker=Path(__file__), chromium=Path("chrome"))
    long_run.execute(args, tmp_path, "rail_itinerary", 1, {})
    url = "http://127.0.0.1:8000/rail_itinerary/"
    assert events == [("run", url), ("reset", "rail_itinerary"), ("run", url)]
    name = "1-rail_itinerary-decision-browser-openai.json"
    primary, retry, scored = (json.loads((tmp_path / folder / name).read_text())
                              for folder in ("primary", "retry503", "scored"))
    assert primary["is_infra"] and primary["http"][0]["status"] == 503
    assert primary["request_sha256"] == retry["request_sha256"]
    assert primary["pricing_sha256"] == long_run.digest(Path(long_run.protocol.__file__))
    assert primary["pricing_sha256"] == retry["pricing_sha256"]
    assert primary["terra_agent_sha256"] is None
    assert scored == retry and scored["confirmed"] and scored["attempt_no"] == 2


def test_nonzero_child_preserves_page_evidence_without_confirming_lifecycle(tmp_path, monkeypatch):
    directory = Path(__file__).resolve().parents[2] / "scripts/benchmarks"
    monkeypatch.syspath_prepend(str(directory))
    import long_run

    result = {
        "status": "done",
        "reason": "goal_achieved",
        "checks": [True],
        "cleanup": {"browser": "closed"},
        "usage": {"usage_complete": True},
    }
    child = SimpleNamespace(
        returncode=9, communicate=lambda *_args, **_kwargs: (json.dumps(result), "")
    )
    monkeypatch.setattr(long_run.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(long_run.bench, "environment", lambda *_args: {})
    args = SimpleNamespace(
        browsers=tmp_path,
        engine="terra",
        route="responses",
        python=Path("python"),
        worker=tmp_path / "worker",
    )
    recorded, _ = long_run.run_child(args, {}, {})
    assert recorded["returncode"] == 9 and recorded["checks"] == [True]
    score = long_run.bench.outcome("terra", recorded, {"success": True})
    assert score["observed"] is True and score["native_success"] is True
    assert score["confirmed"] is False and score["lifecycle_success"] is False
