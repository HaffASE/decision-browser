"""Physical HTTP accounting shared by all comparison adapters.

Only numeric usage, status codes, sizes and exception class names are retained.
Rates are estimates for gpt-6-luna, standard 30-minute cache storage; never an
invoice. The spend threshold is checked before each call, not a prepaid limit.
"""

from __future__ import annotations

import json
import math
import threading
import time
from typing import Any

import httpx

MODEL = "gpt-6-luna"
ORIGIN = "https://api.openai.com"
ENDPOINTS = {"/v1/decisions", "/v1/responses"}


def optional_number(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def estimate(row: dict[str, Any]) -> float | None:
    """Missing, inconsistent or invalid usage is unknown, never zero."""
    endpoint = row.get("endpoint", "").rsplit("/", 1)[-1]
    values = [row.get(key) for key in
              ("input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens")]
    if endpoint not in {"decisions", "responses"} or any(
        optional_number(value) is None for value in values
    ):
        return None
    total, cached, written, output = values
    if cached + written > total:
        return None
    long = total > 272_000
    cost = (2 if long else 1) * (
        .10 * (total - cached - written) + .01 * cached + .125 * written
    )
    if endpoint == "responses":
        cost += .50 * (1.5 if long else 1) * output
    return cost / 1_000_000


class RequestLedger:
    """Thread-safe actual attempts, including speculative and failed requests."""

    def __init__(self, max_calls: int, max_input_bytes: int = 131072,
                 max_cost_usd: float = .25):
        if (type(max_calls) is not int or max_calls <= 0
                or type(max_input_bytes) is not int or max_input_bytes <= 0
                or type(max_cost_usd) not in (int, float) or not math.isfinite(max_cost_usd)
                or max_cost_usd <= 0):
            raise ValueError("invalid_benchmark_budget")
        self.max_calls = max_calls
        self.max_input_bytes = max_input_bytes
        self.max_cost_usd = max_cost_usd
        self.lock = threading.Lock()
        self.rows: list[dict[str, Any]] = []
        self.input_bytes = 0
        self.blocked_by_budget = 0
        self.blocked_by_bytes = 0
        self.blocked_by_cost = 0
        self.rejected_routes = 0

    def attempt(self, endpoint: str, input_bytes: int) -> dict[str, Any] | None:
        if endpoint not in {"decisions", "responses"} or optional_number(input_bytes) is None:
            raise ValueError("invalid_benchmark_attempt")
        with self.lock:
            completed = [row for row in self.rows if row["state"] != "pending"]
            costs = [estimate(row) for row in completed]
            if any(cost is None for cost in costs) or sum(costs) >= self.max_cost_usd:
                self.blocked_by_cost += 1
                return None
            if len(self.rows) >= self.max_calls:
                self.blocked_by_budget += 1
                return None
            if self.input_bytes + input_bytes > self.max_input_bytes:
                self.blocked_by_bytes += 1
                return None
            self.input_bytes += input_bytes
            row = {"endpoint": endpoint, "state": "pending", "input_bytes": input_bytes}
            self.rows.append(row)
            return row

    def complete(self, row: dict[str, Any], response: httpx.Response, elapsed: float) -> None:
        usage = None
        try:
            body = response.json()
            if isinstance(body, dict):
                usage = body.get("usage")
        except (ValueError, httpx.ResponseNotRead):
            pass
        with self.lock:
            row.update(state="response", status=response.status_code,
                       duration_ms=round(elapsed * 1000), usage_reported=isinstance(usage, dict))
            if isinstance(usage, dict):
                detail = usage.get("input_tokens_details")
                detail = detail if isinstance(detail, dict) else {}
                for key, value in {
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "cached_tokens": detail.get("cached_tokens"),
                    "cache_write_tokens": detail.get("cache_write_tokens"),
                }.items():
                    row[key] = optional_number(value)
            row["estimated_usd"] = estimate(row)

    def failed(self, row: dict[str, Any], error: BaseException, elapsed: float) -> None:
        with self.lock:
            row.update(state="transport_error", error_type=type(error).__name__,
                       duration_ms=round(elapsed * 1000), usage_reported=False, estimated_usd=None)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            rows = [dict(row) for row in self.rows]
            result = {"blocked_by_budget": self.blocked_by_budget,
                      "blocked_by_bytes": self.blocked_by_bytes,
                      "blocked_by_cost": self.blocked_by_cost, "input_bytes": self.input_bytes,
                      "rejected_routes": self.rejected_routes}
        costs = [estimate(row) for row in rows]
        result.update(
            attempts=len(rows),
            decisions_attempts=sum(row["endpoint"] == "decisions" for row in rows),
            responses_attempts=sum(row["endpoint"] == "responses" for row in rows),
            completed_2xx=sum(row.get("state") == "response"
                              and 200 <= row["status"] < 300 for row in rows),
            non_2xx=sum(row.get("state") == "response" and row["status"] >= 300 for row in rows),
            transport_errors=sum(row["state"] == "transport_error" for row in rows),
            in_flight=sum(row["state"] == "pending" for row in rows),
            usage_missing=sum(cost is None for cost in costs),
            usage_complete=not result["rejected_routes"] and all(cost is not None for cost in costs),
            estimated_usd=sum(cost for cost in costs if cost is not None),
            calls=rows,
        )
        for key in ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens"):
            result[key] = sum(row.get(key) or 0 for row in rows)
        result["token_fields_missing"] = {
            key: sum(row.get(key) is None for row in rows)
            for key in ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens")
        }
        return result


def _reject_route(ledger: RequestLedger, request: httpx.Request) -> None:
    with ledger.lock:
        ledger.rejected_routes += 1
    raise httpx.RequestError("benchmark_model_route_rejected", request=request)


def _begin(ledger: RequestLedger, request: httpx.Request) -> dict[str, Any] | None:
    # Only the owned harness command and read-only CDP discovery routes are
    # exempt from provider spend. This is harness policy, not a network sandbox.
    if (request.url.host in {"127.0.0.1", "localhost", "::1"}
            and request.url.scheme == "http" and request.url.port is not None
            and not request.url.query and not request.url.username and not request.url.password
            and (request.method, request.url.path) in {
                ("POST", "/command"), ("GET", "/json/version"),
                ("GET", "/json"), ("GET", "/json/list"),
            }):
        return None
    if request.url.host != "api.openai.com":
        _reject_route(ledger, request)
    if (request.url.scheme != "https" or request.url.port not in (None, 443)
            or request.url.path not in ENDPOINTS or request.method != "POST"
            or request.url.query or request.url.username or request.url.password):
        _reject_route(ledger, request)
    try:
        payload = json.loads(request.content)
        valid = (isinstance(payload, dict) and payload.get("model") == MODEL
                 and payload.get("prompt_cache_retention") not in {"12h", "24h"})
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise httpx.RequestError("benchmark_model_payload_rejected", request=request)
    row = ledger.attempt(request.url.path.rsplit("/", 1)[-1], len(request.content))
    if row is None:
        raise httpx.RequestError("benchmark_cost_call_or_byte_budget", request=request)
    return row


def install_http_meter(ledger: RequestLedger):
    """Install within a disposable child; return the original sync send method."""
    original = httpx.Client.send

    def send(client, request, *args, **kwargs):
        kwargs["follow_redirects"] = False  # Apply to free harness routes as well as provider routes.
        row = _begin(ledger, request)
        if row is None:
            return original(client, request, *args, **kwargs)
        started = time.monotonic()
        try:
            response = original(client, request, *args, **kwargs)
        except BaseException as exc:
            ledger.failed(row, exc, time.monotonic() - started)
            raise
        ledger.complete(row, response, time.monotonic() - started)
        return response

    httpx.Client.send = send
    return original


def install_async_http_meter(ledger: RequestLedger):
    """Same physical-attempt accounting for the core's native async client."""
    original = httpx.AsyncClient.send

    async def send(client, request, *args, **kwargs):
        kwargs["follow_redirects"] = False  # Apply to free harness routes as well as provider routes.
        row = _begin(ledger, request)
        if row is None:
            return await original(client, request, *args, **kwargs)
        started = time.monotonic()
        try:
            response = await original(client, request, *args, **kwargs)
        except BaseException as exc:
            ledger.failed(row, exc, time.monotonic() - started)
            raise
        ledger.complete(row, response, time.monotonic() - started)
        return response

    httpx.AsyncClient.send = send
    return original
