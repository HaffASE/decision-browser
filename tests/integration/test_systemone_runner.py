"""Real loopback HTTP and Chromium with a scripted System One service.

These tests exercise the configured production factory and the independent
page-side oracle. They make no live Jev, OpenRouter or Nimble model claims.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_comparison_regressions import _CASES, _FinalURLDriver, _site
from test_runner import finish

from decision_browser.contracts import Check, Limits, PrivacyPolicy, TaskRequest
from decision_browser.runner import RunManager
from decision_browser.server import production_factory


def _choice(description):
    option = json.loads(description)
    return option['choice'] if isinstance(option, dict) and 'choice' in option else option


def _answer(criteria, desired):
    matches = [key for key, description in criteria.items()
               if _choice(description) == desired]
    assert len(matches) == 1, (criteria, desired)
    selected = matches[0]
    return {"type": "choice", "choice": selected, "confidence": 1,
            "probabilities": {key: int(key == selected) for key in criteria}}


@contextmanager
def _systemone(actions):
    pending = list(actions)
    telemetry = {"decisions": 0, "chat": 0, "authorization": [], "errors": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            try:
                telemetry["authorization"].append(self.headers.get("Authorization"))
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/v1/chat/completions":
                    assert body["model"] == "fixture-text"
                    assert body["stream"] is False
                    assert len(body["messages"]) == 1
                    telemetry["chat"] += 1
                    response = {"choices": [{"index": 0, "finish_reason": "stop", "message": {
                        "role": "assistant", "content": "orbital mechanics",
                    }}]}
                else:
                    assert self.path == "/v1/systemone" and body["model"] == "fixture-jev"
                    telemetry["decisions"] += 1
                    state = json.loads(body["state"])
                    assert state["context_format"] == "decision-browser-choice-table-v3"
                    operation, name, value = pending.pop(0)
                    candidates = state["candidates"][operation]
                    targets = [key for key, row in candidates.items()
                               if row["name"] == name
                               and (value is None or row["option"]["value"] == value)]
                    assert len(targets) == 1, (operation, name, candidates)
                    answers = {}
                    for head, question in body["questions"].items():
                        criteria = question["criteria"]
                        if head == "operation":
                            desired = operation
                        elif head == "target_" + operation:
                            desired = targets[0]
                            assert any(
                                _choice(entry) == desired and name in json.loads(entry)["description"]
                                for entry in criteria.values()
                            )
                        else:
                            desired = _choice(next(iter(criteria.values())))
                        answers[head] = _answer(criteria, desired)
                    response = {"model": "fixture-jev-dated-alias", "answers": answers, "usage": {}}
                raw = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (AssertionError, KeyError, TypeError, ValueError, OSError) as exc:
                telemetry["errors"].append(type(exc).__name__)
                self.send_error(500)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", telemetry, pending
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def provider_environment(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("DECISION_BROWSER_"):
            monkeypatch.delenv(name)
    for name in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.setenv(name, "cloud-key-must-not-reach-local-fixture")


async def _run(case, url, *, supplied=True):
    driver = _FinalURLDriver("isolated")
    manager = RunManager(production_factory, lambda mode: driver, admission_mode="task_scope")
    request = TaskRequest(
        goal=f"Complete {case} and verify {_CASES[case][1]}.", start_url=url, mode="isolated",
        values={"query": "orbital mechanics"} if supplied and case == "search" else {},
        checks=[Check(kind="text", target="#status", expected=_CASES[case][1])],
        limits=Limits(max_seconds=20), privacy=PrivacyPolicy(), visual=False,
    )
    try:
        result = await finish(manager, manager.start_task(request))
        return result, driver
    finally:
        await manager.aclose()


@pytest.mark.parametrize("case", _CASES)
async def test_configured_local_systemone_verifies_owned_browser_cases(monkeypatch, case):
    with _site(case) as (url, oracle), _systemone(_CASES[case][2]) as (endpoint, telemetry, pending):
        monkeypatch.setenv("DECISION_BROWSER_PROVIDER", "local")
        monkeypatch.setenv("DECISION_BROWSER_ENDPOINT", endpoint + "/v1/systemone")
        monkeypatch.setenv("DECISION_BROWSER_MODEL", "fixture-jev")
        result, driver = await _run(case, url)
        assert (result.terminal, result.functional) == ("verified", "verified"), result
        assert result.development_only is False
        assert result.admission_class == "ordinary_task_scoped"
        assert result.usage.calls == telemetry["decisions"] == len(_CASES[case][2])
        assert telemetry["chat"] == 0 and not pending and not telemetry["errors"]
        assert telemetry["authorization"] == [None] * result.usage.calls
        expected = {"click": [{"name": "activate"}],
                    "search": [{"name": "search", "value": "orbital mechanics"}],
                    "select": [{"name": "sort", "value": "newest"}],
                    "navigate": [], "counter": [{"name": "increment"}] * 3}
        assert oracle["events"] == expected[case]
        assert oracle["details"] == int(case == "navigate")
        expected_url = url + "details" if case == "navigate" else url
        assert driver.final_url == expected_url


@pytest.mark.parametrize("configured", [False, True])
async def test_local_unbound_search_requires_independent_text_helper(monkeypatch, configured):
    with _site("search") as (url, oracle), _systemone(_CASES["search"][2]) as (endpoint, telemetry, _pending):
        monkeypatch.setenv("DECISION_BROWSER_PROVIDER", "local")
        monkeypatch.setenv("DECISION_BROWSER_ENDPOINT", endpoint + "/v1/systemone")
        monkeypatch.setenv("DECISION_BROWSER_MODEL", "fixture-jev")
        if configured:
            monkeypatch.setenv("DECISION_BROWSER_TEXT_ENDPOINT", endpoint + "/v1/chat/completions")
            monkeypatch.setenv("DECISION_BROWSER_TEXT_MODEL", "fixture-text")
        result, _driver = await _run("search", url, supplied=False)
        if configured:
            assert (result.terminal, result.functional) == ("verified", "verified"), result
            assert telemetry["chat"] == 1 and result.usage.calls == 3
            assert oracle["events"] == [{"name": "search", "value": "orbital mechanics"}]
        else:
            assert result.terminal == "failed" and result.functional != "verified"
            assert telemetry["chat"] == 0 and result.usage.calls == 1
            assert oracle["events"] == []
        assert not telemetry["errors"]
        assert telemetry["authorization"] == [None] * result.usage.calls
