"""Scripted standard MCP form channel. A scripted host response is not a human.

Uses the real AdmissionStore production record path. No browser, API, or
protocol process. Native human proof stays NOT_RUN.
"""

import asyncio
from types import SimpleNamespace

import pytest
from mcp import types
from pydantic import ValidationError

from decision_browser.admission import AdmissionStore
from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest, TaskResult
from decision_browser.desk import DeskUnavailable, ProductionDesk
from decision_browser.runner import RunManager, RunnerError
from decision_browser.server import StandardFormDesk, _mcp_ctx, create_server

_SCHEMA = {
    "type": "object",
    "properties": {"reviewed": {
        "type": "boolean",
        "title": "I reviewed the displayed action scope",
        "description": "Confirm the destinations, operations, limits and residual risks.",
    }},
    "required": ["reviewed"],
    "additionalProperties": False,
}


def _store(now: float = 10.0):
    clock = [now]
    return AdmissionStore(clock=lambda: clock[0], cleanup_grace=2.0), clock


def _alpha(store: AdmissionStore, run: str = "run-1"):
    return store.propose_alpha(
        run, ["https://example.test/"], ["click"], {"steps": 1}, 100.0, 50.0, False
    )


def _resp(action: str, content: object = None):
    return SimpleNamespace(action=action, content=content)


class ScriptedSession:
    def __init__(self, responses, capable: bool = True, before=None):
        self._responses = list(responses)
        self.capable = capable
        self.before = before
        self.calls = []
        self.capability = None
        self.client_capabilities = types.ClientCapabilities(
            elicitation=types.ElicitationCapability(form=types.FormElicitationCapability())
        )

    def check_client_capability(self, capability):
        self.capability = capability
        return self.capable

    async def elicit_form(self, message, requested_schema, related_request_id):
        self.calls.append(
            {
                "message": message,
                "schema": requested_schema,
                "related_request_id": related_request_id,
            }
        )
        if self.before is not None:
            await self.before()
        return self._responses.pop(0)


async def _present(desk, proposal, session, request_id="req-1"):
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(session=session), request_id=request_id
    )
    token = _mcp_ctx.set(ctx)
    try:
        return await desk.present(proposal)
    finally:
        _mcp_ctx.reset(token)


def _manager_of(server):
    tools = server._tool_manager._tools
    for name in ("get_task", "start_task", "present_admission", "capabilities"):
        fn = tools[name].fn
        seen = 0
        while fn is not None and seen < 6:
            for cell in getattr(fn, "__closure__", None) or ():
                try:
                    value = cell.cell_contents
                except ValueError:
                    continue
                if isinstance(value, RunManager):
                    return value
            fn = getattr(fn, "__wrapped__", None)
            seen += 1
    raise AssertionError("shared manager")


def test_shared_store_clock_and_grace():
    server = create_server(None)
    manager = _manager_of(server)
    store = manager._admission
    assert isinstance(manager._desk, StandardFormDesk)
    assert manager._desk._store is store
    assert store._cleanup_grace == 2.0
    assert manager._grace == 2.0
    assert store._clock is time_monotonic()
    assert manager._clock is time_monotonic()
    assert type(manager._desk).__name__ != "ProductionDesk"


def time_monotonic():
    import time

    return time.monotonic


