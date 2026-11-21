"""F13 provider regressions: controlled HTTP streams, no configured live service."""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import tracemalloc
import zlib
from pathlib import Path

import httpx
import pytest
from test_provider import _wire_answers
from test_systemone_provider import reply

from decision_browser.budget import Budget, BudgetExceeded
from decision_browser.contracts import Limits, PrivacyPolicy
from decision_browser.decision_codec import ChoiceQuestion, Decision
from decision_browser.privacy import PrivacyGate
from decision_browser.provider import OpenAIProvider
from decision_browser.provider_http import ProviderError, post_json
from decision_browser.systemone_provider import SystemOneProvider

_QUESTIONS = (
    ChoiceQuestion("operation", "Choose action", ("click", "DONE")),
    ChoiceQuestion("target_click", "Choose target", ("first", "last")),
)
_HEADS = {"click": "target_click"}


def gate():
    return PrivacyGate(PrivacyPolicy())


def record_allocation(values):
    if evidence_path := os.environ.get("DECISION_BROWSER_TEST_ALLOCATION_EVIDENCE"):
        with Path(evidence_path).open("a") as output:
            output.write(json.dumps(values) + "\n")


class Stream(httpx.AsyncByteStream):
    def __init__(self, raw, *, error=None, close_error=None, close_delay=0, chunk_size=None):
        self.raw = raw
        self.error = error
        self.close_error = close_error
        self.close_delay = close_delay
        self.closes = 0
        self.chunk_size = chunk_size or max(len(raw), 1)
        self.reads = 0

    async def __aiter__(self):
        for start in range(0, len(self.raw), self.chunk_size):
            self.reads += 1
            yield self.raw[start:start + self.chunk_size]
        if self.error is not None:
            raise self.error

    async def aclose(self):
        self.closes += 1
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        if self.close_error is not None:
            raise self.close_error


class ObservedBudget(Budget):
    def __init__(self, limit):
        super().__init__(Limits(max_output_bytes=limit))
        self.decoded_chunks = []

    def consume_output(self, amount):
        self.decoded_chunks.append(amount)
        super().consume_output(amount)


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
@pytest.mark.parametrize("status", [200, 502])
async def test_compressed_output_is_bounded_before_allocation(encoding, status):
    expanded_size = 8 * 1024 * 1024
    raw_json = json.dumps({"text": "x" * expanded_size}).encode()
    raw = gzip.compress(raw_json) if encoding == "gzip" else zlib.compress(raw_json)
    limit = 16 * 1024
    assert len(raw) < limit
    stream = Stream(raw)
    used = ObservedBudget(limit)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"content-encoding": encoding}, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tracemalloc.start()
        try:
            with pytest.raises(BudgetExceeded, match="output_bytes"):
                await post_json(client, "https://example.test/model", "synthetic-key", {},
                                budget=used, privacy=gate())
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    record_allocation({"encoding": encoding, "status": status,
                       "raw_bytes": len(raw), "decoded_fixture_bytes": len(raw_json),
                       "limit": limit, "peak_traced_bytes": peak,
                       "largest_consumed_chunk": max(used.decoded_chunks, default=0)})
    assert max(used.decoded_chunks) <= limit + 1
    # A broad relative allocation bound, not an OS/RSS or precise allocator claim.
    assert peak < expanded_size // 4
    assert len(calls) == used.usage().calls == stream.closes == 1


