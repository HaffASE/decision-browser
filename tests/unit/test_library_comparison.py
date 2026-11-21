"""Offline contracts for fair scoring, physical metering and bounded comparison."""

from __future__ import annotations

import importlib.util
import json
import threading
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def command():
    return load_module("library_comparison_under_test", ROOT / "scripts" / "compare_browser_libraries.py")


@pytest.fixture
def common():
    return load_module("comparison_meter_under_test", ROOT / "scripts" / "benchmarks" / "common.py")


def numeric_usage(total=100, cached=10, written=20, output=4):
    return {"input_tokens": total, "output_tokens": output,
            "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": written}}


def cost_row(endpoint="decisions", total=100, cached=10, written=20, output=4):
    return {"endpoint": endpoint, "input_tokens": total, "cached_tokens": cached,
            "cache_write_tokens": written, "output_tokens": output}


def install_sync(common, ledger, monkeypatch):
    # Register the original method before the helper replaces it globally.
    monkeypatch.setattr(httpx.Client, "send", httpx.Client.send)
    common.install_http_meter(ledger)


def install_async(common, ledger, monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "send", httpx.AsyncClient.send)
    common.install_async_http_meter(ledger)


def test_current_sixteen_cases_are_listed_without_model_or_browser(command, monkeypatch, capsys):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Listing must not inspect a credential or execute a run")

    monkeypatch.setattr(command, "preflight", forbidden)
    monkeypatch.setattr(command, "get_key", forbidden)
    monkeypatch.setattr(command, "execute", forbidden)
    monkeypatch.setattr("sys.argv", ["comparison", "--list"])
    assert command.main() == 0
    assert capsys.readouterr().out.splitlines() == [
        "click", "search", "select", "navigate", "counter", "public_about",
        "hotel", "profile_form", "autocomplete", "calendar", "styled_filter", "consent",
        "delayed_menu", "loading_overlay", "below_fold", "article",
    ]


def test_goal_protocol_is_identical_and_contains_only_original_task_facts(command):
    goal = "Complete the task."
    values = {"Query": "Gödel", "Name": "Ada"}
    assert command.canonical_goal(goal, values) == (
        'Complete the task.\n\nProvided field values: {"Name":"Ada","Query":"Gödel"}'
    )
    assert command.canonical_goal(goal, dict(reversed(list(values.items())))) == (
        command.canonical_goal(goal, values)
    )
    assert command.canonical_goal(goal, {}) == goal


def test_request_does_not_expose_fixture_plan_or_completion_oracle_in_goal(command):
    hidden_plan = "secret fixture action sequence"
    expected = "secret observed status"
    spec = SimpleNamespace(goal="Enter a value.", values={"Query": "public user value"},
                           plan=hidden_plan, expected_status=expected,
                           expected_events=({"secret": "server oracle"},),
                           checks=lambda url: [SimpleNamespace(kind="text", target="#status", expected=expected)])
    request = command.make_request("synthetic", "http://127.0.0.1:1234/", SimpleNamespace(CASES={"synthetic": spec}))
    assert request["goal"] == command.canonical_goal(spec.goal, spec.values)
    assert hidden_plan not in request["goal"] and expected not in request["goal"]
    assert "server oracle" not in request["goal"] and "#status" not in request["goal"]
    assert request["checks"] == [{"type": "text_equals", "target": "#status", "value": expected}]


def test_real_requests_keep_exact_value_and_route_checks(command):
    suite = command.load_scenarios()
    request = command.make_request("autocomplete", "http://127.0.0.1:1234/autocomplete/", suite)
    assert {"type": "value_equals", "target": "#origin", "value": "Zurich Airport (ZRH)"} in request["checks"]
    assert {"type": "url_equals", "target": "", "value": request["start_url"]} in request["checks"]
    assert request["max_steps"] == 24 and request["max_provider_calls"] == 12
    baseline = command.make_request("navigate", "http://127.0.0.1:1234/navigate/", suite)
    assert baseline["max_steps"] == 8
    assert baseline["checks"][-1] == {"type": "url_equals", "value": baseline["start_url"] + "details"}
    public = command.make_request("public_about", command.PUBLIC_URL, suite)
    assert public["checks"] == [{"type": "url_equals", "value": command.PUBLIC_EXPECTED}]


