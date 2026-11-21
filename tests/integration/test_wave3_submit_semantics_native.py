"""F09 review regression: native button enum is the destination-admission oracle."""
import html

import pytest
from test_wave3_forms_native import drive, pick
from test_wave3_forms_native import page as owned_page

from decision_browser.dom import DOMSession

page = owned_page

KINDS = [
    (None, "submit"), ("", "submit"), ("invalid", "submit"),
    ("submit", "submit"), ("SUBMIT", "submit"), ("SuBmIt", "submit"),
    (" submit", "submit"), ("submit ", "submit"),
    ("button", "button"), ("BUTTON", "button"),
    (" button", "submit"), ("button ", "submit"),
    ("reset", "reset"), ("RESET", "reset"), (" reset ", "submit"),
]


def markup(raw, destination):
    attribute = "" if raw is None else ' type="' + html.escape(raw, quote=True) + '"'
    return f'<form action="{destination}" method="get"><input name="q" value="private-fixture"><button{attribute}>Go</button></form>'


@pytest.mark.parametrize("raw,effective", KINDS)
async def test_native_button_enum_cannot_submit_to_unadmitted_origin(page, raw, effective):
    source = markup(raw, "https://other.test/search")
    await page.set_content(source)
    await page.evaluate("() => document.querySelector('form').addEventListener('submit', e => {e.preventDefault(); window.submissions=(window.submissions||0)+1})")
    # Chromium's reflected enum plus an actual click/submit event are an
    # independent oracle, rather than duplicating the implementation's table.
    assert await page.locator("button").evaluate("el => el.type") == effective
    await page.locator("button").click()
    assert await page.evaluate("() => window.submissions || 0") == (1 if effective == "submit" else 0)
    result, driver, events = await drive(source, ["click", "DONE"])
    assert driver.hits == ["https://example.test/same"], result.evidence
    assert result.terminal == ("blocked" if effective == "submit" else "completed_unverified")
    expected_steps = 1 if effective == "submit" else 2
    assert result.usage.steps == expected_steps
    assert result.usage.calls == (1 if effective == "submit" else 2)
    assert sum(event.get("event") == "before_dispatch" for event in events) == expected_steps
    assert sum(event.get("event") == "outcome" and event.get("status") == "acknowledged" for event in events) == expected_steps


@pytest.mark.parametrize("raw,effective", [KINDS[index] for index in (0, 1, 2, 4, 7, 8, 12)])
async def test_button_submission_destination_and_post_check_share_native_enum(raw, effective):
    result, driver, _events = await drive(markup(raw, "/search"), ["click", "DONE"])
    assert result.terminal == "completed_unverified", result.evidence
    expected = ["https://example.test/same"]
    if effective == "submit":
        expected.append("https://example.test/search?q=private-fixture")
    assert driver.hits == expected
    assert result.usage.steps == 2 and result.usage.calls == 2


@pytest.mark.parametrize("operation,before,after", [
    ("click", None, ""), ("click", "SUBMIT", "submit"),
    ("enter", None, ""), ("enter", "SUBMIT", "submit"),
])
async def test_raw_type_changes_stale_even_with_identical_effective_submit_enum(page, operation, before, after):
    source = markup(before, "https://example.test/search").replace('<input name="q"', '<input aria-label="Query" name="q"')
    await page.set_content(source)
    await page.evaluate("() => document.querySelector('form').addEventListener('submit', e => {e.preventDefault(); window.submissions=(window.submissions||0)+1})")
    dom = DOMSession(page)
    try:
        obs = await dom.observe()
        token = pick(obs, operation, "Go" if operation == "click" else "Query")
        action = await dom.prepare(operation, token)
        await page.locator("button").evaluate("(el, value) => el.setAttribute('type', value)", after)
        attempts = []
        result = await dom.dispatch(action, lambda _action: attempts.append(1))
        assert result.status == "stale" and attempts == []
        assert await page.evaluate("() => window.submissions || 0") == 0
    finally:
        await dom.close()