@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate", "raw-deflate"])
async def test_small_streamed_json_preserves_decode_and_exact_decoded_accounting(encoding):
    body = b'{"ready":true}'
    if encoding == "gzip":
        raw = gzip.compress(body)
    elif encoding == "deflate":
        raw = zlib.compress(body)
    elif encoding == "raw-deflate":
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        raw = compressor.compress(body) + compressor.flush()
    else:
        raw = body
    stream = Stream(raw)
    used = Budget(Limits(max_output_bytes=len(body)))
    headers = {} if encoding == "identity" else {"content-encoding": "deflate" if encoding == "raw-deflate" else encoding}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers=headers, stream=stream)
    )) as client:
        assert await post_json(client, "https://example.test/model", None, {},
                               budget=used, privacy=gate()) == {"ready": True}
    assert used.usage().calls == 1
    assert used.usage().output_bytes == len(body)
    assert stream.closes == 1


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "raw-deflate"])
@pytest.mark.parametrize("chunk_size", [1, 7, 16384])
async def test_compressed_tail_is_drained_across_bounded_chunks(encoding, chunk_size):
    body = json.dumps({"value": "abcdef" * 20000}).encode()
    if encoding == "gzip":
        raw = gzip.compress(body)
    else:
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS if encoding == "raw-deflate" else zlib.MAX_WBITS)
        raw = compressor.compress(body) + compressor.flush()
    stream = Stream(raw, chunk_size=chunk_size)
    used = Budget(Limits(max_output_bytes=len(body)))
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-encoding": "gzip" if encoding == "gzip" else "deflate"}, stream=stream)
    )) as client:
        assert await post_json(client, "https://example.test/model", None, {},
                               budget=used, privacy=gate()) == json.loads(body)
    assert used.usage().output_bytes == len(body)
    assert used.usage().calls == stream.closes == 1


async def test_raw_compression_framing_has_a_bounded_allowance():
    body = b"{}"
    normal = gzip.compress(body)
    # A valid gzip comment may consume input without producing decoded bytes.
    header = normal[:3] + b"\x10" + normal[4:10]
    raw = header + b"x" * (128 * 1024) + b"\0" + normal[10:]
    stream = Stream(raw, chunk_size=1024)
    used = Budget(Limits(max_output_bytes=32))
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-encoding": "gzip"}, stream=stream)
    )) as client:
        with pytest.raises(BudgetExceeded, match="output_bytes"):
            await post_json(client, "https://example.test/model", None, {}, budget=used, privacy=gate())
    assert stream.reads < len(raw) // 1024
    assert used.usage().calls == stream.closes == 1


async def test_empty_raw_chunks_still_observe_the_original_deadline():
    now = [0.0]

    def clock():
        now[0] += 0.01
        return now[0]

    class EmptyStream(Stream):
        async def __aiter__(self):
            for _ in range(1000):
                self.reads += 1
                yield b""

    stream = EmptyStream(b"")
    used = Budget(Limits(max_seconds=0.05), clock=clock)
    original = used.original_deadline
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream)
    )) as client:
        with pytest.raises(BudgetExceeded, match="deadline_expired"):
            await post_json(client, "https://example.test/model", None, {}, budget=used, privacy=gate())
    assert stream.reads < 1000
    assert used.original_deadline == original
    assert used.usage().calls == stream.closes == 1


@pytest.mark.parametrize("status,marker", [
    (401, "http_status:unauthorized"), (429, "http_status:rate_limited"),
    (502, "http_status:server_error"), (400, "http_status:client_error"),
    (302, "http_status:redirect"),
])
@pytest.mark.parametrize("oversized", [False, True])
async def test_http_status_diagnostic_survives_body_budget_failure(status, marker, oversized):
    raw = b"PRIVATE_BODY_CANARY" * (10 if oversized else 1)
    used = Budget(Limits(max_output_bytes=10 if oversized else 4096))
    expected_type = BudgetExceeded if oversized else ProviderError
    expected_code = "output_bytes" if oversized else "http_error"
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, stream=Stream(raw))
    )) as client:
        with pytest.raises(expected_type) as caught:
            await post_json(client, "https://example.test/model", "synthetic-key", {},
                            budget=used, privacy=gate())
    assert caught.value.code == expected_code
    assert marker in getattr(caught.value, "diagnostics", ())
    assert "PRIVATE_BODY_CANARY" not in str(caught.value)
    assert "synthetic-key" not in repr(caught.value)
    assert used.usage().calls == 1