def test_schedule_covers_every_case_repetition_library_once_in_rotated_triples(command):
    cases = [*command.BASELINE, "public_about", *command.load_scenarios().CASES]
    rows = list(command.schedule(cases, 3))
    assert len(rows) == 144 and len(set(rows)) == 144
    assert Counter((case, engine) for _, case, engine in rows) == Counter(
        {(case, engine): 3 for case in cases for engine in command.ENGINES}
    )
    assert rows == list(command.schedule(cases, 3))
    assert rows != list(command.schedule(cases, 3, seed=84))
    first_positions = set()
    for index in range(0, len(rows), 3):
        triple = rows[index:index + 3]
        assert len({(rep, case) for rep, case, _ in triple}) == 1
        assert {engine for _, _, engine in triple} == set(command.ENGINES)
        first_positions.add(triple[0][2])
    assert first_positions == set(command.ENGINES)


@pytest.mark.parametrize("engine,result", [
    ("decision-browser", {"status": "verified", "functional": "verified"}),
    ("jev-ultrafast", {"status": "done", "reason": "goal_achieved"}),
    ("jev-ra", {"status": "done", "reason": "goal_achieved"}),
])
def test_confirmed_success_requires_native_terminal_and_all_exact_checks(command, engine, result):
    result = {**complete_external_result(), **result}
    if engine == "decision-browser":
        result["cleanup"] = {"driver_state": "closed", "errors": []}
    assert command.outcome(engine, {**result, "checks": [True, True]}, {"success": True}) == {
        "confirmed": True, "observed": True, "native_success": True, "functional_check_passed": True,
        "lifecycle_success": True,
    }
    assert command.outcome(engine, {**result, "checks": [True, False]}, {"success": True})["confirmed"] is False
    assert command.outcome(engine, {**result, "checks": [True]}, {"success": False})["confirmed"] is False


@pytest.mark.parametrize("checks", [None, [], [1], ["true"], [True, None], "true"])
def test_missing_empty_or_nonboolean_checks_never_claim_observed_success(command, checks):
    result = {"status": "done", "reason": "goal_achieved", "checks": checks}
    score = command.outcome("jev-ra", result, {"success": True})
    assert score["native_success"] is True
    assert score["observed"] is False and score["confirmed"] is False


@pytest.mark.parametrize("success", [None, 1, "true", False])
def test_server_oracle_must_report_boolean_true(command, success):
    score = command.outcome("jev-ra", {"status": "done", "reason": "goal_achieved", "checks": [True]},
                            {"success": success})
    assert score["observed"] is False and score["confirmed"] is False


def test_page_achieved_after_native_failure_is_reported_separately(command):
    score = command.outcome("jev-ra", {"status": "blocked", "reason": "stuck_loop", "checks": [True]},
                            {"success": True})
    assert score == {"confirmed": False, "observed": True,
                     "native_success": False, "functional_check_passed": True, "lifecycle_success": False}
    core = command.outcome("decision-browser", {"status": "verified", "functional": "failed", "checks": [True]},
                           {"success": True})
    assert core["observed"] is True and core["confirmed"] is False


@pytest.mark.parametrize("checks,observed", [([True], False), ([True, True, True], True),
                                           ([True, True, True, True], False)])
def test_omitted_or_extra_check_results_cannot_pass_the_expected_contract(command, checks, observed):
    score = command.outcome("jev-ra", {**complete_external_result(), "checks": checks},
                            {"success": True}, expected_checks=3)
    assert score["native_success"] is True
    assert score["functional_check_passed"] is observed
    assert score["observed"] is observed and score["confirmed"] is observed


def test_cost_separates_uncached_read_write_and_helper_output(common):
    decisions = common.estimate(cost_row())
    assert decisions == pytest.approx((.10 * 70 + .01 * 10 + .125 * 20) / 1_000_000)
    assert common.estimate(cost_row(endpoint="responses")) == pytest.approx(decisions + .50 * 4 / 1_000_000)
    assert common.estimate(cost_row(output=10_000)) == decisions


@pytest.mark.parametrize("total,multiplier,output_multiplier", [(272_000, 1, 1), (272_001, 2, 1.5)])
def test_long_context_rate_boundary_is_per_request(common, total, multiplier, output_multiplier):
    row = cost_row(endpoint="responses", total=total)
    expected = (multiplier * (.10 * (total - 30) + .01 * 10 + .125 * 20)
                + .50 * output_multiplier * 4) / 1_000_000
    assert common.estimate(row) == pytest.approx(expected)


