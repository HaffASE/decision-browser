"""Read-only, English Google Flights acceptance evidence, independent of DONE.

Evaluate ``FLIGHTS_EVIDENCE_SCRIPT`` in DOMSession's isolated world. Capture a
pre-search snapshot immediately before dispatch and bind its ``evidence_digest``
to the *host's* acknowledged Search attempt. Supply two subsequent snapshots at
least 250 ms apart. The validator does not execute, navigate, book, call a model,
or establish that a provider was live. Its inputs must come from the host's trace,
never from model prose. A URL or correctly filled form alone is not evidence.

The optional ``date_selection`` is the latest acknowledged calendar Done action
before Search, carrying its original accessible name with the selected year.
Only a Search starting without preexisting results is accepted. This bounded
oracle does not establish freshness for repeated queries on a results page.
Unknown/localized DOM, partial captures, or ambiguous evidence fail closed.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from typing import Any, Literal, TypeGuard
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .flights_query import AIRPORTS, FIXED_QUERY, FlightsQuery

MIN_SETTLE_MS = 250
MAX_PRE_SEARCH_AGE_MS = 5000
MAX_EVIDENCE_BYTES = 65536

# Fixed, read-only script. No page globals, events, clicks, field writes, network,
# or hidden/password/file values. Limits apply after visibility filtering.
FLIGHTS_EVIDENCE_SCRIPT = r"""() => {
  let truncated = false;
  const text = (s, cap = 512) => {
    const value = String(s || '').replace(/\s+/g, ' ').trim();
    if (value.length > cap) truncated = true;
    return value.slice(0, cap);
  };
  // Painted ignores ARIA: hazards count anything a person could see.
  const painted = el => {
    if (!el || !el.isConnected) return false;
    for (let p = el; p; p = p.parentElement) {
      const s = getComputedStyle(p);
      if (s.display === 'none' || s.visibility === 'hidden' || s.visibility === 'collapse' || Number(s.opacity) === 0) return false;
    }
    const r = el.getBoundingClientRect();
    return !!el.getClientRects().length && r.width > 0 && r.height > 0;
  };
  // A hit test may land in ``box`` only for a pointer-events:none row overlay
  // whose own parent holds no other result link (see rowBox below).
  const shown = (el, viewport = false, box = null) => {
    if (!el || !el.isConnected || el.closest('[hidden],[aria-hidden="true"],template')) return false;
    if (!painted(el)) return false;
    const r = el.getBoundingClientRect();
    if (viewport && (r.bottom <= 0 || r.right <= 0 || r.top >= innerHeight || r.left >= innerWidth)) return false;
    if (viewport) {
      const left = Math.max(0, r.left), right = Math.min(innerWidth, r.right);
      const top = Math.max(0, r.top), bottom = Math.min(innerHeight, r.bottom);
      const points = [[(left + right) / 2, (top + bottom) / 2],
        [left + (right - left) / 4, top + (bottom - top) / 4],
        [right - (right - left) / 4, bottom - (bottom - top) / 4]];
      if (!points.some(([x, y]) => {
        const hit = document.elementFromPoint(x, y);
        return hit === el || (hit && el.contains(hit)) || (box && hit && box.contains(hit));
      })) return false;
    }
    return true;
  };
  const take = (items, cap) => {
    if (items.length > cap) truncated = true;
    return items.slice(0, cap);
  };
  const inputs = Array.from(document.querySelectorAll('input'))
    .filter(el => !['password', 'file', 'hidden'].includes(el.type) && shown(el));
  const fields = pattern => take(inputs.filter(el => pattern.test(el.getAttribute('aria-label') || '')), 4)
    .map(el => ({label: text(el.getAttribute('aria-label')), value: text(el.value),
      expanded: el.getAttribute('aria-expanded')}));
  const controls = take(Array.from(document.querySelectorAll('[role="combobox"],button[aria-label]'))
    .filter(el => shown(el) && (el.getAttribute('role') === 'combobox' || /passenger|adult/i.test(el.getAttribute('aria-label') || ''))), 24)
    .map(el => text([el.getAttribute('aria-label'), el.innerText].filter(Boolean).join(' ')));
  const headings = take(Array.from(document.querySelectorAll('h1,h2,h3,[role="heading"]'))
    .filter(el => shown(el) && /^Search results$/i.test((el.innerText || '').trim())), 4)
    .map(el => text(el.innerText));
  const querySummaries = take(Array.from(document.querySelectorAll('[role="switch"][aria-label]'))
    .filter(el => shown(el) && /^Track prices from /i.test(el.getAttribute('aria-label') || '')), 8)
    .map(el => text(el.getAttribute('aria-label')));
  // Count display-visible result overlays independently of the viewport. An old
  // below-fold result scrolled into view is not evidence of a fresh Search.
  const resultLinks = Array.from(document.querySelectorAll('[role="link"][aria-label]'))
    .filter(el => /^From\s/i.test(el.getAttribute('aria-label') || '') && /Select flight\s*$/i.test(el.getAttribute('aria-label') || ''));
  const displayedRows = take(resultLinks.filter(el => shown(el)), 512);
  // Google's row link (observed 2026-10-11) is an empty pointer-events:none
  // overlay, so hit tests land in the row's painted content beside it.
  const rowBox = el => {
    const parent = el.parentElement;
    return parent && getComputedStyle(el).pointerEvents === 'none'
      && resultLinks.filter(other => parent.contains(other)).length === 1 ? parent : null;
  };
  const rows = take(displayedRows.filter(el => shown(el, true, rowBox(el))), 8)
    .map(el => ({aria_label: text(el.getAttribute('aria-label'), 2048), text: text(el.innerText, 2048), visible: true}));
  // Body text is inspected for status/quantity only, never returned or used as a
  // substitute for the result-associated switch and a complete individual row.
  const body = document.body ? document.body.innerText : '';
  const adultSummaries = take(Array.from(new Set(body.match(/Prices include required taxes\s*\+\s*fees for\s+\d+\s+adults?\./gi) || [])), 4)
    .map(value => text(value));
  const visible = selector => Array.from(document.querySelectorAll(selector)).some(el => shown(el));
  // innerText keeps opacity:0 text (Google's idle "Loading results" bar), so a
  // phrase counts only if a smallest element containing it is painted.
  const paintedText = pattern => pattern.test(body) && Array.from(document.querySelectorAll('body *'))
    .some(el => pattern.test(el.textContent || '')
      && !Array.from(el.children).some(child => pattern.test(child.textContent || '')) && painted(el));
  return {
    schema_version: 1, captured_at_ms: Date.now(), url: text(location.href, 4096),
    form: {origin: fields(/^Where from\?/i), destination: fields(/^Where to\?/i),
      departure: fields(/^Departure(?: date)?$/i), controls},
    results: {headings, query_summaries: querySummaries, adult_summaries: adultSummaries,
      displayed_row_count: displayedRows.length, rows},
    hazards: {
      loading: document.readyState !== 'complete' || visible('[aria-busy="true"],[role="progressbar"]') || paintedText(/loading (?:flights|results)|searching for flights/i),
      captcha: visible('iframe[src*="recaptcha"],iframe[src*="captcha"],#captcha') || /unusual traffic|verify (?:that )?you(?: are|.re) (?:a )?human|not a robot/i.test(body),
      no_flights: /no (?:matching )?flights (?:found|available|match)|couldn.t find any flights/i.test(body),
      dialog: visible('[role="dialog"][aria-modal="true"],dialog[open]')
    },
    truncated
  };
}"""


_DEFAULT_QUERY = FIXED_QUERY

class _EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Input(_EvidenceModel):
    label: str = Field(max_length=512)
    value: str = Field(max_length=512)
    expanded: Literal["true", "false"] | None


class _Form(_EvidenceModel):
    origin: list[_Input] = Field(max_length=4)
    destination: list[_Input] = Field(max_length=4)
    departure: list[_Input] = Field(max_length=4)
    controls: list[str] = Field(max_length=24)


class _Row(_EvidenceModel):
    aria_label: str = Field(max_length=2048)
    text: str = Field(max_length=2048)
    visible: bool


class _Results(_EvidenceModel):
    headings: list[str] = Field(max_length=4)
    query_summaries: list[str] = Field(max_length=8)
    adult_summaries: list[str] = Field(max_length=4)
    displayed_row_count: int = Field(ge=0, le=512)
    rows: list[_Row] = Field(max_length=8)


class _Hazards(_EvidenceModel):
    loading: bool
    captcha: bool
    no_flights: bool
    dialog: bool


class _Snapshot(_EvidenceModel):
    schema_version: Literal[1]
    captured_at_ms: int | float
    url: str = Field(max_length=4096)
    form: _Form
    results: _Results
    hazards: _Hazards
    truncated: bool


_AIRPORTS = AIRPORTS
_CURRENCIES = {
    "US dollars": "USD", "British pounds": "GBP", "euros": "EUR",
    "Canadian dollars": "CAD", "Australian dollars": "AUD",
    "Japanese yen": "JPY", "Indian rupees": "INR", "Swiss francs": "CHF",
    "Russian rubles": "RUB",
}
_ROW = re.compile(
    r"^From (?P<price>\d+(?:,\d{3})*(?:\.\d{1,2})?) (?P<currency>[^.]+)\. "
    r"(?:Nonstop|\d+ stops?) flight with (?P<carrier>.+?)\. "
    r"Leaves (?P<origin>.+?) at (?P<departure_time>\d{1,2}:\d{2} [AP]M) "
    r"on (?P<departure_date>.+?) and arrives at (?P<destination>.+?) at "
    r"(?P<arrival_time>\d{1,2}:\d{2} [AP]M) on (?P<arrival_date>[^.]+)\. "
    r".+\bSelect flight\s*$", re.IGNORECASE,
)


def evidence_digest(value: object) -> str:
    """Canonical JSON SHA-256 for a host-captured pre-dispatch snapshot.

    Invalid/nonfinite/oversized evidence raises ValueError before dispatch.
    The digest is binding evidence, not authentication of its origin.
    """
    try:
        raw = json.dumps(value, ensure_ascii=True, allow_nan=False,
                         sort_keys=True, separators=(",", ":")).encode("ascii")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid_flights_evidence") from exc
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise ValueError("oversized_flights_evidence")
    return hashlib.sha256(raw).hexdigest()


def _snapshot(value: object) -> _Snapshot | None:
    try:
        evidence_digest(value)
        result = _Snapshot.model_validate(value)
    except (ValueError, ValidationError, TypeError):
        return None
    if not _time(result.captured_at_ms):
        return None
    if result.results.displayed_row_count < len(result.results.rows):
        return None
    for texts in (result.form.controls, result.results.headings,
                  result.results.query_summaries, result.results.adult_summaries):
        if any(len(item) > 512 for item in texts):
            return None
    return result


def _time(value: object) -> TypeGuard[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


def _normal(value: str) -> str:
    return " ".join(value.split()).casefold()


def _google_flights(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in {"www.google.com", "google.com"}
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and (parsed.path == "/travel/flights" or parsed.path.startswith("/travel/flights/")))
    except ValueError:
        return False


def _airport(inputs: list[_Input], prefix: str, code: str) -> bool:
    if len(inputs) != 1:
        return False
    item = inputs[0]
    city, full_name = _AIRPORTS[code]
    # A typed JFK or city value is deliberately insufficient. The committed
    # control's accessible label must carry the airport identity while collapsed.
    return (item.expanded != "true"
            and _normal(item.label) in {_normal(f"{prefix} {city} {code}"),
                                       _normal(f"{prefix} {full_name} {code}")}
            and _normal(item.value) in {_normal(city), _normal(full_name), code.casefold()})


def _date_text(value: str, expected: date, *, require_year: bool) -> bool:
    value = _normal(value).replace(",", "")
    months = ("january", "february", "march", "april", "may", "june", "july", "august",
              "september", "october", "november", "december")
    alternatives = "|".join(dict.fromkeys([*months, *(month[:3] for month in months)]))
    matches = re.findall(rf"\b({alternatives})\s+(\d{{1,2}})\b", value)
    if not matches:
        return value == expected.isoformat()
    if len(matches) != 1:
        return False
    month, day = matches[0]
    if month[:3] != months[expected.month - 1][:3] or int(day) != expected.day:
        return False
    weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    weekday_words = "|".join([*weekdays, *(day[:3] for day in weekdays)])
    if any(day[:3] != weekdays[expected.weekday()][:3]
           for day in re.findall(rf"\b({weekday_words})\b", value)):
        return False
    years = re.findall(r"\b\d{4}\b", value)
    return (years == [str(expected.year)] if require_year
            else not years or years == [str(expected.year)])


def _form_reasons(form: _Form, expected: FlightsQuery, selection: object,
                  captured_at: float) -> list[str]:
    reasons = []
    if not _airport(form.origin, "Where from?", expected.origin):
        reasons.append("origin_not_selected")
    if not _airport(form.destination, "Where to?", expected.destination):
        reasons.append("destination_not_selected")
    controls = [_normal(value) for value in form.controls]
    if "one way" not in controls:
        reasons.append("trip_type_mismatch")
    if not any(re.fullmatch(r"economy(?:\s*\(include(?:s)? basic\))?", value)
               for value in controls):
        reasons.append("cabin_mismatch")
    if not any(re.fullmatch(r"1 (?:passenger|adult)(?: 1)?", value)
               or value == "1 passenger, change number of passengers. 1"
               for value in controls):
        reasons.append("passenger_count_mismatch")
    expected_date = date.fromisoformat(expected.departure_date)
    departure = form.departure[0].value if len(form.departure) == 1 else ""
    if not _date_text(departure, expected_date, require_year=False):
        reasons.append("departure_date_mismatch")
    elif (not _date_text(departure, expected_date, require_year=True)
          and not (isinstance(selection, dict)
                and isinstance(selection.get("attempt_id"), str) and selection["attempt_id"]
                and selection.get("operation") == "click"
                and selection.get("outcome") == "acknowledged"
                and _time(selection.get("acknowledged_at_ms"))
                and math.floor(selection["acknowledged_at_ms"]) <= math.floor(captured_at)
                and isinstance(selection.get("name"), str)
                and re.match(r"^Done\b", selection["name"], re.IGNORECASE)
                and _date_text(selection["name"], expected_date, require_year=True))):
        reasons.append("departure_year_unproven")
    return reasons


def _arrival_date(value: str, departure: date) -> bool:
    # Only the accepted English calendar grammar is supported. In particular a
    # plausible-looking but impossible date or contradictory weekday is denied.
    matched = re.fullmatch(r"(?:(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),? )?"
                           r"([A-Za-z]+) (\d{1,2})(?:,? (\d{4}))?", value, re.IGNORECASE)
    if matched is None:
        return False
    weekday, month, day, year = matched.groups()
    months = ("january", "february", "march", "april", "may", "june", "july", "august",
              "september", "october", "november", "december")
    try:
        month_number = next(i for i, name in enumerate(months, 1)
                            if month.casefold() in {name, name[:3]})
        arrival = date(int(year) if year else departure.year, month_number, int(day))
    except (ValueError, StopIteration):
        return False
    return arrival >= departure and (weekday is None or weekday.casefold() == arrival.strftime("%A").casefold())


def _aria_row(label: str, expected: FlightsQuery) -> dict[str, str] | None:
    matched = _ROW.fullmatch(" ".join(label.split()))
    if matched is None:
        return None
    fields = matched.groupdict()
    currencies = {_normal(key): value for key, value in _CURRENCIES.items()}
    currency = currencies.get(_normal(fields["currency"]))
    if currency is None or not re.search(r"[1-9]", fields["price"]):
        return None
    for field in ("departure_time", "arrival_time"):
        if not re.fullmatch(r"(?:0?[1-9]|1[0-2]):[0-5]\d [AP]M", fields[field], re.IGNORECASE):
            return None
    for field, code in (("origin", expected.origin), ("destination", expected.destination)):
        if _normal(fields[field]) not in {_normal(_AIRPORTS[code][1]), code.casefold()}:
            return None
    if not _date_text(fields["departure_date"], date.fromisoformat(expected.departure_date),
                      require_year=False):
        return None
    if not _arrival_date(fields["arrival_date"], date.fromisoformat(expected.departure_date)):
        return None
    if not fields["carrier"].strip():
        return None
    return {**fields, "origin": expected.origin, "destination": expected.destination,
            "departure_date": expected.departure_date,
            "price": fields["price"].replace(",", ""), "currency": currency}


def _consistent_row_text(row: _Row, fields: dict[str, str]) -> bool:
    """Unknown nonempty formats cannot silently override an accessible label.

    The observed Google result is an empty-text accessible link overlay. Empty
    text is permitted, but is *not* proof that the price was visually rendered.
    Nonempty text uses a deliberately closed set of same-row evidence formats.
    """
    text = _normal(row.text)
    if not text or text == _normal(row.aria_label):
        return True
    carrier = _normal(fields["carrier"])
    if text == carrier:
        return True
    price, currency = fields["price"], fields["currency"]
    symbols = {"GBP": ("£",), "USD": ("$", "US$"), "EUR": ("€",),
               "CAD": ("C$", "CA$"), "AUD": ("A$", "AU$"),
               "JPY": ("JP¥",), "INR": ("₹",), "CHF": (), "RUB": ()}
    whole, dot, fraction = price.partition(".")
    grouped = f"{int(whole):,}" + dot + fraction
    prices = [f"{prefix}{amount}" for amount in {price, grouped} for prefix in symbols[currency]]
    prices.extend(value for amount in {price, grouped}
                  for value in (f"{currency} {amount}", f"{amount} {currency}"))
    price_pattern = "(?:" + "|".join(re.escape(_normal(value)) for value in prices) + ")"
    times = (re.escape(_normal(fields["departure_time"])) + r"\s*(?:-|–|—|to)\s*"
             + re.escape(_normal(fields["arrival_time"])))
    return re.fullmatch(re.escape(carrier) + r"(?:\s+" + times + r")?\s+" + price_pattern,
                        text) is not None


def _row(row: _Row, expected: FlightsQuery) -> dict[str, str] | None:
    if not row.visible:
        return None
    fields = _aria_row(row.aria_label, expected)
    if fields is None or not _consistent_row_text(row, fields):
        return None
    return {**fields, "evidence_source": "visible_row_accessible_label",
            "text_corroboration": "consistent" if row.text.strip() else "empty"}


def _rows_digest(snapshot: _Snapshot) -> str:
    # Visibility, arbitrary decorative text, order, and duplicate nodes cannot
    # make a previous priced itinerary into fresh results. Parse the label only:
    # contradictory text must not affect the before/after freshness comparison.
    signatures = set()
    for row in snapshot.results.rows:
        fields = _aria_row(row.aria_label, _DEFAULT_QUERY)
        if fields is None:
            signature: dict[str, str] = {"unparsed_label": _normal(row.aria_label)}
        else:
            signature = {key: _normal(value) for key, value in fields.items()}
            signature["price"] = format(Decimal(fields["price"]).normalize(), "f")
            for key in ("departure_time", "arrival_time"):
                clock, period = fields[key].upper().split()
                hour, minute = map(int, clock.split(":"))
                signature[key] = f"{hour % 12 + (12 if period == 'PM' else 0):02d}:{minute:02d}"
        signatures.add(evidence_digest(signature))
    return evidence_digest(sorted(signatures))


def _settled_digest(snapshot: _Snapshot) -> str:
    return evidence_digest({"form": snapshot.form.model_dump(),
                            "results": snapshot.results.model_dump(),
                            "hazards": snapshot.hazards.model_dump(),
                            "truncated": snapshot.truncated})


def validate_flights_evidence(
    pre_search: object,
    post_search: object,
    search_action: object,
    *,
    previous_post_search: object,
    date_selection: object = None,
    expected: FlightsQuery = _DEFAULT_QUERY,
) -> dict[str, Any]:
    """Return bounded raw + normalized evidence; ambiguity is NOT_VERIFIED.

    ``search_action`` fields: attempt_id, operation (click/submit), original name
    (Search/Search for flights), outcome (acknowledged), pre_search_digest,
    dispatched_at_ms and acknowledged_at_ms. Times are host epoch milliseconds.
    ``date_selection`` fields: attempt_id, operation (click), name (original Done
    label with year), outcome (acknowledged), acknowledged_at_ms. Callers must
    supply the latest relevant trace entries; this function cannot attest them.
    """
    report: dict[str, Any] = {
        "status": "NOT_VERIFIED", "reasons": [], "query": asdict(expected),
        "row": None, "hashes": {}, "raw": {},
    }
    reasons: list[str] = report["reasons"]
    # This is deliberately a bounded acceptance oracle, not a universal travel
    # parser. Expanding the fixture requires researched DOM and negative tests.
    if expected != _DEFAULT_QUERY:
        reasons.append("unsupported_query")
        return report
    before, earlier, after = (_snapshot(pre_search), _snapshot(previous_post_search),
                              _snapshot(post_search))
    if before is None or earlier is None or after is None:
        reasons.append("invalid_snapshot")
        return report
    report["raw"] = {"pre_search": before.model_dump(),
                     "previous_post_search": earlier.model_dump(),
                     "post_search": after.model_dump()}
    report["hashes"] = {"pre_search": evidence_digest(pre_search),
                        "pre_rows": _rows_digest(before), "post_rows": _rows_digest(after)}
    if not isinstance(search_action, dict):
        reasons.append("search_not_acknowledged")
        return report
    action = search_action
    if (not isinstance(action.get("attempt_id"), str) or not action["attempt_id"]
            or action.get("outcome") != "acknowledged"
            or action.get("operation") not in ("click", "submit")
            or action.get("name") not in ("Search", "Search for flights")):
        reasons.append("search_not_acknowledged")
    if action.get("pre_search_digest") != evidence_digest(pre_search):
        reasons.append("search_not_bound_to_query")
    dispatch, acknowledged = action.get("dispatched_at_ms"), action.get("acknowledged_at_ms")
    if (not _time(dispatch) or not _time(acknowledged)
            or not (before.captured_at_ms <= dispatch <= acknowledged
                    and math.floor(acknowledged) <= math.floor(earlier.captured_at_ms))
            or dispatch - before.captured_at_ms > MAX_PRE_SEARCH_AGE_MS
            or after.captured_at_ms - earlier.captured_at_ms < MIN_SETTLE_MS):
        reasons.append("invalid_search_chronology")
    for snapshot in (before, earlier, after):
        if not _google_flights(snapshot.url):
            reasons.append("unexpected_url")
        if snapshot.truncated:
            reasons.append("incomplete_capture")
        reasons.extend(_form_reasons(snapshot.form, expected, date_selection,
                                     before.captured_at_ms))
        reasons.extend(name for name, value in snapshot.hazards.model_dump().items() if value)
    if _settled_digest(earlier) != _settled_digest(after):
        reasons.append("results_not_settled")
    if (before.results.headings or before.results.query_summaries or before.results.adult_summaries
            or before.results.rows or before.results.displayed_row_count):
        reasons.append("preexisting_results")
    if _rows_digest(before) == _rows_digest(after):
        reasons.append("stale_results")
    expected_summary = (f"Track prices from {_AIRPORTS[expected.origin][0]} to "
                        f"{_AIRPORTS[expected.destination][0]} departing {expected.departure_date}")
    summaries = {_normal(value) for value in after.results.query_summaries}
    if summaries != {_normal(expected_summary)}:
        reasons.append("result_query_mismatch")
    if {_normal(value) for value in after.results.adult_summaries} != {
            "prices include required taxes + fees for 1 adult."}:
        reasons.append("result_adult_count_mismatch")
    if "search results" not in {_normal(value) for value in after.results.headings}:
        reasons.append("results_heading_missing")
    rows = [_row(row, expected) for row in after.results.rows]
    valid_rows = [row for row in rows if row is not None]
    if not valid_rows:
        reasons.append("complete_visible_row_missing")
    elif any(row is None for row in rows):
        # Mixed old/new or unparseable visible results are not safely attributable
        # to the latest query, even when one row appears correct.
        reasons.append("ambiguous_visible_rows")
    else:
        report["row"] = valid_rows[0]
    report["reasons"] = list(dict.fromkeys(reasons))
    if not reasons:
        report["status"] = "PASS"
    return report