@pytest.mark.parametrize("failure", ["budget", "transport"])
async def test_response_close_error_preserves_primary_sanitized_failure(failure):
    stream = Stream(b"x" * (50 if failure == "budget" else 1),
                    error=httpx.ReadError("PRIVATE_READ_CANARY") if failure == "transport" else None,
                    close_error=RuntimeError("PRIVATE_CLOSE_CANARY"))
    used = Budget(Limits(max_output_bytes=10))
    expected_type = BudgetExceeded if failure == "budget" else ProviderError
    expected_code = "output_bytes" if failure == "budget" else "transport_error"
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream)
    )) as client:
        with pytest.raises(expected_type) as caught:
            await post_json(client, "https://example.test/model", "synthetic-key", {},
                            budget=used, privacy=gate())
    assert caught.value.code == expected_code
    assert "provider:response_close_failed" in getattr(caught.value, "diagnostics", ())
    assert "PRIVATE_" not in str(caught.value)
    assert stream.closes == 1


async def test_slow_close_has_its_own_bound_without_overwriting_primary_budget_error():
    stream = Stream(b"x" * 50, close_delay=10)
    # Shutdown must finish within the one-second guard while the request still has time.
    used = Budget(Limits(max_output_bytes=10, max_seconds=5))
    deadline = used.original_deadline
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream)
    )) as client:
        async with asyncio.timeout(1):
            with pytest.raises(BudgetExceeded, match="output_bytes") as caught:
                await post_json(client, "https://example.test/model", None, {}, budget=used, privacy=gate())
    assert "provider:response_close_failed" in getattr(caught.value, "diagnostics", ())
    assert used.original_deadline == deadline
    assert stream.closes == 1


async def test_cancelled_stream_retains_cancellation_when_close_also_fails():
    stream = Stream(b"x", error=asyncio.CancelledError(), close_error=RuntimeError("PRIVATE_CLOSE_CANARY"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream)
    )) as client:
        with pytest.raises(asyncio.CancelledError) as caught:
            await post_json(client, "https://example.test/model", None, {}, budget=Budget(Limits()), privacy=gate())
    assert "provider:response_close_failed" in getattr(caught.value, "diagnostics", ())
    assert stream.closes == 1


@pytest.mark.parametrize("body,expected", [(b'{"ready":true}', "response_close_failed"),
                                            (b"malformed", "malformed_response")])
async def test_close_failure_does_not_hide_parsing_or_publish_success(body, expected):
    stream = Stream(body, close_error=RuntimeError("PRIVATE_CLOSE_CANARY"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream)
    )) as client:
        with pytest.raises(ProviderError) as caught:
            await post_json(client, "https://example.test/model", None, {}, budget=Budget(Limits()), privacy=gate())
    assert caught.value.code == expected
    assert "provider:response_close_failed" in caught.value.diagnostics
    assert "PRIVATE_" not in repr(caught.value)
    assert stream.closes == 1


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
async def test_truncated_compression_is_rejected_even_when_json_text_was_decoded(encoding):
    raw = gzip.compress(b'{"ready":true}') if encoding == "gzip" else zlib.compress(b'{"ready":true}')
    stream = Stream(raw[:-2], chunk_size=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-encoding": encoding}, stream=stream)
    )) as client:
        with pytest.raises(ProviderError, match="transport_error"):
            await post_json(client, "https://example.test/model", None, {}, budget=Budget(Limits()), privacy=gate())
    assert stream.closes == 1


