"""Limits, cancel, retention, and fail-closed admission. No native grant pass."""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest
from test_runner import RecordingDriver, ScriptedProvider

from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.runner import DevelopmentConfig, RunManager, RunnerError

_OPEN = frozenset({"queued", "awaiting_human_admission", "running"})
_NAV = frozenset({("navigate", "__start__")})
_URL = "http://127.0.0.1/"


class Life:
    def __init__(self, hang_start: bool = False, hang_close: bool = False) -> None:
        self.starts = self.closes = 0
        self.cleanup_errors: list[str] = []
        self.residual_owned = False
        self.foreign_mismatch = False
        self.page = None
        self.gate = asyncio.Event()
        self.hang_start = hang_start
        self.hang_close = hang_close

    async def start(self) -> None:
        self.starts += 1
        if self.hang_start:
            await self.gate.wait()

    async def close(self) -> None:
        self.closes += 1
        if self.hang_close:
            await self.gate.wait()


class HangProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__(["DONE"])
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

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
        self.entered.set()
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("provider released")


def _req(url: str, goal: str, seconds: float) -> TaskRequest:
    return TaskRequest(
        goal=goal,
        start_url=url,
        values={},
        checks=[],
        limits=Limits(max_seconds=seconds),
        privacy=PrivacyPolicy(),
    )


def _raise_code(call) -> str:
    try:
        call()
    except RunnerError as exc:
        return exc.code
    raise AssertionError("expected RunnerError")


async def _stop(host: RunManager) -> None:
    run = host._run
    task = None if run is None else run.task
    if task is None:
        return
    if not task.done():
        run.cancel_event.set()
        task.cancel()
    try:
        async with asyncio.timeout(2):
            await task
    except asyncio.CancelledError:
        return


async def _until(host: RunManager, run_id: str, terminal: str, bound: float) -> None:
    async with asyncio.timeout(bound):
        while host.get_task(run_id).terminal in _OPEN:
            await asyncio.sleep(0)
        got = host.get_task(run_id)
        assert got.terminal == terminal, (
            f"expected {terminal}, got {got.terminal}: {got.reason}; usage={got.usage}"
        )
        task = host._run.task if host._run is not None else None
        if task is not None and not task.done():
            await task


async def test_sync_start_busy_copy_unknown_retention() -> None:
    clock = {"t": 0.0}
    driver = Life()
    host = RunManager(
        lambda: ScriptedProvider(["DONE"]),
        lambda _m: driver,
        clock=lambda: clock["t"],
        cleanup_grace=0.05,
        development_config=DevelopmentConfig(_URL.rstrip("/"), frozenset()),
    )
    assert _raise_code(lambda: host.get_task("missing")) == "unknown"
    run_id = host.start_task(_req(_URL + "purchase", "Review the page.", 0.1))
    assert type(run_id) is str and host.get_task(run_id).run_id == run_id
    try:
        assert (
            _raise_code(lambda: host.start_task(_req(_URL + "purchase", "Review the page.", 0.1))) == "busy"
        )
        await _until(host, run_id, "blocked", 2)
        blocked = host.get_task(run_id)
        assert (
            driver.starts == 0 and blocked.reason == "consequential_action_blocked"
        )
        assert blocked.development_only is True and blocked.effect_guarantee == "none"
        viewed = host.get_task(run_id)
        viewed.artifact_ids.append("mutated")
        assert host.get_task(run_id).artifact_ids == []
        clock["t"] = 0.12
        assert (
            _raise_code(lambda: host.start_task(_req(_URL + "purchase", "Review the page.", 0.1))) == "busy"
        )
        assert host.get_task(run_id).run_id == run_id
        assert (
            _raise_code(lambda: host.start_task(_req(_URL + "purchase", "Review the page.", 0.1))) == "busy"
        )
        clock["t"] = 59.99
        assert host.get_task(run_id).terminal == "blocked"
        assert _raise_code(lambda: host.start_task(_req(_URL + "purchase", "Review the page.", 0.1))) == "busy"
        clock["t"] = 60.01
        assert _raise_code(lambda: host.get_task(run_id)) == "expired"
        nxt = host.start_task(_req(_URL + "purchase", "Review the page.", 0.1))
        await _until(host, nxt, "blocked", 2)
    finally:
        await _stop(host)


