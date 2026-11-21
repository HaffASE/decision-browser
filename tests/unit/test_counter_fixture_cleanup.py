"""Real loopback ownership regressions, with bounded client waits."""

import asyncio
import select
import socket
import time

import pytest
from test_counter_acceptance import (
    test_listener_is_exact_bounded_and_closes_partial_client as original_probe,
)

import decision_browser.counter_acceptance as counter


def connect_before_yield(fixture):
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(.5)
    port = int(fixture.url.split(":")[2].split("/")[0])
    client.connect(("127.0.0.1", port))
    client.setblocking(False)
    # A completed client handshake can precede listener readiness on macOS.
    # Wait without yielding: these probes must accept before a responder runs.
    try:
        ready, _, _ = select.select([fixture._listener], [], [], .5)
        assert ready, "fixture listener did not become ready after connect"
    except BaseException:
        client.close()
        raise
    return client


async def eof_or_reset(client):
    try:
        assert await asyncio.wait_for(asyncio.get_running_loop().sock_recv(client, 1), 1) == b""
    except ConnectionResetError:
        pass


async def test_original_partial_client_probe_repeated_100_times():
    for _ in range(100):
        await asyncio.wait_for(original_probe(), 3)


@pytest.mark.parametrize("schedule_close", [False, True])
async def test_close_owns_socket_before_responder_first_instruction(monkeypatch, schedule_close):
    fixture = counter.CounterFixture()
    await fixture.start()
    began = []

    async def delayed(conn):
        began.append(True)
        await asyncio.Event().wait()

    monkeypatch.setattr(fixture, "_respond", delayed)
    client = connect_before_yield(fixture)
    try:
        fixture._accept_ready()
        tasks = tuple(fixture._clients)
        assert len(tasks) == len(fixture._connections) == 1 and began == []
        # wait_for(coroutine) schedules a separate task on Python 3.11, but
        # awaits inline on 3.12+. Control scheduling explicitly so both paths
        # are tested on every supported version, with the same bounded wait.
        async with asyncio.timeout(2):
            if schedule_close:
                await asyncio.create_task(fixture.close())
            else:
                await fixture.close()
        assert began == ([True] if schedule_close else [])
        assert all(task.done() for task in tasks)
        assert fixture.clean and not fixture._connections
        await eof_or_reset(client)
        fixture._accept_ready()  # Already queued callback after removal.
        await asyncio.gather(fixture.close(), fixture.close())
        assert fixture.clean and not fixture._clients
    finally:
        client.close()
        await fixture.close()