async def test_capabilities_name_the_form_channel(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    body = (await create_server().call_tool("capabilities", {})).structured_content
    assert body["human_grant_channel"] == "standard_mcp_form"
    assert body["production_ready"] is False
    assert body["native_human"] == "NOT_RUN"
    assert body["live_ac10"] == "NOT_RUN"
    assert body["both_modes"] == "NOT_RUN"
    assert body["form_candidate_unverified"] is True
    assert body["provider_ready"] is False
    tools = await create_server().list_tools()
    present = next(tool for tool in tools if tool.name == "present_admission")
    assert "grants nothing" not in (present.description or "")
    assert "Standard MCP form" in (present.description or "")


async def test_capability_gate_and_request_id():
    store, _clock = _store()
    proposal = _alpha(store)
    desk = StandardFormDesk(store)
    with pytest.raises(DeskUnavailable) as caught:
        await desk.present(proposal)
    assert caught.value.reason == "no_supported_human_grant_channel"
    blind = ScriptedSession([], capable=False)
    with pytest.raises(DeskUnavailable):
        await _present(desk, proposal, blind)
    assert blind.calls == []
    session = ScriptedSession([_resp("accept", {"reviewed": True})])
    assert await _present(desk, proposal, session, "req-7") is True
    assert session.calls[0]["related_request_id"] == "req-7"
    assert session.calls[0]["schema"] == _SCHEMA
    assert "candidate grants nothing" not in session.calls[0]["message"]
    assert "standard MCP form" in session.calls[0]["message"]
    assert proposal.digest in session.calls[0]["message"]
    assert session.capability is not None
    assert store._dev[proposal.proposal_id] is False
    assert store.is_live(proposal.proposal_id)


async def test_accept_writes_nondev_record_once():
    store, _clock = _store()
    proposal = _alpha(store)
    desk = StandardFormDesk(store)
    session = ScriptedSession([_resp("accept", {"reviewed": True})])
    assert await _present(desk, proposal, session) is True
    assert store.get_proposal(proposal.proposal_id) is proposal
    assert store.is_live(proposal.proposal_id) is True
    assert store._dev[proposal.proposal_id] is False
    assert len(store._records) == 1
    assert await _present(desk, proposal, ScriptedSession([])) is False
    assert store.accept_standard_form(proposal.proposal_id, proposal.digest) is False
    assert len(store._records) == 1


def _task_request() -> TaskRequest:
    return TaskRequest(
        goal="Read artificial page",
        start_url="https://example.test/",
        mode="isolated",
        values={},
        checks=[],
        viewports=[{"width": 390, "height": 844}],
        limits=Limits(
            max_steps=12,
            max_seconds=30.0,
            max_provider_calls=12,
            max_input_bytes=262144,
            max_output_bytes=262144,
            max_stored_bytes=10485760,
        ),
        privacy=PrivacyPolicy(blocked_literals=[], send_images=False),
        visual=False,
    )


class _IdleProvider:
    async def decide(self, *args, **kwargs):
        raise AssertionError("decide")

    async def generate_text(self, *args, **kwargs):
        raise AssertionError("generate_text")

    async def aclose(self) -> None:
        return None


class _GateDriver:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.page = None
        self.cleanup_errors = []
        self.residual_owned = False
        self.foreign_mismatch = False

    async def start(self) -> None:
        self.started.set()
        await self.release.wait()

    async def close(self) -> None:
        return None


class _BareTrueDesk:
    """Returns True and writes no AdmissionStore record."""

    @property
    def development_only(self) -> bool:
        return False

    async def present(self, proposal) -> bool:
        del proposal
        return True


def _bound_manager(store, desk, driver):
    return RunManager(
        lambda: _IdleProvider(),
        lambda mode: driver,
        admission_store=store,
        desk=desk,
        clock=store._clock,
        cleanup_grace=2.0,
    )


async def _await_pending(manager: RunManager, run_id: str):
    for _ in range(50):
        if manager.get_task(run_id).terminal == "awaiting_human_admission":
            pending = manager.pending_proposal(run_id)
            if pending is not None:
                return pending
        await asyncio.sleep(0)
    raise AssertionError("pending")


@pytest.mark.parametrize("goal", [
    "Найти рейсы. Не переходить к покупке или бронированию.",
    "Find flights. Do not purchase or book.",
    "purchase",
])
async def test_goal_words_do_not_block_safe_bootstrap_or_grant_admission(goal):
    store, _clock = _store()
    driver = _GateDriver("isolated")
    manager = _bound_manager(store, StandardFormDesk(store), driver)
    request = _task_request().model_copy(update={"goal": goal})
    run_id = manager.start_task(request)
    try:
        pending = await _await_pending(manager, run_id)
        result = manager.get_task(run_id)
        assert result.terminal == "awaiting_human_admission"
        assert result.usage.calls == result.usage.steps == 0
        assert not driver.started.is_set()
        assert not store.is_live(pending.proposal_id)
        assert store._records == {}
    finally:
        await manager.aclose()


async def _present_on_manager(manager, session, run_id, proposal_id, digest):
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(session=session), request_id="req-manager"
    )
    token = _mcp_ctx.set(ctx)
    try:
        return await manager.present_admission(run_id, proposal_id, digest)
    finally:
        _mcp_ctx.reset(token)


