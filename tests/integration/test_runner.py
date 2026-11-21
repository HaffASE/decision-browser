"""Decision Browser runner integration tests. Development fixture and isolated Chromium only.

Shared helpers for sibling modules:
ScriptedProvider(operations: list[str])
RecordingDriver(mode: str)
request(url: str, checks: list[Check] | None = None, limits: Limits | None = None) -> TaskRequest
manager(url: str, provider: ScriptedProvider) -> RunManager
finish(manager: RunManager, run_id: str) -> TaskResult
record_final_verification(monkeypatch) -> list[bool]
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import ClassVar

import pytest
from playwright.async_api import Error

from decision_browser.browser import BrowserBlocked, BrowserDriver
from decision_browser.contracts import (
    Check,
    Limits,
    PrivacyPolicy,
    TaskRequest,
    TaskResult,
)
from decision_browser.decision_codec import Decision
from decision_browser.runner import DevelopmentConfig, RunManager

_OPEN = frozenset({"queued", "awaiting_human_admission", "running"})
_ACTIONS = frozenset({("navigate", "__start__"), ("click", "inc")})


def record_final_verification(monkeypatch):
    calls = []
    original = RunManager._verify

    async def verify(self, run, *, finish=False, **kwargs):
        if finish:
            calls.append(True)
        return await original(self, run, finish=finish, **kwargs)

    monkeypatch.setattr(RunManager, "_verify", verify)
    return calls


class ScriptedProvider:
    """Scripted decide() port. Factory is zero-arg and returns this instance."""

    def __init__(self, operations: list[str]) -> None:
        self._operations = list(operations)
        self.calls = 0
        self.stop_choices = None

    async def decide(
        self,
        questions,
        input_text,
        *,
        budget,
        privacy,
        operation_head="operation",
        target_heads=None,
    ):
        budget.reserve_provider(len(input_text.encode()))
        budget.consume_output(1)
        self.calls += 1
        operation = self._operations.pop(0)
        if operation in {"DONE", "UNABLE"}:
            self.stop_choices = next(question.choices for question in questions
                                     if question.name == operation_head)
            return Decision(operation, None, 1.0)
        head = target_heads[operation]
        target = next(
            question for question in questions if question.name == head
        ).choices[0]
        return Decision(operation, target, 1.0)

    async def generate_text(
        self, prompt, *, budget, privacy, max_text_chars=4096
    ) -> str:
        raise AssertionError("generate_text")

    async def aclose(self) -> None:
        return None


class RecordingDriver(BrowserDriver):
    """Isolated Chromium driver. close() records #count before the real close."""

    seen: ClassVar[list[RecordingDriver]] = []

    def __init__(self, mode: str) -> None:
        super().__init__(mode)
        self.observed_count: int | None = None
        RecordingDriver.seen.append(self)

    async def close(self) -> None:
        try:
            with contextlib.suppress(BrowserBlocked, Error, ValueError):
                page = self.page
                locator = page.locator("#count")
                if await locator.count():
                    self.observed_count = int((await locator.inner_text()).strip())
        finally:
            await super().close()


def request(
    url: str, checks: list[Check] | None = None, limits: Limits | None = None
) -> TaskRequest:
    """TaskRequest with values={}, PrivacyPolicy(), and Limits(max_seconds=15.0) by default."""
    return TaskRequest(
        goal="Update the counter.",
        start_url=url,
        values={},
        checks=[] if checks is None else checks,
        limits=Limits(max_seconds=15.0) if limits is None else limits,
        privacy=PrivacyPolicy(),
    )


def manager(url: str, provider: ScriptedProvider) -> RunManager:
    """driver_factory(mode) plus DevelopmentConfig for url.rstrip('/') and the counter fixture actions."""
    return RunManager(
        lambda: provider,
        lambda mode: RecordingDriver(mode),
        development_config=DevelopmentConfig(url.rstrip("/"), _ACTIONS),
    )


async def finish(manager: RunManager, run_id: str) -> TaskResult:
    """Await a public terminal result in 20s.

    Propagate post-terminal task failures. Finally cancel and await the run
    task.
    """
    task = manager._run.task if manager._run is not None else None
    try:
        async with asyncio.timeout(20):
            while manager.get_task(run_id).terminal in _OPEN:
                await asyncio.sleep(0)
            if task is not None:
                await task
    finally:
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                async with asyncio.timeout(20):
                    await task
    return manager.get_task(run_id)


def _count_check() -> Check:
    return Check(kind="text", target="#count", expected="1")


async def _drive(
    url: str, operations: list[str], checks: list[Check], limits: Limits | None = None
):
    RecordingDriver.seen.clear()
    provider = ScriptedProvider(operations)
    host = manager(url, provider)
    result = await finish(host, host.start_task(request(url, checks, limits)))
    return result, provider, RecordingDriver.seen[-1]


async def test_click_then_done_verifies_count(counter_page_url: str) -> None:
    result, provider, driver = await _drive(
        counter_page_url, ["click"], [_count_check()]
    )
    assert result.terminal == "verified" and result.functional == "verified"
    assert result.development_only is True and result.effect_guarantee == "none"
    assert (
        result.usage.calls == 1
        and result.usage.steps == 2
        and result.usage.output_bytes == 1
    )
    assert provider.calls == 1 and driver.observed_count == 1


@pytest.mark.parametrize("goal", [
    "Обновить счётчик. Не переходить к покупке или бронированию.",
    "Update the counter. Do not purchase, book, send, or delete anything.",
])
async def test_safe_action_verifies_despite_task_prohibitions(
    counter_page_url: str, goal: str
) -> None:
    provider = ScriptedProvider(["click"])
    host = manager(counter_page_url, provider)
    req = request(counter_page_url, [_count_check()])
    req.goal = goal
    result = await finish(host, host.start_task(req))
    assert (result.terminal, result.functional) == ("verified", "verified")
    assert result.development_only is True and result.effect_guarantee == "none"
    assert result.usage.calls == provider.calls == 1
    assert result.usage.steps == 2
    assert RecordingDriver.seen[-1].observed_count == 1