async def test_cancel_before_browser_is_idempotent() -> None:
    driver, provider = Life(), ScriptedProvider(["DONE"])
    host = RunManager(
        lambda: provider,
        lambda _m: driver,
        development_config=DevelopmentConfig(_URL.rstrip("/"), frozenset()),
    )
    run_id = host.start_task(_req(_URL, "Update the counter.", 3.0))
    try:
        async with asyncio.timeout(2):
            while host.get_task(run_id).terminal != "awaiting_human_admission":
                await asyncio.sleep(0)
        assert driver.starts == 0 and provider.calls == 0
        first, second = host.cancel_task(run_id), host.cancel_task(run_id)
        assert (
            first.terminal == second.terminal == "running"
            and second.reason == "finalizing"
            and second.development_only is True
        )
        await _until(host, run_id, "cancelled", 2)
        assert host.get_task(run_id).reason == "cancelled"
        assert (
            driver.starts == 0
            and provider.calls == 0
            and host.get_task(run_id).usage.calls == 0
        )
    finally:
        await _stop(host)


async def test_production_admission_blocks_without_dispatch() -> None:
    driver, provider = Life(), ScriptedProvider(["DONE"])
    host = RunManager(
        lambda: provider,
        lambda _m: driver,
        development_config=DevelopmentConfig(_URL.rstrip("/"), frozenset()),
    )
    run_id = host.start_task(_req(_URL, "Update the counter.", 3.0))
    try:
        async with asyncio.timeout(2):
            while host.pending_proposal(run_id) is None:
                await asyncio.sleep(0)
        assert driver.starts == 0 and provider.calls == 0
        pending = host.pending_proposal(run_id)
        shown = await host.present_admission(
            run_id, pending.proposal_id, pending.digest
        )
        assert (
            shown.terminal == "running"
            and shown.reason == "finalizing"
            and shown.development_only is True
        )
        await _until(host, run_id, "blocked", 2)
        assert host.get_task(run_id).reason == "no_supported_human_grant_channel"
        assert (
            driver.starts == 0
            and provider.calls == 0
            and host.get_task(run_id).usage.calls == 0
        )
    finally:
        await _stop(host)


async def test_admission_expires_on_original_deadline() -> None:
    driver = Life()
    host = RunManager(
        lambda: ScriptedProvider(["DONE"]),
        lambda _m: driver,
        cleanup_grace=0.05,
        development_config=DevelopmentConfig(_URL.rstrip("/"), frozenset()),
    )
    run_id = host.start_task(_req(_URL, "Update the counter.", 0.1))
    try:
        await _until(host, run_id, "limit_exceeded", 1)
        got = host.get_task(run_id)
        assert got.reason == "limit_exceeded" and got.timings["total"] < 1
        assert got.development_only is True and got.effect_guarantee == "none"
        assert driver.starts == 0 and driver.closes == 1 and got.usage.calls == 0
    finally:
        await _stop(host)


async def test_provider_hang_after_bootstrap(counter_page_url: str) -> None:
    RecordingDriver.seen.clear()
    provider = HangProvider()
    host = RunManager(
        lambda: provider,
        lambda mode: RecordingDriver(mode),
        development_config=DevelopmentConfig(counter_page_url.rstrip("/"), _NAV),
    )
    # Match the normal browser integration budget. Startup is part of this
    # original deadline; provider entry never resets or extends it.
    run_id = host.start_task(_req(counter_page_url, "Update the counter.", 15.0))
    deadline = host._run.budget.original_deadline
    try:
        async with asyncio.timeout(15 + host._grace + 2):
            while not provider.entered.is_set():
                got = host.get_task(run_id)
                assert got.terminal in _OPEN, (
                    f"provider not entered: {got.terminal}: {got.reason}; usage={got.usage}"
                )
                await asyncio.sleep(0)
        assert provider.calls == 1
        assert host._run.budget.original_deadline == deadline
        await _until(
            host, run_id, "limit_exceeded",
            max(0, deadline - time.monotonic()) + host._grace + 2,
        )
        got = host.get_task(run_id)
        assert got.reason == "limit_exceeded" and provider.calls == 1
        assert got.usage.calls == 1 and got.usage.output_bytes == 1
        assert provider.cancelled.is_set()
        assert time.monotonic() >= deadline
        assert host._run.budget.original_deadline == deadline
    finally:
        provider.gate.set()
        await _stop(host)