@pytest.mark.parametrize(
    "kind",
    ["accept", "decline", "reviewed_false", "cancel"],
)
async def test_real_manager_present_admission_uses_standard_form_record(kind):
    now = [1_000.0]
    store = AdmissionStore(clock=lambda: now[0], cleanup_grace=2.0)
    desk = StandardFormDesk(store)
    driver = _GateDriver("isolated")
    manager = _bound_manager(store, desk, driver)
    assert manager._desk._store is store
    assert manager._grace == 2.0
    assert store._cleanup_grace == 2.0
    assert type(manager._desk).__name__ == "StandardFormDesk"
    run_id = manager.start_task(_task_request())
    try:
        pending = await _await_pending(manager, run_id)
        assert store._dev[pending.proposal_id] is False
        kept = store.propose_alpha(
            "kept-run",
            ["https://kept.test/"],
            ["click"],
            {"steps": 1},
            100_000.0,
            50_000.0,
            False,
        )
        extra = store.propose_alpha(
            run_id,
            ["https://example.test/other"],
            ["wait"],
            {"steps": 1},
            pending.deadline,
            pending.expires_at,
            False,
        )
        if kind == "accept":
            response = _resp("accept", {"reviewed": True})
        elif kind == "decline":
            response = _resp("decline", None)
        elif kind == "reviewed_false":
            response = _resp("accept", {"reviewed": False})
        else:
            response = _resp("cancel", None)
        session = ScriptedSession([response])
        shown = await _present_on_manager(
            manager, session, run_id, pending.proposal_id, pending.digest
        )
        assert isinstance(shown, TaskResult)
        assert shown.run_id == run_id
        assert shown.reason != "no_supported_human_grant_channel"
        assert len(session.calls) == 1
        if kind == "accept":
            assert store.get_proposal(pending.proposal_id) is pending
            assert store.is_live(pending.proposal_id) is True
            assert store._state[extra.proposal_id] == "pending"
            assert "kept-run" not in store._revoked
            record = store._records[pending.proposal_id]
            progressed = False
            for _ in range(20):
                follow = manager.get_task(run_id)
                if (
                    driver.started.is_set()
                    and follow.terminal == "running"
                    and manager.pending_proposal(run_id) is None
                ):
                    progressed = True
                    break
                await asyncio.sleep(0)
            assert progressed is True
            assert manager._run is not None and manager._run.admission_accepted is True
            follow = manager.get_task(run_id)
            assert follow.terminal == "running"
            assert follow.development_only is False
            assert follow.effect_guarantee == "none"
            with pytest.raises(RunnerError) as caught:
                await _present_on_manager(
                    manager,
                    ScriptedSession([_resp("accept", {"reviewed": True})]),
                    run_id,
                    pending.proposal_id,
                    pending.digest,
                )
            assert caught.value.code == "presentation_resolved"
            assert store._records[pending.proposal_id] == record
            assert len(store._records) == 1
            assert store.get_proposal(pending.proposal_id) is pending
        elif kind == "cancel":
            assert shown.terminal == "running"
            assert shown.reason == "finalizing"
            assert run_id in store._revoked
            assert "kept-run" not in store._revoked
            assert store._state[pending.proposal_id] == "revoked"
            assert store._state[extra.proposal_id] == "revoked"
            assert store._state[kept.proposal_id] == "pending"
            assert pending.proposal_id not in store._records
            assert manager.get_task(run_id).terminal == "running"
            with pytest.raises(RunnerError) as caught:
                await _present_on_manager(
                    manager, ScriptedSession([]), run_id, pending.proposal_id, pending.digest
                )
            assert caught.value.code == "presentation_resolved"
            assert pending.proposal_id not in store._records
        else:
            assert shown.terminal == "running"
            assert shown.reason == "finalizing"
            assert store._state[pending.proposal_id] == "cancelled"
            assert store._state[extra.proposal_id] == "pending"
            assert store._state[kept.proposal_id] == "pending"
            assert run_id not in store._revoked
            assert "kept-run" not in store._revoked
            assert pending.proposal_id not in store._records
            assert store.is_live(pending.proposal_id) is False
            follow = manager.get_task(run_id)
            assert follow.terminal == "running"
            assert follow.reason == "finalizing"
            with pytest.raises(RunnerError) as caught:
                await _present_on_manager(
                    manager,
                    ScriptedSession([_resp("accept", {"reviewed": True})]),
                    run_id,
                    pending.proposal_id,
                    pending.digest,
                )
            assert caught.value.code == "presentation_resolved"
            assert store._state[pending.proposal_id] == "cancelled"
            assert run_id not in store._revoked
            assert pending.proposal_id not in store._records
        if kind != "accept":
            async with asyncio.timeout(2):
                while manager.get_task(run_id).terminal == "running":
                    await asyncio.sleep(0)
            final = manager.get_task(run_id)
            assert final.terminal == "blocked"
            assert final.reason == "blocked"
    finally:
        driver.release.set()
        task = manager._run.task if manager._run is not None else None
        if task is not None and not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def test_bare_desk_true_without_record_fails_real_manager():
    now = [1_000.0]
    store = AdmissionStore(clock=lambda: now[0], cleanup_grace=2.0)
    driver = _GateDriver("isolated")
    manager = _bound_manager(store, _BareTrueDesk(), driver)
    run_id = manager.start_task(_task_request())
    try:
        pending = await _await_pending(manager, run_id)
        shown = await manager.present_admission(run_id, pending.proposal_id, pending.digest)
        assert isinstance(shown, TaskResult)
        assert shown.terminal == "running"
        assert shown.reason == "finalizing"
        assert shown.reason != "no_supported_human_grant_channel"
        assert store.is_live(pending.proposal_id) is False
        assert store._state[pending.proposal_id] == "pending"
        assert pending.proposal_id not in store._records
        assert manager._run is not None and manager._run.admission_accepted is False
        follow = manager.get_task(run_id)
        assert follow.terminal == "running"
        assert follow.reason == "finalizing"
        async with asyncio.timeout(2):
            while manager.get_task(run_id).terminal == "running":
                await asyncio.sleep(0)
        final = manager.get_task(run_id)
        assert final.terminal == "blocked"
        assert final.reason == "blocked"
    finally:
        task = manager._run.task if manager._run is not None else None
        if task is not None and not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.parametrize("action,content", [("decline", None), ("decline", {"reviewed": True}), ("accept", {"reviewed": False})])
