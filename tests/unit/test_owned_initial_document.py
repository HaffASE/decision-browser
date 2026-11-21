from __future__ import annotations

import asyncio
import inspect
import time
from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError

from decision_browser.artifacts import ArtifactError, ArtifactStore, Journal
from decision_browser.browser import (
    BrowserBlocked,
    BrowserDriver,
    OwnedDocumentBody,
    _document_response_headers,
    _expected_document_pause,
    _owned_main_frame,
)
from decision_browser.budget import Budget, BudgetExceeded
from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.dom import (
    Action,
    DispatchResult,
    DOMSession,
    GuardResult,
    Observation,
)
from decision_browser.owned_fixture import CONTENT_IDS, resolve
from decision_browser.owned_fixture_server import CleanupReport, OwnedFixtureResource
from decision_browser.runner import (
    _OWNED_INITIAL_CONTENT_ID,
    RunManager,
    RunnerError,
    _OwnedLoopbackTicket,
)

_FRAME = "frame-main"
_TARGET = "target-owned"
_REQUEST = "req-owned-document"
_STREAM = "stream-handle-1"


class _Session:
    def __init__(self, reads: list[dict[str, object]], stream: str = _STREAM) -> None:
        self.reads = list(reads)
        self.stream = stream
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.fulfilled = asyncio.Event()
        self._on_paused: object = None
        self.fail_disable = False
        self.raise_disable = False
        self.raise_detach = False
        self.extra_during_read = False
        self.late_during_fulfill = False
        self.detached = False
        self._extra_sent = False

    def on(self, event: str, handler: object) -> None:
        if event == "Fetch.requestPaused":
            self._on_paused = handler

    def emit(self, event: dict[str, object]) -> None:
        handler = self._on_paused
        if callable(handler):
            handler(event)

    async def detach(self) -> None:
        if self.raise_detach:
            raise BrowserBlocked("cleanup_uncertain")
        self.detached = True

    async def send(self, method: str, params: dict[str, object] | None = None) -> object:
        payload = dict(params or {})
        self.calls.append((method, payload))
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": _TARGET}}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": _FRAME}}}
        if method == "Fetch.takeResponseBodyAsStream":
            return {"stream": self.stream}
        if method == "IO.read":
            if self.extra_during_read and not self._extra_sent:
                self._extra_sent = True
                self.emit(_pause_event("http://127.0.0.1/ignored", request_id="req-extra"))
            if not self.reads:
                return {"base64Encoded": False, "data": "", "eof": False}
            return self.reads.pop(0)
        if method == "Fetch.fulfillRequest":
            if self.late_during_fulfill:
                self.emit(_pause_event("http://127.0.0.1/late", request_id="req-late"))
            self.fulfilled.set()
            return {}
        if method == "Fetch.disable" and self.raise_disable:
            raise RuntimeError("disable")
        if method == "Fetch.disable" and self.fail_disable:
            raise BrowserBlocked("cleanup_uncertain")
        return {}


class _Context:
    def __init__(self, session: _Session) -> None:
        self.session = session
        self.pages: list[object] = []
        self.cdp_sessions = 0

    async def new_cdp_session(self, page: object) -> _Session:
        self.cdp_sessions += 1
        return self.session

    async def close(self) -> None:
        return None