async def test_unsupported_compression_fails_closed_before_body_decode():
    stream = Stream(b"{}")
    used = Budget(Limits())
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-encoding": "unsupported"}, stream=stream)
    )) as client:
        with pytest.raises(ProviderError, match="unsupported_content_encoding"):
            await post_json(client, "https://example.test/model", None, {}, budget=used, privacy=gate())
    assert stream.reads == used.usage().output_bytes == 0
    assert used.usage().calls == stream.closes == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_uncooperative_close_is_bounded_and_owned_by_the_awaited_operation(cancel):
    release = asyncio.Event()
    started = asyncio.Event()

    class UncooperativeStream(Stream):
        async def aclose(self):
            self.closes += 1
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

    stream = UncooperativeStream(b"x" * 50)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream)
    )) as client:
        operation = asyncio.create_task(post_json(
            client, "https://example.test/model", None, {},
            budget=Budget(Limits(max_output_bytes=10)), privacy=gate(),
        ))
        residuals = ()
        try:
            async with asyncio.timeout(1):
                await started.wait()
            if cancel:
                operation.cancel()
            done, pending = await asyncio.wait({operation}, timeout=1)
            assert operation in done and not pending
            with pytest.raises(asyncio.CancelledError if cancel else BudgetExceeded) as caught:
                await operation
            if cancel:
                assert "provider:primary_output_bytes" in caught.value.diagnostics
            else:
                assert caught.value.code == "output_bytes"
            residuals = caught.value.residual_tasks
            assert len(residuals) == 1 and not residuals[0].done()
            assert operation._decision_browser_residual_tasks == residuals
            assert "provider:response_close_failed" in caught.value.diagnostics
        finally:
            release.set()
            done, pending = await asyncio.wait({operation, *residuals}, timeout=1)
            assert not pending
            for job in done:
                if not job.cancelled():
                    job.exception()
    assert stream.closes == 1


@pytest.mark.parametrize("mapping", [
    {"operation_head": "missing", "target_heads": _HEADS},
    {"target_heads": {"click": "missing"}},
    {"target_heads": {"not-offered": "target_click"}},
])
async def test_openai_mapping_fails_before_any_http_or_provider_reservation(mapping):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=_wire_answers(json.loads(request.content), {
            "operation": "click", "target_click": "last",
        }))

    provider = OpenAIProvider("synthetic-key", transport=httpx.MockTransport(handler))
    used = Budget(Limits())
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.decide(_QUESTIONS, "observed page", budget=used, privacy=gate(), **mapping)
    finally:
        await provider.aclose()
    assert caught.value.code in {"invalid_mapping", "missing_target"}
    assert calls == [] and used.usage().calls == used.usage().input_bytes == 0


@pytest.mark.parametrize("questions,heads", [
    (_QUESTIONS, None),
    (_QUESTIONS, {}),
    ((ChoiceQuestion("operation", "Choose action", ("click", "select", "DONE")),
      _QUESTIONS[1], ChoiceQuestion("target_select", "Choose option", ("first", "last"))),
     {"click": "target_click"}),
], ids=["none", "empty", "partial"])
async def test_every_offered_nonterminal_requires_mapping_before_http(questions, heads):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=_wire_answers(json.loads(request.content), {
            "operation": "click", "target_click": "last", "target_select": "last",
        }))

    provider = OpenAIProvider("synthetic-key", transport=httpx.MockTransport(handler))
    used = Budget(Limits())
    try:
        with pytest.raises(ProviderError, match="missing_target"):
            await provider.decide(questions, "observed page", budget=used, privacy=gate(),
                                  target_heads=heads)
    finally:
        await provider.aclose()
    assert calls == []
    assert used.usage().calls == used.usage().input_bytes == 0


@pytest.mark.parametrize("heads", [None, {}])
@pytest.mark.parametrize("choices,selected", [
    (("DONE",), "DONE"), (("UNABLE",), "UNABLE"),
    (("DONE", "UNABLE"), "DONE"), (("DONE", "UNABLE"), "UNABLE"),
])
async def test_terminal_only_decisions_require_no_target_mapping(choices, selected, heads):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=_wire_answers(json.loads(request.content), {
            "operation": selected,
        }))

    provider = OpenAIProvider("synthetic-key", transport=httpx.MockTransport(handler))
    used = Budget(Limits())
    try:
        assert await provider.decide(
            (ChoiceQuestion("operation", "Choose terminal", choices),), "observed page",
            budget=used, privacy=gate(), target_heads=heads,
        ) == Decision(selected, None, 1.0)
    finally:
        await provider.aclose()
    assert len(calls) == used.usage().calls == 1


