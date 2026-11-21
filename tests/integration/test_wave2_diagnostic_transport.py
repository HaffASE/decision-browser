"""Coded transport causes survive the public native runner; no external network."""

import json

import httpx
import pytest
from test_bootstrap_origin import OwnedRouteDriver
from test_runner import finish

from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.provider import OpenAIProvider
from decision_browser.runner import RunManager

CANARY = "SYNTHETIC_PRIVATE_RESPONSE_BODY_42"


class BrokenErrorStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield CANARY.encode() * 16

    async def aclose(self):
        raise RuntimeError(CANARY)


@pytest.mark.parametrize("error_response", [True, False])
async def test_public_runner_retains_status_budget_and_close_codes(error_response):
    requests = []

    def response(request):
        requests.append(request)
        if error_response:
            return httpx.Response(429, stream=BrokenErrorStream(), request=request)
        body = json.loads(request.content)
        answers = []
        for question in body["questions"]:
            choices = [row["value"] for row in question["choices"]]
            selected = "DONE" if question["name"] == "operation" else choices[0]
            answers.append({
                "name": question["name"], "type": "choice", "choice": selected,
                "confidence": 1.0, "probabilities": [{
                    "value": value,
                    "probability": float(type(value) is type(selected) and value == selected),
                } for value in choices],
            })
        return httpx.Response(200, json={"model": "gpt-6-luna", "usage": {}, "answers": answers})

    provider = OpenAIProvider("synthetic-key", transport=httpx.MockTransport(response))
    url = "https://example.test/"
    manager = RunManager(
        lambda: provider, lambda mode: OwnedRouteDriver(mode, url), admission_mode="task_scope",
    )
    run_id = manager.start_task(TaskRequest(
        goal="Review the owned page", start_url=url, values={}, checks=[],
        privacy=PrivacyPolicy(blocked_literals=[CANARY]),
        limits=Limits(max_seconds=15, max_output_bytes=10 if error_response else 16_384),
    ))
    result = await finish(manager, run_id)
    assert len(requests) == result.usage.calls == 1
    assert result.usage.steps == 1
    assert CANARY not in result.model_dump_json()
    assert len(result.evidence) <= 32
    if error_response:
        assert result.terminal == "limit_exceeded"
        assert {"budget:output_bytes", "http_status:rate_limited", "provider:response_close_failed"} <= set(result.evidence)
    else:
        assert result.terminal == "completed_unverified"
        assert not any(note.startswith(("budget:", "http_status:", "provider:")) for note in result.evidence)
