"""The oracle collector on a live-shaped results page (observed 2026-10-11), in sandboxed Chromium.

Every request is fulfilled locally; no provider, ledger or network surface.
"""
from copy import deepcopy

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from decision_browser.flights_evidence import (
    FLIGHTS_EVIDENCE_SCRIPT,
    evidence_digest,
    validate_flights_evidence,
)

URL = 'https://www.google.com/travel/flights/search?tfs=opaque&hl=en'
ROW = ('From 14330 Russian rubles. 1 stop flight with JetBlue. Leaves John F. Kennedy International Airport '
       'at 6:00 AM on Tuesday, November 10 and arrives at Los Angeles International Airport at 12:32 PM on '
       'Tuesday, November 10. Total duration 9 hr 32 min.  Layover (1 of 1) is a 42 min layover at '
       'Fort Lauderdale-Hollywood International Airport in Fort Lauderdale. 1 carry-on bag included. '
       '0 checked bags included.  Select flight')
HTML = '''<!doctype html><style>body{margin:0}.row{position:relative;height:74px;margin:4px}
.overlay{position:absolute;inset:0;pointer-events:none}</style>
<div role="progressbar" id="bar" style="opacity:0;height:4px"></div>
<div id="idle" style="opacity:0;height:4px">Loading results</div>
<input aria-label="Where from? New York JFK" aria-expanded="false" value="New York">
<input aria-label="Where to? Los Angeles LAX" value="Los Angeles">
<input aria-label="Departure" value="Tue, Nov 10">
<div role="combobox">One way</div><div role="combobox">1 passenger 1</div>
<div role="combobox">Economy (include Basic)</div>
<h2>Search results</h2>
<div role="switch" aria-label="Track prices from New York to Los Angeles departing 2026-11-10">Track prices</div>
<div>Prices include required taxes + fees for 1 adult.</div>
<ul role="list" id="list"><li><div class="row" id="row"><div>JetBlue 6:00 AM – 12:32 PM</div>
 <div role="link" class="overlay" id="link" aria-label="__ROW__"></div></div></li></ul>'''.replace('__ROW__', ROW)
ATTACKS = {
    'visible_loading': "idle.style.opacity='1'",
    'split_loading': "document.body.insertAdjacentHTML('beforeend','<span>Loading <b>results</b></span>')",
    'aria_hidden_loading': ("document.body.insertAdjacentHTML('beforeend',"
                            "'<span aria-hidden=\"true\">Searching for flights</span>')"),
    'visible_progress': "bar.style.opacity='1'",
}
ROW_ATTACKS = {
    # A non-row layer over the row: the hit lands outside the overlay's parent.
    'covered': ("document.body.insertAdjacentHTML('beforeend','<div style=\"position:fixed;inset:0;"
                "background:white\">ad</div>')"),
    # Two result links share one parent, so painted content there is not this row's.
    'shared_parent': "row.append(link.cloneNode())",
    # A hit-testable overlay must be hit itself; its content sibling is not enough.
    'pointer_auto': "link.style.pointerEvents='auto';row.firstElementChild.style.cssText='position:absolute;inset:0;z-index:1;background:white'",
    'hidden_row': "row.style.opacity='0'",
}


@pytest_asyncio.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, chromium_sandbox=True, channel="chromium")
        try:
            context = await browser.new_context()
            await context.route('**/*', lambda route: route.fulfill(
                status=200, content_type='text/html', body=HTML))
            page = await context.new_page()
            await page.goto(URL)
            yield page
        finally:
            await browser.close()


def verdict(after):
    before = deepcopy(after)
    before.update(captured_at_ms=after['captured_at_ms'] - 2000, url='https://www.google.com/travel/flights?hl=en')
    before['results'] = {'headings': [], 'query_summaries': [], 'adult_summaries': [],
                         'displayed_row_count': 0, 'rows': []}
    earlier = deepcopy(after)
    earlier['captured_at_ms'] = after['captured_at_ms'] - 500
    action = {'attempt_id': 'search', 'operation': 'click', 'name': 'Search', 'outcome': 'acknowledged',
              'pre_search_digest': evidence_digest(before),
              'dispatched_at_ms': before['captured_at_ms'] + 100, 'acknowledged_at_ms': before['captured_at_ms'] + 200}
    selection = {'attempt_id': 'done', 'operation': 'click', 'outcome': 'acknowledged',
                 'name': 'Done. Search for one-way flights, departing on November 10, 2026',
                 'acknowledged_at_ms': before['captured_at_ms'] - 100}
    return validate_flights_evidence(before, after, action, previous_post_search=earlier, date_selection=selection)


async def test_live_shaped_results_pass_with_hidden_idle_loading_bar(page):
    snapshot = await page.evaluate(FLIGHTS_EVIDENCE_SCRIPT)
    assert snapshot['hazards'] == {'loading': False, 'captcha': False, 'no_flights': False, 'dialog': False}
    assert snapshot['results']['rows'] == [{'aria_label': ' '.join(ROW.split()), 'text': '', 'visible': True}]
    report = verdict(snapshot)
    assert report['status'] == 'PASS', report['reasons']
    assert (report['row']['price'], report['row']['currency']) == ('14330', 'RUB')
    assert report['row']['text_corroboration'] == 'empty'


@pytest.mark.parametrize('attack', ATTACKS)
async def test_painted_loading_text_or_progress_still_blocks(page, attack):
    await page.evaluate('() => {' + ATTACKS[attack] + '}')
    snapshot = await page.evaluate(FLIGHTS_EVIDENCE_SCRIPT)
    assert snapshot['hazards']['loading'] is True
    assert 'loading' in verdict(snapshot)['reasons']


@pytest.mark.parametrize('attack', ROW_ATTACKS)
async def test_row_overlay_relaxation_is_closed(page, attack):
    await page.evaluate('() => {' + ROW_ATTACKS[attack] + '}')
    snapshot = await page.evaluate(FLIGHTS_EVIDENCE_SCRIPT)
    assert snapshot['results']['rows'] == []
    assert verdict(snapshot)['status'] == 'NOT_VERIFIED'
