"""F19 closed visual contract and PNG encoder/decoder parity controls."""
import random

import pytest

from decision_browser.owned_fixture_frame import (
    ENCODED_PNG_MAX,
    RGBA_OCTETS,
    PngDenied,
    decode_owned_png,
    encode_canonical_png,
)
from decision_browser.provider import _VISUAL_SCHEMA
from decision_browser.verification import validate_visual_response


def finding(**changes):
    return {
        "origin": "model", "evidence": "note", "element": "button",
        "viewport": {"width": 390, "height": 844},
        "region": {"x": 0, "y": 0, "width": 1, "height": 1},
        "severity": "low", "uncertainty": 0.5, **changes,
    }


@pytest.mark.parametrize("number", [0, 1, 0.0, 1.0, 0.5])
def test_uncertainty_accepts_schema_numbers(number):
    report = validate_visual_response({"findings": [finding(uncertainty=number)]}, safe_capture=True)
    assert report.diagnostic == "complete"
    assert report.findings[0].uncertainty == number


@pytest.mark.parametrize("number", [True, False, "0", -0.1, 1.1, float("nan"), float("inf")])
def test_uncertainty_rejects_non_schema_numbers(number):
    report = validate_visual_response({"findings": [finding(uncertainty=number)]}, safe_capture=True)
    assert report.diagnostic == "failed" and not report.findings


@pytest.mark.parametrize("text", ["a" * 1024, "я" * 600, "🙂" * 1024])
def test_visual_schema_character_limit_accepts_unicode(text):
    schema = _VISUAL_SCHEMA["properties"]["findings"]["items"]["properties"]["evidence"]
    assert schema["maxLength"] == 1024
    assert len(text) <= schema["maxLength"]
    report = validate_visual_response({"findings": [finding(evidence=text, element=text)]}, safe_capture=True)
    assert report.diagnostic == "complete"


@pytest.mark.parametrize("text", ["", "   ", "a" * 1025, "я" * 1025, "\ud800"])
def test_visual_text_invalid_control(text):
    report = validate_visual_response({"findings": [finding(evidence=text)]}, safe_capture=True)
    assert report.diagnostic == "failed"


def test_png_high_entropy_explicitly_rejected_before_decode():
    rgba = random.Random(19).randbytes(RGBA_OCTETS)
    with pytest.raises(PngDenied, match="^denied$"):
        encode_canonical_png(rgba, original_deadline=10, clock=lambda: 1)


def test_png_accepted_encoder_output_roundtrips_within_decoder_cap():
    rgba = b"\x11\x22\x33\xff" * (RGBA_OCTETS // 4)
    png = encode_canonical_png(rgba, original_deadline=10, clock=lambda: 1)
    assert len(png) <= ENCODED_PNG_MAX
    assert decode_owned_png(png, original_deadline=10, clock=lambda: 1) == rgba


@pytest.mark.parametrize("stage", ["refresh", "evaluate", "close"])
async def test_geometry_cdp_failure_is_closed_and_restores_viewport(monkeypatch, stage):
    from playwright.async_api import Error as PlaywrightError

    from decision_browser import verification
    from decision_browser.contracts import Viewport

    class Page:
        def __init__(self):
            self.viewport_size = {"width": 1280, "height": 720}

        async def set_viewport_size(self, size):
            self.viewport_size = size

    closed = []

    class World:
        def __init__(self, page, *, world_name):
            assert world_name == "decision-browser-geometry-verification-v1"

        async def refresh(self):
            if stage == "refresh":
                raise PlaywrightError("missing context")

        async def evaluate(self, probe):
            if stage == "evaluate":
                raise PlaywrightError("stale context")
            return {"nodes": []}

        async def close(self):
            closed.append(True)
            if stage == "close":
                raise PlaywrightError("detach failed")

    monkeypatch.setattr(verification, "_CDPWorld", World)
    page = Page()
    report = await verification.inspect_geometry(page, Viewport(width=390, height=844))
    assert report.diagnostic == "failed"
    assert report.reason == "playwright_error"
    assert report.findings == []
    assert closed == [True]
    assert page.viewport_size == {"width": 1280, "height": 720}


async def test_geometry_cancellation_closes_world_and_restores_viewport(monkeypatch):
    import asyncio

    from decision_browser import verification
    from decision_browser.contracts import Viewport

    class Page:
        def __init__(self):
            self.viewport_size = {"width": 1280, "height": 720}

        async def set_viewport_size(self, size):
            self.viewport_size = size

    closed = []

    class World:
        def __init__(self, page, *, world_name):
            pass

        async def refresh(self):
            pass

        async def evaluate(self, probe):
            raise asyncio.CancelledError

        async def close(self):
            closed.append(True)

    monkeypatch.setattr(verification, "_CDPWorld", World)
    page = Page()
    with pytest.raises(asyncio.CancelledError):
        await verification.inspect_geometry(page, Viewport(width=390, height=844))
    assert closed == [True]
    assert page.viewport_size == {"width": 1280, "height": 720}