async def test_decline_and_reviewed_false_cancel_only_proposal(action, content):
    store, _clock = _store()
    proposal = _alpha(store)
    other = _alpha(store, "run-1")
    desk = StandardFormDesk(store)
    session = ScriptedSession([_resp(action, content)])
    assert await _present(desk, proposal, session) is False
    assert store._state[proposal.proposal_id] == "cancelled"
    assert store._state[other.proposal_id] == "pending"
    assert proposal.run_id not in store._revoked
    assert store.is_live(proposal.proposal_id) is False


async def test_form_cancel_revokes_run_only():
    store, _clock = _store()
    proposal = _alpha(store)
    other = _alpha(store)
    kept = _alpha(store, "run-2")
    desk = StandardFormDesk(store)
    session = ScriptedSession([_resp("cancel", {"reviewed": True})])
    assert await _present(desk, proposal, session) is False
    assert proposal.run_id in store._revoked
    assert store._state[proposal.proposal_id] == "revoked"
    assert store._state[other.proposal_id] == "revoked"
    assert store._state[kept.proposal_id] == "pending"
    assert "run-2" not in store._revoked


@pytest.mark.parametrize(
    "content",
    [None, {}, {"reviewed": 1}, {"reviewed": "true"}, {"reviewed": "True"}, {"reviewed": True, "extra": 1}],
)
async def test_malformed_accept_does_not_record(content):
    store, _clock = _store()
    proposal = _alpha(store)
    desk = StandardFormDesk(store)
    session = ScriptedSession([_resp("accept", content)])
    assert await _present(desk, proposal, session) is False
    assert store._state[proposal.proposal_id] == "pending"
    assert proposal.proposal_id not in store._records
    with pytest.raises(ValidationError):
        from decision_browser.server import Confirm

        Confirm.model_validate(content)


@pytest.mark.parametrize("action", ["auto_accept", "approve", ""])
async def test_other_actions_do_not_record(action):
    store, _clock = _store()
    proposal = _alpha(store)
    desk = StandardFormDesk(store)
    assert await _present(desk, proposal, ScriptedSession([_resp(action, {"reviewed": True})])) is False
    assert store._state[proposal.proposal_id] == "pending"


