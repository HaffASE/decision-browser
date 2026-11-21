from __future__ import annotations

import asyncio
import base64
import hashlib
import time

import pytest

from decision_browser.browser import BrowserBlocked, BrowserDriver

_URL = "https://owned.example/doc"
_FRAME = "frame-main"
_TARGET = "target-owned"
_REQUEST = "req-owned"
_STREAM = "stream-1"
_KEPT = (("Content-Type", "text/plain"), ("X-Trace", "ok"))
_DROPPED = (
    ("Cookie", "session=secret"),
    ("Set-Cookie", "a=b"),
    ("Authorization", "Bearer z"),
)
_READ = 16_384
_DENIED = "Owned document body was not retained."
_STATIC = {
    "Target.getTargetInfo": {"targetInfo": {"targetId": _TARGET}},
    "Page.getFrameTree": {"frameTree": {"frame": {"id": _FRAME}}},
    "Fetch.takeResponseBodyAsStream": {"stream": _STREAM},
}


def _headers() -> list[dict[str, str]]:
    ordered = (_KEPT[0], *_DROPPED, _KEPT[1])
    return [{"name": name, "value": value} for name, value in ordered]


def _pause() -> dict[str, object]:
    return {
        "requestId": _REQUEST,
        "frameId": _FRAME,
        "resourceType": "Document",
        "responseStatusCode": 200,
        "request": {"url": _URL, "method": "GET", "headers": {}},
        "responseHeaders": _headers(),
    }


def _bad_pause(**overrides: object) -> dict[str, object]:
    event = _pause()
    request = dict(event["request"]) if isinstance(event["request"], dict) else {}
    event["request"] = request
    for key, value in overrides.items():
        if key in {"url", "method"}:
            request[key] = value
        else:
            event[key] = value
    return event


def _chunk(data: str, *, eof: bool, encoded: bool = False) -> dict[str, object]:
    return {"base64Encoded": encoded, "data": data, "eof": eof}


class _Session:
    def __init__(
        self,
        reads: list[dict[str, object]],
        *,
        pause: dict[str, object] | None = None,
        target_ids: tuple[str, ...] = (_TARGET,),
    ) -> None:
        self._reads = list(reads)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.trace: list[str] = []
        self._on_paused: object = None
        self._pause_override = pause
        self._target_ids = list(target_ids)
        self.detached = False
        self.closed = False

    def on(self, event: str, callback: object) -> None:
        self.trace.append(f"on:{event}")
        if event == "Fetch.requestPaused":
            self._on_paused = callback

    async def detach(self) -> None:
        self.detached = True

    async def close(self) -> None:
        self.closed = True

    async def send(self, method: str, params: dict[str, object] | None = None) -> object:
        self.calls.append((method, dict(params or {})))
        self.trace.append(method)
        if method == "Target.getTargetInfo":
            target_id = self._target_ids.pop(0) if len(self._target_ids) > 1 else self._target_ids[0]
            return {"targetInfo": {"targetId": target_id}}
        if method == "Fetch.enable" and callable(self._on_paused):
            event = self._pause_override if self._pause_override is not None else _pause()
            self._on_paused(event)
            return {}
        if method == "IO.read":
            return self._reads.pop(0)
        return _STATIC.get(method, {})


class _Page:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _Context:
    def __init__(self, session: _Session) -> None:
        self.session = session
        self.closed = False
        self.cdp_sessions = 0

    async def new_cdp_session(self, page: object) -> _Session:
        if page is None or self.closed:
            raise RuntimeError("cdp session unavailable")
        self.cdp_sessions += 1
        return self.session

    async def close(self) -> None:
        self.closed = True
        await self.session.close()


def _open(session: _Session) -> tuple[BrowserDriver, _Page, _Context]:
    page, context = _Page(), _Context(session)
    driver = BrowserDriver("isolated")
    driver._state = "open"
    driver._page = page
    driver._context = context
    driver._owned_page_target_id = _TARGET
    return driver, page, context


