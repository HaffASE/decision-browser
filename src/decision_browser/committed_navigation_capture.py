# committed_navigation_capture.py
"""Bounded reference-page document observation. Not authority and not a producer."""

from __future__ import annotations

import asyncio
import base64
import math
import time
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from playwright.async_api import BrowserContext, Page

from decision_browser.browser import BrowserBlocked, BrowserDriver, BrowserMode

_CLEANUP_SECONDS = 1.0
_RESIDUAL_TASKS = "_decision_browser_residual_tasks"
_BODY_CAP = 65536
_IO_READ_SIZE = 16384
_ID_CHARS = 256
_DROPPED_HEADERS = frozenset({"cookie", "set-cookie", "authorization"})

__all__ = [
    "CommittedDocumentNavigation",
    "capture_committed_document_navigation",
]


@dataclass(frozen=True, slots=True)
class CommittedDocumentNavigation:
    """Retained pause bytes plus correlated public navigation facts.

    A caller-built copy is not an input and is not authority.
    """

    body: bytes
    pause_url: str
    frame_id: str
    reference_target_id: str
    page_url: str
    proof_possible: Literal[False] = False

    def __post_init__(self) -> None:
        if (
            type(self.body) is not bytes
            or self.proof_possible is not False
            or type(self.pause_url) is not str
            or type(self.frame_id) is not str
            or type(self.reference_target_id) is not str
            or type(self.page_url) is not str
            or self.pause_url == ""
            or self.frame_id == ""
            or self.reference_target_id == ""
            or self.page_url != self.pause_url
        ):
            raise BrowserBlocked("document_read_denied")


