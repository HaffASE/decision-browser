"""Functional verification against an owned Chromium page."""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import Page, async_playwright

from decision_browser.contracts import Check
from decision_browser.verification import FunctionalReport, verify_functional

_FORM = """
<form>
  <label for="field">Query</label>
  <input id="field" name="field" type="text" value="alpha">
  <p id="note">Atlas note</p>
  <button id="go" type="button">Go</button>
</form>
"""

_STATE: Mapping[str, str] = {"phase": "open"}

_SEARCH_FORM = """<!DOCTYPE html>
<html><body>
<p id="banner">DONE</p>
<form method="POST" action="/result">
<label for="query">Query</label>
<input id="query" name="query" type="text">
<button type="submit">Search</button>
</form>
</body></html>
"""

_ATLAS_RESULT = """<!DOCTYPE html>
<html><body>
<p id="banner">DONE</p>
<input id="query" name="query" value="atlas">
<p id="result">Found atlas</p>
</body></html>
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


def _ok_checks() -> list[Check]:
    return [
        Check(kind="value", target="#field", expected="alpha"),
        Check(kind="text", target="#note", expected="Atlas note"),
        Check(kind="route", target="", expected="about:blank"),
        Check(kind="visible", target="#go", expected=True),
        Check(kind="count", target="button", expected=1),
        Check(kind="fixture_state", target="phase", expected="open"),
    ]


async def test_positive_value_text_route_visible_count_fixture_state(
    page: Page,
) -> None:
    await page.set_content(_FORM)
    assert page.url == "about:blank"
    checks = _ok_checks()
    report: FunctionalReport = await verify_functional(
        page, checks, fixture_state=_STATE
    )
    assert report.terminal == "verified"
    assert report.functional == "verified"
    assert len(report.evidence) == len(checks)
    assert all(item.passed is True for item in report.evidence)


async def test_route_match_is_exact(page: Page) -> None:
    await page.set_content(_FORM)
    report: FunctionalReport = await verify_functional(
        page,
        [Check(kind="route", target="", expected="about:blank/")],
        fixture_state=_STATE,
    )
    assert report.terminal == "verification_failed"
    assert report.functional == "failed"


async def test_changed_field_fails_on_its_own(page: Page) -> None:
    await page.set_content(_FORM)
    await page.fill("#field", "beta")
    report: FunctionalReport = await verify_functional(
        page,
        [Check(kind="value", target="#field", expected="alpha")],
        fixture_state=_STATE,
    )
    assert report.terminal == "verification_failed"
    assert report.functional == "failed"


async def test_changed_text_fails_on_its_own(page: Page) -> None:
    await page.set_content(_FORM)
    await page.locator("#note").evaluate("node => { node.textContent = 'Other note'; }")
    report: FunctionalReport = await verify_functional(
        page,
        [Check(kind="text", target="#note", expected="Atlas note")],
        fixture_state=_STATE,
    )
    assert report.terminal == "verification_failed"
    assert report.functional == "failed"


async def test_done_marker_does_not_excuse_unmet_checks(page: Page) -> None:
    await page.set_content(
        "<body><p id='banner'>DONE</p><input id='field' value='nope'></body>"
    )
    report: FunctionalReport = await verify_functional(
        page,
        [
            Check(kind="text", target="#banner", expected="DONE"),
            Check(kind="value", target="#field", expected="alpha"),
        ],
        fixture_state=_STATE,
    )
    assert report.terminal == "verification_failed"
    assert report.functional == "failed"


async def test_empty_checks_are_unverified(page: Page) -> None:
    await page.set_content(_FORM)
    report: FunctionalReport = await verify_functional(page, [], fixture_state=_STATE)
    assert report.terminal == "completed_unverified"
    assert report.functional == "unverified"


@pytest.mark.parametrize(
    "check",
    [
        Check.model_construct(kind="count", target="button", expected="1"),
        Check.model_construct(kind="visible", target="#go", expected="true"),
        Check.model_construct(kind="value", target="#field", expected=1),
    ],
)
async def test_wrong_expected_type_fails(page: Page, check: Check) -> None:
    await page.set_content(_FORM)
    report: FunctionalReport = await verify_functional(
        page, [check], fixture_state=_STATE
    )
    assert report.terminal == "verification_failed"
    assert report.functional == "failed"


async def test_invalid_selector_fails(page: Page) -> None:
    await page.set_content(_FORM)
    report: FunctionalReport = await verify_functional(
        page,
        [Check(kind="visible", target="button[", expected=True)],
        fixture_state=_STATE,
    )
    assert report.terminal == "verification_failed"
    assert report.functional == "failed"


@pytest.mark.parametrize("target", ["xpath=//button", "#go >> visible=true"])
async def test_engine_selectors_rejected(page: Page, target: str) -> None:
    await page.set_content(_FORM)
    report: FunctionalReport = await verify_functional(
        page,
        [Check.model_construct(kind="visible", target=target, expected=True)],
        fixture_state=_STATE,
    )
    assert report.terminal == "verification_failed"
    assert report.functional == "failed"


async def test_oversize_check_list_fails(page: Page) -> None:
    await page.set_content(_FORM)
    checks = [Check(kind="count", target="button", expected=1) for _ in range(10_000)]
    report: FunctionalReport = await verify_functional(
        page, checks, fixture_state=_STATE
    )
    assert report.terminal == "verification_failed"
    assert report.functional == "failed"


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("t" * 513, "Atlas note"),
        ("#note", "界" * 2000),
    ],
    ids=["target_bytes", "expected_utf8_bytes"],
)
async def test_oversized_target_or_expected_fails(
    page: Page, target: str, expected: str
) -> None:
    await page.set_content(_FORM)
    report: FunctionalReport = await verify_functional(
        page,
        [Check.model_construct(kind="text", target=target, expected=expected)],
        fixture_state=_STATE,
    )
    assert report.terminal == "verification_failed"
    assert report.functional == "failed"


async def test_owned_state_mismatch_fails(page: Page) -> None:
    await page.set_content(_FORM)
    report: FunctionalReport = await verify_functional(
        page,
        [Check(kind="fixture_state", target="phase", expected="closed")],
        fixture_state=_STATE,
    )
    assert report.terminal == "verification_failed"
    assert report.functional == "failed"


@pytest.mark.parametrize("state", [None, {}])
async def test_missing_fixture_state_fails(
    page: Page, state: Mapping[str, str] | None
) -> None:
    await page.set_content(_FORM)
    report: FunctionalReport = await verify_functional(
        page,
        [Check(kind="fixture_state", target="phase", expected="open")],
        fixture_state=state,
    )
    assert report.terminal == "verification_failed"
    assert report.functional == "failed"


_SCALARS: Mapping[str, bool | int | str] = {"ready": True, "count": 2, "phase": "open"}


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("ready", True),
        ("count", 2),
        ("phase", "open"),
    ],
    ids=["bool", "int", "string"],
)
async def test_fixture_state_scalars_match(
    page: Page, target: str, expected: bool | int | str
) -> None:
    await page.set_content(_FORM)
    report: FunctionalReport = await verify_functional(
        page,
        [Check(kind="fixture_state", target=target, expected=expected)],
        fixture_state=_SCALARS,
    )
    assert report.terminal == "verified"
    assert report.functional == "verified"


async def test_fixture_state_bool_true_is_not_int_one(page: Page) -> None:
    await page.set_content(_FORM)
    report: FunctionalReport = await verify_functional(
        page,
        [Check(kind="fixture_state", target="ready", expected=1)],
        fixture_state={"ready": True},
    )
    assert report.terminal == "verification_failed"
    assert report.functional == "failed"


def _search_handler(
    state: dict[str, str | bool | int],
) -> type[BaseHTTPRequestHandler]:
    class SearchHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def _write(self, status: int, html: str) -> None:
            data = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if urlsplit(self.path).path != "/":
                self.send_error(404)
                return
            self._write(200, _SEARCH_FORM)

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/result":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400)
                return
            if length < 0 or length > 1024:
                self.send_error(413)
                return
            fields = parse_qs(self.rfile.read(length).decode("ascii", "replace"))
            if fields.get("query") != ["atlas"]:
                self.send_error(400)
                return
            state["submitted"] = True
            state["query"] = "atlas"
            self._write(200, _ATLAS_RESULT)

    return SearchHandler


@pytest.fixture
def form_search_server() -> Iterator[tuple[str, dict[str, str | bool | int]]]:
    state: dict[str, str | bool | int] = {"submitted": False, "query": ""}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _search_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _host, port = server.server_address[:2]
    try:
        yield f"http://127.0.0.1:{port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_form_search_verifies_only_after_real_submit(
    page: Page,
    form_search_server: tuple[str, dict[str, str | bool | int]],
) -> None:
    base, state = form_search_server
    result_url = f"{base}/result"
    await page.goto(f"{base}/")
    early_checks = (
        Check(kind="route", target="", expected=result_url),
        Check(kind="text", target="#result", expected="Found atlas"),
        Check(kind="fixture_state", target="submitted", expected=True),
    )
    for check in early_checks:
        early: FunctionalReport = await verify_functional(
            page,
            [Check(kind="text", target="#banner", expected="DONE"), check],
            fixture_state=state,
        )
        assert early.terminal == "verification_failed"
        assert early.functional == "failed"
    assert state["submitted"] is False
    await page.fill("#query", "atlas")
    await page.click("button[type='submit']")
    await page.wait_for_url(result_url)
    checks = [
        Check(kind="route", target="", expected=result_url),
        Check(kind="value", target="#query", expected="atlas"),
        Check(kind="text", target="#result", expected="Found atlas"),
        Check(kind="fixture_state", target="submitted", expected=True),
        Check(kind="fixture_state", target="query", expected="atlas"),
    ]
    report: FunctionalReport = await verify_functional(
        page, checks, fixture_state=state
    )
    assert report.terminal == "verified"
    assert report.functional == "verified"
    assert len(report.evidence) == len(checks)
    assert all(item.passed is True for item in report.evidence)
    assert state["submitted"] is True
    assert state["query"] == "atlas"
