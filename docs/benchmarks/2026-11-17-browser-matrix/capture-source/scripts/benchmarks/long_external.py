"""Run one jev-ra or jev-ultrafast trial on its native policy and a metered route."""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager

import httpx
import jev_runner
import openai_transport
import ultrafast_runner
from common import optional_number
from long_protocol import charge

OPENAI_DECISIONS = "https://api.openai.com/v1/decisions"
OPENAI_TEXT = "https://api.openai.com/v1/responses"
OPENCODE_DECISIONS = "https://opencode.ai/inference/systemone/v1/systemone"


class Provider:
    """A shared, thread-safe wire budget for native decisions, speculation and text."""

    def __init__(self, data, route, openai_key, opencode_key):
        if route not in ("openai", "opencode"):
            raise ValueError("unknown_route")
        self.route = route
        self.decision_endpoint = OPENAI_DECISIONS if route == "openai" else OPENCODE_DECISIONS
        self.decision_key = openai_key if route == "openai" else opencode_key
        self.decision_model = "gpt-6-luna" if route == "openai" else "jev-1.13"
        self.text_key = openai_key
        self.max_calls = data["max_calls"]
        self.max_bytes = data["max_input_bytes"]
        self.max_cost = data.get("max_cost_usd", .25)
        self.rows = []
        self.input_bytes = 0
        self.lock = threading.Lock()
        self.policy_error = None
        self.limit_reason = None

    def ultrafast_transport(self, client):
        # Upstream asks its text helper for field values on both routes.
        text_and_openai = openai_transport.make_post_json(self.text_key, client)
        if self.route == "openai":
            return text_and_openai

        def post(url, key, body):
            if url == openai_transport._TEXT_SOURCE:
                return text_and_openai(url, key, body)
            if url != openai_transport._DECISION_SOURCE or body.get("model") != self.decision_model:
                raise ValueError("invalid_ultrafast_route")
            result = client.post(self.decision_endpoint, json=body,
                                 headers={"Authorization": "Bearer " + self.decision_key})
            if result.is_error:
                raise openai_transport.TransportError("http_" + str(result.status_code))
            return result.json()

        return post

    def reserve(self, request):
        url = str(request.url)
        if url == self.decision_endpoint:
            key, model = self.decision_key, self.decision_model
        elif url == OPENAI_TEXT and self.text_key:
            key, model = self.text_key, "gpt-6-luna"
        elif request.url.host in ("127.0.0.1", "localhost", "::1"):
            return None
        else:
            self.policy_error = "unexpected_model_route"
            raise httpx.RequestError(self.policy_error, request=request)
        valid = (request.method == "POST" and request.headers.get("Authorization") == "Bearer " + key
                 and json.loads(request.content).get("model") == model)
        if not valid:
            self.policy_error = "unexpected_model_request"
            raise httpx.RequestError(self.policy_error, request=request)
        size = len(request.content)
        with self.lock:
            if self.policy_error:
                raise httpx.RequestError(self.policy_error, request=request)
            if len(self.rows) >= self.max_calls:
                self.limit_reason = "call_limit"
                raise httpx.RequestError("physical_request_budget", request=request)
            if self.input_bytes + size > self.max_bytes:
                self.limit_reason = "input_byte_limit"
                raise httpx.RequestError("physical_request_budget", request=request)
            finished = [row for row in self.rows if row["state"] != "pending"]
            if any(row.get("estimated_usd") is None for row in finished):
                raise httpx.RequestError("incomplete_provider_usage", request=request)
            if sum(row["estimated_usd"] for row in finished) >= self.max_cost:
                self.limit_reason = "cost_limit"
                raise httpx.RequestError("cost_budget", request=request)
            row = {"state": "pending", "input_bytes": size, "endpoint": request.url.path.rsplit("/", 1)[-1],
                   "request_model": model}
            self.input_bytes += size
            self.rows.append(row)
        return row

    def finish(self, row, response):
        try:
            payload = response.json()
        except (ValueError, httpx.ResponseNotRead):
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        usage = payload.get("usage")
        clean = {"status": response.status_code, "usage_reported": isinstance(usage, dict)}
        if isinstance(usage, dict):
            detail = usage.get("input_tokens_details")
            detail = detail if isinstance(detail, dict) else {}
            for field in ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens",
                          "cached_tokens", "cache_write_tokens"):
                value = optional_number(usage.get(field, detail.get(field)))
                if value is not None:
                    clean[field] = value
        if "input_tokens" not in clean and "prompt_tokens" in clean:
            clean["input_tokens"] = clean["prompt_tokens"]
        if payload.get("model") == row["request_model"]:
            clean["model"] = payload["model"]
        with self.lock:
            row.update(clean)
            cost = charge(row, row["request_model"])
            row.update(state="response", estimated_usd=float(cost) if cost is not None else None)
            if response.is_success and (cost is None or "model" not in row):
                self.policy_error = "unverified_response_model_or_usage"

    @contextmanager
    def meter(self):
        original = httpx.Client.send

        def send(client, request, *args, **kwargs):
            row = self.reserve(request)
            if row is None:
                return original(client, request, *args, **kwargs)
            start = time.monotonic()
            try:
                response = original(client, request, *args, **kwargs)
                self.finish(row, response)
                return response
            except BaseException:
                with self.lock:
                    row.update(state="transport_error", estimated_usd=None)
                raise
            finally:
                with self.lock:
                    row["duration_ms"] = round((time.monotonic() - start) * 1000)

        httpx.Client.send = send
        try:
            yield
        finally:
            httpx.Client.send = original

    def snapshot(self):
        with self.lock:
            rows = [r.copy() for r in self.rows]
        return {"calls": rows, "attempts": len(rows), "request_bytes": self.input_bytes,
                "usage_complete": bool(rows) and all(r.get("estimated_usd") is not None for r in rows)}


def main():
    data = jev_runner.checked_input()
    engine = data.get("engine") or os.environ.get("BENCH_ENGINE")
    route = data.get("route") or os.environ.get("BENCH_ROUTE")
    worker = {"jev-ra": jev_runner, "jev-ultrafast": ultrafast_runner}[engine]
    openai_key = os.environ.pop("OPENAI_API_KEY", "")
    oc_key = os.environ.pop("OPENCODE_API_KEY", "")
    provider = Provider(data, route, openai_key, oc_key)
    if not provider.decision_key or (engine == "jev-ultrafast" and not openai_key):
        raise ValueError("missing_provider_key")
    with provider.meter():
        result = worker.run_case(data, provider)
    if provider.limit_reason and result.get("status") != "done":
        result.update(native_reason=result.get("reason"), reason=provider.limit_reason)
    if provider.policy_error or result.get("unfinished_speculations"):
        result.update(status="runner_error", reason=provider.policy_error or "unfinished_speculations")
    result.update(route=route, model=provider.decision_model)
    return result


if __name__ == "__main__":
    try:
        answer = main()
    except Exception as error:  # noqa: BLE001 -- sanitize exception at the child-process boundary.
        answer = {"status": "runner_error", "reason": type(error).__name__, "checks": []}
    print(json.dumps(answer), flush=True)