class _Page:
    def __init__(self, session: _Session) -> None:
        self.session = session
        self.url = "about:blank"
        self.context = _Context(session)
        self.context.pages.append(self)
        self.gotos: list[str] = []
        self.emit_on_goto = True
        self.hang_goto = False
        self.hang_task: asyncio.Task[object] | None = None
        self._hang_release = asyncio.Event()
        self.driver: BrowserDriver | None = None

    def release_hang(self) -> None:
        """Test-only release of the cancellation-resistant fake goto."""
        self._hang_release.set()

    async def goto(self, url: str, **kwargs: object) -> None:
        self.gotos.append(url)
        if self.hang_goto:
            current = asyncio.current_task()
            if isinstance(current, asyncio.Task):
                self.hang_task = current
            while not self._hang_release.is_set():
                try:
                    await self._hang_release.wait()
                except asyncio.CancelledError:
                    task = asyncio.current_task()
                    if isinstance(task, asyncio.Task):
                        while task.cancelling():
                            task.uncancel()
            task = asyncio.current_task()
            if isinstance(task, asyncio.Task):
                while task.cancelling():
                    task.uncancel()
        if self.emit_on_goto and len(self.gotos) == 1:
            self.session.emit(_pause_event(url))
            await self._until_fulfill_or_waiter()
        self.url = url

    async def _until_fulfill_or_waiter(self) -> None:
        driver = self.driver
        arm = getattr(driver, "_owned_arm", None)
        waiter = getattr(arm, "waiter", None)
        deadline = getattr(arm, "deadline", time.monotonic())
        pending: set[asyncio.Future[object]] = {
            asyncio.ensure_future(self.session.fulfilled.wait())
        }
        if isinstance(waiter, asyncio.Task):
            pending.add(waiter)
        timeout = deadline - time.monotonic()
        timeout = max(timeout, 0)
        _done, still = await asyncio.wait(pending, timeout=timeout)
        for item in still:
            if item is not waiter:
                item.cancel()


class _DOM(DOMSession):
    def __init__(self, page: _Page, status: str, note: object) -> None:
        super().__init__(page, note_navigation=note)
        self.status = status
        self.action: Action | None = None

    async def observe(self) -> Observation:
        return Observation("obs", "null", "doc", "", {}, ())

    async def prepare(
        self,
        op: str,
        token: str | None = None,
        *,
        value: object = None,
        destination: str | None = None,
    ) -> Action:
        action = Action("navigate", None, None, destination, "obs", "{\"semantic\":{}}")
        self.action = action
        return action

    async def guard(self, action: Action) -> GuardResult:
        return GuardResult(self.status, "" if self.status == "pass" else self.status)

    async def dispatch(self, action: Action, before_dispatch: object = None) -> DispatchResult:
        return await DOMSession.dispatch(self, action, before_dispatch)


class _RaisingJournal(Journal):
    def before_dispatch(
        self,
        run_id: str,
        action_digest: str,
        semantic_fingerprint: str,
        admission_class: str,
    ) -> str:
        raise ArtifactError("over_bytes")


class _EmptyJournal(Journal):
    def before_dispatch(
        self,
        run_id: str,
        action_digest: str,
        semantic_fingerprint: str,
        admission_class: str,
    ) -> str:
        return ""


class _OutcomeJournal(Journal):
    def outcome(self, run_id: str, attempt_id: str, status: str) -> str:
        raise ArtifactError("over_bytes")


def _limits(seconds: float = 30.0, steps: int = 4) -> Limits:
    return Limits(max_steps=steps, max_seconds=seconds)


def _request() -> TaskRequest:
    return TaskRequest(
        goal="owned clean document",
        start_url="https://www.google.com/travel/flights",
        values={},
        checks=[],
        limits=_limits(),
        privacy=PrivacyPolicy(),
    )


def _pause_event(url: str, request_id: str = _REQUEST) -> dict[str, object]:
    return {
        "requestId": request_id,
        "frameId": _FRAME,
        "resourceType": "Document",
        "responseStatusCode": 200,
        "request": {"url": url, "method": "GET"},
        "responseHeaders": [
            {"name": "Content-Type", "value": "text/html;charset=utf-8"},
            {"name": "Cookie", "value": "session=secret"},
            {"name": "Set-Cookie", "value": "a=b"},
            {"name": "Authorization", "value": "Bearer z"},
        ],
    }


def _chunk(text: str, *, eof: bool = True, encoded: bool = False) -> dict[str, object]:
    return {"base64Encoded": encoded, "data": text, "eof": eof}


def _payloads(session: _Session, method: str) -> list[dict[str, object]]:
    return [payload for name, payload in session.calls if name == method]


