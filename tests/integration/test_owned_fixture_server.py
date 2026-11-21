from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import math
import re
import socket
import time
from urllib.parse import urlsplit

import pytest

from decision_browser import owned_fixture as catalog
from decision_browser.owned_fixture_server import OwnedFixtureResource

pytestmark = pytest.mark.asyncio
_STYLE = re.compile(r"<style>(.*?)</style>", re.DOTALL)
_IDS = (
    "http://owned-fixture.invalid/v1/clean",
    "http://owned-fixture.invalid/v1/clipping",
    "http://owned-fixture.invalid/v1/overlap",
    "http://owned-fixture.invalid/v1/responsive",
)
_KINDS = [
    "host",
    "duphost",
    "nohost",
    "path",
    "query",
    "post",
    "absolute",
    "fold",
    "badname",
    "nocolon",
    "dup",
    "ctl",
    "length",
    "chunked",
    "body",
    "pipeline",
    "oversize",
]

def _digest(body: str) -> str:
    found = list(_STYLE.finditer(body))
    assert len(found) == 1 and body.count("<style>") == body.count("</style>") == 1
    return base64.b64encode(hashlib.sha256(found[0].group(1).encode()).digest()).decode()

def _body(content_id: str) -> str:
    name = next(key for key, url in catalog.CONTENT_IDS.items() if url == content_id)
    body = catalog.DOCUMENTS[name]
    assert isinstance(body, str)
    pin = hashlib.sha256(body.encode("utf-8")).hexdigest()
    resolved = catalog.resolve(content_id)
    assert resolved is not None and resolved.body == body and catalog.PINS[name] == pin
    return body

@pytest.fixture(autouse=True)
def _forbid_invalid_dns(monkeypatch):
    real = socket.getaddrinfo
    def spy(host, *args, **kwargs):
        if isinstance(host, str) and ".invalid" in host:
            raise AssertionError(host)
        return real(host, *args, **kwargs)
    monkeypatch.setattr(socket, "getaddrinfo", spy)

def _deadline(span: float = 5.0) -> float:
    return time.monotonic() + span

async def _connect(port: int):
    return await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 1)

async def _shutdown(writer):
    writer.close()
    with contextlib.suppress(TimeoutError, OSError, ConnectionError):
        await asyncio.wait_for(writer.wait_closed(), 0.3)

async def _converse(port, payload, timeout):
    reader, writer = await _connect(port)
    data = bytearray()
    try:
        writer.write(payload)
        with contextlib.suppress(TimeoutError, OSError, ConnectionError):
            await asyncio.wait_for(writer.drain(), timeout)
        while len(data) < 65536:
            try:
                block = await asyncio.wait_for(reader.read(8192), timeout)
            except (TimeoutError, OSError, ConnectionError):
                break
            if not block:
                break
            data += block
        return bytes(data)
    finally:
        await _shutdown(writer)

def _malicious(kind, port, token):
    path, host = ("/" + token).encode(), f"127.0.0.1:{port}".encode()
    target = {"path": b"/nope", "query": path + b"?q=1", "absolute": b"http://" + host + path}.get(kind, path)
    method = b"POST " if kind == "post" else b"GET "
    line = {"host": b"Host: localhost", "duphost": b"Host: " + host + b"\r\nHost: " + host, "fold": b"Host: " + host + b"\r\nX-Fold: a\r\n b", "badname": b"Host: " + host + b"\r\nBad Name: x", "nocolon": b"Host: " + host + b"\r\nnocolon", "dup": b"Host: " + host + b"\r\nX-Dup: 1\r\nX-Dup: 2", "ctl": b"Host: " + host + b"\r\nX-Ctl: a\x01b", "length": b"Host: " + host + b"\r\nContent-Length: 4", "chunked": b"Host: " + host + b"\r\nTransfer-Encoding: chunked", "oversize": b"Host: " + host + b"\r\nX-Big: " + (b"z" * 9000)}.get(kind, b"Host: " + host)
    head = method + target + b" HTTP/1.1\r\n" + (b"" if kind == "nohost" else line + b"\r\n") + b"\r\n"
    extra = {"length": b"abcd", "chunked": b"0\r\n\r\n", "body": b"trailing", "pipeline": head}.get(kind, b"")
    return head + extra