async def test_browser_start_hang_has_no_provider_request() -> None:
    driver, provider = Life(hang_start=True), ScriptedProvider(["DONE"])
    host = RunManager(
        lambda: provider,
        lambda _m: driver,
        development_config=DevelopmentConfig(_URL.rstrip("/"), _NAV),
        cleanup_grace=0.05,
    )
    run_id = host.start_task(_req(_URL, "Update the counter.", 0.1))
    try:
        await _until(host, run_id, "limit_exceeded", 2)
        got = host.get_task(run_id)
        assert (
            got.reason == "limit_exceeded"
            and driver.starts == 1
            and provider.calls == 0
            and got.usage.calls == 0
        )
    finally:
        driver.gate.set()
        await _stop(host)


async def test_cleanup_hang_bounds_outcome_unknown() -> None:
    driver = Life(hang_close=True)
    host = RunManager(
        lambda: ScriptedProvider(["DONE"]),
        lambda _m: driver,
        cleanup_grace=0.05,
        development_config=DevelopmentConfig(_URL.rstrip("/"), frozenset()),
    )
    run_id = host.start_task(_req(_URL + "purchase", "Review the page.", 0.1))
    try:
        await _until(host, run_id, "outcome_unknown", 2)
        got = host.get_task(run_id)
        assert (
            got.reason == "cleanup_unknown"
            and got.development_only is True
            and driver.starts == 0
            and driver.closes == 1
        )
    finally:
        driver.gate.set()
        await _stop(host)


async def test_dev_fixture_known_consequential_blocks_without_start() -> None:
    driver = Life()
    provider = ScriptedProvider(["DONE"])
    host = RunManager(
        lambda: provider,
        lambda _m: driver,
        development_config=DevelopmentConfig(_URL.rstrip("/"), _NAV),
    )
    run_id = host.start_task(_req(_URL + "purchase", "Review the page.", 3.0))
    try:
        await _until(host, run_id, "blocked", 2)
        got = host.get_task(run_id)
        assert got.terminal == "blocked"
        assert got.reason == "consequential_action_blocked"
        assert got.admission_class == "consequential"
        assert driver.starts == 0 and provider.calls == 0 and got.usage.calls == 0
    finally:
        await _stop(host)


from decision_browser.admission import AdmissionStore
from decision_browser.desk import DevelopmentDesk


class TrackingDesk(DevelopmentDesk):
    def __init__(self, store, responses, before_response) -> None:
        super().__init__(store, responses, before_response)
        self.displaycount = 0

    async def present(self, proposal):
        self.displaycount += 1
        return await super().present(proposal)


def _pair():
    entered, release = asyncio.Event(), asyncio.Event()

    async def before() -> None:
        entered.set()
        await release.wait()

    return entered, release, before


def _open(responses, before, driver):
    store = AdmissionStore()
    desk = TrackingDesk(store, responses, before)
    host = RunManager(
        lambda: ScriptedProvider(["DONE"]),
        lambda _m: driver,
        admission_store=store,
        desk=desk,
    )
    assert desk.development_only is True
    return store, desk, host


async def _pend(host, run_id):
    async with asyncio.timeout(2):
        while host.pending_proposal(run_id) is None:
            await asyncio.sleep(0)
    return host.pending_proposal(run_id)


async def _end(host, release, task) -> None:
    release.set()
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            async with asyncio.timeout(2):
                await task
    await _stop(host)


async def _reject(host, run_id, pid, digest) -> None:
    try:
        async with asyncio.timeout(2):
            await host.present_admission(run_id, pid, digest)
    except RunnerError:
        return
    raise AssertionError("expected RunnerError")


async def test_concurrent_present_admission_second_is_rejected() -> None:
    entered, release, before = _pair()
    driver = Life()
    _store, desk, host = _open(["decline"], before, driver)
    run_id = host.start_task(_req(_URL, "Update the counter.", 3.0))
    first = None
    try:
        pending = await _pend(host, run_id)
        pid, digest = pending.proposal_id, pending.digest
        first = asyncio.create_task(host.present_admission(run_id, pid, digest))
        async with asyncio.timeout(2):
            await entered.wait()
        await _reject(host, run_id, pid, digest)
        assert desk.displaycount == 1
        release.set()
        async with asyncio.timeout(2):
            shown = await first
        assert shown.terminal in {"running", "blocked"} and driver.starts == 0
        await _until(host, run_id, "blocked", 2)
    finally:
        await _end(host, release, first)


