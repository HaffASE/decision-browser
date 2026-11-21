"""Run one long scenario through the production Core provider and agent loop.

Use with long_run.py --engine decision-browser --worker scripts/benchmarks/long_core.py.
Each invocation receives one request on stdin and one route credential from the
parent. Only the native browser driver and HTTP instrumentation are substituted.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
from dbc_runner import ObservedDriver, native_checks
from long_protocol import charge

from decision_browser import provider, systemone_provider
from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.provider_http import ProviderError, post_json
from decision_browser.runner import RunManager

ROOT = Path(__file__).resolve().parents[2]
ROUTES = {
    "openai": ("OPENAI_API_KEY", "gpt-6-luna", {
        "https://api.openai.com/v1/decisions", "https://api.openai.com/v1/responses",
    }),
    "opencode": ("OPENCODE_API_KEY", "jev-1.13", {
        "https://opencode.ai/inference/systemone/v1/systemone",
    }),
}
SYSTEMONE_ENDPOINT = "https://opencode.ai/inference/systemone/v1/systemone"


class TraceActions:
    async def decide(self, *args, **kwargs):
        answer = await super().decide(*args, **kwargs)
        self.actions.append(answer.operation)
        return answer


class OpenAI(TraceActions, provider.OpenAIProvider):
    pass


class OpenCode(TraceActions, systemone_provider.SystemOneProvider):
    pass


class Meter:
    def __init__(self, route, key, request):
        self.route = route
        self.key = key
        self.limits = request
        self.rows = []
        self.input_bytes = 0
        self.spend = Decimal(0)
        self.policy_error = None

    async def request(self, request):
        _, model, endpoints = ROUTES[self.route]
        if (str(request.url) not in endpoints or request.method != "POST"
                or request.headers.get("Authorization") != "Bearer " + self.key
                or json.loads(request.content).get("model") != model):
            self.policy_error = "unexpected_route"
            raise ProviderError(self.policy_error)
        size = len(request.content)
        if (len(self.rows) >= self.limits["max_calls"]
                or self.input_bytes + size > self.limits["max_input_bytes"]
                or self.spend >= Decimal(str(self.limits["max_cost_usd"]))):
            raise ProviderError("benchmark_budget_exhausted")
        self.input_bytes += size
        row = {"endpoint": request.url.path.rsplit("/", 1)[-1], "input_bytes": size,
               "request_model": model, "state": "pending", "usage_reported": False}
        self.rows.append(row)
        request.extensions["benchmark_row"] = row

    async def response(self, response):
        row = response.request.extensions["benchmark_row"]
        row.update(status=response.status_code, state="response")

    async def post(self, *args, **kwargs):
        start = time.monotonic()
        before = len(self.rows)
        try:
            result = await post_json(*args, **kwargs)
            row = self.rows[before]
            usage = result.get("usage")
            expected = ROUTES[self.route][1]
            if result.get("model") != expected:
                self.policy_error = "unexpected_response_model"
                raise ProviderError(self.policy_error)
            row["model"] = expected
            if isinstance(usage, dict):
                row["usage_reported"] = True
                details = usage.get("input_tokens_details")
                details = details if isinstance(details, dict) else {}
                for name in ("input_tokens", "output_tokens", "total_tokens",
                             "cached_tokens", "cache_write_tokens"):
                    value = usage.get(name, details.get(name))
                    if type(value) is int and value >= 0:
                        row[name] = value
            cost = charge(row, expected)
            if cost is None:
                self.policy_error = "unknown_response_cost"
                raise ProviderError(self.policy_error)
            self.spend += cost
            return result
        finally:
            if len(self.rows) > before:
                self.rows[before]["duration_ms"] = round((time.monotonic() - start) * 1000)


async def run(data, route, key):
    meter = Meter(route, key, data)
    captured = {}
    driver = ObservedDriver(data, captured)
    limits = Limits(max_steps=data["max_steps"], max_provider_calls=data["max_calls"],
                    max_seconds=data["timeout_s"], max_input_bytes=data["max_input_bytes"])
    task = TaskRequest(start_url=data["start_url"], goal=data["goal"], values=data["values"],
                       checks=native_checks(data["checks"]), mode="isolated", privacy=PrivacyPolicy(),
                       visual=False, limits=limits)
    async with httpx.AsyncClient(trust_env=False, timeout=None, follow_redirects=False,
                                 event_hooks={"request": [meter.request], "response": [meter.response]}) as client:
        if route == "openai":
            native = OpenAI(key, client=client)
            module = provider
        else:
            native = OpenCode(endpoint=SYSTEMONE_ENDPOINT, model=ROUTES[route][1], api_key=key,
                              client=client)
            module = systemone_provider
        native.actions = []
        manager = RunManager(lambda: native, lambda mode: driver, admission_mode="task_scope")
        # Keep the production byte limits, streaming and parsing. Observe usage
        # only after the native provider has performed its bounded read.
        with patch.object(module, "post_json", meter.post):
            try:
                run_id = manager.start_task(task)
                await asyncio.wait_for(manager._run.task, data["timeout_s"] + 15)
                result = manager.get_task(run_id)
                answer = {"status": result.terminal, "reason": result.reason,
                          "functional": result.functional, "steps": result.usage.steps,
                          "actions": native.actions, "native_evidence": result.evidence}
            finally:
                await manager.aclose()
    if meter.policy_error:
        answer.update(status="runner_error", reason=meter.policy_error)
    return {**answer, **captured, "route": route, "model": ROUTES[route][1],
            "usage": {"calls": meter.rows, "attempts": len(meter.rows),
                      "request_bytes": meter.input_bytes}}


def main():
    data = json.load(sys.stdin)
    route = data["route"]
    env_name, _, _ = ROUTES[route]
    key = os.environ.pop(env_name, "")
    for name in ("OPENAI_API_KEY", "OPENCODE_API_KEY", "TYPESAFE_API_KEY", "DECISION_BROWSER_API_KEY"):
        os.environ.pop(name, None)
    if (not key or not key.isascii() or any(c.isspace() for c in key)
            or Path(data["source_root"]).resolve() != ROOT):
        raise ValueError("invalid_worker_environment")
    return asyncio.run(run(data, route, key))


if __name__ == "__main__":
    try:
        answer = main()
    except Exception as exc:  # noqa: BLE001 - the worker boundary emits only the exception type.
        answer = {"status": "runner_error", "reason": type(exc).__name__, "checks": []}
    print(json.dumps(answer), flush=True)