@pytest.mark.parametrize("selected", ["click", "select", "DONE", "UNABLE"])
async def test_complete_nonterminal_mapping_preserves_selected_action(selected):
    questions = (ChoiceQuestion("operation", "Choose action", ("click", "select", "DONE", "UNABLE")),
                 ChoiceQuestion("target", "Choose target", ("first", "last")))
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=_wire_answers(json.loads(request.content), {
            "operation": selected, "target": "last",
        }))

    provider = OpenAIProvider("synthetic-key", transport=httpx.MockTransport(handler))
    used = Budget(Limits())
    try:
        assert await provider.decide(
            questions, "observed page", budget=used, privacy=gate(),
            target_heads={"click": "target", "select": "target"},
        ) == Decision(selected, None if selected in {"DONE", "UNABLE"} else "last", 1.0)
    finally:
        await provider.aclose()
    assert len(calls) == used.usage().calls == 1


async def test_explicit_unable_mapping_retains_custom_nonterminal_semantics():
    questions = (ChoiceQuestion("operation", "Choose action", ("UNABLE", "DONE")),
                 ChoiceQuestion("target_unable", "Choose custom target", ("first", "last")))
    provider = OpenAIProvider("synthetic-key", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=_wire_answers(json.loads(request.content), {
            "operation": "UNABLE", "target_unable": "last",
        }))
    ))
    used = Budget(Limits())
    try:
        assert await provider.decide(
            questions, "observed page", budget=used, privacy=gate(),
            target_heads={"UNABLE": "target_unable"},
        ) == Decision("UNABLE", "last", 1.0)
    finally:
        await provider.aclose()
    assert used.usage().calls == 1


@pytest.mark.parametrize("route", ["openai", "systemone"])
async def test_selected_target_confidence_is_aggregated_with_operation(route):
    def handler(request):
        body = json.loads(request.content)
        if route == "systemone":
            data = reply(body)
            data["answers"]["operation"]["confidence"] = 0.9
            data["answers"]["target_click"]["confidence"] = 0.2
        else:
            data = json.loads(_wire_answers(body, {"operation": "click", "target_click": "last"}))
            data["answers"][0]["confidence"] = 0.9
            data["answers"][1]["confidence"] = 0.2
        return httpx.Response(200, json=data)

    transport = httpx.MockTransport(handler)
    provider = OpenAIProvider("synthetic-key", transport=transport) if route == "openai" else SystemOneProvider(
        endpoint="http://127.0.0.1:9000/v1/systemone", model="test-model", transport=transport,
    )
    used = Budget(Limits())
    try:
        assert await provider.decide(_QUESTIONS, "observed page", budget=used, privacy=gate(),
                                     target_heads=_HEADS) == Decision("click", "last", 0.2)
    finally:
        await provider.aclose()
    assert used.usage().calls == 1


@pytest.mark.parametrize("key", ["", None, "nonascii-é", "key\nline", 42])
async def test_openai_constructor_rejects_malformed_key_before_client_creation(key):
    provider = None
    try:
        with pytest.raises(ProviderError, match="invalid_api_key"):
            provider = OpenAIProvider(key)
    finally:
        if provider is not None:
            await provider.aclose()


@pytest.mark.parametrize("key", ["nonascii-é", "key\nline", 42])
async def test_shared_transport_rejects_malformed_key_before_call_reservation(key):
    calls = []
    used = Budget(Limits())
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, json={})
    )) as client:
        with pytest.raises(ProviderError, match="invalid_api_key"):
            await post_json(client, "https://example.test/model", key, {}, budget=used, privacy=gate())
    assert calls == [] and used.usage().calls == 0