@pytest.mark.parametrize("change", [
    {"input_tokens": None}, {"input_tokens": True}, {"input_tokens": -1},
    {"cached_tokens": 81}, {"cache_write_tokens": 91}, {"output_tokens": "4"},
    {"endpoint": "unknown"},
])
def test_missing_or_inconsistent_usage_is_unknown_never_free(common, change):
    assert common.estimate({**cost_row(), **change}) is None


def test_concurrent_physical_call_and_byte_limits_are_atomic(common):
    ledger = common.RequestLedger(4, 3)
    barrier = threading.Barrier(12)

    def attempt():
        barrier.wait()
        ledger.attempt("decisions", 1)

    threads = [threading.Thread(target=attempt) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    snapshot = ledger.snapshot()
    assert snapshot["attempts"] == 3 and snapshot["input_bytes"] == 3
    assert snapshot["blocked_by_bytes"] == 9
    assert snapshot["in_flight"] == 3 and snapshot["usage_complete"] is False
    capped = common.RequestLedger(1, 10)
    assert capped.attempt("responses", 1) is not None
    assert capped.attempt("decisions", 1) is None
    assert capped.snapshot()["blocked_by_budget"] == 1


def test_cost_threshold_blocks_next_attempt_after_known_spend(common):
    ledger = common.RequestLedger(12, max_cost_usd=.000001)
    row = ledger.attempt("decisions", 10)
    ledger.complete(row, httpx.Response(200, json={"usage": numeric_usage()}), .01)
    assert ledger.attempt("responses", 10) is None
    snapshot = ledger.snapshot()
    assert snapshot["attempts"] == 1 and snapshot["blocked_by_cost"] == 1
    assert snapshot["usage_complete"] is True


@pytest.mark.parametrize("endpoint", ["decisions", "responses"])
def test_sync_meter_counts_wire_bytes_usage_and_omits_sensitive_content(common, monkeypatch, endpoint):
    ledger = common.RequestLedger(2)
    install_sync(common, ledger, monkeypatch)
    body = {"model": common.MODEL, "input": "synthetic private page"}
    credential = "synthetic-private-api-key"
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"usage": numeric_usage(), "output": "synthetic private response"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert client.post(f"{common.ORIGIN}/v1/{endpoint}", json=body,
                           headers={"Authorization": "Bearer " + credential}).status_code == 200
    snapshot = ledger.snapshot()
    assert snapshot["attempts"] == 1 and snapshot["completed_2xx"] == 1
    assert snapshot[endpoint + "_attempts"] == 1
    assert snapshot["input_bytes"] == len(seen[0].content)
    assert snapshot["input_tokens"] == 100 and snapshot["usage_complete"] is True
    assert snapshot["estimated_usd"] == pytest.approx(common.estimate(cost_row(endpoint=endpoint)))
    serialized = json.dumps(snapshot)
    assert all(secret not in serialized for secret in (credential, body["input"], "synthetic private response"))


async def test_async_meter_counts_actual_requests_with_the_same_usage_contract(common, monkeypatch):
    ledger = common.RequestLedger(2)
    install_async(common, ledger, monkeypatch)
    seen = []

    async def handle(request):
        seen.append(request)
        assert ledger.snapshot()["in_flight"] == 1
        return httpx.Response(200, json={"usage": numeric_usage()})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        for endpoint in ("decisions", "responses"):
            assert (await client.post(f"{common.ORIGIN}/v1/{endpoint}", json={"model": common.MODEL})).status_code == 200
    snapshot = ledger.snapshot()
    assert snapshot["attempts"] == 2 and snapshot["decisions_attempts"] == 1
    assert snapshot["responses_attempts"] == 1 and snapshot["usage_complete"] is True
    assert snapshot["input_bytes"] == sum(len(request.content) for request in seen)


async def test_native_streamed_response_prices_usage_after_bounded_read(common, monkeypatch):
    from decision_browser.budget import Budget
    from decision_browser.contracts import Limits, PrivacyPolicy
    from decision_browser.privacy import PrivacyGate

    monkeypatch.syspath_prepend(str(ROOT / "scripts" / "benchmarks"))
    runner = load_module("comparison_dbc_stream_under_test", ROOT / "scripts" / "benchmarks" / "dbc_runner.py")
    ledger = common.RequestLedger(2)
    install_async(common, ledger, monkeypatch)
    events = []
    streams = []
    raw = json.dumps({"usage": numeric_usage(), "private": "synthetic private response"}).encode()

    class LazyResponse(httpx.AsyncByteStream):
        read = False
        closed = False

        async def __aiter__(self):
            # The send interceptor may inspect headers but must leave body
            # consumption to the provider's original output-budget boundary.
            assert events[-1] == "meter_before_native_read"
            self.read = True
            events.append("native_stream_read")
            yield raw[:17]
            yield raw[17:]

        async def aclose(self):
            self.closed = True

    async def handle(request):
        stream = LazyResponse()
        streams.append(stream)
        events.append("transport")
        return httpx.Response(200, stream=stream)

    original_complete = ledger.complete

    def complete(row, response, elapsed):
        if not response.is_stream_consumed:
            assert streams[-1].read is False
            events.append("meter_before_native_read")
        else:
            assert streams[-1].read is True and streams[-1].closed is True
            events.append("meter_after_native_read")
        original_complete(row, response, elapsed)

    monkeypatch.setattr(ledger, "complete", complete)
    provider = runner.Provider("synthetic-private-key", [], ledger)
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False)
    budget = Budget(Limits(max_provider_calls=2, max_output_bytes=2 * len(raw)), clock=lambda: 0.)
    gate = PrivacyGate(PrivacyPolicy())
    try:
        for expected_attempts in (1, 2):
            result = await provider._post("/v1/decisions", {"model": common.MODEL}, budget=budget, privacy=gate)
            assert result["usage"] == numeric_usage()
            snapshot = ledger.snapshot()
            assert snapshot["attempts"] == expected_attempts and snapshot["usage_complete"] is True
            assert snapshot["blocked_by_cost"] == 0
            assert snapshot["input_tokens"] == 100 * expected_attempts
    finally:
        await provider.aclose()
    assert events == ["transport", "meter_before_native_read", "native_stream_read",
                      "meter_after_native_read"] * 2
    assert all(stream.read and stream.closed for stream in streams)
    assert budget.usage().calls == 2 and budget.usage().output_bytes == 2 * len(raw)
    serialized = json.dumps(ledger.snapshot())
    assert "synthetic-private-key" not in serialized and "synthetic private response" not in serialized


