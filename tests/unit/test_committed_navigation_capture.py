"""SHA-pinned observations for the frozen committed-navigation helper.

Doubles record CDP and goto ordering. They are not a receipt or authority.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import sys
import time
from pathlib import Path

import pytest

from decision_browser.browser import BrowserBlocked, BrowserMode

_PATH = Path(__file__).resolve().parents[2] / "src/decision_browser/committed_navigation_capture.py"
_SHA = "ad0005f390ca4b771fc8cf33071ffcd37b42972ea9a07f8f305a1780d23e0701"
_NAME = "committed_navigation_capture_frozen_760a8035"
_URL = "https://reference.example/doc"
_FRAME = "frame-main"
_REF = "reference-target"
_OWNED = "owned-target"
_PAGE_TARGET = "owned-page-target"
_IO_SIZE = 16384
_HAPPY_CHUNKS: tuple[dict[str, object], ...] = (
    {"data": "ab", "base64Encoded": False, "eof": False},
    {"data": "c", "base64Encoded": False, "eof": True},
)


def _load():
    payload = _PATH.read_bytes()
    found = hashlib.sha256(payload).hexdigest()
    if found != _SHA:
        raise RuntimeError("frozen helper sha256 mismatch")
    spec = importlib.util.spec_from_file_location(_NAME, _PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("frozen helper loader missing")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_NAME] = module
    spec.loader.exec_module(module)
    return module


_HELPER = _load()
_capture = _HELPER.capture_committed_document_navigation
_Navigation = _HELPER.CommittedDocumentNavigation
if _capture.__module__ != _NAME or _Navigation.__module__ != _NAME:
    raise RuntimeError("frozen helper was not registered before exec")


def _code(exc: BrowserBlocked) -> str:
    marked = getattr(exc, "code", None)
    if isinstance(marked, str):
        return marked
    if exc.args and isinstance(exc.args[0], str):
        return exc.args[0]
    raise AssertionError("blocked code missing")


def _identity(driver: _Driver) -> tuple[object, ...]:
    return (
        driver.mode,
        driver._state,
        driver._page,
        driver._context,
        driver.owned_target_id,
        driver._owned_page_target_id,
        tuple(driver._owned_tasks),
    )


class _Driver:
    def __init__(self) -> None:
        self.mode = BrowserMode.isolated
        self._state = "open"
        self._page = object()
        self._context = object()
        self.owned_target_id = _OWNED
        self._owned_page_target_id = _PAGE_TARGET
        self._owned_tasks: list[object] = []


class _NavRequest:
    def __init__(self, flag: bool) -> None:
        self._flag = flag
        self.calls = 0
        self.body_calls = 0

    def is_navigation_request(self) -> bool:
        self.calls += 1
        return self._flag

    async def body(self) -> bytes:
        self.body_calls += 1
        return b""


class _NavResponse:
    def __init__(self, flag: bool) -> None:
        self.request = _NavRequest(flag)
        self.frame: object | None = object()
        self.body_calls = 0

    async def body(self) -> bytes:
        self.body_calls += 1
        return b""


class _World:
    def __init__(
        self,
        *,
        chunks: tuple[dict[str, object], ...] | list[dict[str, object]] | None = None,
        target_seen: str = _REF,
        pause_frame: str = _FRAME,
        redirect_id: str | None = None,
        nav: bool = True,
        early: bool = False,
        extra: bool = False,
        enable_sleep: float = 0.0,
        io_gate: asyncio.Event | None = None,
    ) -> None:
        self.order: list[str] = []
        self.sent: list[tuple[str, dict[str, object]]] = []
        self.chunks = [dict(item) for item in (chunks or _HAPPY_CHUNKS)]
        self.target_seen = target_seen
        self.pause_frame = pause_frame
        self.redirect_id = redirect_id
        self.early = early
        self.extra = extra
        self.enable_sleep = enable_sleep
        self.io_gate = io_gate
        self.trees = 0
        self.handler = None
        self.session_page: object | None = None
        self.goto_kwargs: dict[str, object] = {}
        self.driver = _Driver()
        self.response = _NavResponse(nav)
        self.fulfilled = asyncio.Event()
        self.io_started = asyncio.Event()
        self.page = self._Page(self)
        self.context = self._Context(self)
        self.session = self._Session(self)

    def emit(self, request_id: str) -> None:
        if self.handler is None:
            raise RuntimeError("pause listener missing")
        self.handler(
            {
                "requestId": request_id,
                "request": {"url": _URL, "method": "GET"},
                "frameId": self.pause_frame,
                "resourceType": "Document",
                "responseStatusCode": 200,
                "responseErrorReason": None,
                "redirectedRequestId": self.redirect_id,
                "responseHeaders": [
                    {"name": "Content-Type", "value": "text/html"},
                    {"name": "Cookie", "value": "secret"},
                ],
            }
        )

    def reply(self, method: str) -> object:
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": self.target_seen}}
        if method == "Page.getFrameTree":
            self.trees += 1
            return {"frameTree": {"frame": {"id": _FRAME}, "childFrames": []}}
        if method == "Fetch.takeResponseBodyAsStream":
            return {"stream": "io-1"}
        if method == "IO.read":
            return self.chunks.pop(0)
        return {}

    async def run(self, deadline: float, **kwargs: object) -> object:
        return await _capture(
            driver=kwargs.get("driver", self.driver),
            context=kwargs.get("context", self.context),
            page=kwargs.get("page", self.page),
            expected_url=_URL,
            original_deadline=deadline,
            body_cap=kwargs.get("body_cap", 32),
            reference_target_id=kwargs.get("target", _REF),
        )

    class _Page:
        def __init__(self, world: _World) -> None:
            self.world = world
            self.url = "about:blank"

        def goto(self, url: str, **kwargs: object):
            async def _goto() -> object:
                self.world.order.append("goto")
                self.world.goto_kwargs = {"url": url, **kwargs}
                self.world.emit("req-1")
                if self.world.extra:
                    self.world.emit("req-2")
                if self.world.early:
                    self.world.order.append("commit")
                    return self.world.response
                await self.world.fulfilled.wait()
                self.url = _URL
                self.world.order.append("commit")
                return self.world.response

            return _goto()

    class _Context:
        def __init__(self, world: _World) -> None:
            self.world = world

        def new_cdp_session(self, page: object):
            async def _open() -> _World._Session:
                self.world.order.append("socket")
                self.world.session_page = page
                return self.world.session

            return _open()

    class _Session:
        def __init__(self, world: _World) -> None:
            self.world = world

        def on(self, event: str, handler: object) -> None:
            if event != "Fetch.requestPaused":
                raise RuntimeError(event)
            self.world.handler = handler

        async def send(self, method: str, params: dict[str, object]) -> object:
            self.world.sent.append((method, dict(params)))
            if method == "IO.read" and self.world.io_gate is not None:
                self.world.io_started.set()
                await self.world.io_gate.wait()
            if method == "Fetch.enable" and self.world.enable_sleep:
                await asyncio.sleep(self.world.enable_sleep)
            result = self.world.reply(method)
            if method == "Fetch.enable":
                self.world.order.append("enable")
            elif method == "IO.read":
                self.world.order.append("io")
            elif method == "Fetch.fulfillRequest":
                self.world.order.append("fulfill")
                self.world.fulfilled.set()
            return result

        async def detach(self) -> None:
            self.world.sent.append(("detach", {}))
            self.world.order.append("detach")


async def _invoke(world: _World, deadline: float, **kwargs: object) -> object:
    remaining = deadline - time.monotonic()
    limit = remaining + 0.5 if remaining > 0 else 0.5
    return await asyncio.wait_for(world.run(deadline, **kwargs), timeout=limit)


async def _denied(world: _World, code: str, **kwargs: object) -> None:
    deadline = kwargs.pop("deadline", time.monotonic() + 3)
    before = _identity(kwargs["driver"]) if "driver" in kwargs else _identity(world.driver)
    with pytest.raises(BrowserBlocked) as caught:
        await _invoke(world, deadline, **kwargs)
    assert _code(caught.value) == code
    assert world.response.body_calls == 0
    assert world.response.request.body_calls == 0
    driver = kwargs.get("driver", world.driver)
    assert _identity(driver) == before


async def test_committed_stream_waits_for_fulfill_and_returns_facts() -> None:
    world = _World()
    driver = world.driver
    before = _identity(driver)
    deadline = time.monotonic() + 5
    found = await _invoke(world, deadline)
    assert isinstance(found, _Navigation)
    assert found.body == b"abc"
    assert found.pause_url == _URL
    assert found.page_url == _URL
    assert found.frame_id == _FRAME
    assert found.reference_target_id == _REF
    assert found.proof_possible is False
    assert world.page.url == _URL
    assert world.session_page is world.page
    assert world.session_page is not driver._page
    assert world.context is not driver._context
    assert world.response.request.calls == 1
    assert world.response.body_calls == 0
    assert world.response.request.body_calls == 0
    assert world.goto_kwargs["url"] == _URL
    assert world.goto_kwargs["wait_until"] == "commit"
    assert world.order.index("socket") < world.order.index("enable")
    assert world.order.index("enable") < world.order.index("goto")
    assert world.order.index("goto") < world.order.index("fulfill")
    assert world.order.index("fulfill") < world.order.index("commit")
    enabled = [params for name, params in world.sent if name == "Fetch.enable"]
    assert enabled == [
        {
            "patterns": [
                {
                    "urlPattern": _URL,
                    "resourceType": "Document",
                    "requestStage": "Response",
                }
            ]
        }
    ]
    reads = [params for name, params in world.sent if name == "IO.read"]
    assert [item["size"] for item in reads] == [_IO_SIZE, _IO_SIZE]
    assert [item["handle"] for item in reads] == ["io-1", "io-1"]
    fulfilled = [params for name, params in world.sent if name == "Fetch.fulfillRequest"]
    assert len(fulfilled) == 1
    assert fulfilled[0]["requestId"] == "req-1"
    assert fulfilled[0]["responseCode"] == 200
    assert base64.b64decode(fulfilled[0]["body"]) == b"abc"
    names = [row["name"].casefold() for row in fulfilled[0]["responseHeaders"]]
    assert names == ["content-type"]
    assert "Fetch.failRequest" not in [name for name, _params in world.sent]
    assert _identity(driver) == before


def test_navigation_fact_rejects_proof_possible() -> None:
    with pytest.raises(BrowserBlocked) as caught:
        _Navigation(
            body=b"abc",
            pause_url=_URL,
            frame_id=_FRAME,
            reference_target_id=_REF,
            page_url=_URL,
            proof_possible=True,
        )
    assert _code(caught.value) == "document_read_denied"


async def test_preflight_rejects_alias_cap_and_expired_clock() -> None:
    driver = _Driver()
    before = _identity(driver)
    expired = _World()
    await _denied(expired, "cleanup_uncertain", deadline=time.monotonic() - 1, driver=driver)
    assert expired.order == []
    aliases = (
        {"page": driver._page},
        {"context": driver._context},
        {"target": _OWNED},
        {"target": _PAGE_TARGET},
    )
    for item in aliases:
        world = _World()
        await _denied(world, "document_read_denied", driver=driver, **item)
        assert world.order == []
    for cap in (0, True, 65537):
        world = _World()
        await _denied(world, "document_read_denied", driver=driver, body_cap=cap)
        assert world.order == []
    assert _identity(driver) == before


async def test_pause_and_stream_violations_deny() -> None:
    cases = (
        (_World(chunks=[{"data": "abc", "base64Encoded": True, "eof": True}]), "document_read_denied", "io"),
        (_World(chunks=[{"data": "abc", "base64Encoded": False}]), "cleanup_uncertain", "io"),
        (_World(chunks=[{"data": "é", "base64Encoded": False, "eof": True}]), "document_read_denied", "io"),
        (_World(chunks=[{"data": "abcd", "base64Encoded": False, "eof": True}]), "document_read_denied", "io"),
        (_World(redirect_id="earlier"), "document_read_denied", "goto"),
        (_World(pause_frame="other-frame"), "document_read_denied", "goto"),
        (_World(extra=True), "document_read_denied", "goto"),
        (_World(target_seen="other-target"), "document_read_denied", "socket"),
        (_World(nav=False), "document_read_denied", "fulfill"),
        (_World(early=True), "document_read_denied", "commit"),
    )
    for world, code, marker in cases:
        body_cap = 3 if world.chunks == [{"data": "abcd", "base64Encoded": False, "eof": True}] else 32
        await _denied(world, code, body_cap=body_cap)
        assert marker in world.order
        if marker != "fulfill":
            assert "fulfill" not in world.order


async def test_expiry_before_io_does_not_start_goto() -> None:
    world = _World(enable_sleep=30)
    deadline = time.monotonic() + 0.2
    await _denied(world, "cleanup_uncertain", deadline=deadline)
    assert time.monotonic() <= deadline + 0.05
    assert "enable" not in world.order
    assert "goto" not in world.order
    assert "io" not in world.order


async def test_cancel_during_io_stays_inside_original_deadline() -> None:
    gate = asyncio.Event()
    world = _World(io_gate=gate)
    deadline = time.monotonic() + 1.0
    task = asyncio.create_task(world.run(deadline))
    try:
        await asyncio.wait_for(world.io_started.wait(), timeout=0.5)
        task.cancel()
        with pytest.raises(BrowserBlocked) as caught:
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
        assert _code(caught.value) == "cleanup_uncertain"
        assert time.monotonic() <= deadline + 0.05
        assert "fulfill" not in world.order
        assert world.response.body_calls == 0
        assert _identity(world.driver)[4:] == (_OWNED, _PAGE_TARGET, ())
    finally:
        if not task.done():
            task.cancel()
        with pytest.raises(BrowserBlocked):
            await task


class _SixPauseWorld(_World):
    def __init__(self) -> None:
        super().__init__()
        self.page = self._SixPage(self)

    class _SixPage(_World._Page):
        def goto(self, url: str, **kwargs: object):
            async def _goto() -> object:
                self.world.order.append("goto")
                self.world.goto_kwargs = {"url": url, **kwargs}
                for offset in range(6):
                    self.world.emit(f"req-{offset + 1}")
                await self.world.fulfilled.wait()
                self.url = _URL
                self.world.order.append("commit")
                return self.world.response

            return _goto()


class _LateDetachWorld(_World):
    def __init__(self) -> None:
        super().__init__()
        self.session = self._LateSession(self)

    class _LateSession(_World._Session):
        async def detach(self) -> None:
            self.world.sent.append(("detach", {}))
            self.world.order.append("detach")
            self.world.emit("req-late")


def _request_ids(world: _World) -> set[str]:
    found: set[str] = set()
    for _method, params in world.sent:
        request_id = params.get("requestId")
        if isinstance(request_id, str):
            found.add(request_id)
    return found


async def test_sixth_distinct_pause_is_unretained_cleanup_uncertain() -> None:
    world = _SixPauseWorld()
    deadline = time.monotonic() + 3.0
    before = _identity(world.driver)
    with pytest.raises(BrowserBlocked) as caught:
        await _invoke(world, deadline)
    assert _code(caught.value) == "cleanup_uncertain"
    assert time.monotonic() <= deadline + 0.05
    assert _identity(world.driver) == before
    assert world.response.body_calls == 0
    assert world.response.request.body_calls == 0
    assert "req-6" not in _request_ids(world)


async def test_late_callback_during_detach_withholds_after_fulfill() -> None:
    world = _LateDetachWorld()
    deadline = time.monotonic() + 3.0
    before = _identity(world.driver)
    with pytest.raises(BrowserBlocked) as caught:
        await _invoke(world, deadline)
    assert _code(caught.value) == "cleanup_uncertain"
    assert time.monotonic() <= deadline + 0.05
    assert _identity(world.driver) == before
    assert world.response.body_calls == 0
    assert world.response.request.body_calls == 0
    assert "fulfill" in world.order
    assert "detach" in world.order
    assert world.order.index("fulfill") < world.order.index("detach")
    assert "req-late" not in _request_ids(world)