async def test_late_accept_after_cancel_stays_cancelled() -> None:
    entered, release, before = _pair()
    driver = Life()
    store, desk, host = _open(["accept"], before, driver)
    run_id = host.start_task(_req(_URL, "Update the counter.", 3.0))
    first = None
    try:
        pending = await _pend(host, run_id)
        first = asyncio.create_task(
            host.present_admission(run_id, pending.proposal_id, pending.digest)
        )
        async with asyncio.timeout(2):
            await entered.wait()
        assert host.cancel_task(run_id).terminal == "running"
        release.set()
        async with asyncio.timeout(2):
            await first
        await _until(host, run_id, "cancelled", 2)
        assert host.get_task(run_id).terminal == "cancelled" and desk.displaycount == 1
        assert store.is_live(pending.proposal_id) is False and driver.starts == 0
    finally:
        await _end(host, release, first)


async def test_first_decline_wins_later_present_rejected() -> None:
    entered, release, before = _pair()
    driver = Life()
    store, desk, host = _open(["decline"], before, driver)
    run_id = host.start_task(_req(_URL, "Update the counter.", 3.0))
    first = None
    try:
        pending = await _pend(host, run_id)
        pid, digest = pending.proposal_id, pending.digest
        first = asyncio.create_task(host.present_admission(run_id, pid, digest))
        async with asyncio.timeout(2):
            await entered.wait()
        release.set()
        async with asyncio.timeout(2):
            shown = await first
        assert shown.terminal in {"running", "blocked"} and desk.displaycount == 1
        await _reject(host, run_id, pid, digest)
        await _until(host, run_id, "blocked", 2)
        assert desk.displaycount == 1 and host.get_task(run_id).terminal == "blocked"
        assert store.is_live(pid) is False and driver.starts == 0
    finally:
        await _end(host, release, first)


from test_runner import finish, request

from decision_browser import runner
from decision_browser.contracts import Check


async def test_verify_functional_hang_limit_no_click(
    counter_page_url, monkeypatch
) -> None:
    entered, gate, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def verify_functional(*_args, **_kwargs):
        entered.set()
        try:
            await gate.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("verifier released")

    monkeypatch.setattr(runner, "verify_functional", verify_functional)
    RecordingDriver.seen.clear()
    provider = ScriptedProvider(["DONE"])
    host = manager_fixture(counter_page_url, provider)
    # Chromium startup and bootstrap consume the same original deadline.
    # A 3s total budget can expire during navigation, before this test reaches
    # verification; an unacknowledged dispatch correctly yields outcome_unknown.
    limits = Limits(max_seconds=15.0)
    check = Check(kind="text", target="#count", expected="1")
    run_id = host.start_task(request(counter_page_url, [check], limits))
    deadline = host._run.budget.original_deadline
    try:
        async with asyncio.timeout(limits.max_seconds + host._grace + 2):
            while not entered.is_set():
                got = host.get_task(run_id)
                assert got.terminal in _OPEN, (
                    f"verifier not entered: {got.terminal}: {got.reason}; "
                    f"evidence={got.evidence}; usage={got.usage}"
                )
                await asyncio.sleep(0)
        assert host._run.budget.original_deadline == deadline
        assert not host._run.queued
        await _until(
            host, run_id, "limit_exceeded",
            max(0, deadline - time.monotonic()) + host._grace + 2,
        )
        result = host.get_task(run_id)
        assert result.terminal == "limit_exceeded" and result.reason == "limit_exceeded", (
            f"{result.terminal}: {result.reason}; evidence={result.evidence}"
        )
        assert cancelled.is_set() and not gate.is_set()
        assert provider.calls == 0 and result.usage.calls == 0
        assert RecordingDriver.seen[-1].observed_count == 0
        assert host._run.budget.original_deadline == deadline
        assert deadline <= time.monotonic() < deadline + host._grace + 2
    finally:
        gate.set()
        await _stop(host)


def manager_fixture(url, provider):
    from test_runner import manager

    return manager(url, provider)


