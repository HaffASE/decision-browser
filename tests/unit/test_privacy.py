import base64
import hashlib
import json

import pytest

from decision_browser.contracts import PrivacyPolicy
from decision_browser.privacy import (
    CaptureEvidence,
    PrivacyError,
    PrivacyGate,
)

SECRET = "super-secret-token"
PNG = b"\x89PNG\r\n\x1a\nfake-pixels"
GATE = PrivacyGate(PrivacyPolicy(blocked_literals=[SECRET], send_images=True))


def _ev(image: bytes, **overrides: object) -> CaptureEvidence:
    fields: dict[str, object] = {
        "capture_sha256": hashlib.sha256(image).hexdigest(),
        "sanitized": True,
        "sensitive_content_detected": False,
    }
    fields.update(overrides)
    return CaptureEvidence(**fields)  # type: ignore[arg-type]


def _url(image: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(image).decode("ascii")


def _body(url: str) -> bytes:
    return json.dumps({"img": url}).encode()


def _code(body: bytes, **kwargs: object) -> str:
    with pytest.raises(PrivacyError) as caught:
        GATE.check_payload(body, **kwargs)  # type: ignore[arg-type]
    assert SECRET not in str(caught.value)
    return caught.value.code


def test_escaped_text_secret():
    body = b'{"note":"\\u0073uper-secret-token"}'
    assert _code(body) == "blocked_literal"


def test_raw_secret_body():
    assert _code(json.dumps({"note": SECRET}).encode()) == "blocked_literal"


def test_missing_unsafe_mismatched_evidence():
    body = _body(_url(PNG))
    assert _code(body, contains_image=True) == "evidence_missing"
    assert (
        _code(body, capture=_ev(PNG, sanitized=False), contains_image=True)
        == "evidence_unsafe"
    )
    assert (
        _code(
            body,
            capture=_ev(PNG, sensitive_content_detected=True),
            contains_image=True,
        )
        == "evidence_unsafe"
    )
    assert (
        _code(body, capture=_ev(b"other"), contains_image=True) == "evidence_mismatch"
    )


def test_image_flag_bypass_and_send_images_false():
    body = _body(_url(PNG))
    assert _code(body, capture=_ev(PNG), contains_image=False) == "image_flag"
    closed = PrivacyGate(PrivacyPolicy(blocked_literals=[SECRET], send_images=False))
    with pytest.raises(PrivacyError) as caught:
        closed.check_payload(body, _ev(PNG), contains_image=True)
    assert caught.value.code == "images_disabled"


def test_safe_image_matching_hash():
    evidence = _ev(PNG)
    url = _url(PNG)
    GATE.check_payload(_body(url), evidence, contains_image=True)
    assert GATE.prepare_image(PNG, evidence) == url


def test_non_base64_image_not_accepted():
    body = _body("data:image/png,not-base64-pixels")
    assert _code(body) == "image_flag"
    assert _code(body, capture=_ev(PNG), contains_image=True) == "image_undecodable"
