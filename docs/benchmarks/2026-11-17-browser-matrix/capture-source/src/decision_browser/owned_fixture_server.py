from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import re
import secrets
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from decision_browser.owned_fixture import CONTENT_IDS, resolve

_ALLOWED_IDS = frozenset(CONTENT_IDS.values())
_STYLE_RE = re.compile(r"<style>(.*?)</style>", re.DOTALL)
_MAX_HEADER = 8192
_MAX_LIFE = 120.0
_READ_CAP = 1.0
_CLEANUP_CAP = 1.0


class CleanupError(Exception):
    """Owned listener, tasks, or connections were not proven finished."""


class _Pinned(Protocol):
    @property
    def content_id(self) -> str: ...

    @property
    def body(self) -> str: ...

    @property
    def signals(self) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class CleanupReport:
    clean: bool
    unknown: bool
    unfinished_tasks: int
    owned_tasks: int
    listener_closed: bool


class OwnedFixtureResource:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        if not callable(clock):
            raise TypeError("clock")
        self._clock = clock
        self._failed_tasks = 0
        self._deadline: float | None = None
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._token = ""
        self._port = 0
        self._response = b""
        self._url = ""
        self._content_id = ""
        self._closing = False
        self._started = False
        self._report: CleanupReport | None = None

    @property
    def url(self) -> str:
        if not self._url:
            raise RuntimeError("not started")
        return self._url

    @property
    def content_id(self) -> str:
        if not self._content_id:
            raise RuntimeError("not started")
        return self._content_id

    def armed_entity_bytes(self) -> bytes:
        """Entity bytes of the armed response. Owner comparison only."""
        if not self._started or self._closing or self._report is not None:
            raise RuntimeError("not started")
        marker = self._response.find(b"\r\n\r\n")
        if marker < 0:
            raise RuntimeError("not started")
        return bytes(self._response[marker + 4 :])

    @property
    def original_deadline(self) -> float:
        if self._deadline is None:
            raise RuntimeError("not started")
        return self._deadline

    @property
    def cleanup_status(self) -> CleanupReport | None:
        return self._report

    @property
    def owned_task_count(self) -> int:
        return len(self._tasks)

    @property
    def open_connection_count(self) -> int:
        return len(self._writers)

    async def start(self, content_id: str, original_deadline: float) -> None:
        if self._started or self._server is not None or self._report is not None:
            raise RuntimeError("already started")
        self._validate_deadline(original_deadline)
        self._arm(self._load(content_id))
        self._deadline = float(original_deadline)
        try:
            self._server = await asyncio.start_server(
                self._client,
                host="127.0.0.1",
                port=0,
                family=socket.AF_INET,
                reuse_address=False,
            )
            sockets = tuple(self._server.sockets or ())
            if len(sockets) != 1:
                raise RuntimeError("bind")
            sockaddr = sockets[0].getsockname()
            host, port = sockaddr[0], sockaddr[1]
            if host != "127.0.0.1" or not isinstance(port, int):
                raise RuntimeError("bind")
            self._port = port
            self._url = f"http://127.0.0.1:{port}/{self._token}"
            self._track(asyncio.create_task(self._expiry(), name="owned-fixture-expiry"))
            self._started = True
        except BaseException as exc:
            report = await self.close()
            if report.unknown:
                raise CleanupError("cleanup unknown") from exc
            raise

    async def close(self) -> CleanupReport:
        if self._report is not None and self._report.clean:
            return self._report
        server = self._server
        self._shutdown_owned()
        deadline = time.monotonic() + _CLEANUP_CAP
        listener_closed = not self._listener_alive()
        if server is not None and not listener_closed:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                try:
                    await asyncio.wait_for(server.wait_closed(), remaining)
                except (TimeoutError, OSError, RuntimeError):
                    pass
            listener_closed = not self._listener_alive()
        current = asyncio.current_task()
        pending = [task for task in self._tasks if task is not current and not task.done()]
        remaining = deadline - time.monotonic()
        if pending and remaining > 0:
            await asyncio.wait(pending, timeout=remaining)
        for task in tuple(self._tasks):
            if task.done():
                self._task_done(task)
        unfinished = [task for task in self._tasks if not task.done()]
        unknown = bool(unfinished) or not listener_closed or self._failed_tasks > 0
        report = CleanupReport(
            clean=not unknown,
            unknown=unknown,
            unfinished_tasks=len(unfinished),
            owned_tasks=len(self._tasks),
            listener_closed=listener_closed,
        )
        self._report = report
        return report

    def _validate_deadline(self, original_deadline: float) -> None:
        kind = type(original_deadline)
        if kind is bool or kind not in (int, float):
            raise ValueError("deadline")
        value = float(original_deadline)
        if not math.isfinite(value):
            raise ValueError("deadline")
        remaining = value - self._clock()
        if remaining <= 0 or remaining > _MAX_LIFE:
            raise ValueError("deadline")

    def _load(self, content_id: str) -> _Pinned:
        if type(content_id) is not str or content_id not in _ALLOWED_IDS:
            raise ValueError("content_id")
        document = resolve(content_id)
        if document is None or document.content_id != content_id:
            raise ValueError("content_id")
        body = document.body
        signals = document.signals
        if type(body) is not str or type(signals) is not tuple:
            raise ValueError("fixture")
        return document

    def _arm(self, document: _Pinned) -> None:
        body = document.body
        encoded = body.encode("utf-8")
        found = list(_STYLE_RE.finditer(body))
        one = len(found) == 1 and body.count("<style>") == 1
        if not one or body.count("</style>") != 1:
            raise ValueError("style")
        inner = found[0].group(1).encode("utf-8")
        digest = base64.b64encode(hashlib.sha256(inner).digest()).decode("ascii")
        policy = (
            "default-src 'none'; script-src 'none'; "
            f"style-src 'sha256-{digest}'; style-src-elem 'sha256-{digest}'; "
            "img-src 'none'; connect-src 'none'; frame-src 'none'; "
            "object-src 'none'; base-uri 'none'; form-action 'none'"
        )
        header = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/html;charset=utf-8\r\n"
            f"Content-Length: {len(encoded)}\r\n"
            "Connection: close\r\n"
            "Cache-Control: no-store\r\n"
            "X-Content-Type-Options: nosniff\r\n"
            f"Content-Security-Policy: {policy}\r\n"
            "\r\n"
        )
        self._response = header.encode("ascii") + encoded
        self._content_id = document.content_id
        self._token = secrets.token_urlsafe(32)

    def _client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._closing or self._report is not None:
            self._abort_writer(writer)
            return
        self._track(asyncio.create_task(self._handle(reader, writer)))

    def _track(self, task: asyncio.Task[None]) -> None:
        self._tasks.add(task)

        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        if task not in self._tasks:
            return
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._failed_tasks += 1

    async def _expiry(self) -> None:
        assert self._deadline is not None
        delay = self._deadline - self._clock()
        if delay > 0:
            await asyncio.sleep(delay)
        self._shutdown_owned()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._writers.add(writer)
        served = False
        try:
            raw = await self._read_headers(reader)
            if raw is not None and not self._closing and self._acceptable(raw):
                await self._send(writer)
                served = True
        except (TimeoutError, OSError, RuntimeError, ConnectionError):
            served = False
        finally:
            self._writers.discard(writer)
            if served:
                try:
                    writer.close()
                except (OSError, RuntimeError):
                    self._abort_writer(writer)
            else:
                self._abort_writer(writer)

    async def _read_headers(self, reader: asyncio.StreamReader) -> bytes | None:
        assert self._deadline is not None
        end = min(self._deadline, self._clock() + _READ_CAP)
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            if len(buf) >= _MAX_HEADER:
                return None
            left = min(end, self._deadline) - self._clock()
            if left <= 0:
                return None
            chunk = await asyncio.wait_for(reader.read(_MAX_HEADER - len(buf)), left)
            if not chunk:
                return None
            buf += chunk
        marker = buf.find(b"\r\n\r\n")
        if marker < 0 or marker + 4 != len(buf) or len(buf) > _MAX_HEADER:
            return None
        return bytes(buf)

    async def _send(self, writer: asyncio.StreamWriter) -> None:
        assert self._deadline is not None
        remaining = self._deadline - self._clock()
        if remaining <= 0 or self._closing:
            raise TimeoutError
        writer.write(self._response)
        await asyncio.wait_for(writer.drain(), remaining)

    def _acceptable(self, raw: bytes) -> bool:
        if b"\x00" in raw or not self._token or self._port <= 0:
            return False
        head, _, extra = raw.partition(b"\r\n\r\n")
        if extra:
            return False
        lines = head.split(b"\r\n")
        parts = lines[0].split(b" ")
        expected_path = b"/" + self._token.encode("ascii")
        if len(parts) != 3 or parts[0] != b"GET" or parts[2] != b"HTTP/1.1":
            return False
        if parts[1] != expected_path:
            return False
        hosts: list[bytes] = []
        seen: set[bytes] = set()
        blocked = (b"content-length", b"transfer-encoding")
        for line in lines[1:]:
            if not line or line[:1] in (b" ", b"\t") or b":" not in line:
                return False
            name, value = line.split(b":", 1)
            if (
                not name
                or b" " in name
                or b"\t" in name
                or any(byte < 0x20 or byte == 0x7F for byte in name)
                or any(byte < 0x20 or byte == 0x7F for byte in value)
            ):
                return False
            lowered = name.lower()
            if lowered in blocked or lowered in seen:
                return False
            seen.add(lowered)
            if lowered == b"host":
                hosts.append(value.strip())
        expected = f"127.0.0.1:{self._port}".encode("ascii")
        return hosts == [expected]

    def _shutdown_owned(self) -> None:
        self._closing = True
        server = self._server
        if server is not None:
            server.close()
        for writer in tuple(self._writers):
            self._abort_writer(writer)
        current = asyncio.current_task()
        for task in tuple(self._tasks):
            if task is not current and not task.done():
                task.cancel()

    def _listener_alive(self) -> bool:
        server = self._server
        if server is None:
            return False
        for sock in tuple(server.sockets or ()):
            try:
                if sock.fileno() != -1:
                    return True
            except OSError:
                continue
        return False

    @staticmethod
    def _abort_writer(writer: asyncio.StreamWriter) -> None:
        transport = getattr(writer, "transport", None)
        if transport is not None:
            transport.abort()
            return
        try:
            writer.close()
        except (OSError, RuntimeError):
            return