async def test_cleanup_revokes_live_after_unknown_ack(
    counter_page_url, monkeypatch
) -> None:
    store, in_native, box = AdmissionStore(), asyncio.Event(), {}

    class LostAck(runner.DOMSession):
        async def _native(self, action):
            result = await super()._native(action)
            if action.operation == "click":
                assert store.is_live(box["pid"]) is True
                in_native.set()
                raise RuntimeError("ack lost")
            return result

    monkeypatch.setattr(runner, "DOMSession", LostAck)
    desk = DevelopmentDesk(store, ["accept"])
    provider = ScriptedProvider(["click"])
    RecordingDriver.seen.clear()
    host = RunManager(
        lambda: provider,
        lambda mode: RecordingDriver(mode),
        admission_store=store,
        desk=desk,
    )
    assert desk.development_only is True and host is not None
    run_id = host.start_task(request(counter_page_url, limits=Limits(max_seconds=15.0)))
    try:
        pending = await _pend(host, run_id)
        box["pid"] = pending.proposal_id
        await host.present_admission(run_id, box["pid"], pending.digest)
        async with asyncio.timeout(15):
            await in_native.wait()
        result = await finish(host, run_id)
        assert result.terminal == "outcome_unknown" and result.development_only is True
        assert result.effect_guarantee == "none" and provider.calls == 1
        assert RecordingDriver.seen[-1].observed_count == 1
        assert store.is_live(box["pid"]) is False
    finally:
        await _stop(host)


async def test_default_production_admission_fail_closed() -> None:
    trap = {"n": 0}

    def trapfactory():
        trap["n"] += 1
        raise AssertionError("lazy provider constructed")

    host = RunManager(provider_factory=trapfactory)
    try:
        run_id = host.start_task(_req(_URL, "Update the counter.", 3.0))
        assert type(run_id) is str and trap["n"] == 0
        early = host.get_task(run_id)
        assert early.development_only is False
        assert early.terminal == "queued" and early.reason == "preparing_admission"
        assert host.pending_proposal(run_id) is None
        assert early.usage.calls == 0 and early.usage.steps == 0
        pending = await _pend(host, run_id)
        for _ in range(4):
            view = host.get_task(run_id)
            view.artifact_ids.append("mutated")
            fresh = host.get_task(run_id)
            assert fresh.artifact_ids == [] and fresh.usage.calls == 0
            assert fresh.usage.steps == 0 and trap["n"] == 0
        blocked = await host.present_admission(
            run_id, pending.proposal_id, pending.digest
        )
        assert blocked.terminal == "running"
        assert blocked.reason == "finalizing"
        await _until(host, run_id, "blocked", 2)
        done = host.get_task(run_id)
        assert done.reason == "no_supported_human_grant_channel"
        assert done.development_only is False and done.effect_guarantee == "none"
        assert done.usage.calls == 0 and done.usage.steps == 0 and trap["n"] == 0
    finally:
        await _stop(host)


from decision_browser.decision_codec import Decision
from decision_browser.desk import DevelopmentDesk


class SlowProvider(ScriptedProvider):
    def __init__(self):
        super().__init__(["DONE"])
        self.calls = 0
        self.provider_entered = asyncio.Event()
        self.provider_release = asyncio.Event()

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
        self.provider_entered.set()
        await self.provider_release.wait()
        return Decision("DONE", None, 1.0)


async def test_original_deadline_cumulative_human_browser_provider(counter_page_url):
    t = {"now": 0.0}
    human_entered = asyncio.Event()
    human_release = asyncio.Event()

    async def before_response():
        human_entered.set()
        await human_release.wait()

    def clock():
        return t["now"]

    store = AdmissionStore(clock=clock)
    desk = DevelopmentDesk(store, ["accept"], before_response=before_response)
    provider = SlowProvider()
    host = RunManager(
        lambda: provider,
        lambda mode: RecordingDriver(mode),
        admission_store=store,
        desk=desk,
        clock=clock,
    )
    presentation = None
    try:
        run_id = host.start_task(
            request(counter_page_url, limits=Limits(max_seconds=120.0))
        )
        proposal = await _pend(host, run_id)
        presentation = asyncio.create_task(
            host.present_admission(run_id, proposal.proposal_id, proposal.digest)
        )
        async with asyncio.timeout(5):
            await human_entered.wait()
        t["now"] = 100.0
        human_release.set()
        async with asyncio.timeout(5):
            await presentation
        async with asyncio.timeout(5):
            await provider.provider_entered.wait()
        t["now"] = 121.0
        provider.provider_release.set()
        done = await finish(host, run_id)
        assert done.terminal == "limit_exceeded"
        assert done.usage.calls == provider.calls == 1
        assert done.usage.steps == 1
        assert RecordingDriver.seen[-1].observed_count == 0
        assert done.functional == "not_run"
        assert done.development_only is True
        assert done.effect_guarantee == "none"
    finally:
        human_release.set()
        provider.provider_release.set()
        if presentation is not None and not presentation.done():
            presentation.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                async with asyncio.timeout(5):
                    await presentation
        await _stop(host)