async def _bind(
    seconds: float = 30.0,
    steps: int = 4,
    status: str = "pass",
    reads: list[dict[str, object]] | None = None,
    journal: Journal | None = None,
    emit: bool = True,
    stream: str = _STREAM,
) -> tuple[RunManager, object, BrowserDriver, _Page, _Session, _DOM]:
    document = resolve(_OWNED_INITIAL_CONTENT_ID)
    assert document is not None
    body = document.body.encode("utf-8")
    session = _Session(reads if reads is not None else [_chunk(body.decode("ascii"))], stream)
    page = _Page(session)
    driver = BrowserDriver("isolated")
    driver._state = "open"
    driver._page = page
    driver._context = page.context
    driver._owned_page_target_id = _TARGET
    driver.owned_target_id = _TARGET
    page.driver = driver
    page.emit_on_goto = emit
    manager = RunManager(lambda: None, lambda mode: driver)
    run = manager._open(_request())
    run.request = TaskRequest(
        goal="owned clean document",
        start_url="https://www.google.com/travel/flights",
        values={},
        checks=[],
        limits=_limits(seconds, steps),
        privacy=PrivacyPolicy(),
    )
    run.budget = Budget(run.request.limits, time.monotonic)
    manager._run = run
    if journal is not None:
        run.journal = journal
    dom = _DOM(page, status, lambda: manager._on_owned_navigation(run))
    run.dom = dom
    return manager, run, driver, page, session, dom


async def test_positive_stream_slots_fulfill_before_url_and_listener_stays_up() -> None:
    """In-process fake. Not Chromium pause or native fulfill-before-commit proof."""
    assert _OWNED_INITIAL_CONTENT_ID == CONTENT_IDS["clean"]
    manager, run, driver, page, session, dom = await _bind()
    policy_calls = 0
    grant_calls = 0
    real_evaluate = run.policy.evaluate

    def evaluate(*args: object, **kwargs: object) -> object:
        nonlocal policy_calls
        policy_calls += 1
        return real_evaluate(*args, **kwargs)

    def grant(*args: object, **kwargs: object) -> None:
        nonlocal grant_calls
        grant_calls += 1

    run.policy.evaluate = evaluate
    manager._grant = grant
    proposals = len(manager._admission._proposals)
    await manager.bind_owned_initial_document(run)
    seal = run.owned_initial_seal
    assert seal is not None
    assert seal.page is page and seal.budget is run.budget and seal.run is run
    assert seal.resource is run.owned_resource
    assert seal.content_id == _OWNED_INITIAL_CONTENT_ID
    assert seal.document_url == run.owned_resource.url
    assert "body" not in seal.__dataclass_fields__
    assert page.url == run.owned_resource.url and page.gotos == [run.owned_resource.url]
    assert run.budget.usage().steps == 1 and run.queued is False
    assert policy_calls == 0 and grant_calls == 0
    assert len(manager._admission._proposals) == proposals
    assert run.owned_resource.cleanup_status is None
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", urlsplit(run.owned_resource.url).port), 1
    )
    writer.close()
    await writer.wait_closed()
    del reader
    take = _payloads(session, "Fetch.takeResponseBodyAsStream")
    read = _payloads(session, "IO.read")
    close = _payloads(session, "IO.close")
    fulfill = _payloads(session, "Fetch.fulfillRequest")
    assert take[0]["requestId"] == _REQUEST
    assert read[0]["handle"] == _STREAM and read[0]["size"] == 16384
    assert close[0]["handle"] == _STREAM
    assert fulfill[0]["requestId"] == _REQUEST
    names = [name for name, _payload in session.calls]
    assert names.index("Fetch.fulfillRequest") < names.index("Fetch.disable")
    stored = run.store.read(run.attempt_id or "")
    assert b"owned_loopback_document" in stored.content
    assert b"acknowledged" in stored.content or any(
        b"acknowledged" in run.store.read(item).content for item in run.result.artifact_ids
    )
    assert run.result.effect_guarantee == "none"
    assert OwnedDocumentBody(sha256="ab", body=b"a", response_headers=()).proof_possible is False
    steps = run.budget.usage().steps
    with pytest.raises(RunnerError) as replay:
        manager._owned_initial_before(run, dom.action)
    assert replay.value.code == "blocked" and run.budget.usage().steps == steps
    assert len(page.gotos) == 1 and driver.owned_initial_arm_active() is False
    await run.owned_resource.close()