async def _cleanup(driver: BrowserDriver, page: _Page, context: _Context) -> None:
    pending = [task for task in getattr(driver, "_owned_tasks", ()) if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    if not page.closed:
        await page.close()
    if not context.closed:
        await context.close()


async def _finish(
    driver: BrowserDriver,
    page: _Page,
    context: _Context,
    session: _Session,
) -> None:
    await _cleanup(driver, page, context)
    owned_tasks = getattr(driver, "_owned_tasks", None)
    if owned_tasks is not None:
        assert not owned_tasks
    assert session.closed and page.closed and context.closed


async def _read(session: _Session) -> object:
    driver, page, context = _open(session)
    try:
        return await driver.read_owned_document_body(_URL, time.monotonic() + 2.0)
    finally:
        await _finish(driver, page, context, session)


def _prepare(
    session: _Session,
    *,
    mode: str = "isolated",
    state: str = "open",
    private: str | None = _TARGET,
    public: str | None = None,
) -> tuple[BrowserDriver, _Page, _Context]:
    if mode == "isolated":
        driver, page, context = _open(session)
    else:
        page, context = _Page(), _Context(session)
        driver = BrowserDriver(mode)
        driver._page = page
        driver._context = context
    driver._state = state
    driver._owned_page_target_id = private
    if public is not None:
        driver.owned_target_id = public
    return driver, page, context


def _payloads(session: _Session, method: str) -> list[dict[str, object]]:
    return [payload for name, payload in session.calls if name == method]


def _assert_kept(headers: object) -> None:
    pairs = headers
    if not isinstance(headers, tuple):
        pairs = tuple((item["name"], item["value"]) for item in headers)
    assert pairs == _KEPT
    assert not any(name.lower() in {"cookie", "set-cookie", "authorization"} for name, _v in pairs)


async def test_ascii_eof_hash_base64_filtered_and_immutable() -> None:
    raw = b"owned-ascii"
    session = _Session([_chunk(raw.decode("ascii"), eof=True)])
    body = await _read(session)
    assert type(body.body) is bytes and body.body == raw
    assert body.sha256 == hashlib.sha256(raw).hexdigest()
    assert body.proof_possible is False
    _assert_kept(body.response_headers)
    with pytest.raises(AttributeError):
        body.proof_possible = True
    assert _payloads(session, "Fetch.enable")[0]["patterns"] == [
        {"urlPattern": _URL, "resourceType": "Document", "requestStage": "Response"}
    ]
    fulfill = _payloads(session, "Fetch.fulfillRequest")
    assert len(fulfill) == 1 and fulfill[0]["requestId"] == _REQUEST
    assert fulfill[0]["responseCode"] == 200
    assert fulfill[0]["body"] == base64.b64encode(raw).decode("ascii")
    assert base64.b64decode(fulfill[0]["body"]) == raw
    _assert_kept(fulfill[0]["responseHeaders"])
    assert _payloads(session, "Fetch.failRequest") == []
    assert session.detached is True


async def test_empty_data_does_not_count_as_eof() -> None:
    raw = b"after-empty"
    session = _Session([_chunk("", eof=False), _chunk(raw.decode("ascii"), eof=True)])
    body = await _read(session)
    assert len(_payloads(session, "IO.read")) == 2
    assert body.body == raw
    assert body.sha256 == hashlib.sha256(raw).hexdigest()
    assert body.sha256 != hashlib.sha256(b"").hexdigest()
    coded = _payloads(session, "Fetch.fulfillRequest")[0]["body"]
    assert base64.b64decode(coded) == raw


@pytest.mark.parametrize(
    "bad",
    [
        {"base64Encoded": True, "data": "QUJD", "eof": True},
        {"data": "still-ascii", "eof": True},
        {"base64Encoded": False, "data": "caf\u00e9", "eof": True},
    ],
)
async def test_invalid_chunk_after_prefix_is_not_fulfilled(bad: dict[str, object]) -> None:
    session = _Session([_chunk("PREFIX", eof=False), bad])
    with pytest.raises(BrowserBlocked) as caught:
        await _read(session)
    assert caught.value.code == "document_read_denied"
    assert caught.value.message == _DENIED
    assert "PREFIX" not in str(caught.value)
    assert _payloads(session, "Fetch.fulfillRequest") == []
    failed = _payloads(session, "Fetch.failRequest")
    assert failed[0]["requestId"] == _REQUEST
    assert failed[0]["errorReason"] == "BlockedByClient"


async def test_cap_overflow_rejects_valid_chunks_without_short_body() -> None:
    piece = "a" * _READ
    reads = [_chunk(piece, eof=False) for _ in range(4)]
    reads.append(_chunk("b", eof=True))
    session = _Session(reads)
    with pytest.raises(BrowserBlocked) as caught:
        await _read(session)
    assert caught.value.code == "document_read_denied"
    assert caught.value.message == _DENIED
    assert piece not in str(caught.value)
    seen = _payloads(session, "IO.read")
    assert len(seen) == 5
    assert all(item["handle"] == _STREAM and item["size"] == _READ for item in seen)
    assert _payloads(session, "Fetch.fulfillRequest") == []
    assert _payloads(session, "Fetch.failRequest")[0]["errorReason"] == "BlockedByClient"


_NO_BODY = ("Fetch.takeResponseBodyAsStream", "IO.read", "IO.close")


def _names(session: _Session) -> list[str]:
    return [name for name, _payload in session.calls]


@pytest.mark.parametrize(
    "overrides",
    [
        {"url": "https://foreign.example/doc"},
        {"method": "POST"},
        {"resourceType": "Script"},
        {"frameId": "frame-foreign"},
        {"responseStatusCode": 404},
        {"responseErrorReason": "Failed"},
        {"responseStatusCode": 302},
        {"url": f"{_URL}#section"},
    ],
)
async def test_pause_shape_rejects_before_body_io(overrides: dict[str, object]) -> None:
    session = _Session([], pause=_bad_pause(**overrides))
    with pytest.raises(BrowserBlocked) as caught:
        await _read(session)
    assert caught.value.code == "document_read_denied"
    assert caught.value.message == _DENIED
    assert _payloads(session, "Fetch.fulfillRequest") == []
    assert not any(name in _NO_BODY for name in _names(session))
    failed = _payloads(session, "Fetch.failRequest")
    assert failed and failed[0]["requestId"] == _REQUEST
    assert failed[0]["errorReason"] == "BlockedByClient"


async def test_public_owned_target_id_does_not_select_private_creation_target() -> None:
    raw = b"public-independent"
    session = _Session([_chunk(raw.decode("ascii"), eof=True)])
    driver, page, context = _prepare(session, public="public-different")
    try:
        body = await driver.read_owned_document_body(_URL, time.monotonic() + 2.0)
    finally:
        await _finish(driver, page, context, session)
    assert body.body == raw
    assert driver.owned_target_id == "public-different"
    assert driver._owned_page_target_id == _TARGET


@pytest.mark.parametrize("private", ["", None])
async def test_missing_private_creation_target_refuses_before_cdp(private: str | None) -> None:
    session = _Session([])
    driver, page, context = _prepare(session, private=private, public=_TARGET)
    try:
        with pytest.raises(BrowserBlocked) as caught:
            await driver.read_owned_document_body(_URL, time.monotonic() + 2.0)
        assert caught.value.code == "not_started"
    finally:
        await _finish(driver, page, context, session)
    assert context.cdp_sessions == 0
    assert session.calls == []


@pytest.mark.parametrize(
    ("target_ids", "enable_count"),
    [(("live-other",), 0)],
)
async def test_live_target_mismatch_rejects_before_body_io(
    target_ids: tuple[str, ...],
    enable_count: int,
) -> None:
    session = _Session([], target_ids=target_ids)
    driver, page, context = _prepare(session, public="live-other")
    try:
        with pytest.raises(BrowserBlocked) as caught:
            await driver.read_owned_document_body(_URL, time.monotonic() + 2.0)
        assert caught.value.code == "cleanup_uncertain"
    finally:
        await _finish(driver, page, context, session)
    assert _names(session).count("Fetch.enable") == enable_count
    assert not any(name in _NO_BODY for name in _names(session))
    assert _payloads(session, "Fetch.fulfillRequest") == []


async def test_normal_owned_body_has_no_target_query_inside_fetch_window() -> None:
    raw = b"window-owned"
    session = _Session([_chunk(raw.decode("ascii"), eof=True)])
    body = await _read(session)
    assert type(body.body) is bytes and body.body == raw
    assert body.sha256 == hashlib.sha256(raw).hexdigest()
    assert body.proof_possible is False
    fulfill = _payloads(session, "Fetch.fulfillRequest")
    assert len(fulfill) == 1 and fulfill[0]["requestId"] == _REQUEST
    assert fulfill[0]["responseCode"] == 200
    assert fulfill[0]["body"] == base64.b64encode(raw).decode("ascii")
    assert base64.b64decode(fulfill[0]["body"]) == raw
    enable_at = session.trace.index("Fetch.enable")
    disable_at = session.trace.index("Fetch.disable")
    assert enable_at < disable_at
    assert session.trace.index("Target.getTargetInfo") < enable_at
    assert session.trace.index("on:Fetch.requestPaused") < enable_at
    assert session.trace.index("Page.getFrameTree") < enable_at
    assert "Target.getTargetInfo" not in session.trace[enable_at + 1 : disable_at]
    assert session.trace.count("Target.getTargetInfo") == 1
    assert session.detached is True


async def test_subscription_and_main_frame_precede_fetch_enable() -> None:
    session = _Session([_chunk("frame-order", eof=True)])
    body = await _read(session)
    assert body.body == b"frame-order"
    enable_at = session.trace.index("Fetch.enable")
    assert session.trace.index("on:Fetch.requestPaused") < enable_at
    assert session.trace.index("Page.getFrameTree") < enable_at
    assert session.trace.index("Target.getTargetInfo") < enable_at


async def test_existing_mode_refuses_before_cdp() -> None:
    session = _Session([])
    driver, page, context = _prepare(session, mode="existing_chrome", public=_TARGET)
    try:
        with pytest.raises(BrowserBlocked) as caught:
            await driver.read_owned_document_body(_URL, time.monotonic() + 2.0)
        assert caught.value.code == "document_read_denied"
        assert caught.value.message == _DENIED
    finally:
        await _finish(driver, page, context, session)
    assert context.cdp_sessions == 0
    assert session.calls == []


@pytest.mark.parametrize("state", ["idle", "starting", "closing", "closed"])
async def test_non_open_refuses_before_cdp(state: str) -> None:
    session = _Session([])
    driver, page, context = _prepare(session, state=state)
    try:
        with pytest.raises(BrowserBlocked) as caught:
            await driver.read_owned_document_body(_URL, time.monotonic() + 2.0)
        assert caught.value.code == "not_started"
    finally:
        await _finish(driver, page, context, session)
    assert context.cdp_sessions == 0
    assert session.calls == []


class _CapSession(_Session):
    def __init__(self) -> None:
        super().__init__([])
        self.secret = "raw-cookie=supersecret"
        self._late = False

    def _emit(self, request_id: str, *, foreign: bool) -> None:
        event = _bad_pause(url="https://foreign.example/extra") if foreign else _pause()
        event["requestId"] = request_id
        headers = list(event["responseHeaders"]) if isinstance(event.get("responseHeaders"), list) else []
        headers.append({"name": "Cookie", "value": self.secret})
        headers.append({"name": "Authorization", "value": self.secret})
        event["responseHeaders"] = headers
        callback = self._on_paused
        if callable(callback):
            callback(event)

    async def send(self, method: str, params: dict[str, object] | None = None) -> object:
        if method == "Fetch.failRequest" and not self._late:
            self._late = True
            self._emit("p-late", foreign=True)
        if method == "Fetch.enable" and callable(self._on_paused):
            self.calls.append((method, dict(params or {})))
            self.trace.append(method)
            for index in range(8):
                self._emit(f"p{index}", foreign=index != 0)
            return {}
        return await super().send(method, params)


class _DetachHold(_Session):
    def __init__(self, release: asyncio.Event, started: asyncio.Event) -> None:
        super().__init__([_chunk("held", eof=True)])
        self._release = release
        self._started = started

    async def detach(self) -> None:
        self._started.set()
        task = asyncio.current_task()
        while not self._release.is_set():
            if task is not None:
                while task.cancelling():
                    task.uncancel()
            try:
                await asyncio.wait_for(self._release.wait(), 0.02)
            except (TimeoutError, asyncio.CancelledError):
                continue
        self.detached = True


class _StalledRead(_Session):
    def __init__(self) -> None:
        super().__init__([])
        self.entered = asyncio.Event()
        self.stop = asyncio.Event()

    async def send(self, method: str, params: dict[str, object] | None = None) -> object:
        if method == "IO.read":
            self.calls.append((method, dict(params or {})))
            self.trace.append(method)
            self.entered.set()
            await self.stop.wait()
            return _chunk("", eof=True)
        return await super().send(method, params)


class _CloseFails(_Session):
    def __init__(self) -> None:
        super().__init__([_chunk("close-order", eof=True)])

    async def send(self, method: str, params: dict[str, object] | None = None) -> object:
        if method == "IO.close":
            self.calls.append((method, dict(params or {})))
            self.trace.append(method)
            raise RuntimeError("close failed")
        return await super().send(method, params)

    async def detach(self) -> None:
        self.trace.append("detach")
        await super().detach()


def _failed_ids(session: _Session) -> list[object]:
    return [payload.get("requestId") for payload in _payloads(session, "Fetch.failRequest")]


async def _close_bounded(
    driver: BrowserDriver,
    page: _Page,
    context: _Context,
    *extra: asyncio.Task[object],
) -> None:
    """Cancel owned work, wait at most one second, then close fakes.

    Callers that arm a cancellation-resistant hold must release that hold
    before calling this helper. The wait is finite even if a task swallows
    cancellation.
    """
    owned = tuple(getattr(driver, "_owned_tasks", ()))
    pending = {task for task in (*extra, *owned) if not task.done()}
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending, timeout=1.0)
    for task in (*extra, *tuple(getattr(driver, "_owned_tasks", ()))):
        if task.done() and not task.cancelled():
            task.exception()


