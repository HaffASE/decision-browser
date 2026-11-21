from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page, ViewportSize, async_playwright

from decision_browser.contracts import Viewport
from decision_browser.verification import GeometryReport, inspect_geometry

_CJK70 = "测" * 70
_EMOJI70 = "😀" * 70
_ORIGIN: ViewportSize = {"width": 1280, "height": 720}


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _document(attr: str, value: str, left: int) -> str:
    style = f"position:absolute;left:{left}px;top:0;width:80px;height:24px"
    if attr == "for":
        tag = f"<label for='{value}' style='{style}'>L</label>"
    else:
        tag = f"<button {attr}='{value}' style='{style}'>B</button>"
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head>"
        f"<body>{tag}</body></html>"
    )


def _responsive_count(report: GeometryReport) -> int:
    return sum(1 for finding in report.findings if finding.kind == "responsive")


def _assert_output_bounds(report: GeometryReport) -> None:
    assert len(report.findings) <= 128
    for finding in report.findings:
        assert len(finding.element.encode("utf-8")) <= 1024
        assert len(finding.evidence.encode("utf-8")) <= 1024


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True, chromium_sandbox=True, channel="chromium"
        )
        context = await browser.new_context(viewport=_ORIGIN)
        tab = await context.new_page()
        try:
            yield tab
        finally:
            await browser.close()


async def _inspect(page: Page) -> GeometryReport:
    report = await inspect_geometry(page, Viewport(width=390, height=720))
    assert report.diagnostic == "complete"
    assert page.viewport_size == _ORIGIN
    _assert_output_bounds(report)
    return report


@pytest.mark.parametrize("attr", ["id", "for", "aria-labelledby"])
@pytest.mark.parametrize("value", [_CJK70, _EMOJI70], ids=["cjk70", "emoji70"])
async def test_unicode_metadata_still_reports_responsive(
    page: Page, attr: str, value: str
) -> None:
    assert _utf16_units(value) <= 200
    assert len(value.encode("utf-8")) > 200
    await page.set_content(_document(attr, value, 500))
    report = await _inspect(page)
    assert _responsive_count(report) == 1


@pytest.mark.parametrize("attr", ["id", "for", "aria-labelledby"])
@pytest.mark.parametrize("value", [_CJK70, _EMOJI70], ids=["cjk70", "emoji70"])
async def test_clean_control_x10_same_metadata(
    page: Page, attr: str, value: str
) -> None:
    assert _utf16_units(value) <= 200
    assert len(value.encode("utf-8")) > 200
    await page.set_content(_document(attr, value, 10))
    report = await _inspect(page)
    assert report.findings == []


_FIELD = {"id": "htmlId", "for": "forId", "aria-labelledby": "labelledBy"}


def _no_lone_surrogate(value: str) -> None:
    assert all(not 0xD800 <= ord(ch) <= 0xDFFF for ch in value)
    assert len(value.encode("utf-8")) <= 200


async def _probe_metadata(page: Page, attr: str) -> str:
    from decision_browser.verification import _GEOMETRY_PROBE

    payload = await page.evaluate(_GEOMETRY_PROBE)
    assert len(payload["nodes"]) == 1
    meta = payload["nodes"][0][_FIELD[attr]]
    _no_lone_surrogate(meta)
    return meta


@pytest.mark.parametrize("attr", ["id", "for", "aria-labelledby"])
async def test_astral_on_utf16_boundary_is_not_sliced(page: Page, attr: str) -> None:
    value = "a" * 199 + "😀"
    assert _utf16_units(value) == 201
    await page.set_content(_document(attr, value, 500))
    meta = await _probe_metadata(page, attr)
    assert meta == "a" * 199
    report = await _inspect(page)
    assert _responsive_count(report) == 1


@pytest.mark.parametrize("attr", ["id", "for", "aria-labelledby"])
@pytest.mark.parametrize(
    ("value", "kept"),
    [("测" * 250, "测" * 66), ("😀" * 150, "😀" * 50)],
    ids=["cjk_over_200", "emoji_over_200"],
)
async def test_overlong_unicode_keeps_node(
    page: Page, attr: str, value: str, kept: str
) -> None:
    assert _utf16_units(value) > 200
    await page.set_content(_document(attr, value, 500))
    meta = await _probe_metadata(page, attr)
    assert meta == kept
    report = await _inspect(page)
    assert _responsive_count(report) == 1


@pytest.mark.parametrize("attr", ["id", "for", "aria-labelledby"])
async def test_lone_surrogate_run_stays_inside_transport_window(
    page: Page, attr: str
) -> None:
    await page.set_content(_document(attr, "x", 500))
    await page.evaluate(
        """(attr) => {
          const el = document.querySelector("button, label");
          const lone = String.fromCharCode(0xD800);
          el.setAttribute(attr, "a".repeat(50) + lone.repeat(5000) + "TAIL");
        }""",
        attr,
    )
    meta = await _probe_metadata(page, attr)
    assert meta == "a" * 50
    report = await _inspect(page)
    assert _responsive_count(report) == 1