async def test_pause_after_enable_before_note_fails_closed() -> None:
    manager, run, _driver, page, session, dom = await _bind(seconds=2.0, emit=False)

    async def guard(action: Action) -> GuardResult:
        session.emit(_pause_event(action.destination or ""))
        await asyncio.sleep(0)
        return GuardResult("pass", "")

    dom.guard = guard
    with pytest.raises(BrowserBlocked) as caught:
        await manager.bind_owned_initial_document(run)
    assert caught.value.code == "cleanup_uncertain"
    assert run.owned_initial_seal is None and run.owned_ticket is None
    assert page.gotos == [] and _payloads(session, "Fetch.fulfillRequest") == []


async def test_zero_pause_and_original_deadline_cancellation() -> None:
    manager, run, _driver, page, session, _dom = await _bind(seconds=0.6, emit=False)
    started = time.monotonic()
    deadline = run.budget.original_deadline
    with pytest.raises(BrowserBlocked) as caught:
        await manager.bind_owned_initial_document(run)
    assert caught.value.code == "cleanup_uncertain"
    assert time.monotonic() - started < 1.5
    assert run.budget.original_deadline == deadline
    assert run.owned_initial_seal is None and len(page.gotos) == 1
    assert run.queued is True
    assert _payloads(session, "Fetch.fulfillRequest") == []
    manager_h, run_h, driver_h, page_h, session_h, _domh = await _bind(seconds=0.4, emit=False)
    page_h.hang_goto = True
    try:
        with pytest.raises(BrowserBlocked) as hanging:
            await manager_h.bind_owned_initial_document(run_h)
        assert hanging.value.code == "cleanup_uncertain"
        assert run_h.queued is True and run_h.owned_initial_seal is None
        owned_goto = page_h.hang_task
        assert (
            owned_goto is not None
            and owned_goto in driver_h._owned_tasks
            and not owned_goto.done()
        )
        assert len(page_h.gotos) == 1
        assert session_h.calls
    finally:
        page_h.release_hang()
        owned_goto = page_h.hang_task
        if isinstance(owned_goto, asyncio.Task):
            _done, pending_hang = await asyncio.wait({owned_goto}, timeout=1)
            assert not pending_hang and owned_goto.done()
            if not owned_goto.cancelled():
                owned_goto.exception()


async def test_journal_raise_and_empty_id_do_not_call_apply() -> None:
    manager, run, _driver, page, _session, _dom = await _bind(journal=_RaisingJournal(ArtifactStore(1024)))
    with pytest.raises(RunnerError) as raised:
        await manager.bind_owned_initial_document(run)
    assert raised.value.code == "outcome_unknown"
    assert run.owned_ticket is not None and run.owned_ticket.state == "issued"
    assert run.budget.usage().steps == 0
    assert run.queued is False and page.gotos == [] and run.owned_initial_seal is None
    deadline = run.budget.original_deadline
    manager._apply(run, RunnerError("outcome_unknown"))
    assert run.result.terminal == "outcome_unknown"
    assert run.budget.original_deadline == deadline
    manager2, run2, _d2, page2, _s2, _dom2 = await _bind(journal=_EmptyJournal(ArtifactStore(1024)))
    with pytest.raises(RunnerError) as missing:
        await manager2.bind_owned_initial_document(run2)
    assert missing.value.code == "outcome_unknown"
    assert run2.queued is False and page2.gotos == []


async def test_post_id_bookkeeping_and_outcome_ack() -> None:
    manager, run, _driver, page, _session, _dom = await _bind()
    real_set = manager._set

    def boom(target: object, **fields: object) -> object:
        if fields.get("admission_class") == "owned_loopback_document":
            raise RuntimeError("bookkeeping")
        return real_set(target, **fields)

    manager._set = boom
    with pytest.raises(RunnerError) as booked:
        await manager.bind_owned_initial_document(run)
    assert booked.value.code == "outcome_unknown"
    assert run.queued is True and page.gotos == [] and run.owned_initial_seal is None
    manager3, run3, _d3, page3, _s3, _dom3 = await _bind(journal=_OutcomeJournal(ArtifactStore(65536)))
    with pytest.raises(RunnerError) as outcome:
        await manager3.bind_owned_initial_document(run3)
    assert outcome.value.code == "outcome_unknown"
    assert run3.queued is True and run3.owned_initial_seal is None and len(page3.gotos) == 1