# Browser-free regressions for the integration helpers and provider watchdog.
@pytest.mark.parametrize("state", ["idle", "closed"])
async def test_recording_driver_close_without_page_is_safe(state) -> None:
    driver = RecordingDriver("isolated")
    # Failed/cancelled BrowserDriver.start leaves state closed and no page.
    driver._state = state
    await driver.close()
    assert driver.observed_count is None
    assert driver._state == "closed"


async def test_recording_driver_preserves_real_cleanup_failure(monkeypatch) -> None:
    from decision_browser.browser import BrowserDriver

    async def fail_close(self):
        raise RuntimeError("real cleanup failure")

    monkeypatch.setattr(BrowserDriver, "close", fail_close)
    with pytest.raises(RuntimeError, match="real cleanup failure"):
        await RecordingDriver("isolated").close()


async def test_until_reports_unexpected_public_terminal_promptly() -> None:
    driver = Life()
    host = RunManager(
        lambda: ScriptedProvider(["DONE"]),
        lambda _m: driver,
        development_config=DevelopmentConfig(_URL.rstrip("/"), frozenset()),
    )
    run_id = host.start_task(_req(_URL + "purchase", "Review the page.", 3.0))
    try:
        async with asyncio.timeout(0.5):
            with pytest.raises(AssertionError, match="blocked.*consequential_action_blocked"):
                await _until(host, run_id, "limit_exceeded", 2)
    finally:
        await _stop(host)


async def test_provider_watchdog_cancels_at_original_deadline_without_browser() -> None:
    provider, driver = HangProvider(), Life()

    class ProviderStageManager(RunManager):
        async def _body(self, run):
            self._set(run, terminal="running")
            run.provider = provider
            await provider.decide([], "fixture", budget=run.budget, privacy=run.privacy)

    host = ProviderStageManager(lambda: provider, lambda _m: driver, cleanup_grace=0.05)
    run_id = host.start_task(_req(_URL, "Update the counter.", 0.1))
    deadline = host._run.budget.original_deadline
    try:
        async with asyncio.timeout(1):
            await provider.entered.wait()
        assert provider.calls == 1
        await _until(host, run_id, "limit_exceeded", 1)
        got = host.get_task(run_id)
        assert got.reason == "limit_exceeded"
        assert got.usage.calls == 1 and got.usage.output_bytes == 1
        assert got.usage.input_bytes == len(b"fixture")
        assert provider.cancelled.is_set() and not provider.gate.is_set()
        assert host._run.budget.original_deadline == deadline
        assert deadline <= time.monotonic() < deadline + 1
        assert host._run.task.done() and driver.closes == 1
    finally:
        provider.gate.set()
        await _stop(host)


async def test_verifier_watchdog_cancels_at_original_deadline_without_browser(
    monkeypatch,
) -> None:
    entered, gate, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    provider, driver = ScriptedProvider(["DONE"]), Life()

    async def verify_functional(page, checks):
        assert page is driver.page
        assert checks == (check,)
        entered.set()
        try:
            await gate.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("verifier released")

    class VerificationStageManager(RunManager):
        async def _body(self, run):
            self._set(run, terminal="running")
            await self._verify(run)

    monkeypatch.setattr(runner, "verify_functional", verify_functional)
    host = VerificationStageManager(
        lambda: provider, lambda _m: driver, cleanup_grace=0.05,
    )
    check = Check(kind="text", target="#count", expected="1")
    run_id = host.start_task(request(_URL, [check], Limits(max_seconds=0.1)))
    deadline = host._run.budget.original_deadline
    try:
        async with asyncio.timeout(1):
            await entered.wait()
        assert not host._run.queued
        await _until(host, run_id, "limit_exceeded", 1)
        result = host.get_task(run_id)
        assert result.terminal == "limit_exceeded" and result.reason == "limit_exceeded"
        assert provider.calls == 0 and result.usage.calls == 0
        assert cancelled.is_set() and not gate.is_set()
        assert host._run.budget.original_deadline == deadline
        assert deadline <= time.monotonic() < deadline + 1
        assert host._run.task.done() and driver.closes == 1
    finally:
        gate.set()
        await _stop(host)
