"""A parsed mock decision cannot bypass a blocked DOM guard or leak its data."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from decision_browser.dom import Action, Candidate, GuardResult, Observation
from decision_browser.experiment_budget import ExperimentLedger
from decision_browser.flights_pilot import FlightsRunManager, pilot_request
from decision_browser.flights_provider import FlightsProvider
from decision_browser.provider import ProviderError
from decision_browser.runner import RunnerError

CANARY = "TEST_ONLY_PRIVATE_VALUE_NOT_A_CREDENTIAL"


@pytest.mark.parametrize("reason,expected", [
    ("covered", "covered"), ("offscreen", "offscreen"), ("context", "context"),
    ("popup", "popup"), ("value", "value"), (CANARY, "unknown"),
    ("popup:" + CANARY, "unknown"),
])
async def test_valid_decision_then_blocked_guard_is_coded_without_dispatch(
    tmp_path, reason, expected
):
    requests = []

    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        answers = []
        for question in body["questions"]:
            values = [item["value"] for item in question["choices"]]
            selected = (
                "click" if question["name"] == "operation"
                else "control" if question["name"] == "target_click"
                else values[0]
            )
            answers.append({
                "type": "choice", "name": question["name"], "choice": selected,
                "confidence": 0.75,
                "probabilities": [{"value": value, "probability": float(value == selected)}
                                  for value in values],
            })
        return httpx.Response(200, json={
            "model": "gpt-6-luna", "answers": answers,
            "usage": {"input_tokens": 42, "output_tokens": 0, "total_tokens": 42},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with ExperimentLedger(tmp_path / "synthetic-ledger", create=True) as ledger:
            provider = FlightsProvider("test-only", ledger, client=client)
            manager = FlightsRunManager(provider, ledger)
            run = manager._open(pilot_request(30))
            manager._run = run
            semantic = {
                "origin": "https://www.google.com", "url": run.request.start_url,
                "node": {"name": CANARY},
            }
            candidate = Candidate("control", ("click",), {"click": semantic}, None)
            obs = Observation("obs", "https://www.google.com", "doc", CANARY,
                              {"control": candidate}, ())
            action = Action("click", "control", None, None, "obs",
                            json.dumps({"semantic": semantic}))
            run.dom = SimpleNamespace(
                prepare=AsyncMock(return_value=action),
                guard=AsyncMock(return_value=GuardResult("blocked", reason)),
                dispatch=AsyncMock(),
            )
            manager._set(run, evidence=[f"existing:{i}" for i in range(32)])
            with pytest.raises(RunnerError, match="^blocked$") as caught:
                await manager._choose(run, obs)
            manager._apply(run, caught.value)
            assert manager.failure_code == "blocked"
            assert run.result.terminal == "blocked"
            assert run.result.evidence[0] == "action_guard:" + expected
            assert len(run.result.evidence) == 32
            assert CANARY not in run.result.model_dump_json()
            assert len(requests) == 1 and ledger.snapshot["calls"] == 1
            assert ledger.snapshot["steps"] == 0
            assert manager.trace == []
            run.dom.dispatch.assert_not_called()


@pytest.mark.parametrize("error,terminal,code", [
    (ProviderError("invalid_answer"), "failed", "unclassified_failure"),
    (ProviderError("invalid_model"), "failed", "unclassified_failure"),
    (ProviderError(CANARY), "failed", "unclassified_failure"),
    (ProviderError("rejected_sentinel"), "failed", "unclassified_failure"),
    (ProviderError("refusal"), "refused", "unclassified_failure"),
    (RunnerError("navigation"), "failed", "navigation"),
    (RunnerError("blocked"), "blocked", "blocked"),
    (RunnerError(CANARY), "failed", "unclassified_failure"),
])
def test_parser_and_navigation_failures_are_not_the_observed_guard_block(error, terminal, code):
    manager = FlightsRunManager(SimpleNamespace(), SimpleNamespace())
    run = manager._open(pilot_request(30))
    manager._apply(run, error)
    assert run.result.terminal == terminal
    assert manager.failure_code == code
    if isinstance(error, ProviderError) and error.code in {"invalid_answer", "invalid_model", "rejected_sentinel"}:
        assert run.result.evidence == ["provider:" + error.code]
    elif isinstance(error, RunnerError) and error.code == "navigation":
        assert run.result.evidence == ["runner:navigation"]
    else:
        assert run.result.evidence == []
    assert CANARY not in run.result.model_dump_json()