async def test_real_predicates_headers_cap_base64_and_missing_eof() -> None:
    url = "http://127.0.0.1:9/token"
    request_id, headers = _expected_document_pause(
        _pause_event(url), expected_url=url, main_frame_id=_FRAME
    )
    assert request_id == _REQUEST
    assert headers == (("Content-Type", "text/html;charset=utf-8"),)
    secret = _document_response_headers(
        {
            "responseHeaders": [
                {"name": "Cookie", "value": "c"},
                {"name": "Set-Cookie", "value": "s"},
                {"name": "Authorization", "value": "a"},
                {"name": "Content-Type", "value": "text/plain"},
            ]
        }
    )
    assert secret == (("Content-Type", "text/plain"),)
    with pytest.raises(BrowserBlocked):
        _owned_main_frame({"frameTree": {"frame": {"id": ""}}})
    piece = "a" * 16384
    over = [_chunk(piece, eof=False) for _ in range(4)]
    over.append(_chunk("b", eof=True))
    manager_over, run_over, _d, page_over, session_over, _dom = await _bind(seconds=2.0, reads=over)
    with pytest.raises(BrowserBlocked) as capped:
        await manager_over.bind_owned_initial_document(run_over)
    assert capped.value.code == "document_read_denied" and run_over.owned_initial_seal is None
    assert all(item["handle"] == _STREAM and item["size"] == 16384 for item in _payloads(session_over, "IO.read"))
    manager_b64, run_b64, _d2, _p2, _s2, _h2 = await _bind(
        seconds=2.0, reads=[_chunk("QUJD", eof=True, encoded=True)]
    )
    with pytest.raises(BrowserBlocked) as encoded:
        await manager_b64.bind_owned_initial_document(run_b64)
    assert encoded.value.code == "document_read_denied" and run_b64.owned_initial_seal is None
    manager_eof, run_eof, _d3, _p3, _s3, _h3 = await _bind(
        seconds=0.5, reads=[_chunk("partial", eof=False)]
    )
    started = time.monotonic()
    with pytest.raises(BrowserBlocked) as missing_eof:
        await manager_eof.bind_owned_initial_document(run_eof)
    assert missing_eof.value.code == "cleanup_uncertain"
    assert time.monotonic() - started < 2.0 and run_eof.owned_initial_seal is None
    assert page_over.gotos
    bad = _pause_event(url)
    bad["responseStatusCode"] = 500
    session_bad = _Session([_chunk("no")])
    page_bad = _Page(session_bad)
    page_bad.emit_on_goto = False
    driver_bad = BrowserDriver("isolated")
    driver_bad._state = "open"
    driver_bad._page = page_bad
    driver_bad._context = page_bad.context
    driver_bad._owned_page_target_id = _TARGET
    page_bad.driver = driver_bad
    manager_bad = RunManager(lambda: None, lambda mode: driver_bad)
    run_bad = manager_bad._open(_request())
    run_bad.budget = Budget(run_bad.request.limits, time.monotonic)
    manager_bad._run = run_bad
    dom_bad = _DOM(page_bad, "pass", lambda: manager_bad._on_owned_navigation(run_bad))
    run_bad.dom = dom_bad

    async def goto_bad(target: str, **kwargs: object) -> None:
        page_bad.gotos.append(target)
        session_bad.emit(bad)
        await page_bad._until_fulfill_or_waiter()
        page_bad.url = target

    page_bad.goto = goto_bad
    with pytest.raises(RunnerError) as refused:
        await manager_bad.bind_owned_initial_document(run_bad)
    assert refused.value.code == "outcome_unknown"
    assert run_bad.queued is True and run_bad.owned_ticket is not None
    assert run_bad.owned_ticket.state == "consumed" and run_bad.owned_initial_seal is None
    assert _payloads(session_bad, "Fetch.fulfillRequest") == []
    assert _payloads(session_bad, "IO.read") == []


