"""Shared bounded HTTP boundary for explicitly configured model providers."""

from __future__ import annotations

import asyncio
import json
import math
import zlib
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .budget import Budget, BudgetExceeded
from .privacy import CaptureEvidence, PrivacyGate
from .provider_config import ProviderConfigError, validate_api_key

_CHUNK_BYTES = 16 * 1024
_COMPRESSION_OVERHEAD = 64 * 1024
_CLOSE_SECONDS = 0.1
_CLOSE_JOIN_SECONDS = 0.05
_RESIDUAL_TASKS = "_decision_browser_residual_tasks"


class ProviderError(Exception):
    """Stable safe code, with no request or response attached."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.diagnostics: tuple[str, ...] = ()
        super().__init__(code)


def _diagnostic(error: BaseException, code: str) -> None:
    current = getattr(error, "diagnostics", ())
    if type(current) is not tuple or any(type(item) is not str for item in current):
        current = ()
    error.__dict__["diagnostics"] = tuple(dict.fromkeys((code, *current)))[:8]


def _status_diagnostic(status: int) -> str | None:
    if status == 401:
        return "http_status:unauthorized"
    if status == 429:
        return "http_status:rate_limited"
    if 500 <= status < 600:
        return "http_status:server_error"
    if 400 <= status < 500:
        return "http_status:client_error"
    if 300 <= status < 400:
        return "http_status:redirect"
    return None


def _consume_close(job: asyncio.Future[Any]) -> None:
    if not job.cancelled():
        job.exception()


def _remember_residual(error: BaseException, jobs: tuple[asyncio.Task[None], ...]) -> None:
    if not jobs:
        return
    previous = getattr(error, "residual_tasks", ())
    if type(previous) is not tuple:
        previous = ()
    held_error = tuple(job for job in previous if isinstance(job, asyncio.Task) and not job.done())
    error.__dict__["residual_tasks"] = tuple(dict.fromkeys((*held_error, *jobs)))
    owner = asyncio.current_task()
    if owner is not None:
        current = getattr(owner, _RESIDUAL_TASKS, ())
        if type(current) is not tuple:
            current = ()
        held = tuple(job for job in current if isinstance(job, asyncio.Task) and not job.done())
        setattr(owner, _RESIDUAL_TASKS, tuple(dict.fromkeys((*held, *jobs))))


async def _close_response(response: httpx.Response) -> tuple[bool, tuple[asyncio.Task[None], ...]]:
    """Give close its own fixed bound; retain any uncooperative child by identity."""
    job = asyncio.create_task(response.aclose())
    job.add_done_callback(_consume_close)
    try:
        done, _pending = await asyncio.wait({job}, timeout=_CLOSE_SECONDS)
        if job in done:
            return job.cancelled() or job.exception() is not None, ()
        job.cancel()
        done, _pending = await asyncio.wait({job}, timeout=_CLOSE_JOIN_SECONDS)
        return True, () if job in done else (job,)
    except asyncio.CancelledError as exc:
        job.cancel()
        try:
            await asyncio.wait({job}, timeout=_CLOSE_JOIN_SECONDS)
        finally:
            _remember_residual(exc, () if job.done() else (job,))
        raise


async def _decoded_chunks(response: httpx.Response, budget: Budget) -> AsyncIterator[bytes]:
    """Bound both encoded input and each decompressor result before retention.

    gzip/deflate framing gets a fixed extra raw allowance, not an unbounded
    drain. Prebuffered custom transports have already decoded their content;
    they retain compatibility but cannot prove allocation at this boundary.
    """
    if response.is_stream_consumed:
        content = response.content
        for start in range(0, len(content), _CHUNK_BYTES):
            chunk = content[start:start + _CHUNK_BYTES]
            budget.consume_output(len(chunk))
            yield chunk
        return
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"", "identity", "gzip", "deflate"}:
        raise ProviderError("unsupported_content_encoding")
    compressed = encoding in {"gzip", "deflate"}
    raw_allowance = budget.remaining_output_bytes + (_COMPRESSION_OVERHEAD if compressed else 0)
    raw_used = 0
    decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16) if encoding == "gzip" else None
    prefix = b""
    if not isinstance(response.stream, httpx.AsyncByteStream):
        raise ProviderError("transport_error")
    # aiter_raw closes at EOF. Read the public transport stream here so close
    # has a separate bound and cannot replace a later parsing/read failure.
    async for raw_bytes in response.stream:
        budget.remaining()
        if type(raw_bytes) is not bytes:
            raise ProviderError("transport_error")
        for start in range(0, len(raw_bytes), _CHUNK_BYTES):
            raw = raw_bytes[start:start + _CHUNK_BYTES]
            raw_used += len(raw)
            if raw_used > raw_allowance:
                raise BudgetExceeded("output_bytes")
            if not compressed:
                budget.consume_output(len(raw))
                yield raw
                continue
            if decompressor is None:
                prefix += raw
                if len(prefix) < 2:
                    continue
                # Preserve HTTPX's zlib-wrapped and legacy raw deflate support.
                # Detect the two-byte zlib header across chunk edges.
                header = (prefix[0] << 8) | prefix[1]
                wrapped = prefix[0] & 15 == 8 and prefix[0] >> 4 <= 7 and header % 31 == 0
                decompressor = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
                raw, prefix = prefix, b""
            if decompressor.eof:
                raise ProviderError("transport_error")
            while True:
                # Zero means unlimited to zlib; the extra byte detects quota
                # overflow while still permitting a zero-output trailer.
                maximum = min(_CHUNK_BYTES, budget.remaining_output_bytes + 1)
                try:
                    decoded = decompressor.decompress(raw, max_length=maximum)
                except zlib.error:
                    raise ProviderError("transport_error") from None
                tail = decompressor.unconsumed_tail
                if decoded:
                    budget.consume_output(len(decoded))
                    yield decoded
                if decompressor.unused_data:
                    raise ProviderError("transport_error")
                if not tail and (len(decoded) < maximum or decompressor.eof):
                    break
                if not decoded and tail == raw:
                    raise ProviderError("transport_error")
                raw = tail
    if compressed and (decompressor is None or not decompressor.eof):
        raise ProviderError("transport_error")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    found: dict[str, object] = {}
    for key, value in pairs:
        if key in found:
            raise ValueError("duplicate_key")
        found[key] = value
    return found


def _constant(_token: str) -> float:
    raise ValueError("non_finite")


def _finite(token: str) -> float:
    try:
        value = float(token)
    except (ValueError, OverflowError):
        raise ValueError("non_finite") from None
    if not math.isfinite(value):
        raise ValueError("non_finite")
    return value


def load_json(raw: bytes) -> dict[str, object]:
    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
            parse_float=_finite,
        )
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise ProviderError("malformed_response") from None
    if type(parsed) is not dict:
        raise ProviderError("malformed_response")
    return parsed


async def post_json(
    client: httpx.AsyncClient,
    endpoint: str,
    api_key: str | None,
    payload: object,
    *,
    budget: Budget,
    privacy: PrivacyGate,
    capture: CaptureEvidence | None = None,
    contains_image: bool = False,
) -> dict[str, object]:
    """Check the exact outgoing bytes before reserving a call or opening a socket.

    Each physical request spends from the caller's original budget. There is
    no retry, redirect, new deadline or provider fallback at this boundary.
    Endpoints are validated by provider configuration; credential syntax is
    also checked here for direct Python callers.
    """
    try:
        api_key = validate_api_key(api_key)
    except ProviderConfigError:
        raise ProviderError("invalid_api_key") from None
    response = None
    primary: BaseException | None = None
    status_note = None
    try:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise ProviderError("invalid_request") from None
    privacy.check_payload(body, capture, contains_image)
    budget.reserve_provider(len(body))
    headers = {"Content-Type": "application/json"}
    if api_key is not None:
        headers["Authorization"] = "Bearer " + api_key
    try:
        async with asyncio.timeout(budget.remaining()):
            # Build an explicit request so a supplied client's default headers,
            # cookies or query parameters cannot carry one route's credentials
            # to another. Its transport/pool remains reusable; auth is explicit.
            request = httpx.Request(
                "POST", endpoint, content=body, headers=headers,
                extensions={"timeout": httpx.Timeout(None).as_dict()},
            )
            response = await client.send(request, auth=None, stream=True, follow_redirects=False)
            status_note = _status_diagnostic(response.status_code)
            parts: list[bytes] = []
            async for chunk in _decoded_chunks(response, budget):
                if response.status_code == 200:
                    parts.append(chunk)
            if response.status_code != 200:
                raise ProviderError("http_error")
            parsed = load_json(b"".join(parts))
            budget.remaining()
            return parsed
    except (ProviderError, BudgetExceeded, asyncio.CancelledError) as exc:
        primary = exc
        if status_note is not None:
            _diagnostic(exc, status_note)
        raise
    except TimeoutError:
        primary = BudgetExceeded("deadline_expired")
        if status_note is not None:
            _diagnostic(primary, status_note)
        raise primary from None
    except Exception:  # noqa: BLE001 - fixed transport boundary; no raw exception escapes
        primary = ProviderError("transport_error")
        if status_note is not None:
            _diagnostic(primary, status_note)
        raise primary from None
    finally:
        if response is not None:
            try:
                failed, residuals = await _close_response(response)
            except asyncio.CancelledError as exc:
                _diagnostic(exc, "provider:response_close_failed")
                if status_note is not None:
                    _diagnostic(exc, status_note)
                if primary is not None:
                    code = getattr(primary, "code", None)
                    if code in {"output_bytes", "deadline_expired", "http_error", "malformed_response",
                                "transport_error", "unsupported_content_encoding"}:
                        _diagnostic(exc, "provider:primary_" + code)
                raise
            if failed:
                error = primary if primary is not None else ProviderError("response_close_failed")
                _diagnostic(error, "provider:response_close_failed")
                if status_note is not None:
                    _diagnostic(error, status_note)
                _remember_residual(error, residuals)
                if primary is None:
                    raise error