async def test_extra_late_and_over_cap_pauses_are_failed_without_retention() -> None:
    session = _CapSession()
    driver, page, context = _open(session)
    flight = asyncio.create_task(
        driver.read_owned_document_body(_URL, time.monotonic() + 2.0)
    )
    try:
        _done, pending = await asyncio.wait({flight}, timeout=3.0)
        assert not pending
        with pytest.raises(BrowserBlocked) as caught:
            await flight
        assert caught.value.code in {"document_read_denied", "cleanup_uncertain"}
        assert caught.value.message in {_DENIED, "Browser cleanup could not be confirmed."}
        assert session.secret not in str(caught.value)
    finally:
        if not flight.done():
            flight.cancel()
        await _close_bounded(driver, page, context, flight)
    assert driver._owned_tasks == set()
    assert session.secret not in repr(session.calls)
    assert _payloads(session, "Fetch.fulfillRequest") == []
    assert set(_failed_ids(session)) >= {f"p{index}" for index in range(8)} | {"p-late"}


async def test_one_flight_cancel_and_registry_clears_after_original_deadline() -> None:
    release = asyncio.Event()
    started = asyncio.Event()
    session = _DetachHold(release, started)
    driver, page, context = _open(session)
    deadline_at = time.monotonic() + 2.0
    flight = asyncio.create_task(driver.read_owned_document_body(_URL, deadline_at))
    starter = asyncio.create_task(started.wait())
    overlap: asyncio.Task[object] | None = None
    follow: asyncio.Task[object] | None = None
    try:
        await asyncio.wait({flight, starter}, timeout=1.5, return_when=asyncio.FIRST_COMPLETED)
        if flight.done() and not started.is_set():
            await flight
        assert started.is_set()
        overlap = asyncio.create_task(driver.read_owned_document_body(_URL, deadline_at))
        _overlap_done, overlap_pending = await asyncio.wait({overlap}, timeout=1.0)
        assert not overlap_pending
        with pytest.raises(BrowserBlocked) as caught:
            await overlap
        assert caught.value.code == "document_read_denied"
        assert context.cdp_sessions == 1
        flight.cancel()
        remain = deadline_at + 0.5 - time.monotonic()
        _flight_done, flight_pending = await asyncio.wait(
            {flight}, timeout=remain if remain > 0 else 0.05
        )
        assert time.monotonic() <= deadline_at + 0.5
        assert not flight_pending
        with pytest.raises(asyncio.CancelledError):
            await flight
        release.set()
        owned = list(driver._owned_tasks)
        if owned:
            _done, pending = await asyncio.wait(set(owned), timeout=1.0)
            assert not pending
        for task in owned:
            if task.done() and not task.cancelled():
                task.exception()
        for _ in range(20):
            if not driver._owned_tasks:
                break
            await asyncio.sleep(0.01)
        assert driver._owned_tasks == set()
        context.session = _Session([_chunk("next", eof=True)])
        follow = asyncio.create_task(
            driver.read_owned_document_body(_URL, time.monotonic() + 2.0)
        )
        _follow_done, follow_pending = await asyncio.wait({follow}, timeout=2.5)
        assert not follow_pending
        body = await follow
        assert body.body == b"next"
    finally:
        release.set()
        extras = [
            task
            for task in (flight, starter, overlap, follow)
            if task is not None
        ]
        await _close_bounded(driver, page, context, *extras)