async def test_async_transport_failure_is_counted_and_cannot_be_retried_unpriced(common, monkeypatch):
    ledger = common.RequestLedger(2)
    install_async(common, ledger, monkeypatch)
    calls = 0

    async def handle(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("synthetic private response timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.ReadTimeout):
            await client.post(common.ORIGIN + "/v1/decisions", json={"model": common.MODEL})
        with pytest.raises(httpx.RequestError, match="benchmark_cost_call_or_byte_budget"):
            await client.post(common.ORIGIN + "/v1/decisions", json={"model": common.MODEL})
    snapshot = ledger.snapshot()
    assert calls == 1 and snapshot["attempts"] == 1 and snapshot["transport_errors"] == 1
    assert snapshot["usage_complete"] is False and snapshot["blocked_by_cost"] == 1
    assert "synthetic private response timeout" not in json.dumps(snapshot)


def test_physical_retry_with_known_usage_counts_both_attempts_and_cost(common, monkeypatch):
    ledger = common.RequestLedger(2)
    install_sync(common, ledger, monkeypatch)
    attempts = []

    def handle(request):
        attempts.append(request)
        return httpx.Response(429 if len(attempts) == 1 else 200, json={"usage": numeric_usage()})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert client.post(common.ORIGIN + "/v1/decisions", json={"model": common.MODEL}).status_code == 429
        assert client.post(common.ORIGIN + "/v1/decisions", json={"model": common.MODEL}).status_code == 200
        with pytest.raises(httpx.RequestError, match="benchmark_cost_call_or_byte_budget"):
            client.post(common.ORIGIN + "/v1/decisions", json={"model": common.MODEL})
    snapshot = ledger.snapshot()
    assert len(attempts) == 2 and snapshot["attempts"] == 2
    assert snapshot["non_2xx"] == 1 and snapshot["completed_2xx"] == 1
    assert snapshot["usage_complete"] is True and snapshot["blocked_by_budget"] == 1
    assert snapshot["estimated_usd"] == pytest.approx(2 * common.estimate(cost_row()))


def test_snapshot_is_independent_of_later_completion(common):
    ledger = common.RequestLedger(2)
    row = ledger.attempt("decisions", 10)
    before = ledger.snapshot()
    ledger.complete(row, httpx.Response(200, json={"usage": numeric_usage()}), .01)
    assert before["in_flight"] == 1 and before["calls"][0]["state"] == "pending"
    assert before["usage_complete"] is False
    after = ledger.snapshot()
    assert after["in_flight"] == 0 and after["calls"][0]["state"] == "response"
    assert after["usage_complete"] is True


@pytest.mark.parametrize("mode", ["transport_error", "missing_usage", "malformed_usage"])
def test_failed_or_unpriced_attempt_is_preserved_and_prevents_more_calls(common, monkeypatch, mode):
    ledger = common.RequestLedger(12)
    install_sync(common, ledger, monkeypatch)
    seen = []

    def handle(request):
        seen.append(request)
        if mode == "transport_error":
            raise httpx.ConnectError("synthetic secret transport detail", request=request)
        usage = {} if mode == "missing_usage" else {"usage": {"input_tokens": 100}}
        return httpx.Response(429, json={**usage, "error": "synthetic secret response detail"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        if mode == "transport_error":
            with pytest.raises(httpx.ConnectError):
                client.post(common.ORIGIN + "/v1/decisions", json={"model": common.MODEL})
        else:
            assert client.post(common.ORIGIN + "/v1/decisions", json={"model": common.MODEL}).status_code == 429
        with pytest.raises(httpx.RequestError, match="benchmark_cost_call_or_byte_budget"):
            client.post(common.ORIGIN + "/v1/decisions", json={"model": common.MODEL})
    snapshot = ledger.snapshot()
    assert len(seen) == 1 and snapshot["attempts"] == 1
    assert snapshot["usage_complete"] is False and snapshot["usage_missing"] == 1
    assert snapshot["blocked_by_cost"] == 1 and "synthetic secret" not in json.dumps(snapshot)
    assert snapshot["transport_errors"] == int(mode == "transport_error")
    assert snapshot["non_2xx"] == int(mode != "transport_error")


@pytest.mark.parametrize("url,method,payload", [
    ("http://api.openai.com/v1/decisions", "POST", {"model": "gpt-6-luna"}),
    ("https://api.openai.com:444/v1/decisions", "POST", {"model": "gpt-6-luna"}),
    ("https://api.openai.com/v1/chat/completions", "POST", {"model": "gpt-6-luna"}),
    ("https://api.openai.com/v1/decisions", "GET", {"model": "gpt-6-luna"}),
    ("https://api.openai.com/v1/decisions", "POST", {"model": "different-model"}),
    ("https://api.openai.com/v1/decisions", "POST", {"model": "gpt-6-luna", "prompt_cache_retention": "12h"}),
])
def test_model_route_and_model_are_rejected_before_transport(common, monkeypatch, url, method, payload):
    ledger = common.RequestLedger(2)
    install_sync(common, ledger, monkeypatch)

    def forbidden(_request):
        raise AssertionError("Rejected routing must not reach transport")

    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client, pytest.raises(httpx.RequestError):
        client.request(method, url, json=payload)
    assert ledger.snapshot()["attempts"] == 0
    if payload.get("model") == common.MODEL and "prompt_cache_retention" not in payload:
        assert ledger.snapshot()["rejected_routes"] == 1


def test_loopback_harness_traffic_is_not_counted_as_model_spend(common, monkeypatch):
    ledger = common.RequestLedger(1)
    install_sync(common, ledger, monkeypatch)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True})
        )
    ) as client:
        assert client.post(
            "http://127.0.0.1:1234/command", json={"command": "observe"}
        ).json() == {"ok": True}
    assert ledger.snapshot()["attempts"] == 0


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_unknown_remote_never_reaches_unmetered_transport(
    common, monkeypatch, asynchronous
):
    ledger = common.RequestLedger(2)
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"usage": numeric_usage()})

    if asynchronous:
        install_async(common, ledger, monkeypatch)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(
                httpx.RequestError, match="benchmark_model_route_rejected"
            ):
                await client.post(
                    "https://unapproved.example/v1/responses",
                    json={"model": common.MODEL},
                )
    else:
        install_sync(common, ledger, monkeypatch)
        with (
            httpx.Client(transport=httpx.MockTransport(handle)) as client,
            pytest.raises(httpx.RequestError, match="benchmark_model_route_rejected"),
        ):
            client.post(
                "https://unapproved.example/v1/responses",
                json={"model": common.MODEL},
            )
    assert seen == []
    snapshot = ledger.snapshot()
    assert snapshot["rejected_routes"] == 1 and snapshot["usage_complete"] is False
    assert "unapproved.example" not in json.dumps(snapshot)


