"""Embedded data:image markers must not leave without image checks."""

from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest

from decision_browser.budget import Budget
from decision_browser.contracts import Limits, PrivacyPolicy
from decision_browser.decision_codec import ChoiceQuestion
from decision_browser.privacy import CaptureEvidence, PrivacyError, PrivacyGate
from decision_browser.provider import OpenAIProvider

SECRET = "super-secret-token"
PNG = b"\x89PNG\r\n\x1a\n" + SECRET.encode()
CLEAN = b"\x89PNG\r\n\x1a\nclean-pixels"


def _gate() -> PrivacyGate:
    return PrivacyGate(PrivacyPolicy(blocked_literals=[SECRET], send_images=True))


def _evidence(image: bytes) -> CaptureEvidence:
    return CaptureEvidence(
        capture_sha256=hashlib.sha256(image).hexdigest(),
        sanitized=True,
        sensitive_content_detected=False,
    )


def _embedded(prefix: str) -> bytes:
    payload = base64.b64encode(PNG).decode("ascii")
    text = f"{prefix}data:image/png;base64,{payload}"
    body = json.dumps({"dom": text}).encode()
    assert SECRET.encode() not in body
    return body


def _code(body: bytes, **kwargs: object) -> str:
    with pytest.raises(PrivacyError) as caught:
        _gate().check_payload(body, **kwargs)  # type: ignore[arg-type]
    assert SECRET not in str(caught.value)
    return caught.value.code


@pytest.mark.parametrize(
    "prefix",
    ["see ", " ", "x" * 63, "x" * 64, "x" * 80],
)
def test_embedded_data_image_rejected_when_flag_false(prefix: str) -> None:
    assert _code(_embedded(prefix), contains_image=False) == "image_flag"


def test_uppercase_embedded_marker_rejected() -> None:
    payload = base64.b64encode(PNG).decode("ascii")
    body = json.dumps({"dom": f"see DATA:IMAGE/png;base64,{payload}"}).encode()
    assert SECRET.encode() not in body
    assert _code(body) == "image_flag"


def test_embedded_with_flag_is_not_a_bound_image() -> None:
    body = _embedded("see ")
    assert _code(body, capture=_evidence(PNG), contains_image=True) == "image_undecodable"


def test_ordinary_text_still_passes() -> None:
    _gate().check_payload(json.dumps({"dom": "see the page and database catalog"}).encode())


def test_exact_data_url_controls_unchanged() -> None:
    url = "data:image/png;base64," + base64.b64encode(CLEAN).decode("ascii")
    body = json.dumps({"img": url}).encode()
    assert SECRET.encode() not in body
    assert _code(body, contains_image=False) == "image_flag"
    _gate().check_payload(body, _evidence(CLEAN), contains_image=True)


@pytest.mark.asyncio
async def test_provider_embedded_image_makes_zero_requests() -> None:
    hits = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal hits
        hits += 1
        return httpx.Response(200, content=b"{}")

    provider = OpenAIProvider("test-key", transport=httpx.MockTransport(handler))
    with pytest.raises(PrivacyError) as caught:
        await provider.decide(
            (ChoiceQuestion("operation", "choose", ("DONE",)),),
            _embedded("see ").decode(),
            budget=Budget(Limits(), clock=lambda: 0.0),
            privacy=_gate(),
        )
    await provider.aclose()
    assert caught.value.code == "image_flag"
    assert hits == 0
    assert SECRET not in str(caught.value)


def test_nested_duplicate_keys_rejected() -> None:
    body = b'{"wrap":{"k":1,"k":2}}'
    assert _code(body) == "malformed_json"


def test_escaped_duplicate_key_rejected() -> None:
    body = b'{"dom":"catalog","\\u0064om":"page"}'
    assert SECRET.encode() not in body
    assert _code(body) == "malformed_json"


def test_earlier_escaped_blocked_literal_in_duplicate_rejected() -> None:
    body = b'{"dom":"super-\\u0073ecret-token","dom":"see the page"}'
    assert SECRET.encode() not in body
    assert _code(body) == "malformed_json"


def test_canonical_unique_keys_pass() -> None:
    body = json.dumps({"dom": "see the page", "note": "catalog"}).encode()
    _gate().check_payload(body)


def test_canonical_nested_unique_keys_pass() -> None:
    body = json.dumps({"wrap": {"k": "a", "m": "b"}, "dom": "catalog"}).encode()
    _gate().check_payload(body)