async def test_fixtures_match_catalog_bytes_and_pins():
    assert set(catalog.CONTENT_IDS.values()) == set(_IDS)
    checked_idle = False
    for content_id in _IDS:
        body, deadline, resource = _body(content_id), _deadline(), OwnedFixtureResource()
        try:
            await resource.start(content_id, deadline)
            assert resource.original_deadline == deadline and resource.content_id == content_id
            parts = urlsplit(resource.url)
            port, token = parts.port, parts.path[1:]
            assert parts.scheme == "http" and parts.hostname == "127.0.0.1" and parts.query == "" and parts.path == "/" + token
            raw = await _converse(port, _malicious("ok", port, token), 1)
            head, _, payload = raw.partition(b"\r\n\r\n")
            text, digest = head.decode("ascii"), _digest(body)
            assert text.startswith("HTTP/1.1 200 OK\r\n") and "Content-Type: text/html;charset=utf-8\r\n" in text
            assert "X-Content-Type-Options: nosniff\r\n" in text and "Connection: close\r\n" in text
            assert "unsafe-inline" not in text and "script-src 'none'" in text
            assert f"style-src 'sha256-{digest}'" in text and f"style-src-elem 'sha256-{digest}'" in text
            await asyncio.sleep(0)  # completed connection callback has released its Task
            assert payload.decode("utf-8") == body and resource.owned_task_count == 1 and resource.open_connection_count == 0
            if not checked_idle:
                reader, writer = await _connect(port)
                started = time.monotonic()
                try:
                    assert await asyncio.wait_for(reader.read(32), 1.8) == b"" and time.monotonic() - started < 1.8
                finally:
                    await _shutdown(writer)
                checked_idle = True
        finally:
            await resource.close()

@pytest.mark.parametrize("kind", _KINDS)
async def test_malformed_exchange_omits_fixture(kind: str):
    body, resource = _body(_IDS[0]).encode(), OwnedFixtureResource()
    await resource.start(_IDS[0], _deadline())
    parts = urlsplit(resource.url)
    try:
        assert body not in await _converse(parts.port, _malicious(kind, parts.port, parts.path[1:]), 0.5)
    finally:
        await resource.close()

async def test_bad_inputs_bind_failure_and_catalog_mismatch(monkeypatch):
    calls = 0
    async def wrapped(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("listened")
    monkeypatch.setattr(asyncio, "start_server", wrapped)
    samples = [(_IDS[0], None), (_IDS[0], True), (_IDS[0], False), (_IDS[0], math.nan), (_IDS[0], math.inf), (_IDS[0], -math.inf), (_IDS[0], time.monotonic()), (_IDS[0], time.monotonic() - 1), (_IDS[0], time.monotonic() + 121), ("http://owned-fixture.invalid/missing", _deadline())]
    for content_id, deadline in samples:
        resource = OwnedFixtureResource()
        with pytest.raises(ValueError):
            await resource.start(content_id, deadline)
        await resource.close()
    assert calls == 0
    import decision_browser.owned_fixture_server as server
    server.resolve = catalog.resolve
    content_id, original, saved = _IDS[1], _body(_IDS[1]), dict(catalog._BODIES)
    try:
        for key, value in list(saved.items()):
            if value == original:
                catalog._BODIES[key] = original.replace("</style>", "/*x*/</style>", 1)
                break
        else:
            catalog._BODIES[content_id] = original.replace("</style>", "/*x*/</style>", 1)
        resource = OwnedFixtureResource()
        with pytest.raises(RuntimeError):
            await resource.start(content_id, _deadline())
        await resource.close()
    finally:
        catalog._BODIES.clear()
        catalog._BODIES.update(saved)
    assert calls == 0
    async def boom(*args, **kwargs):
        raise OSError("bind")
    monkeypatch.setattr(asyncio, "start_server", boom)
    resource = OwnedFixtureResource()
    with pytest.raises(OSError):
        await resource.start(_IDS[0], _deadline())
    assert resource.cleanup_status is not None and resource.cleanup_status.listener_closed
    assert await resource.close() == resource.cleanup_status

async def test_expiry_close_joins_owned_tasks_and_preserves_foreign_socket():
    async def peer(reader, writer):
        writer.close()
    foreign = await asyncio.start_server(peer, "127.0.0.1", 0)
    foreign_port, deadline, resource = foreign.sockets[0].getsockname()[1], _deadline(0.4), OwnedFixtureResource()
    job = None
    try:
        await resource.start(_IDS[2], deadline)
        port = urlsplit(resource.url).port
        assert resource.original_deadline == deadline
        reader, writer = await _connect(port)
        try:
            assert await asyncio.wait_for(reader.read(32), 1.5) == b""
        finally:
            await _shutdown(writer)
        with pytest.raises(OSError):
            await _connect(port)
        await asyncio.sleep(0)
        async def parked():
            await asyncio.Event().wait()
        job = asyncio.create_task(parked())
        resource._tasks.add(job)
        started = time.monotonic()
        report = await resource.close()
        assert time.monotonic() - started < 0.5
        assert report.unfinished_tasks == 0 and not report.unknown and report.clean
        assert report.owned_tasks == resource.owned_task_count == 0
        assert await resource.close() == report and resource.original_deadline == deadline
        assert resource.open_connection_count == 0
        with pytest.raises(OSError):
            await _connect(port)
        _reader, other = await _connect(foreign_port)
        await _shutdown(other)
    finally:
        if job is not None:
            job.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await job
        foreign.close()
        await asyncio.wait_for(foreign.wait_closed(), 1)
        await resource.close()