async def test_responder_creation_failure_closes_accepted_socket(monkeypatch):
    fixture = counter.CounterFixture()
    await fixture.start()
    client = connect_before_yield(fixture)

    def refuse(_coroutine):
        raise RuntimeError("owned_test_task_creation_failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(counter.asyncio, "create_task", refuse)
            with pytest.raises(RuntimeError, match="owned_test_task_creation_failure"):
                fixture._accept_ready()
        assert not fixture._connections and not fixture._clients
        await eof_or_reset(client)
    finally:
        client.close()
        await fixture.close()
    assert fixture.clean


async def test_unsupported_reader_setup_closes_listener(monkeypatch):
    fixture = counter.CounterFixture()
    loop = asyncio.get_running_loop()

    def refuse(*_args):
        raise NotImplementedError("owned_test_unsupported_loop")

    with monkeypatch.context() as patch:
        patch.setattr(loop, "add_reader", refuse)
        with pytest.raises(NotImplementedError, match="owned_test_unsupported_loop"):
            await fixture.start()
    assert fixture.closed and fixture.clean
    assert fixture._listener.fileno() == -1 and not fixture._reader_registered


async def test_ninth_client_closed_and_foreign_listener_preserved():
    fixture = counter.CounterFixture()
    await fixture.start()
    foreign = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    foreign.settimeout(.5)
    foreign.bind(("127.0.0.1", 0))
    foreign.listen(1)
    clients = []
    try:
        for _ in range(9):
            clients.append(connect_before_yield(fixture))
            fixture._accept_ready()
        assert len(fixture._connections) == len(fixture._clients) == 8
        await eof_or_reset(clients[-1])
        await fixture.close()
        for client in clients:
            await eof_or_reset(client)
        assert fixture.clean and not fixture._connections
        peer = socket.create_connection(foreign.getsockname(), timeout=.5)
        accepted, _ = foreign.accept()
        try:
            peer.sendall(b"owned_foreign_probe")
            assert accepted.recv(64) == b"owned_foreign_probe"
        finally:
            accepted.close()
            peer.close()
    finally:
        for client in clients:
            client.close()
        foreign.close()
        await fixture.close()


@pytest.mark.parametrize("wire_request", [
    b"GET /wrong HTTP/1.1\r\n\r\n",
    b"GET /counter.html HTTP/1.1\r\nX:" + b"a" * 8192 + b"\r\n\r\n",
    b"GET /counter", b"",
])
async def test_wrong_oversized_partial_and_silent_clients_bounded(wire_request):
    fixture = counter.CounterFixture()
    await fixture.start()
    port = int(fixture.url.split(":")[2].split("/")[0])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(wire_request)
        await writer.drain()
        try:
            assert await asyncio.wait_for(reader.read(), 1.5) == b""
        except ConnectionResetError:
            pass
    finally:
        writer.close()
        await fixture.close()
    assert fixture.clean


@pytest.mark.parametrize("operation", ["sock_recv", "sock_sendall"])
async def test_read_and_send_errors_close_owned_connection(monkeypatch, operation):
    fixture = counter.CounterFixture()
    await fixture.start()
    client = connect_before_yield(fixture)
    client.send(b"GET /counter.html HTTP/1.1\r\n\r\n")

    async def refuse(*_args):
        raise OSError("owned_test_io_failure")

    try:
        fixture._accept_ready()
        tasks = tuple(fixture._clients)
        with monkeypatch.context() as patch:
            patch.setattr(asyncio.get_running_loop(), operation, refuse)
            await asyncio.wait_for(asyncio.gather(*tasks), 2)
        await eof_or_reset(client)
        assert not fixture._connections
    finally:
        client.close()
        await fixture.close()
    assert fixture.clean


async def test_cancellation_resistant_responder_keeps_cleanup_unknown(monkeypatch):
    fixture = counter.CounterFixture()
    await fixture.start()
    started, release = asyncio.Event(), asyncio.Event()

    async def resist(conn):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    monkeypatch.setattr(fixture, "_respond", resist)
    client = connect_before_yield(fixture)
    try:
        fixture._accept_ready()
        tasks = tuple(fixture._clients)
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(fixture.close(), 2)
        assert fixture.closed and not fixture.clean and not fixture._connections
        await eof_or_reset(client)
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
        client.close()


async def test_expiry_closes_listener_and_connections():
    fixture = counter.CounterFixture()
    await fixture.start()
    client = connect_before_yield(fixture)
    try:
        fixture._accept_ready()
        fixture.hold_until(time.monotonic() - 1)
        await asyncio.wait_for(fixture._expiry, 2)
        assert fixture.closed and fixture.clean and not fixture._connections
        await eof_or_reset(client)
    finally:
        client.close()
        await fixture.close()


async def test_one_socket_close_failure_still_closes_others_and_joins_tasks():
    fixture = counter.CounterFixture()
    await fixture.start()
    client = connect_before_yield(fixture)

    class UnclosedProbe:
        def close(self):
            raise OSError("owned_test_close_failure")

        def fileno(self):
            return 1234  # Synthetic descriptor; no OS resource is manipulated.

    failed = UnclosedProbe()

    class FailureFirst(set):
        def __iter__(self):
            return iter([failed, *(conn for conn in super().__iter__() if conn is not failed)])

    try:
        fixture._accept_ready()
        tasks = tuple(fixture._clients)
        sockets = tuple(fixture._connections)
        fixture._connections = FailureFirst([failed, *sockets])
        await asyncio.wait_for(fixture.close(), 2)
        assert fixture.closed and not fixture.clean
        assert all(conn.fileno() < 0 for conn in sockets)
        assert all(task.done() for task in tasks)
        assert failed in fixture._connections
        await eof_or_reset(client)
    finally:
        client.close()
        fixture._connections.discard(failed)