async def test_second_pause_and_late_pause_do_not_seal() -> None:
    manager, run, _driver, page, session, _dom = await _bind()
    session.extra_during_read = True
    with pytest.raises(RunnerError) as second:
        await manager.bind_owned_initial_document(run)
    assert second.value.code == "outcome_unknown"
    assert run.queued is True
    assert run.owned_initial_seal is None
    assert _payloads(session, "Fetch.fulfillRequest") == []
    manager2, run2, _d2, page2, session2, _dom2 = await _bind()
    session2.late_during_fulfill = True
    with pytest.raises(RunnerError) as late:
        await manager2.bind_owned_initial_document(run2)
    assert late.value.code == "outcome_unknown"
    assert run2.queued is True
    assert run2.owned_initial_seal is None and len(page2.gotos) == 1
    assert len(_payloads(session2, "Fetch.fulfillRequest")) == 1
    assert page.gotos


async def test_equal_request_and_stream_strings_still_use_slots() -> None:
    manager, run, _driver, page, session, _dom = await _bind(stream=_REQUEST)
    await manager.bind_owned_initial_document(run)
    assert run.owned_initial_seal is not None
    take = _payloads(session, "Fetch.takeResponseBodyAsStream")[0]
    read = _payloads(session, "IO.read")[0]
    fulfill = _payloads(session, "Fetch.fulfillRequest")[0]
    assert take["requestId"] == read["handle"] == fulfill["requestId"] == _REQUEST
    assert "requestId" in take and "handle" in read and "requestId" not in read
    await run.owned_resource.close()
    assert page.url == run.owned_resource.url


async def test_release_disable_raises_and_is_not_assigned() -> None:
    manager, run, _driver, page, session, _dom = await _bind()
    session.fail_disable = True
    with pytest.raises(RunnerError) as caught:
        await manager.bind_owned_initial_document(run)
    assert caught.value.code == "outcome_unknown"
    assert run.queued is True
    assert run.owned_initial_seal is None
    assert len(_payloads(session, "Fetch.fulfillRequest")) == 1 and len(page.gotos) == 1
    assert not any(name == "release_ok" for name, _payload in session.calls)
    manager_d, run_d, _dd, page_d, session_d, _domd = await _bind()
    session_d.raise_detach = True
    with pytest.raises(RunnerError) as detached:
        await manager_d.bind_owned_initial_document(run_d)
    assert detached.value.code == "outcome_unknown"
    assert run_d.queued is True and run_d.owned_ticket is not None
    assert run_d.owned_ticket.state == "consumed" and run_d.owned_initial_seal is None
    assert len(page_d.gotos) == 1
    assert [name for name, _payload in session_d.calls].count("Fetch.enable") == 1
    manager_r, run_r, _dr, page_r, session_r, _domr = await _bind()
    session_r.raise_disable = True
    with pytest.raises(RunnerError) as released:
        await manager_r.bind_owned_initial_document(run_r)
    assert released.value.code == "outcome_unknown"
    assert run_r.queued is True and run_r.owned_initial_seal is None
    assert len(_payloads(session_r, "Fetch.fulfillRequest")) == 1 and len(page_r.gotos) == 1
    assert run_r.owned_ticket is not None and run_r.owned_ticket.state == "consumed"


async def test_unknown_and_missing_cleanup_report_drop_seal() -> None:
    manager, run, _driver, _page, _session, _dom = await _bind()
    await manager.bind_owned_initial_document(run)
    assert run.owned_initial_seal is not None and run.owned_ticket is not None

    resource = run.owned_resource

    async def unknown_close() -> CleanupReport:
        return CleanupReport(
            clean=False,
            unknown=True,
            unfinished_tasks=1,
            owned_tasks=1,
            listener_closed=False,
        )

    resource.close = unknown_close
    await manager._cleanup(run)
    assert run.owned_initial_seal is None and run.owned_ticket is None
    manager2, run2, _d2, _p2, _s2, _dom2 = await _bind()
    await manager2.bind_owned_initial_document(run2)
    resource = run2.owned_resource

    async def missing() -> object:
        raise RuntimeError("missing report")

    resource.close = missing
    await manager2._cleanup(run2)
    assert run2.owned_initial_seal is None and run2.owned_ticket is None


