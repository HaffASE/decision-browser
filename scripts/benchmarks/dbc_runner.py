"""One native decision-browser run; JSON stdin/stdout, no credential discovery."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path

from common import RequestLedger, install_async_http_meter

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import httpx

from decision_browser.browser import BrowserDriver
from decision_browser.contracts import (
    Check,
    Limits,
    PrivacyPolicy,
    TaskRequest,
)
from decision_browser.provider import OpenAIProvider
from decision_browser.runner import RunManager


def native_checks(checks):
    kinds = {"text_equals": "text", "url_equals": "route", "value_equals": "value"}
    return [Check(kind=kinds[row["type"]], target=row.get("target", ""),
                  expected=row["value"]) for row in checks]


class ObservedDriver(BrowserDriver):
    def __init__(self, request, captured):
        super().__init__("isolated")
        self.request = request
        self.captured = captured

    async def _start_isolated(self):
        # Playwright's executable_path property names full Chrome, whereas its
        # default headless launch selects headless-shell. Pin the common binary
        # explicitly without changing the native driver's sandbox or lifecycle.
        chromium = self._playwright.chromium
        original = chromium.launch

        async def launch(*args, **kwargs):
            kwargs["executable_path"] = self.request["browser_executable"]
            return await original(*args, **kwargs)

        chromium.launch = launch
        try:
            await super()._start_isolated()
        finally:
            chromium.launch = original
        await self.page.set_viewport_size({"width": 1280, "height": 720})

    async def close(self):
        if self._state == "open":
            start = time.monotonic()
            try:
                async with asyncio.timeout(10):
                    observed = await self.page.evaluate("""checks => ({
                      final_url: location.href,
                      viewport: {width: innerWidth, height: innerHeight},
                      checks: checks.map(c => {
                        if (c.type === 'url_equals') return location.href === c.value;
                        const el = document.querySelector(c.target);
                        if (!el) return false;
                        if (c.type === 'value_equals') return el.value === c.value;
                        if (c.type === 'text_equals') return el.innerText.trim() === c.value;
                        return false;
                      })})""", self.request["checks"])
                    self.captured.update(observed)
            except Exception as exc:  # noqa: BLE001 -- Record only a sanitized exception class.
                self.captured.update(checks=[], observation_error=type(exc).__name__)
            self.captured["independent_verification_ms"] = round((time.monotonic() - start) * 1000)
        await super().close()
        self.captured["cleanup"] = {"driver_state": self._state,
                                    "errors": list(self.cleanup_errors)}


class Provider(OpenAIProvider):
    def __init__(self, key, actions, ledger):
        super().__init__(key)
        self.actions = actions
        self.ledger = ledger

    async def _post(self, *args, **kwargs):
        # The production HTTP boundary reads its response incrementally under
        # the native output budget. Collect usage after that bounded read, not
        # by eagerly buffering the response inside the send interceptor.
        before = len(self.ledger.rows)
        start = time.monotonic()
        result = await super()._post(*args, **kwargs)
        if len(self.ledger.rows) > before:
            row = self.ledger.rows[before]
            self.ledger.complete(row, httpx.Response(row["status"], json={"usage": result.get("usage")}),
                                 time.monotonic() - start)
        return result

    async def decide(self, *args, **kwargs):
        answer = await super().decide(*args, **kwargs)
        self.actions.append(answer.operation)
        return answer


async def run(data, key, ledger):
    captured, actions = {}, []
    driver = ObservedDriver(data, captured)
    manager = RunManager(lambda: Provider(key, actions, ledger), lambda mode: driver,
                         admission_mode="task_scope")
    request = TaskRequest(
        start_url=data["start_url"], goal=data["goal"], values=data["values"], mode="isolated",
        checks=native_checks(data["checks"]), privacy=PrivacyPolicy(), visual=False,
        limits=Limits(max_steps=data["max_steps"], max_provider_calls=data["max_calls"],
                      max_seconds=data["timeout_s"], max_input_bytes=data["max_input_bytes"]),
    )
    start = time.monotonic()
    try:
        run_id = manager.start_task(request)
        task = manager._run.task
        try:
            async with asyncio.timeout(data["timeout_s"] + 15):
                await task
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        result = manager.get_task(run_id)
        answer = {"status": result.terminal, "reason": result.reason,
                  "functional": result.functional, "steps": result.usage.steps,
                  "actions": actions, "task_ms": round((time.monotonic() - start) * 1000),
                  "admission_class": result.admission_class,
                  "development_only": result.development_only,
                  "evidence": result.evidence, "native_timings": result.timings}
    finally:
        await manager.aclose()
    return {**answer, **captured, "usage": ledger.snapshot()}


def main():
    # The model key is removed before Playwright spawns any browser processes.
    key = os.environ.pop("OPENAI_API_KEY", "")
    ledger = None
    original = None
    try:
        from jev_runner import checked_input

        data = checked_input()
        if not key or key.startswith("sk-or-"):
            raise ValueError("missing_openai_key")
        ledger = RequestLedger(data["max_calls"], data["max_input_bytes"], data["max_cost_usd"])
        original = install_async_http_meter(ledger)
        answer = asyncio.run(run(data, key, ledger))
    except Exception as exc:  # noqa: BLE001 -- Sanitized subprocess boundary.
        answer = {"status": "runner_error", "reason": type(exc).__name__,
                  "checks": [], "usage": ledger.snapshot() if ledger else None}
    finally:
        if original is not None:
            httpx.AsyncClient.send = original
    print(json.dumps(answer, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
