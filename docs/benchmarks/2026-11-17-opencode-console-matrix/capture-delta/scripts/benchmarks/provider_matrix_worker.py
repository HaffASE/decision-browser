"""One real browser matrix attempt; JSON stdin/stdout, no credential discovery.

Core keeps its production provider, bounded HTTP reader, runner and guards. The
meter observes metadata after that reader finishes; it never saves wire bodies.
Terra reuses the standalone benchmark agent with its original medium policy.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from unittest.mock import patch

import httpx
from dbc_runner import ObservedDriver, native_checks
from long_terra import load_agent

from decision_browser import provider, systemone_provider
from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.provider_config import load_provider_settings
from decision_browser.provider_http import ProviderError, post_json
from decision_browser.runner import RunManager

ROOT = Path(__file__).resolve().parents[2]
CONSOLE_PRESETS = {"opencode_jev": "opencode", "opencode_gemini": "gemini"}
CONSOLE_SETTINGS = {
    arm: load_provider_settings({"DECISION_BROWSER_PROVIDER": preset})
    for arm, preset in CONSOLE_PRESETS.items()
}
ROUTES = {
    "decision_api": ("OPENAI_API_KEY", "gpt-6-luna", {
        "https://api.openai.com/v1/decisions", "https://api.openai.com/v1/responses",
    }),
    "jev": ("TYPESAFE_API_KEY", "jev-latest", {"https://api.typesafe.ai/v1/systemone"}),
    "terra": ("OPENAI_API_KEY", "gpt-6.1-terra", {"https://api.openai.com/v1/responses"}),
    **{arm: ("OPENCODE_API_KEY", settings.model, {settings.endpoint})
       for arm, settings in CONSOLE_SETTINGS.items()},
}
TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cached_tokens",
                "cache_write_tokens", "reasoning_tokens")
_MODELS = re.compile(
    r"(?:gpt-6-luna|gpt-6\.1-terra|gemini-decision-1)(?:-\d{4}-\d{2}-\d{2})?|jev-(?:latest|\d+(?:\.\d+)*)"
)
_ERROR_CODES = {"http_error", "transport_error", "malformed_response", "response_close_failed",
                "unsupported_content_encoding", "output_bytes", "deadline_expired"}
_API_ERRORS = {"invalid_api_key", "insufficient_quota", "rate_limit_exceeded", "model_not_found",
               "permission_denied", "invalid_request_error", "billing_not_active", "server_error"}


def numeric(value):
    return value if type(value) is int and value >= 0 else None


def read_usage(row, usage):
    """Unknown is distinct from reported zero, including nested cache fields."""
    row["usage_reported"] = isinstance(usage, dict)
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("input_tokens_details")
    details = details if isinstance(details, dict) else {}
    output = usage.get("output_tokens_details")
    output = output if isinstance(output, dict) else {}
    for name in TOKEN_FIELDS:
        value = usage.get(name)
        if name not in usage:
            value = (output if name == "reasoning_tokens" else details).get(name)
        row[name] = numeric(value)


def safe_calls(rows):
    """Allowlist metadata from both workers, excluding arbitrary API strings."""
    calls = []
    for raw in rows:
        row = {name: numeric(raw.get(name)) for name in TOKEN_FIELDS}
        for name in ("status", "input_bytes", "duration_ms"):
            if numeric(raw.get(name)) is not None:
                row[name] = raw[name]
        endpoint = raw.get("endpoint", "").rsplit("/", 1)[-1]
        row["endpoint"] = endpoint if endpoint in {"decisions", "responses", "systemone"} else "unknown"
        for name in ("model", "request_model"):
            value = raw.get(name)
            if isinstance(value, str) and _MODELS.fullmatch(value):
                row[name] = value
        if raw.get("reasoning_effort") == "medium":
            row["reasoning_effort"] = "medium"
        row["state"] = raw.get("state") if raw.get("state") in {
            "pending", "response", "parsed", "http_error", "transport_error", "read_error", "cancelled"
        } else "unknown"
        row["usage_reported"] = raw.get("usage_reported") is True
        if raw.get("error_code") in _ERROR_CODES:
            row["error_code"] = raw["error_code"]
        if raw.get("provider_error") in _API_ERRORS:
            row["provider_error"] = raw["provider_error"]
        error_type = raw.get("error_type")
        if isinstance(error_type, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", error_type):
            row["error_type"] = error_type
        calls.append(row)
    return calls


def summarize(rows):
    calls = safe_calls(rows)
    missing = {name: sum(row[name] is None for row in calls) for name in TOKEN_FIELDS}
    complete = bool(calls) and all(
        row.get("status") == 200 and row["usage_reported"]
        and row["input_tokens"] is not None and row["output_tokens"] is not None
        for row in calls
    )
    out = {"calls": calls, "attempts": len(calls),
           "request_bytes": sum(row.get("input_bytes", 0) for row in calls),
           "usage_complete": complete, "token_fields_missing": missing,
           "field_complete": {name: not count for name, count in missing.items()},
           "reported_tokens": {name: sum(row[name] or 0 for row in calls) for name in TOKEN_FIELDS},
           "non_2xx": sum("status" in row and not 200 <= row["status"] < 300 for row in calls),
           "transport_errors": sum(row["state"] == "transport_error" for row in calls)}
    out.update({name: None if missing[name] else out["reported_tokens"][name] for name in TOKEN_FIELDS})
    return out


def mark_failure(row, exc):
    row.update(error_type=type(exc).__name__, state=(
        "cancelled" if isinstance(exc, asyncio.CancelledError) else
        "transport_error" if "status" not in row else
        "http_error" if row["status"] != 200 else "read_error"))
    if getattr(exc, "code", None) in _ERROR_CODES:
        row["error_code"] = exc.code


class Meter:
    def __init__(self, arm, key, data):
        self.arm, self.key, self.data = arm, key, data
        self.rows = []
        self.input_bytes = 0

    async def request(self, request):
        _, model, endpoints = ROUTES[self.arm]
        try:
            payload = json.loads(request.content)
        except (ValueError, UnicodeError):
            raise ProviderError("matrix_unexpected_route") from None
        if (str(request.url) not in endpoints or request.method != "POST"
                or request.headers.get("Authorization") != "Bearer " + self.key
                or not isinstance(payload, dict) or payload.get("model") != model):
            raise ProviderError("matrix_unexpected_route")
        size = len(request.content)
        if (len(self.rows) >= self.data["max_calls"]
                or self.input_bytes + size > self.data["max_input_bytes"]):
            raise ProviderError("matrix_budget_exhausted")
        row = {"endpoint": request.url.path.rsplit("/", 1)[-1], "request_model": model,
               "input_bytes": size, "state": "pending", "usage_reported": False}
        self.rows.append(row)
        self.input_bytes += size
        request.extensions["provider_matrix_row"] = row

    async def response(self, response):
        row = response.request.extensions["provider_matrix_row"]
        row.update(status=response.status_code, state="response")

    async def post(self, *args, **kwargs):
        before, start = len(self.rows), time.monotonic()
        try:
            reply = await post_json(*args, **kwargs)
            if len(self.rows) > before:
                row = self.rows[before]
                row["state"] = "parsed"
                read_usage(row, reply.get("usage"))
                value = reply.get("model")
                if isinstance(value, str) and _MODELS.fullmatch(value):
                    row["model"] = value
            return reply
        except BaseException as exc:
            if len(self.rows) > before:
                mark_failure(self.rows[before], exc)
            raise
        finally:
            if len(self.rows) > before:
                self.rows[before]["duration_ms"] = round((time.monotonic() - start) * 1000)


class TraceActions:
    async def decide(self, *args, **kwargs):
        result = await super().decide(*args, **kwargs)
        self.actions.append(result.operation)
        return result


class OpenAI(TraceActions, provider.OpenAIProvider):
    pass


class SystemOne(TraceActions, systemone_provider.SystemOneProvider):
    pass


async def run_core(data, arm, key, transport=None):
    meter, captured = Meter(arm, key, data), {}
    driver = ObservedDriver(data, captured)
    answer = {"status": "runner_error", "reason": "not_started", "checks": []}
    limits = Limits(max_steps=data["max_steps"], max_provider_calls=data["max_calls"],
                    max_seconds=data["timeout_s"], max_input_bytes=data["max_input_bytes"])
    task = TaskRequest(start_url=data["start_url"], goal=data["goal"], values=data["values"],
                       checks=native_checks(data["checks"]), mode="isolated", visual=False,
                       privacy=PrivacyPolicy(blocked_literals=[key]), limits=limits)
    async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=None, follow_redirects=False,
                                event_hooks={"request": [meter.request], "response": [meter.response]}) as client:
        if arm == "decision_api":
            native = OpenAI(key, client=client)
        else:
            _, model, endpoints = ROUTES[arm]
            (endpoint,) = endpoints
            native = SystemOne(endpoint=endpoint, model=model, api_key=key, client=client)
        native.actions = []
        module = provider if arm == "decision_api" else systemone_provider
        manager = RunManager(lambda: native, lambda mode: driver, admission_mode="task_scope")
        with patch.object(module, "post_json", meter.post):
            try:
                run_id = manager.start_task(task)
                await asyncio.wait_for(manager._run.task, data["timeout_s"] + 15)
                result = manager.get_task(run_id)
                answer = {"status": result.terminal, "reason": result.reason,
                          "functional": result.functional, "steps": result.usage.steps,
                          "native_evidence": result.evidence, "native_timings": result.timings,
                          "admission_class": result.admission_class,
                          "development_only": result.development_only}
            except Exception as exc:  # noqa: BLE001 -- Never publish exception messages.
                answer.update(status="runner_error", reason=type(exc).__name__)
            finally:
                try:
                    await manager.aclose()
                except Exception as exc:  # noqa: BLE001 -- Preserve the measured attempt.
                    captured["cleanup"] = {"driver_state": "unknown", "errors": [type(exc).__name__]}
    return {**answer, **captured, "actions": native.actions, "arm": arm,
            "model": ROUTES[arm][1], "usage": summarize(meter.rows)}


async def run_terra(data, key):
    agent = load_agent()

    class MeasuredTerra(agent.Terra):
        async def choose(self, observation):
            before = len(self.rows)
            try:
                return await super().choose(observation)
            except BaseException as exc:
                if len(self.rows) > before:
                    mark_failure(self.rows[before], exc)
                raise

    agent.Terra = MeasuredTerra
    answer = await agent.run(data, key)
    answer.update(arm="terra", model="gpt-6.1-terra", reasoning_effort="medium")
    answer["usage"] = summarize(answer.get("usage", {}).get("calls", []))
    return answer


def main():
    data = json.load(sys.stdin)
    arm = data["arm"]
    env_name = ROUTES[arm][0]
    key = os.environ.pop(env_name, "")
    for name in ("OPENAI_API_KEY", "TYPESAFE_API_KEY", "OPENCODE_API_KEY", "OPENROUTER_API_KEY",
                 "DECISION_BROWSER_API_KEY", "DECISION_BROWSER_TEXT_API_KEY"):
        os.environ.pop(name, None)
    if (not key or not key.isascii() or any(c.isspace() for c in key)
            or Path(data["source_root"]).resolve() != ROOT):
        raise ValueError("invalid_worker_environment")
    data.setdefault("max_calls", data.get("max_provider_calls"))
    data.setdefault("max_cost_usd", .25)
    answer = asyncio.run(run_terra(data, key) if arm == "terra" else run_core(data, arm, key))
    if key in json.dumps(answer, ensure_ascii=False):
        raise ValueError("unsafe_worker_output")
    return answer


if __name__ == "__main__":
    try:
        result = main()
    except Exception as exc:  # noqa: BLE001 -- Final worker boundary emits only a class name.
        result = {"status": "runner_error", "reason": type(exc).__name__, "checks": [],
                  "usage": summarize([])}
    print(json.dumps(result, ensure_ascii=False), flush=True)
