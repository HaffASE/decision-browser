"""Local geometry inspection and closed visual-response validation."""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from playwright.async_api import Page, async_playwright

from decision_browser.contracts import Viewport
from decision_browser.verification import (
    GeometryReport,
    inspect_geometry,
    validate_visual_response,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "visual"
_SHOTS = Path(".omx/implementation/verification/screenshots")
_VIEWPORTS = [(390, 844), (768, 1024), (1440, 900)]
_DEFECT = {"clipping": "clipping", "overlap": "overlap", "responsive": "responsive"}

_MODEL_FINDING = {
    "origin": "model",
    "evidence": "button contrast is low",
    "viewport": {"width": 390, "height": 844},
    "region": {"x": 0.0, "y": 0.0, "width": 10.0, "height": 10.0},
    "element": "button",
    "severity": "medium",
    "uncertainty": 0.5,
}

_IGNORED_OVERLAP = """
<div data-visual-ignore="true" style="position:absolute;left:0;top:0;width:200px;height:200px">
  <div id="back" style="position:absolute;left:0;top:0;width:120px;height:120px;background:red"></div>
  <div id="front" style="position:absolute;left:40px;top:40px;width:120px;height:120px;background:blue"></div>
</div>
"""


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, chromium_sandbox=True, channel="chromium")
        tab = await browser.new_page()
        try:
            yield tab
        finally:
            await tab.close()
            await browser.close()


@pytest.mark.parametrize("name", ["clean", "clipping", "overlap", "responsive"])
@pytest.mark.parametrize("width,height", _VIEWPORTS)
async def test_geometry_fixtures(
    page: Page, name: str, width: int, height: int
) -> None:
    await page.set_viewport_size({"width": width, "height": height})
    await page.set_content((_FIXTURES / f"{name}.html").read_text(encoding="utf-8"))
    _SHOTS.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(_SHOTS / f"{name}-{width}.png"))
    report: GeometryReport = await inspect_geometry(
        page, Viewport(width=width, height=height)
    )
    assert report.diagnostic == "complete"
    findings = list(report.findings)
    if name == "clean":
        assert findings == []
        return
    assert findings
    assert {item.kind for item in findings} == {_DEFECT[name]}
    for item in findings:
        assert item.origin == "geometry"
        assert item.evidence
        assert item.severity
        assert item.uncertainty is not None
        assert item.viewport.width == width
        assert item.viewport.height == height
        assert math.isfinite(item.region.x) and math.isfinite(item.region.y)
        assert math.isfinite(item.region.width) and math.isfinite(item.region.height)
        assert item.region.width > 0 and item.region.height > 0


async def test_ignored_overlapping_subtree_reports_no_findings(page: Page) -> None:
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.set_content(_IGNORED_OVERLAP)
    report: GeometryReport = await inspect_geometry(
        page, Viewport(width=390, height=844)
    )
    assert report.diagnostic == "complete"
    assert list(report.findings) == []


def test_valid_model_finding_is_independent() -> None:
    result = validate_visual_response(
        {"findings": [dict(_MODEL_FINDING)]}, safe_capture=True
    )
    assert result.diagnostic == "complete"
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.origin == "model"
    assert finding.evidence == "button contrast is low"
    assert finding.severity == "medium"
    assert result.diagnostic != "verified"


def test_empty_model_findings_are_not_proof_of_cleanliness() -> None:
    result = validate_visual_response({"findings": []}, safe_capture=True)
    assert result.diagnostic == "complete"
    assert result.reason == "no_model_findings_not_proof_of_cleanliness"
    assert list(result.findings) == []


def test_malformed_extra_fields_fail() -> None:
    finding = dict(_MODEL_FINDING)
    finding["note"] = "extra"
    result = validate_visual_response({"findings": [finding]}, safe_capture=True)
    assert result.diagnostic == "failed"


def test_empty_evidence_fails() -> None:
    finding = dict(_MODEL_FINDING)
    finding["evidence"] = ""
    result = validate_visual_response({"findings": [finding]}, safe_capture=True)
    assert result.diagnostic == "failed"


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_nonfinite_region_fails(bad: float) -> None:
    finding = dict(_MODEL_FINDING)
    finding["region"] = {"x": 0.0, "y": 0.0, "width": bad, "height": 10.0}
    result = validate_visual_response({"findings": [finding]}, safe_capture=True)
    assert result.diagnostic == "failed"


@pytest.mark.parametrize("payload", [{"refusal": "policy"}, {"incomplete": True}])
def test_refusal_or_incomplete(payload: object) -> None:
    result = validate_visual_response(payload, safe_capture=True)
    assert result.diagnostic == "incomplete"


def test_malformed_primitive_failed_schema() -> None:
    result = validate_visual_response("refusal", safe_capture=True)
    assert result.diagnostic == "failed"


def test_transport_error_is_incomplete_or_failed() -> None:
    result = validate_visual_response(
        {"findings": [dict(_MODEL_FINDING)]},
        safe_capture=True,
        transport_error="connection reset",
    )
    assert result.diagnostic in {"incomplete", "failed"}


def test_unsafe_capture_stays_incomplete() -> None:
    result = validate_visual_response(
        {"findings": [dict(_MODEL_FINDING)]}, safe_capture=False
    )
    assert result.diagnostic == "incomplete"


@pytest.mark.parametrize(
    "payload",
    [
        {"status": []},
        {"type": {}},
        {"findings": [{**_MODEL_FINDING, "evidence": "   "}]},
        {"findings": [{**_MODEL_FINDING, "element": "\t"}]},
        {
            "findings": [
                {
                    **_MODEL_FINDING,
                    "region": {"x": 0, "y": 0, "width": 10**1000, "height": 10},
                }
            ]
        },
    ],
)
def test_closed_response_edges_fail(payload: object) -> None:
    result = validate_visual_response(payload, safe_capture=True)
    assert result.diagnostic == "failed"
    assert list(result.findings) == []