@pytest.mark.parametrize(
    "url,method",
    [
        ("http://127.0.0.1:1234/v1/responses", "POST"),
        ("https://localhost:1234/command", "POST"),
        ("http://127.0.0.1:1234/unknown", "GET"),
    ],
)
def test_unknown_loopback_routes_are_not_free_model_routes(
    common, monkeypatch, url, method
):
    ledger = common.RequestLedger(2)
    install_sync(common, ledger, monkeypatch)
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: pytest.fail("unexpected transport"))
        ) as client,
        pytest.raises(httpx.RequestError, match="benchmark_model_route_rejected"),
    ):
        client.request(method, url)


def complete_external_result():
    return {
        "status": "done",
        "reason": "goal_achieved",
        "checks": [True],
        "returncode": 0,
        "unfinished_speculations": 0,
        "cleanup": {
            "agent": "closed",
            "chrome": "terminated",
            "daemon": "reaped",
            "temporary_profile": "removed",
        },
        "usage": {"usage_complete": True},
    }


@pytest.mark.parametrize(
    "change",
    [
        "cleanup_error",
        "cleanup_unknown",
        "unfinished",
        "exit",
        "usage",
        "missing_cleanup",
    ],
)
def test_page_achievement_is_not_complete_lifecycle_success(command, change):
    result = complete_external_result()
    if change.startswith("cleanup_"):
        result["cleanup"]["chrome"] = (
            "cleanup_error" if change == "cleanup_error" else "unrecognized"
        )
    elif change == "unfinished":
        result["unfinished_speculations"] = 3
    elif change == "exit":
        result["returncode"] = 9
    elif change == "usage":
        result["usage"]["usage_complete"] = False
    else:
        result.pop("cleanup")
    score = command.outcome("jev-ra", result, {"success": True})
    assert score["observed"] is True and score["native_success"] is True
    assert score["confirmed"] is False and score["lifecycle_success"] is False