async def test_expired_revoked_dev_wrong_object_and_stale_state():
    store, clock = _store()
    proposal = _alpha(store)
    desk = StandardFormDesk(store)
    clock[0] = 60.0
    assert await _present(desk, proposal, ScriptedSession([])) is False
    clock[0] = 10.0
    assert store._state[proposal.proposal_id] == "pending"
    store.revoke(proposal.run_id)
    assert await _present(desk, proposal, ScriptedSession([])) is False
    dev_store, _dev_clock = _store()
    dev = dev_store.propose_alpha(
        "run-d", ["https://example.test/"], ["click"], {"steps": 1}, 100.0, 50.0, True
    )
    assert await _present(StandardFormDesk(dev_store), dev, ScriptedSession([])) is False
    assert dev_store._dev[dev.proposal_id] is True
    fresh, _fresh_clock = _store()
    current = _alpha(fresh)
    foreign = _alpha(fresh, "run-z")
    fresh._proposals[current.proposal_id] = foreign
    assert await _present(StandardFormDesk(fresh), current, ScriptedSession([])) is False
    stale, _stale_clock = _store()
    held = _alpha(stale)
    stale._state[held.proposal_id] = "cancelled"
    assert await _present(StandardFormDesk(stale), held, ScriptedSession([])) is False


async def test_expiry_during_elicit_does_not_record():
    store, clock = _store()
    proposal = _alpha(store)

    async def _expire():
        clock[0] = proposal.expires_at

    desk = StandardFormDesk(store)
    session = ScriptedSession([_resp("accept", {"reviewed": True})], before=_expire)
    assert await _present(desk, proposal, session) is False
    assert len(session.calls) == 1
    assert proposal.proposal_id not in store._records
    assert store.is_live(proposal.proposal_id) is False
    assert await _present(
        desk, proposal, ScriptedSession([_resp("accept", {"reviewed": True})])
    ) is False
    assert proposal.proposal_id not in store._records
    assert store.accept_standard_form(proposal.proposal_id, proposal.digest) is False


async def test_revoke_during_elicit_does_not_record():
    store, _clock = _store()
    proposal = _alpha(store)

    async def _revoke():
        store.revoke(proposal.run_id)

    desk = StandardFormDesk(store)
    session = ScriptedSession([_resp("accept", {"reviewed": True})], before=_revoke)
    assert await _present(desk, proposal, session) is False
    assert len(session.calls) == 1
    assert proposal.run_id in store._revoked
    assert proposal.proposal_id not in store._records
    assert store.is_live(proposal.proposal_id) is False
    assert await _present(
        desk, proposal, ScriptedSession([_resp("accept", {"reviewed": True})])
    ) is False
    assert proposal.proposal_id not in store._records
    assert store.accept_standard_form(proposal.proposal_id, proposal.digest) is False


async def test_swap_during_elicit_does_not_record():
    store, _clock = _store()
    proposal = _alpha(store)
    other = _alpha(store, "run-2")

    async def _swap():
        store._proposals[proposal.proposal_id] = other

    desk = StandardFormDesk(store)
    session = ScriptedSession([_resp("accept", {"reviewed": True})], before=_swap)
    assert await _present(desk, proposal, session) is False
    assert store.is_live(proposal.proposal_id) is False
    assert store.is_live(other.proposal_id) is False


async def test_concurrent_accept_is_one_shot():
    store, _clock = _store()
    proposal = _alpha(store)
    desk = StandardFormDesk(store)
    gate = asyncio.Event()
    entered = 0

    class Session:
        client_capabilities = types.ClientCapabilities(
            elicitation=types.ElicitationCapability(form=types.FormElicitationCapability())
        )

        def check_client_capability(self, capability):
            return True

        async def elicit_form(self, message, requested_schema, related_request_id):
            nonlocal entered
            entered += 1
            if entered == 2:
                gate.set()
            await gate.wait()
            return _resp("accept", {"reviewed": True})

    session = Session()
    results = await asyncio.gather(
        _present(desk, proposal, session, "a"), _present(desk, proposal, session, "b")
    )
    assert sorted(results) == [False, True]
    assert store.is_live(proposal.proposal_id)
    assert store.accept_standard_form(proposal.proposal_id, proposal.digest) is False


async def test_production_desk_still_fail_closed():
    store, _clock = _store()
    proposal = _alpha(store)
    with pytest.raises(DeskUnavailable) as caught:
        await ProductionDesk().present(proposal)
    assert caught.value.reason == "no_supported_human_grant_channel"
    assert store.is_live(proposal.proposal_id) is False


async def test_beta_accept_uses_standard_form_api():
    store, _clock = _store()
    proposal = store.propose_beta(
        "run-b",
        "finger",
        "click",
        "https://example.test/",
        "https://example.test/",
        "https://example.test/a",
        100.0,
        50.0,
        False,
    )
    desk = StandardFormDesk(store)
    assert await _present(desk, proposal, ScriptedSession([_resp("accept", {"reviewed": True})])) is True
    assert store._dev[proposal.proposal_id] is False
    assert store.is_live(proposal.proposal_id)