async def test_substituted_budget_guard_steps_and_ordinary_source() -> None:
    session = _Session([])
    page = _Page(session)
    driver = BrowserDriver("isolated")
    driver._state = "open"
    driver._page = page
    driver._context = page.context
    driver._owned_page_target_id = _TARGET
    page.driver = driver
    manager = RunManager(lambda: None, lambda mode: driver)
    run = manager._open(_request())
    manager._run = run
    other = Budget(_limits(), time.monotonic)
    resource = OwnedFixtureResource()
    await resource.start(_OWNED_INITIAL_CONTENT_ID, run.budget.original_deadline)
    try:
        driver.register_owned_binding(
            page=page,
            context=page.context,
            budget=run.budget,
            owner=run,
            resource=resource,
        )
        with pytest.raises(BrowserBlocked) as caught:
            await driver.arm_owned_initial_document(resource.url, other)
        assert caught.value.code == "document_read_denied"
        assert page.context.cdp_sessions == 0 and other.usage().steps == 0
        assert run.budget.original_deadline == run.budget.deadline
    finally:
        await resource.close()
    manager_g, run_g, _dg, page_g, session_g, _domg = await _bind(status="blocked")
    with pytest.raises(RunnerError) as guard:
        await manager_g.bind_owned_initial_document(run_g)
    assert guard.value.code == "blocked"
    assert run_g.owned_ticket is None and page_g.gotos == []
    assert _payloads(session_g, "Fetch.enable")
    manager_s, run_s, _ds, page_s, _ss, _doms = await _bind(steps=1)
    run_s.budget.reserve_step()
    deadline = run_s.budget.original_deadline
    with pytest.raises(BudgetExceeded) as steps:
        await manager_s.bind_owned_initial_document(run_s)
    assert steps.value.code == "steps"
    assert run_s.owned_ticket is not None and run_s.owned_ticket.state == "issued"
    assert page_s.gotos == [] and run_s.budget.original_deadline == deadline
    source = inspect.getsource(RunManager._body)
    assert "bind_owned_initial_document" not in source
    assert "OwnedFixtureResource" not in source
    payload = _request().model_dump()
    payload["content_id"] = _OWNED_INITIAL_CONTENT_ID
    with pytest.raises(ValidationError):
        TaskRequest.model_validate(payload)


async def test_later_navigation_drops_seal_and_forged_ticket_is_not_held() -> None:
    manager, run, driver, page, session, dom = await _bind()
    await manager.bind_owned_initial_document(run)
    assert run.owned_initial_seal is not None
    enable_count = [name for name, _payload in session.calls].count("Fetch.enable")
    await run.dom._native(dom.action)
    assert run.owned_initial_seal is None
    assert [name for name, _payload in session.calls].count("Fetch.enable") == enable_count
    assert len(page.gotos) == 2
    forged = _OwnedLoopbackTicket(
        resource=run.owned_ticket.resource,
        page=run.owned_ticket.page,
        owned_target_id=run.owned_ticket.owned_target_id,
        main_frame_id=run.owned_ticket.main_frame_id,
        budget=run.owned_ticket.budget,
        original_deadline=run.owned_ticket.original_deadline,
        destination_url=run.owned_ticket.destination_url,
        state="consumed",
    )
    assert forged is not run.owned_ticket
    assert manager._owned_candidate_is_ticket(run, forged) is False
    assert manager._owned_candidate_is_ticket(run, run.owned_ticket) is True
    assert urlsplit(run.owned_resource.url).hostname == "127.0.0.1"
    assert driver.owned_initial_arm_active() is False
    await run.owned_resource.close()