def _deadline(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BrowserBlocked("cleanup_uncertain")
    number = float(value)
    if not math.isfinite(number):
        raise BrowserBlocked("cleanup_uncertain")
    return number


def _main_frame_id(payload: object) -> str:
    if not isinstance(payload, Mapping):
        raise BrowserBlocked("cleanup_uncertain")
    frame_tree = payload.get("frameTree")
    if not isinstance(frame_tree, Mapping):
        raise BrowserBlocked("cleanup_uncertain")
    if frame_tree.get("childFrames") not in (None, []):
        raise BrowserBlocked("document_read_denied")
    frame = frame_tree.get("frame")
    if not isinstance(frame, Mapping):
        raise BrowserBlocked("cleanup_uncertain")
    frame_id = frame.get("id")
    if not isinstance(frame_id, str) or frame_id == "" or len(frame_id) > _ID_CHARS:
        raise BrowserBlocked("cleanup_uncertain")
    return frame_id


def _headers(event: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    if "responseHeaders" not in event or event.get("responseHeaders") is None:
        return ()
    rows = event.get("responseHeaders")
    if not isinstance(rows, list):
        raise BrowserBlocked("cleanup_uncertain")
    kept: list[tuple[str, str]] = []
    for item in rows:
        if not isinstance(item, Mapping):
            raise BrowserBlocked("cleanup_uncertain")
        name = item.get("name")
        value = item.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            raise BrowserBlocked("cleanup_uncertain")
        if name.casefold() not in _DROPPED_HEADERS:
            kept.append((name, value))
    return tuple(kept)


def _pause_facts(
    event: Mapping[str, object],
    *,
    expected_url: str,
    main_frame_id: str,
) -> tuple[str, tuple[tuple[str, str], ...]]:
    request = event.get("request")
    request_id = event.get("requestId")
    known = isinstance(request_id, str) and request_id != ""
    code = "document_read_denied" if known else "cleanup_uncertain"
    if not isinstance(request, Mapping):
        raise BrowserBlocked(code)
    fragment = request.get("urlFragment") if "urlFragment" in request else None
    status = event.get("responseStatusCode")
    if (
        request.get("url") != expected_url
        or request.get("method") != "GET"
        or fragment not in (None, "")
        or event.get("frameId") != main_frame_id
        or event.get("resourceType") != "Document"
        or event.get("redirectedRequestId") is not None
        or event.get("responseErrorReason") is not None
        or type(status) is not int
        or status != 200
    ):
        raise BrowserBlocked(code)
    if not isinstance(request_id, str) or request_id == "":
        raise BrowserBlocked("cleanup_uncertain")
    return request_id, _headers(event)


def _navigation_binding(result: object, page: Page, expected_url: str) -> str:
    if result is None:
        raise BrowserBlocked("document_read_denied")
    request = getattr(result, "request", None)
    is_navigation = getattr(request, "is_navigation_request", None)
    if not callable(is_navigation):
        raise BrowserBlocked("cleanup_uncertain")
    try:
        flag = is_navigation()
        current_url = page.url
    except BrowserBlocked:
        raise
    except Exception as exc:
        raise BrowserBlocked("cleanup_uncertain") from exc
    if flag is not True or getattr(result, "frame", None) is None:
        raise BrowserBlocked("document_read_denied")
    if current_url != expected_url:
        raise BrowserBlocked("document_read_denied")
    return current_url


async def capture_committed_document_navigation(
    *,
    driver: BrowserDriver,
    context: BrowserContext,
    page: Page,
    expected_url: str,
    original_deadline: float,
    body_cap: int,
    reference_target_id: str,
) -> CommittedDocumentNavigation:
    """Fulfill one reference document pause, then await that goto's commit.

    The owned page is never replaced. ``Response.body`` is never called.
    """

    deadline_at = _deadline(original_deadline)
    clock = getattr(driver, "_clock", time.monotonic)
    if clock() >= deadline_at:
        raise BrowserBlocked("cleanup_uncertain")
    if isinstance(body_cap, bool) or type(body_cap) is not int or not 0 < body_cap <= _BODY_CAP:
        raise BrowserBlocked("document_read_denied")
    if (
        not isinstance(expected_url, str)
        or expected_url == ""
        or len(expected_url) > 8192
        or any(mark in expected_url for mark in "#*?\\")
    ):
        raise BrowserBlocked("document_read_denied")
    if (
        not isinstance(reference_target_id, str)
        or reference_target_id == ""
        or len(reference_target_id) > _ID_CHARS
    ):
        raise BrowserBlocked("document_read_denied")
    try:
        if driver.mode is not BrowserMode.isolated:
            raise BrowserBlocked("document_read_denied")
        if driver._state != "open" or driver._page is None or driver._context is None:
            raise BrowserBlocked("not_started")
        owned_page = driver._page
        owned_context = driver._context
        owned_target = driver.owned_target_id
        owned_page_target = driver._owned_page_target_id
        if not isinstance(owned_target, str) or owned_target == "":
            raise BrowserBlocked("not_started")
        if not isinstance(owned_page_target, str) or owned_page_target == "":
            raise BrowserBlocked("not_started")
        if page is owned_page or context is owned_context:
            raise BrowserBlocked("document_read_denied")
        if reference_target_id in (owned_target, owned_page_target):
            raise BrowserBlocked("document_read_denied")
        if len(driver._owned_tasks) != 0:
            raise BrowserBlocked("document_read_denied")
    except BrowserBlocked:
        raise
    except (AttributeError, TypeError) as exc:
        raise BrowserBlocked("cleanup_uncertain") from exc
    if not callable(getattr(context, "new_cdp_session", None)):
        raise BrowserBlocked("cleanup_uncertain")
    if not callable(getattr(page, "goto", None)):
        raise BrowserBlocked("cleanup_uncertain")

    owned_tasks: set[asyncio.Task[Any]] = set()
    session: Any = None
    paused_ids: list[str] = []
    extra_ids: list[str] = []
    failed_ids: set[str] = set()
    snapshot: dict[str, object] | None = None
    buffer = bytearray()
    handle: str | None = None
    stream_closed = False
    fulfilled_id: str | None = None
    enabled = False
    disabled = False
    sealed = False
    unknown_pause = False
    outcome: CommittedDocumentNavigation | None = None
    blocked: BrowserBlocked | None = None
    cancelled = False
    release_ok = True
    goto_task: asyncio.Task[Any] | None = None

    def _track(task: asyncio.Task[Any]) -> None:
        owned_tasks.add(task)

        def _done(finished: asyncio.Task[Any]) -> None:
            if finished is not task:
                return
            finished.remove_done_callback(_done)
            owned_tasks.discard(finished)
            if not finished.cancelled() and finished.exception() is None:
                finished.result()

        task.add_done_callback(_done)

    async def _bounded(awaitable: Awaitable[object]) -> object:
        if deadline_at - clock() <= 0:
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise BrowserBlocked("cleanup_uncertain")
        if not asyncio.iscoroutine(awaitable):
            raise BrowserBlocked("cleanup_uncertain")
        child: asyncio.Task[Any] = asyncio.create_task(awaitable)
        _track(child)
        try:
            timeout = deadline_at - clock()
            pending: set[asyncio.Task[Any]] = {child}
            if timeout > 0:
                _done_set, pending = await asyncio.wait({child}, timeout=timeout)
            if child not in pending and child.done() and not child.cancelled():
                error = child.exception()
                if error is not None:
                    raise BrowserBlocked("cleanup_uncertain") from error
                return child.result()
            if not child.done():
                child.cancel()
            rest = deadline_at - clock()
            if rest > 0 and not child.done():
                await asyncio.wait({child}, timeout=rest)
            if not child.done() or child.cancelled():
                raise BrowserBlocked("cleanup_uncertain")
            error = child.exception()
            if error is not None:
                raise BrowserBlocked("cleanup_uncertain") from error
            return child.result()
        except asyncio.CancelledError:
            if not child.done():
                child.cancel()
            rest = deadline_at - clock()
            if rest > 0 and not child.done():
                try:
                    await asyncio.wait({child}, timeout=rest)
                except asyncio.CancelledError:
                    pass
            raise

    async def _send(method: str, params: Mapping[str, object]) -> object:
        cdp = session
        if cdp is None or not callable(getattr(cdp, "send", None)):
            raise BrowserBlocked("cleanup_uncertain")
        try:
            return await _bounded(cdp.send(method, dict(params)))
        except BrowserBlocked:
            raise
        except Exception as exc:
            raise BrowserBlocked("cleanup_uncertain") from exc

    def _remember(request_id: object) -> None:
        nonlocal unknown_pause
        if not isinstance(request_id, str) or request_id == "" or len(request_id) > _ID_CHARS:
            unknown_pause = True
            return
        if request_id in paused_ids or request_id in extra_ids:
            return
        if paused_ids:
            if len(extra_ids) < 4:
                extra_ids.append(request_id)
            else:
                unknown_pause = True
            return
        paused_ids.append(request_id)

    loop = asyncio.get_running_loop()
    waiter: asyncio.Future[None] = loop.create_future()

    def _on_paused(event: object) -> None:
        nonlocal snapshot
        if sealed or not isinstance(event, Mapping):
            _remember(None)
        else:
            request_id = event.get("requestId")
            _remember(request_id)
            if snapshot is None and len(paused_ids) == 1 and not extra_ids and not unknown_pause:
                request = event.get("request")
                if isinstance(request, Mapping):
                    snapshot = {
                        "requestId": request_id,
                        "request": {
                            "url": request.get("url"),
                            "method": request.get("method"),
                            "urlFragment": request.get("urlFragment"),
                        },
                        "frameId": event.get("frameId"),
                        "resourceType": event.get("resourceType"),
                        "responseStatusCode": event.get("responseStatusCode"),
                        "responseErrorReason": event.get("responseErrorReason"),
                        "redirectedRequestId": event.get("redirectedRequestId"),
                        "responseHeaders": event.get("responseHeaders"),
                    }
                else:
                    _remember(None)
        if not waiter.done():
            waiter.set_result(None)

    async def _fail_open() -> bool:
        ok = True
        for paused_id in tuple(dict.fromkeys((*paused_ids, *extra_ids))):
            if paused_id == fulfilled_id or paused_id in failed_ids:
                continue
            try:
                await _send(
                    "Fetch.failRequest",
                    {"requestId": paused_id, "errorReason": "Aborted"},
                )
            except BrowserBlocked:
                ok = False
            else:
                failed_ids.add(paused_id)
        return ok

    try:
        session = await _bounded(context.new_cdp_session(page))
        if not callable(getattr(session, "on", None)):
            raise BrowserBlocked("cleanup_uncertain")
        info = await _send("Target.getTargetInfo", {})
        target_info = info.get("targetInfo") if isinstance(info, Mapping) else None
        seen = target_info.get("targetId") if isinstance(target_info, Mapping) else None
        if seen != reference_target_id:
            raise BrowserBlocked(
                "document_read_denied" if isinstance(seen, str) else "cleanup_uncertain"
            )
        main_frame_id = _main_frame_id(await _send("Page.getFrameTree", {}))
        session.on("Fetch.requestPaused", _on_paused)
        await _send(
            "Fetch.enable",
            {
                "patterns": [
                    {
                        "urlPattern": expected_url,
                        "resourceType": "Document",
                        "requestStage": "Response",
                    }
                ]
            },
        )
        enabled = True
        if deadline_at - clock() <= 0:
            raise BrowserBlocked("cleanup_uncertain")
        timeout_ms = int((deadline_at - clock()) * 1000)
        if timeout_ms < 1:
            raise BrowserBlocked("cleanup_uncertain")
        started = page.goto(expected_url, wait_until="commit", timeout=timeout_ms)
        if not asyncio.iscoroutine(started):
            raise BrowserBlocked("cleanup_uncertain")
        goto_task = asyncio.create_task(started)
        _track(goto_task)
        if not paused_ids and not extra_ids and not unknown_pause:
            pause_timeout = deadline_at - clock()
            if pause_timeout <= 0:
                raise BrowserBlocked("cleanup_uncertain")
            _done_set, pending_wait = await asyncio.wait({waiter}, timeout=pause_timeout)
            if pending_wait or not waiter.done():
                raise BrowserBlocked("cleanup_uncertain")
            waiter.result()
        if unknown_pause:
            raise BrowserBlocked("cleanup_uncertain")
        if extra_ids or len(paused_ids) != 1 or snapshot is None:
            raise BrowserBlocked("document_read_denied")
        request_id, kept_headers = _pause_facts(
            snapshot,
            expected_url=expected_url,
            main_frame_id=main_frame_id,
        )
        snapshot = None
        if extra_ids or unknown_pause or len(paused_ids) != 1:
            raise BrowserBlocked("document_read_denied")
        stream_payload = await _send(
            "Fetch.takeResponseBodyAsStream",
            {"requestId": request_id},
        )
        stream = stream_payload.get("stream") if isinstance(stream_payload, Mapping) else None
        if not isinstance(stream, str) or stream == "":
            raise BrowserBlocked("cleanup_uncertain")
        handle = stream
        reads = 0
        while True:
            reads += 1
            if reads > body_cap + 1 or extra_ids or unknown_pause or len(paused_ids) != 1:
                buffer.clear()
                raise BrowserBlocked("document_read_denied")
            if goto_task.done():
                buffer.clear()
                raise BrowserBlocked("document_read_denied")
            chunk = await _send("IO.read", {"handle": stream, "size": _IO_READ_SIZE})
            if (
                not isinstance(chunk, Mapping)
                or "base64Encoded" not in chunk
                or chunk.get("base64Encoded") is not False
            ):
                buffer.clear()
                raise BrowserBlocked("document_read_denied")
            data = chunk.get("data")
            if not isinstance(data, str):
                buffer.clear()
                raise BrowserBlocked("document_read_denied")
            try:
                raw = data.encode("ascii")
            except UnicodeError:
                buffer.clear()
                raise BrowserBlocked("document_read_denied") from None
            if not raw and chunk.get("eof") is not True:
                buffer.clear()
                raise BrowserBlocked("document_read_denied")
            if len(buffer) + len(raw) > body_cap:
                buffer.clear()
                raise BrowserBlocked("document_read_denied")
            buffer.extend(raw)
            if chunk.get("eof") is True:
                break
            if chunk.get("eof") is not False:
                buffer.clear()
                raise BrowserBlocked("cleanup_uncertain")
        if goto_task.done() or extra_ids or unknown_pause:
            buffer.clear()
            raise BrowserBlocked("document_read_denied")
        retained = bytes(buffer)
        buffer.clear()
        await _send("IO.close", {"handle": stream})
        stream_closed = True
        if goto_task.done():
            raise BrowserBlocked("document_read_denied")
        await _send(
            "Fetch.fulfillRequest",
            {
                "requestId": request_id,
                "responseCode": 200,
                "responseHeaders": [
                    {"name": name, "value": value} for name, value in kept_headers
                ],
                "body": base64.b64encode(retained).decode("ascii"),
            },
        )
        fulfilled_id = request_id
        await _send("Fetch.disable", {})
        disabled = True
        if goto_task.done() and goto_task.cancelled():
            raise BrowserBlocked("cleanup_uncertain")
        rest = deadline_at - clock()
        if rest <= 0:
            raise BrowserBlocked("cleanup_uncertain")
        _done_goto: set[asyncio.Task[Any]]
        _done_goto, pending_goto = await asyncio.wait({goto_task}, timeout=rest)
        if pending_goto or not goto_task.done():
            goto_task.cancel()
            tail = deadline_at - clock()
            if tail > 0:
                await asyncio.wait({goto_task}, timeout=tail)
            raise BrowserBlocked("cleanup_uncertain")
        if goto_task.cancelled():
            raise BrowserBlocked("cleanup_uncertain")
        goto_error = goto_task.exception()
        if goto_error is not None:
            raise BrowserBlocked("cleanup_uncertain") from goto_error
        page_url = _navigation_binding(goto_task.result(), page, expected_url)
        if fulfilled_id != request_id or extra_ids or unknown_pause or len(paused_ids) != 1:
            raise BrowserBlocked("document_read_denied")
        confirmed = _main_frame_id(await _send("Page.getFrameTree", {}))
        if confirmed != main_frame_id:
            raise BrowserBlocked("document_read_denied")
        if (
            driver._page is not owned_page
            or driver._context is not owned_context
            or driver.owned_target_id != owned_target
            or driver._owned_page_target_id != owned_page_target
            or len(driver._owned_tasks) != 0
        ):
            raise BrowserBlocked("cleanup_uncertain")
        outcome = CommittedDocumentNavigation(
            body=retained,
            pause_url=expected_url,
            frame_id=main_frame_id,
            reference_target_id=reference_target_id,
            page_url=page_url,
        )
    except asyncio.CancelledError:
        cancelled = True
        buffer.clear()
    except BrowserBlocked as exc:
        blocked = exc
        buffer.clear()
    except Exception as exc:  # noqa: BLE001 - retain sanitized failure until owned cleanup has run
        buffer.clear()
        blocked = BrowserBlocked("cleanup_uncertain")
        blocked.__cause__ = exc
    finally:
        sealed = True
        buffer.clear()
        snapshot = None
        current = asyncio.current_task()
        # This new window authorizes teardown only, never another navigation.
        clock = time.monotonic
        deadline_at = clock() + _CLEANUP_SECONDS
        try:
            try:
                if handle is not None and not stream_closed:
                    await _send("IO.close", {"handle": handle})
                    stream_closed = True
            except (BrowserBlocked, asyncio.CancelledError):
                release_ok = False
            seen = -1
            while seen != len(paused_ids) + len(extra_ids):
                if deadline_at - clock() <= 0:
                    release_ok = False
                    break
                seen = len(paused_ids) + len(extra_ids)
                try:
                    if not await _fail_open():
                        release_ok = False
                        break
                except (BrowserBlocked, asyncio.CancelledError):
                    release_ok = False
                    break
            if enabled and not disabled:
                try:
                    await _send("Fetch.disable", {})
                    disabled = True
                except (BrowserBlocked, asyncio.CancelledError):
                    release_ok = False
            if unknown_pause or any(
                request_id != fulfilled_id and request_id not in failed_ids
                for request_id in (*paused_ids, *extra_ids)
            ):
                release_ok = False
            if session is not None:
                if deadline_at - clock() <= 0 or not callable(getattr(session, "detach", None)):
                    release_ok = False
                else:
                    try:
                        await _bounded(session.detach())
                    except (BrowserBlocked, asyncio.CancelledError, Exception):  # noqa: BLE001
                        release_ok = False
            resting = deadline_at - clock()
            pending_tasks = {task for task in owned_tasks if task is not current and not task.done()}
            for task in pending_tasks:
                if not task.cancelling():
                    task.cancel()
            if pending_tasks and resting > 0:
                _done_owned: set[asyncio.Task[Any]]
                _done_owned, still = await asyncio.wait(pending_tasks, timeout=resting)
                if still:
                    release_ok = False
            elif pending_tasks:
                release_ok = False
            for task in list(owned_tasks):
                if task is current:
                    continue
                if not task.done():
                    release_ok = False
                    continue
                owned_tasks.discard(task)
            if unknown_pause or any(
                request_id != fulfilled_id and request_id not in failed_ids
                for request_id in (*paused_ids, *extra_ids)
            ):
                release_ok = False
        except asyncio.CancelledError:
            release_ok = False
        except BrowserBlocked:
            release_ok = False
    residuals = tuple(task for task in owned_tasks if not task.done())
    if residuals:
        owner = asyncio.current_task()
        if owner is not None:
            previous = getattr(owner, _RESIDUAL_TASKS, ())
            held = previous if type(previous) is tuple else ()
            setattr(owner, _RESIDUAL_TASKS, tuple(dict.fromkeys(
                (*[task for task in held if isinstance(task, asyncio.Task) and not task.done()], *residuals)
            )))
    if cancelled or not release_ok:
        error = BrowserBlocked("cleanup_uncertain")
        if residuals:
            error.__dict__["residual_tasks"] = residuals
        raise error
    if blocked is not None:
        raise blocked
    if outcome is None:
        raise BrowserBlocked("cleanup_uncertain")
    return outcome