def test_complete_lifecycle_positive_control(command):
    score = command.outcome("jev-ra", complete_external_result(), {"success": True})
    assert score["confirmed"] is True and score["lifecycle_success"] is True


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_model_redirect_cannot_bypass_physical_route_policy(
    common, monkeypatch, asynchronous
):
    ledger = common.RequestLedger(2)
    seen = []

    def handle(request):
        seen.append(str(request.url))
        return httpx.Response(
            307, headers={"location": "https://unapproved.example/v1/responses"}
        )

    if asynchronous:
        install_async(common, ledger, monkeypatch)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), follow_redirects=True
        ) as client:
            response = await client.post(
                common.ORIGIN + "/v1/decisions", json={"model": common.MODEL}
            )
    else:
        install_sync(common, ledger, monkeypatch)
        with httpx.Client(
            transport=httpx.MockTransport(handle), follow_redirects=True
        ) as client:
            response = client.post(
                common.ORIGIN + "/v1/decisions", json={"model": common.MODEL}
            )
    assert response.status_code == 307
    assert seen == [common.ORIGIN + "/v1/decisions"]
    assert (
        ledger.snapshot()["attempts"] == 1
        and ledger.snapshot()["usage_complete"] is False
    )


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_loopback_redirect_cannot_escape_free_harness_routes(
    common, monkeypatch, asynchronous
):
    ledger = common.RequestLedger(2)
    seen = []

    def handle(request):
        seen.append(str(request.url))
        return httpx.Response(
            307, headers={"location": "https://unapproved.example/v1/responses"}
        )

    if asynchronous:
        install_async(common, ledger, monkeypatch)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), follow_redirects=True
        ) as client:
            response = await client.post(
                "http://127.0.0.1:1234/command", json={"command": "observe"}
            )
    else:
        install_sync(common, ledger, monkeypatch)
        with httpx.Client(
            transport=httpx.MockTransport(handle), follow_redirects=True
        ) as client:
            response = client.post(
                "http://127.0.0.1:1234/command", json={"command": "observe"}
            )
    assert response.status_code == 307 and seen == ["http://127.0.0.1:1234/command"]
    assert ledger.snapshot()["attempts"] == 0
