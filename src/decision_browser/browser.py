"""Async Playwright driver for an isolated Chromium or one owned Chrome tab.

Existing mode reads only the selected Chrome ``DevToolsActivePort`` file.
Attaching does not establish that the profile is signed in.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Literal, Protocol, Self, TypeGuard

from playwright.async_api import (
    Browser,
    BrowserContext,
    CDPSession,
    Page,
    Playwright,
    async_playwright,
)

SIGNED_IN_PROVENANCE: Literal["UNKNOWN"] = "UNKNOWN"


def default_devtools_active_port() -> Path:
    """Select only the standard Chrome user-data directory for this OS."""
    if sys.platform == "darwin":
        root = Path.home() / "Library/Application Support/Google/Chrome"
    elif sys.platform.startswith("linux"):
        config = os.environ.get("CHROME_CONFIG_HOME") or os.environ.get("XDG_CONFIG_HOME")
        root = (Path(config) if config and Path(config).is_absolute() else Path.home() / ".config") / "google-chrome"
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        root = (Path(local) if local and Path(local).is_absolute() else Path.home() / "AppData/Local") / "Google/Chrome/User Data"
    else:
        raise BrowserBlocked("existing_chrome_unsupported")
    return root / "DevToolsActivePort"


# Kept as the public platform default; construction resolves it afresh.
DEFAULT_DEVTOOLS_ACTIVE_PORT = (
    default_devtools_active_port()
    if sys.platform in {"darwin", "win32"} or sys.platform.startswith("linux") else None
)
_OWNED_CLEANUP_SECONDS = 1.0

DEFAULT_CDP_TIMEOUT_MS = 10_000.0
MAX_CDP_TIMEOUT_MS = 30_000.0
_DEVTOOLS_PORT_BYTE_CAP = 4096
_INVENTORY_BYTE_CAP = 1_048_576
_INVENTORY_TARGET_CAP = 512
# App retention limits for one owned document. Not a browser, network, or whole-response bound.
_DOCUMENT_IO_READ_SIZE = 16_384
_DOCUMENT_BODY_CAP = 65_536
# Pause-id and header retention for one owned read. Not a browser, RSS, or network buffer.
_PAUSED_ID_CAP = 8
_PAUSED_ID_CHARS = 128
_PAUSED_FIELD_CHARS = 8192
_PAUSED_HEADER_CAP = 64
_PAUSED_METADATA_CAP = 16_384
_DROPPED_DOCUMENT_HEADERS = frozenset({"cookie", "set-cookie", "authorization"})

_PORT_RE = re.compile(r"^[1-9][0-9]{0,4}$")
_BROWSER_PATH_RE = re.compile(r"^/devtools/browser/[A-Za-z0-9-]{1,128}$")
_ENDPOINT_RE = re.compile(
    r"^ws://127\.0\.0\.1:(?P<port>[1-9][0-9]{0,4})"
    r"(?P<path>/devtools/browser/[A-Za-z0-9-]{1,128})$"
)

_BLOCKED_MESSAGES: dict[str, str] = {
    "existing_chrome_unsupported": "Existing Chrome has no supported default profile on this OS.",
    "invalid_mode": "Browser mode must be isolated or existing_chrome.",
    "invalid_timeout": "CDP timeout must be a finite value inside the allowed bound.",
    "invalid_devtools_path": "DevToolsActivePort must be a regular file path.",
    "already_started": "BrowserDriver is already started.",
    "already_closed": "BrowserDriver cannot start again after close.",
    "not_started": "BrowserDriver is not started.",
    "devtools_port_unreadable": "Selected DevToolsActivePort could not be read.",
    "devtools_port_too_large": "DevToolsActivePort exceeds the 4096-byte cap.",
    "devtools_port_malformed": "DevToolsActivePort must be exactly two strict lines.",
    "endpoint_rejected": "DevTools endpoint must be a loopback WebSocket with a full browser path.",
    "cdp_connect_failed": "Could not attach to the selected Chrome endpoint.",
    "context_count": "Existing Chrome must expose exactly one default context.",
    "inventory_failed": "Could not read the local CDP target inventory.",
    "owned_page_failed": "Could not create exactly one owned page.",
    "ownership_unverified": "Owned target id could not be verified.",
    "launch_failed": "Could not launch an isolated Chromium.",
    "screenshot_failed": "Could not capture a screenshot.",
    "document_read_denied": "Owned document body was not retained.",
    "cleanup_uncertain": "Browser cleanup could not be confirmed.",
}


class BrowserBlocked(Exception):
    """Sanitized driver failure. ``code`` is stable; the message has no file body or cookies."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message if message is not None else _BLOCKED_MESSAGES.get(
            code, "Browser operation was blocked."
        )
        super().__init__(f"{self.code}: {self.message}")


class BrowserMode(str, Enum):
    isolated = "isolated"
    existing_chrome = "existing_chrome"


@dataclass(frozen=True, slots=True)
class DevtoolsEndpoint:
    port: int
    browser_path: str
    websocket_url: str