async def test_stalled_read_stays_inside_original_deadline() -> None:
    session = _StalledRead()
    driver, page, context = _open(session)
    deadline_at = time.monotonic() + 0.8
    flight = asyncio.create_task(driver.read_owned_document_body(_URL, deadline_at))
    entered = asyncio.create_task(session.entered.wait())
    try:
        await asyncio.wait(
            {flight, entered}, timeout=1.0, return_when=asyncio.FIRST_COMPLETED
        )
        assert session.entered.is_set()
        remain = deadline_at + 0.4 - time.monotonic()
        _done, pending = await asyncio.wait(
            {flight}, timeout=remain if remain > 0 else 0.05
        )
        assert time.monotonic() <= deadline_at + 0.4
        assert not pending
        with pytest.raises(BrowserBlocked) as caught:
            await flight
        assert caught.value.code in {"document_read_denied", "cleanup_uncertain"}
        assert _payloads(session, "Fetch.fulfillRequest") == []
        assert driver._owned_tasks == set()
    finally:
        session.stop.set()
        await _close_bounded(driver, page, context, flight, entered)


async def test_close_failure_refuses_result_and_still_cleans_owned_pause() -> None:
    session = _CloseFails()
    driver, page, context = _open(session)
    flight = asyncio.create_task(
        driver.read_owned_document_body(_URL, time.monotonic() + 2.0)
    )
    try:
        _done, pending = await asyncio.wait({flight}, timeout=3.0)
        assert not pending
        with pytest.raises(BrowserBlocked) as caught:
            await flight
        assert caught.value.code in {"document_read_denied", "cleanup_uncertain"}
    finally:
        if not flight.done():
            flight.cancel()
        await _close_bounded(driver, page, context, flight)
    assert driver._owned_tasks == set()
    assert _payloads(session, "Fetch.fulfillRequest") == []
    assert _REQUEST in _failed_ids(session)
    close_at = session.trace.index("IO.close")
    fail_at = session.trace.index("Fetch.failRequest")
    disable_at = session.trace.index("Fetch.disable")
    detach_at = session.trace.index("detach")
    assert close_at < fail_at < disable_at < detach_at
    assert session.detached is True
