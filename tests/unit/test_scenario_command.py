"""Report independent outcomes truthfully without implicit model calls."""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def command():
    path = Path(__file__).resolve().parents[2] / "scripts" / "run_browser_scenarios.py"
    spec = importlib.util.spec_from_file_location("scenario_command_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def arguments():
    return argparse.Namespace(provider="scripted", max_steps=24, max_seconds=45.0,
                              max_calls=24, max_input_bytes=262144,
                              cases=["synthetic"], repetitions=1, output=Path("report.json"))


@pytest.mark.parametrize("terminal,functional,oracle,code", [
    ("verified", "verified", True, 0),
    ("verified", "verified", False, 1),
    ("verified", "failed", True, 1),
    ("verification_failed", "verified", True, 1),
])
async def test_report_requires_terminal_checks_and_independent_oracle(
    terminal, functional, oracle, code,
):
    module = command()

    async def scenario(case_id, *, provider_factory, limits):
        assert case_id == "synthetic" and provider_factory is None
        return {"terminal": terminal, "functional": functional,
                "oracle": {"success": oracle}, "success": True}

    stream = io.StringIO()
    result = await module.run(arguments(), SimpleNamespace(run_scenario=scenario), stream)
    report = json.loads(stream.getvalue())
    assert result == code
    assert report["status"] == ("PASS" if code == 0 else "FAIL")
    assert report["passed"] == int(code == 0) and report["total"] == 1
    assert report["runs"][0]["success"] is (code == 0)
    assert report["provider_mode"] == "scripted"


async def test_harness_error_never_logs_exception_text_or_claims_pass():
    module = command()
    secret = "synthetic-private-provider-response"

    async def scenario(*_args, **_kwargs):
        raise RuntimeError(secret)

    stream = io.StringIO()
    assert await module.run(arguments(), SimpleNamespace(run_scenario=scenario), stream) == 1
    report = json.loads(stream.getvalue())
    assert secret not in stream.getvalue()
    assert report["runs"][0]["error_type"] == "RuntimeError"
    assert report["runs"][0]["terminal"] == "harness_error"
    assert report["passed"] == 0


def test_listing_never_runs_a_provider_or_creates_output(monkeypatch, tmp_path, capsys):
    module = command()
    output = tmp_path / "absent.json"
    monkeypatch.setattr(module, "load_scenarios", lambda: SimpleNamespace(
        CASES={"synthetic": SimpleNamespace(goal="Inspect the owned fixture.")},
    ))
    monkeypatch.setattr("sys.argv", ["scenarios", "--list", "--provider", "configured",
                                     "--output", str(output)])
    assert module.main() == 0
    assert json.loads(capsys.readouterr().out) == [
        {"id": "synthetic", "goal": "Inspect the owned fixture."},
    ]
    assert not output.exists()


def test_existing_report_is_preserved_without_running_a_case(monkeypatch, tmp_path):
    module = command()
    output = tmp_path / "previous.json"
    output.write_text("previous evidence\n")
    monkeypatch.setattr(module, "load_scenarios", lambda: SimpleNamespace(CASES={"synthetic": None}))
    monkeypatch.setattr("sys.argv", ["scenarios", "--output", str(output)])
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 2
    assert output.read_text() == "previous evidence\n"


def test_configured_metadata_identifies_route_without_including_any_keys(monkeypatch):
    from decision_browser.provider_config import ProviderSettings, TextSettings

    module = command()
    settings = ProviderSettings(
        "local", "http://127.0.0.1:11434/v1/systemone", "nimble:latest", "decision-key", 26,
        TextSettings("http://127.0.0.1:8001/helper-key/chat", "helper-model", "helper-key"),
    )
    monkeypatch.setattr("decision_browser.provider_config.load_provider_settings", lambda: settings)
    metadata = module.provider_metadata("configured")
    assert metadata["provider"] == "local"
    assert metadata["model"] == "nimble:latest"
    assert metadata["endpoint"] == "http://127.0.0.1:11434/v1/systemone"
    assert "decision-key" not in json.dumps(metadata)
    assert "helper-key" not in json.dumps(metadata)
    assert metadata["text_helper"]["endpoint"] == "http://127.0.0.1:8001/[redacted]/chat"


def test_scripted_metadata_does_not_resolve_provider_credentials(monkeypatch):
    module = command()

    def forbidden():
        raise AssertionError("scripted mode must not resolve model credentials")

    monkeypatch.setattr("decision_browser.provider_config.load_provider_settings", forbidden)
    assert module.provider_metadata("scripted") == {"provider": "scripted", "model": None, "endpoint": None}


@pytest.mark.parametrize("limit", ["nan", "inf", "0", "601"])
def test_invalid_time_limit_does_not_create_evidence(monkeypatch, tmp_path, limit):
    module = command()
    output = tmp_path / "absent.json"
    monkeypatch.setattr(module, "load_scenarios", lambda: SimpleNamespace(CASES={"synthetic": None}))
    monkeypatch.setattr("sys.argv", ["scenarios", "--max-seconds", limit, "--output", str(output)])
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 2
    assert not output.exists()