@pytest.mark.parametrize("terminal", ["DONE", "UNABLE"])
async def test_immediate_done_fails_unmet_count(
    counter_page_url: str, terminal, monkeypatch,
) -> None:
    final_verification_calls = record_final_verification(monkeypatch)
    result, provider, driver = await _drive(
        counter_page_url, [terminal], [_count_check()]
    )
    assert "DONE" not in provider.stop_choices and "UNABLE" in provider.stop_choices
    if terminal == "DONE":
        assert result.terminal == "failed" and result.functional == "failed"
        assert "runner:helper_schema" in result.evidence
        assert final_verification_calls == []
    else:
        assert result.terminal == "verification_failed" and result.functional == "failed"
        assert "runner:helper_schema" not in result.evidence
        assert "model:unable" in result.evidence
        assert final_verification_calls == [True]
    assert result.development_only is True and result.effect_guarantee == "none"
    assert result.usage.calls == 1 and result.usage.steps == 1
    assert provider.calls == 1 and driver.observed_count == 0


async def test_empty_checks_done_is_completed_unverified(counter_page_url: str) -> None:
    result, provider, driver = await _drive(counter_page_url, ["DONE"], [])
    assert (
        result.terminal == "completed_unverified" and result.functional == "unverified"
    )
    assert "DONE" in provider.stop_choices
    assert result.development_only is True and result.effect_guarantee == "none"
    assert result.usage.calls == 1 and result.usage.steps == 1
    assert provider.calls == 1 and driver.observed_count == 0


async def test_max_steps_click_verifier_adds_no_provider_call(
    counter_page_url: str,
) -> None:
    limits = Limits(max_steps=2, max_seconds=15.0)
    result, provider, driver = await _drive(
        counter_page_url, ["click"], [_count_check()], limits
    )
    assert result.terminal == "verified" and result.functional == "verified"
    assert result.development_only is True and result.effect_guarantee == "none"
    assert (
        result.usage.calls == 1
        and result.usage.steps == 2
        and result.usage.output_bytes == 1
    )
    assert provider.calls == 1 and driver.observed_count == 1


async def test_max_steps_one_click_unmet_count_two(counter_page_url: str) -> None:
    limits = Limits(max_steps=2, max_seconds=15.0)
    unmet = Check(kind="text", target="#count", expected="2")
    result, provider, driver = await _drive(
        counter_page_url, ["click"], [unmet], limits
    )
    assert result.terminal == "limit_exceeded" and result.functional == "failed"
    assert result.development_only is True and result.effect_guarantee == "none"
    assert result.usage.calls == 1 and result.usage.steps == 2
    assert provider.calls == 1 and driver.observed_count == 1


async def test_provider_cap_wait_then_done_unmet_count(counter_page_url: str) -> None:
    limits = Limits(max_provider_calls=1, max_seconds=15.0)
    result, provider, driver = await _drive(
        counter_page_url, ["wait", "DONE"], [_count_check()], limits
    )
    assert result.terminal == "limit_exceeded" and result.functional == "failed"
    assert result.development_only is True and result.effect_guarantee == "none"
    assert result.usage.calls == 1 and result.usage.steps == 2
    assert provider.calls == 1 and driver.observed_count == 0


@pytest.mark.parametrize("terminal", ["DONE", "UNABLE"])
async def test_provider_cap_immediate_done_stays_verification_failed(
    counter_page_url: str, terminal, monkeypatch,
) -> None:
    final_verification_calls = record_final_verification(monkeypatch)
    limits = Limits(max_provider_calls=1, max_seconds=15.0)
    result, provider, driver = await _drive(
        counter_page_url, [terminal], [_count_check()], limits
    )
    assert "DONE" not in provider.stop_choices and "UNABLE" in provider.stop_choices
    if terminal == "DONE":
        assert result.terminal == "failed" and result.functional == "failed"
        assert "runner:helper_schema" in result.evidence
        assert final_verification_calls == []
    else:
        assert result.terminal == "verification_failed" and result.functional == "failed"
        assert "runner:helper_schema" not in result.evidence
        assert "model:unable" in result.evidence
        assert final_verification_calls == [True]
    assert result.development_only is True and result.effect_guarantee == "none"
    assert result.usage.calls == 1 and result.usage.steps == 1
    assert provider.calls == 1 and driver.observed_count == 0


async def test_max_steps_one_empty_checks_bootstrap_limit_exceeded(
    counter_page_url: str,
) -> None:
    limits = Limits(max_steps=1, max_seconds=15.0)
    result, provider, driver = await _drive(counter_page_url, [], [], limits)
    assert result.terminal == "limit_exceeded" and result.functional == "unverified"
    assert result.terminal != "completed_unverified"
    assert result.development_only is True and result.effect_guarantee == "none"
    assert result.usage.calls == 0 and result.usage.steps == 1
    assert provider.calls == 0 and driver.observed_count == 0


async def test_max_steps_one_route_check_verifies_on_bootstrap(
    counter_page_url: str,
) -> None:
    limits = Limits(max_steps=1, max_seconds=15.0)
    route = Check(kind="route", target="", expected=counter_page_url)
    result, provider, driver = await _drive(counter_page_url, [], [route], limits)
    assert result.terminal == "verified" and result.functional == "verified"
    assert result.development_only is True and result.effect_guarantee == "none"
    assert result.usage.calls == 0 and result.usage.steps == 1
    assert provider.calls == 0 and driver.observed_count == 0