@dataclass(frozen=True, slots=True)
class TargetRecord:
    target_id: str
    url: str


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    targets: tuple[TargetRecord, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class OwnedDocumentBody:
    """Immutable owned document bytes. Not capture evidence."""

    sha256: str
    body: bytes
    response_headers: tuple[tuple[str, str], ...]
    proof_possible: Literal[False] = False

    def __post_init__(self) -> None:
        if type(self.body) is not bytes or self.proof_possible is not False:
            raise BrowserBlocked("document_read_denied")


def _pause_request_id(event: object) -> str | None:
    if not isinstance(event, Mapping):
        return None
    request_id = event.get("requestId")
    if isinstance(request_id, str) and request_id:
        return request_id
    return None


def _document_response_headers(event: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    """Filter secret headers before any header is retained."""
    if "responseHeaders" not in event or event.get("responseHeaders") is None:
        return ()
    headers = event.get("responseHeaders")
    if not isinstance(headers, list):
        raise BrowserBlocked("cleanup_uncertain")
    kept: list[tuple[str, str]] = []
    for item in headers:
        if not isinstance(item, Mapping):
            raise BrowserBlocked("cleanup_uncertain")
        name = item.get("name")
        value = item.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            raise BrowserBlocked("cleanup_uncertain")
        if name.casefold() in _DROPPED_DOCUMENT_HEADERS:
            continue
        kept.append((name, value))
    return tuple(kept)


def _expected_document_pause(
    event: object,
    *,
    expected_url: str,
    main_frame_id: str,
) -> tuple[str, tuple[tuple[str, str], ...]]:
    if not isinstance(event, Mapping):
        raise BrowserBlocked("cleanup_uncertain")
    request = event.get("request")
    if not isinstance(request, Mapping):
        raise BrowserBlocked("cleanup_uncertain")
    request_id = event.get("requestId")
    known_id = isinstance(request_id, str) and request_id != ""
    shape_code = "document_read_denied" if known_id else "cleanup_uncertain"
    if request.get("url") != expected_url or request.get("method") != "GET":
        raise BrowserBlocked(shape_code)
    if "urlFragment" in request and request.get("urlFragment") not in ("", None):
        raise BrowserBlocked(shape_code)
    if event.get("frameId") != main_frame_id or event.get("resourceType") != "Document":
        raise BrowserBlocked(shape_code)
    if event.get("redirectedRequestId") is not None or event.get("responseErrorReason") is not None:
        raise BrowserBlocked(shape_code)
    status = event.get("responseStatusCode")
    if type(status) is not int or status != 200:
        raise BrowserBlocked(shape_code)
    if not isinstance(request_id, str) or request_id == "":
        raise BrowserBlocked("cleanup_uncertain")
    return request_id, _document_response_headers(event)


def _sanitize_paused_event(event: object) -> dict[str, object] | None:
    """Bounded pause metadata with Cookie, Set-Cookie, and Authorization removed."""
    if not isinstance(event, Mapping):
        return None
    request_id = event.get("requestId")
    if not isinstance(request_id, str) or request_id == "" or len(request_id) > _PAUSED_ID_CHARS:
        return None
    request = event.get("request")
    if not isinstance(request, Mapping):
        return None
    url = request.get("url")
    method = request.get("method")
    if (
        not isinstance(url, str)
        or url == ""
        or len(url) > _PAUSED_FIELD_CHARS
        or not isinstance(method, str)
        or method == ""
        or len(method) > 64
    ):
        return None
    safe_request: dict[str, object] = {"url": url, "method": method}
    if "urlFragment" in request:
        fragment = request.get("urlFragment")
        if fragment is None or (isinstance(fragment, str) and len(fragment) <= _PAUSED_FIELD_CHARS):
            safe_request["urlFragment"] = fragment
        else:
            return None
    frame_id = event.get("frameId")
    resource_type = event.get("resourceType")
    if not isinstance(frame_id, str) or frame_id == "" or len(frame_id) > _PAUSED_ID_CHARS:
        return None
    if not isinstance(resource_type, str) or len(resource_type) > 64:
        return None
    sanitized: dict[str, object] = {
        "requestId": request_id,
        "request": safe_request,
        "frameId": frame_id,
        "resourceType": resource_type,
    }
    if event.get("redirectedRequestId") is not None:
        sanitized["redirectedRequestId"] = ""
    if event.get("responseErrorReason") is not None:
        sanitized["responseErrorReason"] = ""
    if "responseStatusCode" in event and type(event.get("responseStatusCode")) is int:
        sanitized["responseStatusCode"] = event.get("responseStatusCode")
    if "responseHeaders" not in event or event.get("responseHeaders") is None:
        return sanitized
    headers = event.get("responseHeaders")
    if not isinstance(headers, list) or len(headers) > _PAUSED_HEADER_CAP:
        return None
    kept: list[dict[str, str]] = []
    kept_chars = 0
    for item in headers:
        if not isinstance(item, Mapping):
            return None
        name = item.get("name")
        value = item.get("value")
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or name == ""
            or len(name) > _PAUSED_ID_CHARS
            or len(value) > _PAUSED_FIELD_CHARS
        ):
            return None
        if name.casefold() in _DROPPED_DOCUMENT_HEADERS:
            continue
        kept_chars += len(name) + len(value)
        if kept_chars > _PAUSED_METADATA_CAP:
            return None
        kept.append({"name": name, "value": value})
    sanitized["responseHeaders"] = kept
    return sanitized


def digest_inventory(targets: Sequence[TargetRecord]) -> str:
    ordered = tuple(sorted(targets, key=lambda item: item.target_id))
    payload = json.dumps(
        [{"id": item.target_id, "url": item.url} for item in ordered],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _snapshot(targets: Sequence[TargetRecord]) -> InventorySnapshot:
    ordered = tuple(sorted(targets, key=lambda item: item.target_id))
    return InventorySnapshot(targets=ordered, digest=digest_inventory(ordered))


def _snapshot_from_target_infos(payload: object) -> InventorySnapshot:
    """Keep ``type == page`` targets only, bounded by count and canonical bytes."""
    if not isinstance(payload, Mapping):
        raise BrowserBlocked("inventory_failed")
    infos = payload.get("targetInfos")
    if not isinstance(infos, list) or len(infos) > _INVENTORY_TARGET_CAP:
        raise BrowserBlocked("inventory_failed")
    records: list[TargetRecord] = []
    seen: set[str] = set()
    for item in infos:
        if not isinstance(item, Mapping):
            raise BrowserBlocked("inventory_failed")
        if item.get("type") != "page":
            continue
        target_id = item.get("targetId")
        page_url = item.get("url")
        if not isinstance(target_id, str) or not target_id or not isinstance(page_url, str):
            raise BrowserBlocked("inventory_failed")
        if target_id in seen or len(records) >= _INVENTORY_TARGET_CAP:
            raise BrowserBlocked("inventory_failed")
        seen.add(target_id)
        records.append(TargetRecord(target_id=target_id, url=page_url))
    snapshot = _snapshot(records)
    encoded = json.dumps(
        [{"id": item.target_id, "url": item.url} for item in snapshot.targets],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    if len(encoded) > _INVENTORY_BYTE_CAP:
        raise BrowserBlocked("inventory_failed")
    return snapshot


def _port_in_range(text: str) -> int:
    if _PORT_RE.fullmatch(text) is None:
        raise BrowserBlocked("devtools_port_malformed")
    port = int(text)
    if port > 65535:
        raise BrowserBlocked("devtools_port_malformed")
    return port


def parse_devtools_active_port(data: bytes) -> DevtoolsEndpoint:
    """Parse a capped DevToolsActivePort body into a loopback WebSocket URL.

    The file is exactly two strict lines, ``port`` and ``browser_path``. A
    single trailing newline is optional. CR, a blank extra line, or
    surrounding whitespace is rejected.
    """
    if len(data) > _DEVTOOLS_PORT_BYTE_CAP:
        raise BrowserBlocked("devtools_port_too_large")
    if b"\r" in data or data.endswith(b"\n\n"):
        raise BrowserBlocked("devtools_port_malformed")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise BrowserBlocked("devtools_port_malformed") from exc
    text = text.removesuffix("\n")
    fields = text.split("\n")
    if len(fields) != 2:
        raise BrowserBlocked("devtools_port_malformed")
    port_text, browser_path = fields
    if (
        port_text == ""
        or browser_path == ""
        or port_text.strip() != port_text
        or browser_path.strip() != browser_path
    ):
        raise BrowserBlocked("devtools_port_malformed")
    if _BROWSER_PATH_RE.fullmatch(browser_path) is None:
        raise BrowserBlocked("endpoint_rejected")
    port = _port_in_range(port_text)
    websocket_url = f"ws://127.0.0.1:{port}{browser_path}"
    match = _ENDPOINT_RE.fullmatch(websocket_url)
    if match is None or int(match.group("port")) != port or match.group("path") != browser_path:
        raise BrowserBlocked("endpoint_rejected")
    return DevtoolsEndpoint(port=port, browser_path=browser_path, websocket_url=websocket_url)


def read_devtools_active_port(path: Path) -> DevtoolsEndpoint:
    """Read one selected file. Does not search sibling profiles or follow links."""
    if not isinstance(path, Path):
        raise BrowserBlocked("invalid_devtools_path")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise BrowserBlocked("devtools_port_unreadable") from exc
    try:
        info = os.fstat(fd)
        current = path.lstat()
        if (stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
                or (os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o022))):
            raise BrowserBlocked("devtools_port_unreadable")
        blob = os.read(fd, _DEVTOOLS_PORT_BYTE_CAP + 1)
    except BrowserBlocked:
        raise
    except OSError as exc:
        raise BrowserBlocked("devtools_port_unreadable") from exc
    finally:
        os.close(fd)
    return parse_devtools_active_port(blob)


def _coerce_mode(mode: BrowserMode | str) -> BrowserMode:
    try:
        return BrowserMode(mode)
    except ValueError as exc:
        raise BrowserBlocked("invalid_mode") from exc


def _coerce_timeout(timeout_ms: float) -> float:
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, (int, float)):
        raise BrowserBlocked("invalid_timeout")
    value = float(timeout_ms)
    if not math.isfinite(value) or value <= 0 or value > MAX_CDP_TIMEOUT_MS:
        raise BrowserBlocked("invalid_timeout")
    return value


class _CdpEventSource(Protocol):
    def on(self, event: str, handler: object) -> None: ...


def _has_callable_on(session: object) -> TypeGuard[_CdpEventSource]:
    return callable(getattr(session, "on", None))


def _owned_main_frame(tree: object) -> str:
    """Main frame id. Mapping and a non-empty str, or cleanup_uncertain."""
    if not isinstance(tree, Mapping):
        raise BrowserBlocked("cleanup_uncertain")
    frame_tree = tree.get("frameTree")
    if not isinstance(frame_tree, Mapping):
        raise BrowserBlocked("cleanup_uncertain")
    frame = frame_tree.get("frame")
    if not isinstance(frame, Mapping):
        raise BrowserBlocked("cleanup_uncertain")
    main_frame_id = frame.get("id")
    if not isinstance(main_frame_id, str) or main_frame_id == "":
        raise BrowserBlocked("cleanup_uncertain")
    return main_frame_id


def _literal_fetch_pattern(url: str) -> str:
    """CDP Fetch.RequestPattern uses backslash-escaped *, ? and backslash."""
    return url.replace("\\", "\\\\").replace("*", "\\*").replace("?", "\\?")


@dataclass(slots=True)
class _OwnedInitialObservation:
    """Driver-private bytes for one owner comparison. Not a seal."""

    request_id: str
    stream_handle: str
    response_headers: tuple[tuple[str, str], ...]
    body: bytes
    release_ok: bool
    cdp_methods: tuple[str, ...]
    main_frame_id: str

    def drop_body(self) -> None:
        self.body = b""


@dataclass(slots=True)
class _OwnedInitialArm:
    """Single-use Fetch arm. enable_returned is the CDP ACK, not the cleanup flag."""

    expected_url: str
    deadline: float
    budget: object
    owner: object
    resource: object
    page: object
    context: object
    target_id: str
    main_frame_id: str = ""
    session: object | None = None
    enable_returned: bool = False
    pause_before_note: bool = False
    pause_at_enable: bool = False
    unknown_pause: bool = False
    paused_overflow: bool = False
    paused_ids: list[str] = field(default_factory=list)
    overflow_ids: list[str] = field(default_factory=list)
    failed_ids: set[str] = field(default_factory=set)
    tried_ids: set[str] = field(default_factory=set)
    sanitized: list[dict[str, object]] = field(default_factory=list)
    gate: dict[str, object] = field(
        default_factory=lambda: {
            "handle": None,
            "stream_closed": False,
            "request_id": None,
            "fulfilled": False,
            "enabled": False,
            "disabled": False,
        }
    )
    acks: list[str] = field(default_factory=list)
    waiter: asyncio.Task[None] | None = None
    observation: _OwnedInitialObservation | None = None
    release_ok: bool = False
    finished: bool = False
    cleanup_deadline: float | None = None
    note: asyncio.Event = field(init=False)
    wake: asyncio.Future[None] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.note = asyncio.Event()


class BrowserDriver:
    """One isolated browser, or one owned page on an already-running Chrome.

    ``signed_in_provenance`` stays ``UNKNOWN``. A successful attach is not a
    claim that the selected profile is signed in.
    """

    def __init__(
        self,
        mode: BrowserMode | str,
        *,
        devtools_active_port: Path | None = None,
        cdp_timeout_ms: float = DEFAULT_CDP_TIMEOUT_MS,
    ) -> None:
        self._clock: Callable[[], float] = time.monotonic
        self.mode = _coerce_mode(mode)
        self._devtools_path = (
            default_devtools_active_port()
            if devtools_active_port is None and self.mode is BrowserMode.existing_chrome
            else devtools_active_port
        )
        self._timeout_ms = _coerce_timeout(cdp_timeout_ms)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._state: Literal["idle", "starting", "open", "closing", "cleanup_pending", "closed"] = "idle"
        self._cleanup_mode_done = False
        self._owned_page_closed = False
        self._ownership_session: CDPSession | None = None
        self._owned_target_ids: set[str] = set()
        self._closed_target_ids: set[str] = set()
        self._target_openers: dict[str, str] = {}
        self._ownership_discovery_uncertain = False
        self._isolated_context_closed = False
        self._isolated_browser_closed = False
        self._endpoint: DevtoolsEndpoint | None = None
        self.owned_target_id: str | None = None
        self._owned_page_target_id: str | None = None
        self._owned_tasks: set[asyncio.Task[object]] = set()
        self._owned_read_deadline: float | None = None
        self.signed_in_provenance: Literal["UNKNOWN"] = SIGNED_IN_PROVENANCE
        self.foreign_before: InventorySnapshot | None = None
        self.foreign_after: InventorySnapshot | None = None
        self.foreign_mismatch: bool | None = None
        self.residual_owned: bool | None = None
        self.cleanup_errors: list[str] = []
        self._owned_binding: tuple[object, object, object, object, object] | None = None
        self._owned_arm: _OwnedInitialArm | None = None

    @property
    def cleanup_pending(self) -> bool:
        """Exact retained cleanup work; historical diagnostics alone are not work."""
        return self._state == "cleanup_pending" and (
            not self._cleanup_mode_done or self._playwright is not None
            or any(not task.done() for task in self._owned_tasks)
        )

    @property
    def page(self) -> Page:
        if self._page is None:
            raise BrowserBlocked("not_started")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise BrowserBlocked("not_started")
        return self._context

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def start(self) -> None:
        if self._state == "open":
            raise BrowserBlocked("already_started")
        if self._state != "idle":
            raise BrowserBlocked("already_closed")
        self._state = "starting"
        try:
            self._playwright = await async_playwright().start()
            if self.mode is BrowserMode.isolated:
                await self._start_isolated()
            else:
                await self._start_existing()
        except asyncio.CancelledError:
            try:
                await self._cleanup(starting_failure=True)
            finally:
                self._finish_cleanup_state()
            raise
        except Exception:
            try:
                await self._cleanup(starting_failure=True)
            finally:
                self._finish_cleanup_state()
            raise
        self._state = "open"

    async def close(self) -> None:
        if self._state in {"closed", "idle"}:
            self._state = "closed"
            return
        if self._state == "closing":
            return
        self._state = "closing"
        try:
            await self._cleanup(starting_failure=False)
        finally:
            self._finish_cleanup_state()

    def _finish_cleanup_state(self) -> None:
        if (self._cleanup_mode_done and self._playwright is None
                and not any(not task.done() for task in self._owned_tasks)):
            self._page = None
            self._context = None
            self._browser = None
            self._state = "closed"
        else:
            self._state = "cleanup_pending"

    async def screenshot(self) -> bytes:
        if self._state != "open" or self._page is None:
            raise BrowserBlocked("not_started")
        try:
            data = await self._page.screenshot(type="png")
        except Exception as exc:
            raise BrowserBlocked("screenshot_failed") from exc
        if not isinstance(data, (bytes, bytearray)):
            raise BrowserBlocked("screenshot_failed")
        return bytes(data)

    async def read_owned_document_body(self, expected_url: str, deadline: float) -> OwnedDocumentBody:
        """Read one intercepted isolated document body. Does not navigate."""
        if self._state != "open" or self._page is None or self._context is None:
            raise BrowserBlocked("not_started")
        if self.mode is not BrowserMode.isolated:
            raise BrowserBlocked("document_read_denied")
        if not isinstance(self._owned_page_target_id, str) or self._owned_page_target_id == "":
            raise BrowserBlocked("not_started")
        if self._owned_tasks:
            raise BrowserBlocked("document_read_denied")
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(float(deadline))
        ):
            raise BrowserBlocked("cleanup_uncertain")
        deadline_at = float(deadline)
        self._owned_read_deadline = deadline_at
        task: asyncio.Task[OwnedDocumentBody] = asyncio.create_task(
            self._read_owned_document_body_task(expected_url, deadline_at)
        )
        self._owned_tasks.add(task)

        def _release_owned_registration(finished: asyncio.Task[OwnedDocumentBody]) -> None:
            if finished is not task:
                return
            finished.remove_done_callback(_release_owned_registration)
            self._owned_tasks.discard(finished)
            if not finished.cancelled() and finished.exception() is None:
                finished.result()
            if not self._owned_tasks:
                self._owned_read_deadline = None

        task.add_done_callback(_release_owned_registration)
        try:
            return await task
        finally:
            try:
                if not task.done():
                    task.cancel()
                if not task.done():
                    remaining = _OWNED_CLEANUP_SECONDS
                    if remaining > 0:
                        await asyncio.wait({task}, timeout=remaining)
            finally:
                if task.done():
                    self._owned_tasks.discard(task)
                    if not task.cancelled() and task.exception() is None:
                        task.result()
                if not self._owned_tasks:
                    self._owned_read_deadline = None
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise asyncio.CancelledError

    async def _read_owned_document_body_task(
        self,
        expected_url: str,
        deadline: float,
    ) -> OwnedDocumentBody:
        if self.mode is not BrowserMode.isolated:
            raise BrowserBlocked("document_read_denied")
        if self._state != "open" or self._page is None or self._context is None:
            raise BrowserBlocked("not_started")
        if len(self._owned_tasks) > 1:
            raise BrowserBlocked("document_read_denied")
        owned_target_id = self._owned_page_target_id
        if not isinstance(owned_target_id, str) or owned_target_id == "":
            raise BrowserBlocked("not_started")
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(float(deadline))
        ):
            raise BrowserBlocked("cleanup_uncertain")
        deadline_at = float(deadline)
        clock = self._clock
        session = None
        sanitized: list[dict[str, object]] = []
        paused_ids: list[str] = []
        overflow_ids: list[str] = []
        failed_ids: set[str] = set()
        tried_ids: set[str] = set()
        buffer = bytearray()
        outcome: OwnedDocumentBody | None = None
        blocked: BrowserBlocked | None = None
        cancelled = False
        release_ok = True
        unknown_pause = False
        paused_overflow = False
        sealed = False
        gate: dict[str, object] = {
            "handle": None,
            "stream_closed": False,
            "request_id": None,
            "fulfilled": False,
            "enabled": False,
            "disabled": False,
        }

        def _remaining() -> float:
            remaining = deadline_at - clock()
            if remaining <= 0:
                raise BrowserBlocked("cleanup_uncertain")
            return remaining

        async def _bounded_call(factory: object) -> object:
            if deadline_at - clock() <= 0:
                raise BrowserBlocked("cleanup_uncertain")
            if not callable(factory):
                raise BrowserBlocked("cleanup_uncertain")
            awaitable = factory()
            if not asyncio.iscoroutine(awaitable):
                raise BrowserBlocked("cleanup_uncertain")
            child: asyncio.Task[object] = asyncio.create_task(awaitable)

            def _release_child(finished: asyncio.Task[object]) -> None:
                if finished is not child:
                    return
                finished.remove_done_callback(_release_child)
                self._owned_tasks.discard(child)
                if not child.cancelled() and child.exception() is None:
                    child.result()
                if not self._owned_tasks:
                    self._owned_read_deadline = None

            self._owned_tasks.add(child)
            child.add_done_callback(_release_child)
            try:
                timeout = deadline_at - clock()
                pending = {child}
                if timeout > 0:
                    _done, pending = await asyncio.wait({child}, timeout=timeout)
                if child not in pending and child.done():
                    if child.cancelled():
                        raise BrowserBlocked("cleanup_uncertain")
                    return child.result()
                if not child.done():
                    child.cancel()
                rest = deadline_at - clock()
                if rest > 0 and not child.done():
                    await asyncio.wait({child}, timeout=rest)
                if not child.done() or child.cancelled():
                    raise BrowserBlocked("cleanup_uncertain")
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
            if cdp is None:
                raise BrowserBlocked("cleanup_uncertain")
            try:
                return await _bounded_call(lambda: cdp.send(method, dict(params)))
            except BrowserBlocked:
                raise
            except TimeoutError as exc:
                raise BrowserBlocked("cleanup_uncertain") from exc
            except Exception as exc:
                raise BrowserBlocked("cleanup_uncertain") from exc

        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[None] = loop.create_future()

        def _track_pause_id(request_id: object) -> None:
            nonlocal unknown_pause, paused_overflow
            if not isinstance(request_id, str) or request_id == "" or len(request_id) > _PAUSED_ID_CHARS:
                unknown_pause = True
                return
            if request_id in paused_ids or request_id in overflow_ids:
                return
            if len(paused_ids) >= _PAUSED_ID_CAP:
                paused_overflow = True
                if len(overflow_ids) < _PAUSED_ID_CAP:
                    overflow_ids.append(request_id)
                    return
                for index, prior in enumerate(overflow_ids):
                    if prior in failed_ids:
                        overflow_ids[index] = request_id
                        return
                return
            paused_ids.append(request_id)

        def _on_paused(event: object) -> None:
            request_id = _pause_request_id(event)
            if isinstance(request_id, str) and len(request_id) > _PAUSED_ID_CHARS:
                request_id = None
            _track_pause_id(request_id)
            if sealed or unknown_pause or paused_overflow or len(paused_ids) != 1:
                sanitized.clear()
            elif not sanitized:
                kept = _sanitize_paused_event(event)
                if kept is None:
                    _track_pause_id(None)
                    sanitized.clear()
                else:
                    sanitized.append(kept)
            if not waiter.done():
                waiter.set_result(None)

        def _pause_failed() -> bool:
            return unknown_pause or paused_overflow or len(paused_ids) != 1

        async def _drain_unfailed() -> bool:
            drained = True
            for _ in range(_PAUSED_ID_CAP + 1):
                pending = [
                    request_id
                    for request_id in (*paused_ids, *overflow_ids)
                    if request_id not in tried_ids
                    and not (
                        request_id == gate.get("request_id") and gate.get("fulfilled") is True
                    )
                ]
                if not pending:
                    return drained
                for request_id in pending:
                    tried_ids.add(request_id)
                    try:
                        await _send(
                            "Fetch.failRequest",
                            {"requestId": request_id, "errorReason": "BlockedByClient"},
                        )
                    except BrowserBlocked:
                        drained = False
                    else:
                        failed_ids.add(request_id)
            pending = [
                request_id
                for request_id in (*paused_ids, *overflow_ids)
                if request_id not in tried_ids
                and not (request_id == gate.get("request_id") and gate.get("fulfilled") is True)
            ]
            if pending:
                return False
            return drained

        async def _release() -> bool:
            ok = True
            handle = gate.get("handle")
            if isinstance(handle, str) and handle and gate.get("stream_closed") is not True:
                try:
                    await _send("IO.close", {"handle": handle})
                    gate["stream_closed"] = True
                except BrowserBlocked:
                    ok = False
            if not await _drain_unfailed():
                ok = False
            if gate.get("enabled") is True and gate.get("disabled") is not True:
                try:
                    await _send("Fetch.disable", {})
                    gate["disabled"] = True
                except BrowserBlocked:
                    ok = False
            if not await _drain_unfailed():
                ok = False
            if unknown_pause or paused_overflow:
                ok = False
            for request_id in (*paused_ids, *overflow_ids):
                fulfilled = request_id == gate.get("request_id") and gate.get("fulfilled") is True
                if not fulfilled and request_id not in failed_ids:
                    ok = False
            return ok

        try:
            try:
                session = await _bounded_call(
                    lambda: self._context.new_cdp_session(self._page)
                )
            except BrowserBlocked:
                raise
            except TimeoutError as exc:
                raise BrowserBlocked("cleanup_uncertain") from exc
            except Exception as exc:
                raise BrowserBlocked("cleanup_uncertain") from exc
            if self._state != "open":
                raise BrowserBlocked("cleanup_uncertain")
            info = await _send("Target.getTargetInfo", {})
            if not isinstance(info, Mapping):
                raise BrowserBlocked("cleanup_uncertain")
            target_info = info.get("targetInfo")
            if not isinstance(target_info, Mapping) or target_info.get("targetId") != owned_target_id:
                raise BrowserBlocked("cleanup_uncertain")
            tree = await _send("Page.getFrameTree", {})
            if not isinstance(tree, Mapping):
                raise BrowserBlocked("cleanup_uncertain")
            frame_tree = tree.get("frameTree")
            if not isinstance(frame_tree, Mapping):
                raise BrowserBlocked("cleanup_uncertain")
            frame = frame_tree.get("frame")
            if not isinstance(frame, Mapping):
                raise BrowserBlocked("cleanup_uncertain")
            main_frame_id = frame.get("id")
            if not isinstance(main_frame_id, str) or main_frame_id == "":
                raise BrowserBlocked("cleanup_uncertain")
            if not _has_callable_on(session):
                raise BrowserBlocked("cleanup_uncertain")
            session.on("Fetch.requestPaused", _on_paused)
            gate["enabled"] = True
            await _send(
                "Fetch.enable",
                {
                    "patterns": [
                        {
                            "urlPattern": _literal_fetch_pattern(expected_url),
                            "resourceType": "Document",
                            "requestStage": "Response",
                        }
                    ]
                },
            )
            if not paused_ids and not unknown_pause and not paused_overflow:
                try:
                    pause_timeout = _remaining()
                    _done, pending_wait = await asyncio.wait({waiter}, timeout=pause_timeout)
                    if pending_wait or not waiter.done():
                        raise BrowserBlocked("cleanup_uncertain")
                    waiter.result()
                except BrowserBlocked:
                    raise
                except TimeoutError as exc:
                    raise BrowserBlocked("cleanup_uncertain") from exc
            if _pause_failed() or len(sanitized) != 1:
                raise BrowserBlocked("cleanup_uncertain")
            event = sanitized[0]
            request_id = _pause_request_id(event)
            if request_id is not None:
                gate["request_id"] = request_id
            try:
                parsed_id, filtered_headers = _expected_document_pause(
                    event,
                    expected_url=expected_url,
                    main_frame_id=main_frame_id,
                )
            finally:
                del event
                sanitized.clear()
            gate["request_id"] = parsed_id
            if _pause_failed():
                raise BrowserBlocked("cleanup_uncertain")
            if _pause_failed():
                raise BrowserBlocked("cleanup_uncertain")
            stream_payload = await _send(
                "Fetch.takeResponseBodyAsStream",
                {"requestId": parsed_id},
            )
            if not isinstance(stream_payload, Mapping):
                raise BrowserBlocked("cleanup_uncertain")
            handle = stream_payload.get("stream")
            if not isinstance(handle, str) or handle == "":
                raise BrowserBlocked("cleanup_uncertain")
            gate["handle"] = handle
            while True:
                if self._state != "open" or _pause_failed():
                    buffer.clear()
                    raise BrowserBlocked("cleanup_uncertain")
                chunk = await _send(
                    "IO.read",
                    {"handle": handle, "size": _DOCUMENT_IO_READ_SIZE},
                )
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
                if len(buffer) + len(raw) > _DOCUMENT_BODY_CAP:
                    buffer.clear()
                    raise BrowserBlocked("document_read_denied")
                buffer.extend(raw)
                if chunk.get("eof") is True:
                    break
            if _pause_failed() or self._state != "open":
                buffer.clear()
                raise BrowserBlocked("cleanup_uncertain")
            raw_body = bytes(buffer)
            digest = hashlib.sha256(raw_body).hexdigest()
            await _send("IO.close", {"handle": handle})
            gate["stream_closed"] = True
            if _pause_failed():
                buffer.clear()
                raise BrowserBlocked("cleanup_uncertain")
            await _send(
                "Fetch.fulfillRequest",
                {
                    "requestId": parsed_id,
                    "responseCode": 200,
                    "responseHeaders": [
                        {"name": name, "value": value} for name, value in filtered_headers
                    ],
                    "body": base64.b64encode(raw_body).decode("ascii"),
                },
            )
            gate["fulfilled"] = True
            await _send("Fetch.disable", {})
            gate["disabled"] = True
            if _pause_failed():
                raise BrowserBlocked("cleanup_uncertain")
            outcome = OwnedDocumentBody(
                sha256=digest,
                body=raw_body,
                response_headers=filtered_headers,
            )
        except asyncio.CancelledError:
            cancelled = True
        except BrowserBlocked as exc:
            blocked = exc
        except Exception as exc:
            raise BrowserBlocked("cleanup_uncertain") from exc
        finally:
            buffer.clear()
            sanitized.clear()
            sealed = True
            # Execution has ended; cleanup gets one bounded real-time window.
            clock = time.monotonic
            deadline_at = clock() + _OWNED_CLEANUP_SECONDS
            try:
                try:
                    release_ok = await _release()
                except asyncio.CancelledError:
                    release_ok = False
                except BrowserBlocked:
                    release_ok = False
            finally:
                if session is not None:
                    if deadline_at - clock() <= 0:
                        release_ok = False
                    else:
                        try:
                            await _bounded_call(lambda: self._detach_session(session))
                        except TimeoutError:
                            release_ok = False
                        except asyncio.CancelledError:
                            release_ok = False
                        except BrowserBlocked:
                            release_ok = False
                try:
                    if not await _release():
                        release_ok = False
                except asyncio.CancelledError:
                    release_ok = False
                except BrowserBlocked:
                    release_ok = False
                paused_ids.clear()
                overflow_ids.clear()
                tried_ids.clear()
                failed_ids.clear()
                sanitized.clear()
        if cancelled:
            raise asyncio.CancelledError
        if not release_ok or outcome is None and blocked is None:
            raise BrowserBlocked("cleanup_uncertain")
        if blocked is not None or not release_ok:
            raise blocked if blocked is not None else BrowserBlocked("cleanup_uncertain")
        if outcome is None:
            raise BrowserBlocked("cleanup_uncertain")
        return outcome

    def register_owned_binding(
        self,
        *,
        page: Page,
        context: BrowserContext,
        budget: object,
        owner: object,
        resource: object,
    ) -> None:
        """Record the live page, context, Budget, run, and resource objects."""
        if self.mode is not BrowserMode.isolated or self._state != "open":
            raise BrowserBlocked("document_read_denied")
        if self._page is None or self._context is None:
            raise BrowserBlocked("not_started")
        if page is not self._page or context is not self._context:
            raise BrowserBlocked("document_read_denied")
        target = self._owned_page_target_id
        if not isinstance(target, str) or target == "":
            raise BrowserBlocked("not_started")
        if self._owned_tasks or self._owned_arm is not None:
            raise BrowserBlocked("document_read_denied")
        if self._owned_binding is not None:
            raise BrowserBlocked("document_read_denied")
        deadline = getattr(budget, "original_deadline", None)
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            raise BrowserBlocked("cleanup_uncertain")
        if not math.isfinite(float(deadline)):
            raise BrowserBlocked("cleanup_uncertain")
        self._owned_binding = (page, context, budget, owner, resource)

    def owned_initial_arm_active(self) -> bool:
        arm = self._owned_arm
        if arm is None or arm.finished or not arm.enable_returned:
            return False
        return arm.waiter is not None

    def owned_pause_before_note(self) -> bool:
        arm = self._owned_arm
        return arm is not None and (arm.pause_before_note or arm.pause_at_enable)

    async def arm_owned_initial_document(self, expected_url: str, budget: object) -> _OwnedInitialArm:
        """Enable Fetch and return only after that CDP call returns."""
        binding = self._owned_binding
        if binding is None or self._page is None or self._context is None:
            raise BrowserBlocked("not_started")
        page, context, bound_budget, owner, resource = binding
        if budget is not bound_budget or page is not self._page or context is not self._context:
            raise BrowserBlocked("document_read_denied")
        if self.mode is not BrowserMode.isolated or self._state != "open":
            raise BrowserBlocked("document_read_denied")
        if self._owned_tasks or self._owned_arm is not None:
            raise BrowserBlocked("document_read_denied")
        target = self._owned_page_target_id
        if not isinstance(target, str) or target == "" or not isinstance(expected_url, str):
            raise BrowserBlocked("document_read_denied")
        live_url = getattr(resource, "url", None)
        if live_url != expected_url:
            raise BrowserBlocked("document_read_denied")
        deadline = getattr(bound_budget, "original_deadline", None)
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            raise BrowserBlocked("cleanup_uncertain")
        deadline_at = float(deadline)
        if not math.isfinite(deadline_at) or self._clock() >= deadline_at:
            raise BrowserBlocked("cleanup_uncertain")
        arm = _OwnedInitialArm(
            expected_url=expected_url,
            deadline=deadline_at,
            budget=bound_budget,
            owner=owner,
            resource=resource,
            page=page,
            context=context,
            target_id=target,
        )
        arm.wake = asyncio.get_running_loop().create_future()
        self._owned_arm = arm
        try:
            session = await self._owned_send_factory(
                arm, lambda: self._context.new_cdp_session(self._page)
            )
            arm.session = session
            info = await self._owned_cdp(arm, "Target.getTargetInfo", {})
            if not isinstance(info, Mapping):
                raise BrowserBlocked("cleanup_uncertain")
            target_info = info.get("targetInfo")
            if not isinstance(target_info, Mapping) or target_info.get("targetId") != target:
                raise BrowserBlocked("cleanup_uncertain")
            tree = await self._owned_cdp(arm, "Page.getFrameTree", {})
            arm.main_frame_id = _owned_main_frame(tree)
            on = getattr(session, "on", None)
            if not callable(on):
                raise BrowserBlocked("cleanup_uncertain")
            on("Fetch.requestPaused", lambda event: self._owned_on_paused(arm, event))
            gate = arm.gate
            gate["enabled"] = True
            await self._owned_cdp(
                arm,
                "Fetch.enable",
                {
                    "patterns": [
                        {
                            "urlPattern": _literal_fetch_pattern(expected_url),
                            "resourceType": "Document",
                            "requestStage": "Response",
                        }
                    ]
                },
            )
            arm.enable_returned = True
            if arm.paused_ids or arm.unknown_pause or arm.paused_overflow or arm.pause_before_note:
                arm.pause_at_enable = True
                raise BrowserBlocked("cleanup_uncertain")
        except BrowserBlocked:
            await self.disarm_owned_initial_document()
            raise
        return arm

    async def begin_owned_initial_wait(self, arm: _OwnedInitialArm) -> None:
        """Schedule the waiter and return before any pause is observed."""
        if arm is not self._owned_arm or not arm.enable_returned or arm.waiter is not None:
            raise BrowserBlocked("document_read_denied")
        if arm.pause_at_enable or arm.pause_before_note:
            raise BrowserBlocked("cleanup_uncertain")
        self._owned_read_deadline = arm.deadline
        task: asyncio.Task[None] = asyncio.create_task(self._owned_initial_task(arm))
        arm.waiter = task
        self._owned_tasks.add(task)

        def _release(finished: asyncio.Task[None]) -> None:
            if finished is not task:
                return
            finished.remove_done_callback(_release)
            self._owned_tasks.discard(finished)
            if not finished.cancelled() and finished.exception() is None:
                finished.result()
            if not self._owned_tasks:
                self._owned_read_deadline = None

        task.add_done_callback(_release)

    def note_owned_initial_navigation_started(self) -> None:
        """Synchronous. Unarmed, or not yet waiting, is a no-op."""
        arm = self._owned_arm
        if arm is None or not arm.enable_returned or arm.waiter is None or arm.finished:
            return
        if arm.note.is_set():
            return
        arm.note.set()

    def _owned_settle_waiter(self, waiter: asyncio.Task[None]) -> BaseException | None:
        """Read a finished waiter. Pending and cancelled stay cleanup_uncertain."""
        if not waiter.done() or waiter.cancelled():
            return BrowserBlocked("cleanup_uncertain")
        error = waiter.exception()
        if error is None or isinstance(error, BrowserBlocked):
            return error
        return BrowserBlocked("cleanup_uncertain")

    async def finish_owned_initial_document(
        self, arm: _OwnedInitialArm, budget: object
    ) -> _OwnedInitialObservation:
        """Release only after the caller has observed goto's return."""
        if arm is not self._owned_arm or budget is not arm.budget:
            raise BrowserBlocked("document_read_denied")
        waiter = arm.waiter
        if waiter is None:
            raise BrowserBlocked("cleanup_uncertain")
        arm.finished = True
        error: BaseException | None = None
        observation = arm.observation
        released = False
        try:
            try:
                timeout = arm.deadline - self._clock()
                if not waiter.done():
                    if timeout <= 0:
                        waiter.cancel()
                        await asyncio.wait({waiter}, timeout=0)
                        error = self._owned_settle_waiter(waiter)
                    else:
                        _done, pending = await asyncio.wait({waiter}, timeout=timeout)
                        if pending:
                            waiter.cancel()
                            rest = arm.deadline - self._clock()
                            rest = max(rest, 0)
                            _done, still = await asyncio.wait({waiter}, timeout=rest)
                            if still or waiter.cancelled():
                                error = BrowserBlocked("cleanup_uncertain")
                            else:
                                error = self._owned_settle_waiter(waiter)
                        elif waiter.cancelled():
                            error = BrowserBlocked("cleanup_uncertain")
                        else:
                            error = self._owned_settle_waiter(waiter)
                elif waiter.cancelled():
                    error = BrowserBlocked("cleanup_uncertain")
                else:
                    error = self._owned_settle_waiter(waiter)
            except asyncio.CancelledError as exc:
                error = exc
            observation = arm.observation
            self._owned_begin_cleanup(arm)
            release_failed = False
            try:
                arm.release_ok = await self._owned_release(arm)
            except (BrowserBlocked, asyncio.CancelledError):
                arm.release_ok = False
                release_failed = True
            # An interrupted release still owes this same owned session a
            # bounded detach attempt; no execution action is retried here.
            await self._owned_detach(arm)
            released = arm.session is None
            # detach may deliver a last pause before its acknowledgement.
            if self._owned_pause_failed(arm):
                arm.release_ok = False
            if observation is not None:
                observation.release_ok = arm.release_ok
                observation.cdp_methods = tuple(arm.acks)
            if not arm.release_ok or not released:
                release_failed = True
            if release_failed:
                if observation is not None:
                    observation.release_ok = False
                    observation.drop_body()
                    return observation
                if isinstance(error, BrowserBlocked):
                    raise error
                if error is not None:
                    raise BrowserBlocked("cleanup_uncertain") from error
                raise BrowserBlocked("cleanup_uncertain")
            if isinstance(error, BrowserBlocked):
                if observation is not None:
                    observation.drop_body()
                raise error
            if error is not None:
                if observation is not None:
                    observation.drop_body()
                raise BrowserBlocked("cleanup_uncertain") from error
            if observation is None or not arm.release_ok or not released:
                raise BrowserBlocked("cleanup_uncertain")
            return observation
        finally:
            if released and not self._owned_tasks and self._owned_arm is arm:
                self._owned_arm = None
                self._owned_binding = None

    async def disarm_owned_initial_document(self) -> None:
        arm = self._owned_arm
        if arm is None:
            return
        arm.finished = True
        if (arm.cleanup_deadline is not None and arm.cleanup_deadline <= time.monotonic()
                and not any(not task.done() for task in self._owned_tasks)):
            # An explicit retry may open another teardown window once the prior
            # owned operation is terminal. It never retries execution.
            arm.cleanup_deadline = None
        self._owned_begin_cleanup(arm)
        waiter = arm.waiter
        if waiter is not None and not waiter.done():
            waiter.cancel()
        await self._cancel_owned_tasks(arm.cleanup_deadline)
        try:
            ok = await self._owned_release(arm)
        except (BrowserBlocked, asyncio.CancelledError):
            ok = False
        arm.release_ok = ok
        await self._owned_detach(arm)
        arm.release_ok = arm.release_ok and not self._owned_pause_failed(arm)
        if (not arm.release_ok or arm.session is not None) and "cleanup_uncertain" not in self.cleanup_errors:
            self.cleanup_errors.append("cleanup_uncertain")
        if arm.session is None and not self._owned_tasks:
            self._owned_arm = None
            self._owned_binding = None

    async def _owned_initial_task(self, arm: _OwnedInitialArm) -> None:
        buffer = bytearray()
        try:
            event = await self._owned_one_pause(arm)
            request_id, headers = _expected_document_pause(
                event,
                expected_url=arm.expected_url,
                main_frame_id=arm.main_frame_id,
            )
            arm.sanitized.clear()
            gate = arm.gate
            gate["request_id"] = request_id
            if self._owned_pause_failed(arm):
                raise BrowserBlocked("cleanup_uncertain")
            stream_payload = await self._owned_cdp(
                arm, "Fetch.takeResponseBodyAsStream", {"requestId": request_id}
            )
            if not isinstance(stream_payload, Mapping):
                raise BrowserBlocked("cleanup_uncertain")
            handle = stream_payload.get("stream")
            if not isinstance(handle, str) or handle == "":
                raise BrowserBlocked("cleanup_uncertain")
            gate["handle"] = handle
            while True:
                if arm.pause_before_note or self._state != "open" or self._owned_pause_failed(arm):
                    buffer.clear()
                    raise BrowserBlocked("cleanup_uncertain")
                chunk = await self._owned_cdp(
                    arm,
                    "IO.read",
                    {"handle": handle, "size": _DOCUMENT_IO_READ_SIZE},
                )
                if not isinstance(chunk, Mapping) or chunk.get("base64Encoded") is not False:
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
                if len(buffer) + len(raw) > _DOCUMENT_BODY_CAP:
                    buffer.clear()
                    raise BrowserBlocked("document_read_denied")
                buffer.extend(raw)
                if chunk.get("eof") is True:
                    break
            if self._owned_pause_failed(arm) or self._state != "open":
                buffer.clear()
                raise BrowserBlocked("cleanup_uncertain")
            raw_body = bytes(buffer)
            await self._owned_cdp(arm, "IO.close", {"handle": handle})
            gate["stream_closed"] = True
            if self._owned_pause_failed(arm):
                buffer.clear()
                raise BrowserBlocked("cleanup_uncertain")
            await self._owned_cdp(
                arm,
                "Fetch.fulfillRequest",
                {
                    "requestId": request_id,
                    "responseCode": 200,
                    "responseHeaders": [
                        {"name": name, "value": value} for name, value in headers
                    ],
                    "body": base64.b64encode(raw_body).decode("ascii"),
                },
            )
            gate["fulfilled"] = True
            if self._owned_pause_failed(arm):
                buffer.clear()
                raise BrowserBlocked("cleanup_uncertain")
            arm.observation = _OwnedInitialObservation(
                request_id=request_id,
                stream_handle=handle,
                response_headers=headers,
                body=raw_body,
                release_ok=False,
                cdp_methods=tuple(arm.acks),
                main_frame_id=arm.main_frame_id,
            )
        finally:
            buffer.clear()

    async def _owned_one_pause(self, arm: _OwnedInitialArm) -> dict[str, object]:
        while True:
            if arm.pause_before_note or arm.pause_at_enable:
                raise BrowserBlocked("cleanup_uncertain")
            if self._clock() >= arm.deadline:
                raise BrowserBlocked("cleanup_uncertain")
            if (
                arm.note.is_set()
                and not self._owned_pause_failed(arm)
                and len(arm.sanitized) == 1
            ):
                return arm.sanitized[0]
            if self._owned_pause_failed(arm) and arm.note.is_set():
                raise BrowserBlocked("cleanup_uncertain")
            wake = arm.wake
            if wake is None or wake.done():
                wake = asyncio.get_running_loop().create_future()
                arm.wake = wake
            timeout = arm.deadline - self._clock()
            if timeout <= 0:
                raise BrowserBlocked("cleanup_uncertain")
            await asyncio.wait({wake}, timeout=timeout)

    def _owned_on_paused(self, arm: _OwnedInitialArm, event: object) -> None:
        request_id = _pause_request_id(event)
        if isinstance(request_id, str) and len(request_id) > _PAUSED_ID_CHARS:
            request_id = None
        self._owned_track(arm, request_id)
        if not arm.note.is_set():
            arm.pause_before_note = True
        if arm.unknown_pause or arm.paused_overflow or len(arm.paused_ids) != 1:
            arm.sanitized.clear()
        elif not arm.sanitized:
            kept = _sanitize_paused_event(event)
            if kept is None:
                self._owned_track(arm, None)
                arm.sanitized.clear()
            else:
                arm.sanitized.append(kept)
        wake = arm.wake
        if wake is not None and not wake.done():
            wake.set_result(None)

    def _owned_track(self, arm: _OwnedInitialArm, request_id: object) -> None:
        if not isinstance(request_id, str) or request_id == "" or len(request_id) > _PAUSED_ID_CHARS:
            arm.unknown_pause = True
            return
        if request_id in arm.paused_ids or request_id in arm.overflow_ids:
            return
        if len(arm.paused_ids) >= _PAUSED_ID_CAP:
            arm.paused_overflow = True
            if len(arm.overflow_ids) < _PAUSED_ID_CAP:
                arm.overflow_ids.append(request_id)
            return
        arm.paused_ids.append(request_id)

    def _owned_pause_failed(self, arm: _OwnedInitialArm) -> bool:
        return bool(
            arm.unknown_pause
            or arm.paused_overflow
            or arm.pause_before_note
            or arm.pause_at_enable
            or len(arm.paused_ids) != 1
        )

    async def _owned_release(self, arm: _OwnedInitialArm) -> bool:
        ok = True
        gate = arm.gate
        handle = gate.get("handle")
        if isinstance(handle, str) and handle and gate.get("stream_closed") is not True:
            try:
                await self._owned_cdp(arm, "IO.close", {"handle": handle})
                gate["stream_closed"] = True
            except BrowserBlocked:
                ok = False
        if not await self._owned_drain(arm):
            ok = False
        if gate.get("enabled") is True and gate.get("disabled") is not True:
            try:
                await self._owned_cdp(arm, "Fetch.disable", {})
                gate["disabled"] = True
            except BrowserBlocked:
                ok = False
        if not await self._owned_drain(arm):
            ok = False
        if self._owned_pause_failed(arm):
            ok = False
        for request_id in (*arm.paused_ids, *arm.overflow_ids):
            fulfilled = request_id == gate.get("request_id") and gate.get("fulfilled") is True
            if not fulfilled and request_id not in arm.failed_ids:
                ok = False
        return ok

    async def _owned_drain(self, arm: _OwnedInitialArm) -> bool:
        drained = True
        gate = arm.gate
        ids = (*arm.paused_ids, *arm.overflow_ids)
        for request_id in ids:
            if request_id in arm.tried_ids:
                continue
            if request_id == gate.get("request_id") and gate.get("fulfilled") is True:
                continue
            arm.tried_ids.add(request_id)
            try:
                await self._owned_cdp(
                    arm,
                    "Fetch.failRequest",
                    {"requestId": request_id, "errorReason": "BlockedByClient"},
                )
            except BrowserBlocked:
                drained = False
            else:
                arm.failed_ids.add(request_id)
        return drained

    async def _owned_detach(self, arm: _OwnedInitialArm) -> None:
        session = arm.session
        if session is None:
            return
        if self._owned_remaining(arm) <= 0:
            arm.release_ok = False
            return
        detach = getattr(session, "detach", None)
        if not callable(detach):
            arm.release_ok = False
            return
        try:
            await self._owned_send_factory(arm, detach)
        except (BrowserBlocked, asyncio.CancelledError):
            arm.release_ok = False
            return
        arm.session = None

    async def _owned_cdp(
        self, arm: _OwnedInitialArm, method: str, params: Mapping[str, object]
    ) -> object:
        session = arm.session
        if session is None:
            raise BrowserBlocked("cleanup_uncertain")
        send = getattr(session, "send", None)
        if not callable(send):
            raise BrowserBlocked("cleanup_uncertain")

        def _factory() -> object:
            return send(method, dict(params))

        result = await self._owned_send_factory(arm, _factory)
        arm.acks.append(method)
        return result

    def _owned_begin_cleanup(self, arm: _OwnedInitialArm) -> None:
        if arm.cleanup_deadline is None:
            arm.cleanup_deadline = time.monotonic() + _OWNED_CLEANUP_SECONDS

    def _owned_remaining(self, arm: _OwnedInitialArm) -> float:
        if arm.cleanup_deadline is not None:
            return arm.cleanup_deadline - time.monotonic()
        return arm.deadline - self._clock()

    async def _owned_send_factory(self, arm: _OwnedInitialArm, factory: object) -> object:
        if self._owned_remaining(arm) <= 0:
            raise BrowserBlocked("cleanup_uncertain")
        if not callable(factory):
            raise BrowserBlocked("cleanup_uncertain")
        awaitable = factory()
        if not asyncio.iscoroutine(awaitable):
            raise BrowserBlocked("cleanup_uncertain")
        timeout = self._owned_remaining(arm)
        if timeout <= 0:
            awaitable.close()
            raise BrowserBlocked("cleanup_uncertain")
        child: asyncio.Task[object] = asyncio.create_task(awaitable)
        self._owned_tasks.add(child)

        def done(task: asyncio.Task[object]) -> None:
            self._owned_tasks.discard(task)
            if not task.cancelled():
                task.exception()

        child.add_done_callback(done)
        try:
            _done, pending = await asyncio.wait({child}, timeout=timeout)
            if pending:
                child.cancel()
                raise BrowserBlocked("cleanup_uncertain")
            if child.cancelled():
                raise BrowserBlocked("cleanup_uncertain")
            return child.result()
        except asyncio.CancelledError:
            child.cancel()
            raise
        except BrowserBlocked:
            raise
        except Exception as exc:
            raise BrowserBlocked("cleanup_uncertain") from exc

    async def _start_isolated(self) -> None:
        assert self._playwright is not None
        try:
            self._browser = await self._playwright.chromium.launch(chromium_sandbox=True, channel="chromium")
            self._context = await self._browser.new_context()
            self._page = await self._context.new_page()
        except Exception as exc:
            raise BrowserBlocked("launch_failed") from exc
        page_target_id = await self._page_target_id(self._page)
        self.owned_target_id = page_target_id
        self._owned_page_target_id = page_target_id
        empty = _snapshot(())
        self.foreign_before = empty
        self.foreign_mismatch = False
        self.residual_owned = False

    async def _start_existing(self) -> None:
        assert self._playwright is not None
        if self._devtools_path is None:
            raise BrowserBlocked("invalid_devtools_path")
        self._endpoint = read_devtools_active_port(self._devtools_path)
        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(
                self._endpoint.websocket_url,
                timeout=self._timeout_ms,
                no_defaults=True,
            )
        except Exception as exc:
            raise BrowserBlocked("cdp_connect_failed") from exc
        contexts = list(self._browser.contexts)
        if len(contexts) != 1:
            raise BrowserBlocked("context_count")
        self._context = contexts[0]
        before = await self._cdp_inventory()
        self.foreign_before = before
        try:
            self._ownership_session = await self._browser.new_browser_cdp_session()
            self._ownership_session.on("Target.targetCreated", self._record_target_created)
            await self._ownership_session.send("Target.setDiscoverTargets", {"discover": True})
            old_pages = tuple(self._context.pages)
            created = await self._ownership_session.send("Target.createTarget", {"url": "about:blank"})
            created_id = created.get("targetId")
            if not isinstance(created_id, str) or not created_id:
                raise BrowserBlocked("ownership_unverified")
            before_ids = {item.target_id for item in before.targets}
            if created_id in before_ids:
                raise BrowserBlocked("ownership_unverified")
            # The acknowledged createTarget result, never an inventory delta,
            # proves ownership even if Page binding/confirmation subsequently fails.
            self.owned_target_id = created_id
            self._owned_page_target_id = created_id
            self._owned_target_ids.add(created_id)
            self._adopt_opener_targets()
            deadline = time.monotonic() + self._timeout_ms / 1000
            while self._page is None:
                for page in self._context.pages:
                    if any(page is old for old in old_pages):
                        continue
                    if await self._page_target_id(page) == created_id:
                        self._page = page
                        break
                if self._page is None:
                    if time.monotonic() >= deadline:
                        raise BrowserBlocked("ownership_unverified")
                    await asyncio.sleep(0.01)
        except BrowserBlocked:
            raise
        except Exception as exc:
            raise BrowserBlocked("owned_page_failed") from exc
        after = await self._cdp_inventory()
        new_ids = {item.target_id for item in after.targets if item.target_id not in before_ids}
        if new_ids != {created_id}:
            raise BrowserBlocked("ownership_unverified")

    def _record_target_created(self, event: object) -> None:
        if not isinstance(event, Mapping):
            return
        info = event.get("targetInfo")
        if not isinstance(info, Mapping) or info.get("type") != "page":
            return
        target_id, opener = info.get("targetId"), info.get("openerId")
        if (not isinstance(target_id, str) or not target_id
                or not isinstance(opener, str) or not opener
                or self.foreign_before is None
                or any(item.target_id == target_id for item in self.foreign_before.targets)):
            return
        if target_id not in self._target_openers and len(self._target_openers) >= _INVENTORY_TARGET_CAP:
            if not self._ownership_discovery_uncertain:
                self.cleanup_errors.append("ownership_unverified")
            self._ownership_discovery_uncertain = True
            return
        self._target_openers[target_id] = opener
        self._adopt_opener_targets()

    def _adopt_opener_targets(self) -> None:
        # Iterate to include an out-of-order descendant only after its opener is proven.
        changed = True
        while changed:
            changed = False
            for target_id, opener in self._target_openers.items():
                if opener in self._owned_target_ids and target_id not in self._owned_target_ids:
                    self._owned_target_ids.add(target_id)
                    changed = True

    async def _page_target_id(self, page: Page) -> str:
        if self._context is None:
            raise BrowserBlocked("ownership_unverified")
        session = None
        try:
            session = await self._context.new_cdp_session(page)
            payload = await session.send("Target.getTargetInfo")
        except Exception as exc:
            raise BrowserBlocked("ownership_unverified") from exc
        finally:
            if session is not None:
                await self._detach_session(session)
        if not isinstance(payload, Mapping):
            raise BrowserBlocked("ownership_unverified")
        info = payload.get("targetInfo")
        if not isinstance(info, Mapping):
            raise BrowserBlocked("ownership_unverified")
        target_id = info.get("targetId")
        if not isinstance(target_id, str) or not target_id:
            raise BrowserBlocked("ownership_unverified")
        return target_id

    async def _detach_session(self, session: object) -> None:
        detach = getattr(session, "detach", None)
        if detach is None:
            return
        try:
            await detach()
        except Exception as exc:  # noqa: BLE001 - cleanup boundary records the exception class, not its text
            self.cleanup_errors.append(type(exc).__name__)

    async def _cdp_inventory(self) -> InventorySnapshot:
        """Read page targets through a browser CDP session. Does not call ``/json/list``."""
        if self._browser is None:
            raise BrowserBlocked("inventory_failed")
        session = None
        try:
            session = await self._browser.new_browser_cdp_session()
            payload = await session.send("Target.getTargets")
        except Exception as exc:
            raise BrowserBlocked("inventory_failed") from exc
        finally:
            if session is not None:
                await self._detach_session(session)
        snapshot = _snapshot_from_target_infos(payload)
        if self._ownership_session is not None and isinstance(payload, Mapping):
            infos = payload.get("targetInfos")
            if isinstance(infos, list):
                for info in infos:
                    self._record_target_created({"targetInfo": info})
        return snapshot

    async def _cancel_owned_tasks(self, deadline: float | None = None) -> None:
        current = asyncio.current_task()
        tasks = tuple(task for task in self._owned_tasks if task is not current)
        if deadline is None:
            deadline = time.monotonic() + _OWNED_CLEANUP_SECONDS
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        waiting = {task for task in tasks if not task.done()}
        if waiting:
            _done, pending = await asyncio.wait(
                waiting, timeout=max(0.0, deadline - time.monotonic())
            )
            if pending:
                self.cleanup_errors.append("cleanup_uncertain")
        for task in tasks:
            if not task.done():
                continue
            self._owned_tasks.discard(task)
            if not task.cancelled():
                error = task.exception()
                if error is not None:
                    self.cleanup_errors.append(type(error).__name__)
        if not self._owned_tasks:
            self._owned_read_deadline = None

    async def _cleanup(self, *, starting_failure: bool) -> None:
        cancelled: asyncio.CancelledError | None = None
        cleanup_arm = self._owned_arm
        try:
            await self.disarm_owned_initial_document()
        except asyncio.CancelledError as error:
            cancelled = error
            self.cleanup_errors.append("cleanup_cancelled")
        except Exception as error:  # noqa: BLE001 - own-resource cleanup keeps only a class code
            self.cleanup_errors.append(type(error).__name__)
        try:
            # disarm and this final join cover the same arm-owned operations.
            # Only a later explicit attempt may start a fresh teardown window.
            await self._cancel_owned_tasks(
                cleanup_arm.cleanup_deadline if cleanup_arm is not None else None
            )
        except asyncio.CancelledError as error:
            cancelled = cancelled or error
            self.cleanup_errors.append("cleanup_cancelled")
            self.residual_owned = True
        except Exception as error:  # noqa: BLE001 - still attempt independent owned-resource closure
            self.cleanup_errors.append(type(error).__name__)
            self.residual_owned = True
        if not self._cleanup_mode_done:
            try:
                if self.mode is BrowserMode.existing_chrome:
                    await self._cleanup_existing(starting_failure=starting_failure)
                else:
                    await self._cleanup_isolated()
            except asyncio.CancelledError as error:
                cancelled = cancelled or error
                self.cleanup_errors.append("cleanup_cancelled")
                if not self._cleanup_mode_done:
                    self.residual_owned = True
            except Exception as error:  # noqa: BLE001 - retain exact unfinished resources for cleanup retry
                self.cleanup_errors.append(type(error).__name__)
                self.residual_owned = True
        # The transport is a prerequisite for closing an attached owned page or
        # an unclosed isolated browser. Do not destroy it while that work remains.
        if self._cleanup_mode_done:
            try:
                await self._stop_playwright()
            except asyncio.CancelledError as error:
                cancelled = cancelled or error
                self.cleanup_errors.append("cleanup_cancelled")
        if cancelled is not None:
            raise cancelled

    async def _cleanup_isolated(self) -> None:
        cancelled: asyncio.CancelledError | None = None
        if self._context is None:
            self._isolated_context_closed = True
        elif not self._isolated_context_closed:
            try:
                await self._context.close()
                self._isolated_context_closed = True
            except asyncio.CancelledError as error:
                cancelled = error
                self.cleanup_errors.append("cleanup_cancelled")
            except Exception as error:  # noqa: BLE001 - retain the exact owned context for retry
                self.cleanup_errors.append(type(error).__name__)
        if self._browser is None:
            self._isolated_browser_closed = True
        elif not self._isolated_browser_closed:
            try:
                await self._browser.close()
                self._isolated_browser_closed = True
                # An acknowledged owned-browser close also closes its contexts.
                self._isolated_context_closed = True
            except asyncio.CancelledError as error:
                cancelled = cancelled or error
                self.cleanup_errors.append("cleanup_cancelled")
            except Exception as error:  # noqa: BLE001 - do not discard an unclosed owned browser
                self.cleanup_errors.append(type(error).__name__)
        self._cleanup_mode_done = self._isolated_context_closed and self._isolated_browser_closed
        self.residual_owned = not self._cleanup_mode_done or any(not task.done() for task in self._owned_tasks)
        if cancelled is not None:
            raise cancelled

    async def _cleanup_existing(self, *, starting_failure: bool) -> None:
        cancelled: asyncio.CancelledError | None = None
        if self._ownership_session is None and self._owned_target_ids - self._closed_target_ids:
            if self._browser is None:
                raise BrowserBlocked("cleanup_uncertain")
            # Acknowledged detach can reveal a last owned descendant. Reuse the
            # retained browser connection, never reconnect to shared Chrome.
            self._ownership_session = await self._browser.new_browser_cdp_session()
            self._ownership_session.on("Target.targetCreated", self._record_target_created)
            await self._ownership_session.send("Target.setDiscoverTargets", {"discover": True})
        # New descendant events may arrive while an earlier close is awaited.
        attempted: set[str] = set()
        while True:
            while self._owned_target_ids - self._closed_target_ids - attempted:
                target_id = next(iter(self._owned_target_ids - self._closed_target_ids - attempted))
                attempted.add(target_id)
                try:
                    if (target_id == self.owned_target_id and self._page is not None
                            and await self._ownership_matches()):
                        await self._page.close()
                        self._closed_target_ids.add(target_id)
                        self._owned_page_closed = True
                    elif self._ownership_session is not None:
                        result = await self._ownership_session.send("Target.closeTarget", {"targetId": target_id})
                        if result.get("success") is True:
                            self._closed_target_ids.add(target_id)
                            if target_id == self.owned_target_id:
                                self._owned_page_closed = True
                        else:
                            self.cleanup_errors.append("cleanup_uncertain")
                except asyncio.CancelledError as error:
                    cancelled = cancelled or error
                except Exception as error:  # noqa: BLE001 - retain exact ID/session for driver-only retry
                    self.cleanup_errors.append(type(error).__name__)
            after: InventorySnapshot | None = None
            inventory_owned = self._owned_target_ids.copy()
            if self._endpoint is not None and self._browser is not None:
                try:
                    after = await self._cdp_inventory()
                except BrowserBlocked:
                    self.cleanup_errors.append("inventory_after")
            if after is not None:
                live = {item.target_id for item in after.targets}
                # A still-live inventory entry overrides a close acknowledgement.
                self._closed_target_ids.difference_update(live)
                # IDs learned while the inventory was awaited may postdate its
                # snapshot. Their absence is not closure proof.
                self._closed_target_ids.update(inventory_owned - live)
            if not self._owned_target_ids - self._closed_target_ids - attempted:
                break
        targets_done = (self._owned_target_ids <= self._closed_target_ids
                        and not self._ownership_discovery_uncertain)
        if targets_done and self._ownership_session is not None:
            session = self._ownership_session
            try:
                await session.detach()
            except asyncio.CancelledError as error:
                cancelled = cancelled or error
            else:
                self._ownership_session = None
        # detach itself can dispatch a final opener event. Reconcile only after
        # that acknowledgement, retaining browser/transport for exact-ID retry.
        self._record_existing_result(after, starting_failure=starting_failure)
        targets_done = (self._owned_target_ids <= self._closed_target_ids
                        and not self._ownership_discovery_uncertain)
        self._cleanup_mode_done = targets_done and self._ownership_session is None
        if cancelled is not None:
            raise cancelled
        # Disconnect the transport only. Never browser.close() on attached Chrome.

    def _record_existing_result(
        self,
        after: InventorySnapshot | None,
        *,
        starting_failure: bool,
    ) -> None:
        owned = self.owned_target_id
        before = self.foreign_before
        no_page_attempt = (
            starting_failure and before is None and owned is None and self._page is None
        )
        if after is None:
            self.foreign_after = None
            creation_uncertain = before is not None or owned is not None or self._page is not None
            if no_page_attempt:
                self.foreign_mismatch = None
                self.residual_owned = None
            elif creation_uncertain:
                self.foreign_mismatch = None
                self.residual_owned = True
            else:
                self.foreign_mismatch = True
                self.residual_owned = True if owned is not None or self._page is not None else None
            return
        foreign = tuple(item for item in after.targets if item.target_id not in self._owned_target_ids)
        self.foreign_after = _snapshot(foreign)
        if before is None:
            self.foreign_mismatch = None if no_page_attempt else True
        else:
            self.foreign_mismatch = before.targets != self.foreign_after.targets
        if owned is None:
            if no_page_attempt:
                self.residual_owned = None
            else:
                self.residual_owned = self._page is not None or starting_failure
        else:
            self.residual_owned = bool(self._owned_target_ids - self._closed_target_ids
                                       or self._ownership_discovery_uncertain)

    async def _ownership_matches(self) -> bool:
        if self._page is None or self.owned_target_id is None or self._context is None:
            return False
        try:
            live_id = await self._page_target_id(self._page)
        except BrowserBlocked:
            return False
        return live_id == self.owned_target_id

    async def _stop_playwright(self) -> None:
        playwright = self._playwright
        if playwright is None:
            return
        try:
            await playwright.stop()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - cleanup boundary records the exception class, not its text
            self.cleanup_errors.append(type(exc).__name__)
            return
        self._playwright = None
