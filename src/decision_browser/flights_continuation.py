"""Closed public-query contract for the explicitly opted-in Flights pilot.

This authorizes state, never causal ownership. Full DOM fingerprints remain
internal; both provider input and report exports are constructed separately.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .decision_codec import ChoiceQuestion, Decision
from .flights_evidence import FLIGHTS_EVIDENCE_SCRIPT, _airport, _normal, _snapshot
from .flights_query import AIRPORTS, DATE_DONE, DATE_LABEL, DEPARTURE, JFK, LAX
from .provider import ProviderError

_DATE_DONE_LIVE = DATE_DONE.replace("flights departing", "flights, departing")
PICKERS = frozenset({"origin", "destination", "calendar"})
_DATE_PREFIX = rf"(?:(?:{DEPARTURE:%A}|{DEPARTURE:%a}),? )?(?:{DEPARTURE:%B}|{DEPARTURE:%b}) {DEPARTURE.day}"
_DATE = _DATE_PREFIX + rf",? {DEPARTURE.year}"
_DEPARTURE = _DATE_PREFIX + rf"(?:,? {DEPARTURE.year})?"
ORIGIN = "https://www.google.com"

# Supplemental metadata contains only booleans and closed dialog-kind enums.
# It shares the bounded evidence script's visibility rules, without page writes.
STATE_SCRIPT = "() => { const evidence = (" + FLIGHTS_EVIDENCE_SCRIPT + r""")();
 const shown = el => {
   if (!el.isConnected || el.closest('[hidden],[aria-hidden="true"]')) return false;
   for(let p=el;p;p=p.parentElement) {
     const s=getComputedStyle(p);
     if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||Number(s.opacity)===0) return false;
   }
   return !!el.getClientRects().length;
 };
 const visible = (root, selector) => Array.from(root.querySelectorAll(selector)).filter(shown);
 const grids=visible(document,'[role="grid"]');
 const dialogs=visible(document,'[role="dialog"][aria-modal="true"],dialog[open]');
 const normalized=s=>(s||'').replace(/\s+/g,' ').trim().toLowerCase();
 // Kinds come only from the single input's exact label and the grid count.
 // "airport" is Google's "Where else?" input; only the host binding of the
 // confirmed opening click decides whether it is the origin or destination.
 const kinds=dialogs.slice(0,2).map(d=>{
   const inputs=visible(d,'input');
   const label=inputs.length===1?normalized(inputs[0].getAttribute('aria-label')):null;
   const grids=visible(d,'[role="grid"]').length;
   if(visible(d,'button[type="submit"],input[type="submit"],input[type="password"]').length) return 'unknown';
   if(inputs.length===1 && !grids && inputs[0].getAttribute('aria-expanded')==='true') {
     if(label==='where from?') return 'origin';
     if(label==='where to?') return 'destination';
     if(label==='where else?') return 'airport';
   }
   if(grids===1 && (!inputs.length || inputs[0].type==='text' && (label==='departure' || label==='departure date')))
     return 'calendar';
   return 'unknown';
 });
 const options=visible(document,'[role="option"],[role="menuitemradio"]');
 const name=e=>normalized(e.innerText||e.getAttribute('aria-label'));
 const one=options.filter(e=>name(e)==='one way');
 const menu=one.length===1?one[0].closest('[role="listbox"],[role="menu"]'):null;
 const tripMenu=!!menu && shown(menu) && visible(document,'[role="listbox"],[role="menu"]').length===1
   && options.length===3 && options.every(e=>menu.contains(e)
     && e.getAttribute('role')===(menu.getAttribute('role')==='listbox'?'option':'menuitemradio')
     && !e.hasAttribute('aria-labelledby')
     && !(e.labels && e.labels.length)
     && (!e.hasAttribute('aria-label') || normalized(e.getAttribute('aria-label'))===name(e)))
   && ['round trip','one way','multi-city'].every(label=>options.filter(e=>name(e)===label).length===1)
   && !visible(document,'input,textarea,select,[contenteditable="true"],[role="grid"]').length && !dialogs.length;
 return {evidence, dialogs:kinds, password:visible(document,'input[type="password"],form[action*="accounts.google.com"],form[action*="/signin"]').length>0,
   calendar:grids.length===1, trip_menu:tripMenu, trip_picker:options.some(e=>name(e)==='one way')};
}"""


def allowed_url(value: object, *, results: bool = False) -> bool:
    """Independent of the success oracle's broader URL recognizer."""
    if not isinstance(value, str) or len(value) > 4096 or re.search(r"[\s\x00-\x1f\x7f]", value):
        return False
    try:
        parts = urlsplit(value)
        if (parts.scheme != "https" or parts.netloc not in {"www.google.com", "www.google.com:443"}
                or parts.fragment or "#" in value or parts.path not in {
                    "/travel/flights", *(["/travel/flights/search"] if results else [])}):
            return False
        fields = parts.query.split("&")
        if not fields or len(fields) > 3 or any(not field or "=" not in field for field in fields):
            return False
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=3)
        if len(pairs) != len(fields) or len({key for key, _ in pairs}) != len(pairs):
            return False
        for field, (key, item) in zip(fields, pairs, strict=True):
            if field.split("=", 1)[0] != key or key not in {"hl", "tfs", "tfu"}:
                return False
            if key == "hl":
                if field != "hl=en":
                    return False
            elif not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", item):
                return False
        return ("hl", "en") in pairs
    except ValueError:
        return False


def canonical_control(operation: str, node: dict[str, Any], picker: object = None) -> str | None:
    """``picker`` is the host-validated phase; it alone maps Google's "Where else?" input."""
    name = node.get("fullName", node.get("name"))
    role = node.get("role")
    if not isinstance(name, str):
        return None
    normalized = _normal(name)
    if operation in {"click", "fill"} and role in {"combobox", "textbox"}:
        if normalized == "where else?" and picker in {"origin", "destination"}:
            return "Where from?" if picker == "origin" else "Where to?"
        for prefix, city, full, code in (
            ("Where from?", *AIRPORTS["JFK"], "JFK"),
            ("Where to?", *AIRPORTS["LAX"], "LAX"),
        ):
            if normalized in {_normal(prefix), _normal(f"{prefix} {city} {code}"),
                              _normal(f"{prefix} {full} {code}")}:
                return prefix
    if operation == "click":
        if role in {"combobox", "button"} and normalized in {"round trip", "one way"}:
            return "Round trip" if normalized == "round trip" else "One way"
        if role in {"option", "menuitemradio"} and normalized == "one way":
            return "One way"
        if role == "option" and normalized in {_normal(JFK), _normal(LAX)}:
            return JFK if normalized == _normal(JFK) else LAX
        if role in {"combobox", "textbox", "button"} and normalized in {"departure", "departure date"}:
            return "Departure"
        if role == "button" and (normalized in {"next month", "previous month"}
                                 or picker == "calendar" and normalized in {"next", "previous"}):
            return "Next month" if normalized.startswith("next") else "Previous month"
        if role in {"button", "gridcell"} and re.fullmatch(_DATE, " ".join(name.split()), re.IGNORECASE):
            return DATE_LABEL
        if role == "button" and normalized in {_normal(DATE_DONE), _normal(_DATE_DONE_LIVE)}:
            return DATE_DONE
    if operation in {"click", "submit"} and role == "button" and normalized in {"search", "search for flights"}:
        return "Search" if normalized == "search" else "Search for flights"
    return None


STATE_REASONS = frozenset({
    "state_not_object", "snapshot_invalid", "url_mismatch", "url_not_allowed",
    "evidence_truncated", "captcha", "password_or_signin",
    "trip_control_count", "cabin_control_count", "passenger_control_count", "departure_field_count",
    "origin_field_count", "origin_expanded_count", "origin_label_noncanonical", "origin_expanded_label",
    "destination_field_count", "destination_expanded_count", "destination_label_noncanonical",
    "destination_expanded_label", "trip_picker_after_one_way", "dialogs_not_list", "multiple_dialogs",
    "multiple_open_pickers", "dialog_hazard_mismatch", "dialog_kind_mismatch", "departure_label",
    "committed_trip_lost", "committed_origin_lost", "committed_destination_lost", "committed_date_lost",
    "search_not_ready", "search_picker_open", "continuation_picker_open", "continuation_not_one_way",
    "picker_kind_mismatch", "picker_form_fields", "picker_controls", "picker_trip_lost",
})


class PilotStateError(ValueError):
    """A closed, nonsecret reason for rejecting the observed pilot state."""

    def __init__(self, reason: str) -> None:
        if reason not in STATE_REASONS:
            raise AssertionError(reason)
        super().__init__("pilot_state")
        self.reason = reason


def state_shape(value: object) -> dict[str, Any] | None:
    """Counts, booleans and closed enums only; never page text, URLs or values. Never raises."""
    try:
        if not isinstance(value, dict):
            return None
        snap = _snapshot(value.get("evidence"))
        dialogs = value.get("dialogs")
        shape: dict[str, Any] = {
            "dialogs": [kind if kind in {"origin", "destination", "airport", "calendar"} else "unknown"
                        for kind in dialogs[:2]] if isinstance(dialogs, list) else None,
            **{key: value.get(key) if type(value.get(key)) is bool else None
               for key in ("calendar", "trip_menu", "trip_picker", "password")},
            "snapshot_valid": snap is not None,
        }
        if snap is None:
            return shape
        form = snap.form
        controls = [_normal(name) for name in form.controls]
        labels = {"origin": {"where from?", "where from? new york jfk",
                             "where from? john f. kennedy international airport jfk"},
                  "destination": {"where to?", "where to? los angeles lax",
                                  "where to? los angeles international airport lax"}}
        shape.update({
            "trip_controls": sum(name in {"one way", "round trip"} for name in controls),
            "cabin_controls": sum(bool(re.fullmatch(r"economy(?:\s*\(include(?:s)? basic\))?", name))
                                  for name in controls),
            "passenger_controls": sum(bool(re.fullmatch(r"1 (?:passenger|adult)(?: 1)?", name))
                                      or name == "1 passenger, change number of passengers. 1"
                                      for name in controls),
            "other_controls": len(controls),
            "departure_fields": len(form.departure),
            "truncated": snap.truncated,
            "hazards": {"loading": snap.hazards.loading, "captcha": snap.hazards.captcha,
                        "dialog": snap.hazards.dialog},
        })
        shape["other_controls"] -= (shape["trip_controls"] + shape["cabin_controls"]
                                    + shape["passenger_controls"])
        for kind, inputs in (("origin", form.origin), ("destination", form.destination)):
            shape[f"{kind}_fields"] = len(inputs)
            shape[f"{kind}_expanded"] = sum(item.expanded == "true" for item in inputs)
            shape[f"{kind}_canonical_labels"] = sum(_normal(item.label) in labels[kind] for item in inputs)
        return shape
    except Exception:  # noqa: BLE001 — diagnostics must never mask the primary outcome
        return None


def _hidden_picker(value: dict[str, Any], form: Any, picker: str, committed: frozenset[str]) -> dict[str, Any]:
    """One host-bound picker modal that hides the underlying form (observed live 2026-10-11).

    Only the picker's own input is observable. Hidden values are neither read nor
    projected; the full form is revalidated once the picker closes.
    """
    dialogs = value["dialogs"]
    kinds = {"origin": {"origin", "airport"}, "destination": {"destination", "airport"},
             "calendar": {"calendar"}}[picker]
    if dialogs[0] not in kinds or value.get("calendar") is not (picker == "calendar"):
        raise PilotStateError("picker_kind_mismatch")
    controls = [_normal(name) for name in form.controls]
    if picker == "calendar":
        if (form.origin or form.destination or len(form.departure) != 1
                or _normal(form.departure[0].label) not in {"departure", "departure date"}):
            raise PilotStateError("picker_form_fields")
        if len(controls) > 1 or any(name not in {"one way", "round trip"} for name in controls):
            raise PilotStateError("picker_controls")
        if "One way" in committed and controls not in ([], ["one way"]):
            raise PilotStateError("picker_trip_lost")
    else:
        own, other, prefix = ((form.origin, form.destination, "where from?") if picker == "origin"
                              else (form.destination, form.origin, "where to?"))
        if (other or form.departure or len(own) > 1
                or any(item.expanded != "true" or _normal(item.label) != prefix for item in own)):
            raise PilotStateError("picker_form_fields")
        if len(controls) != 1 or controls[0] not in {prefix, "where else?"}:
            raise PilotStateError("picker_controls")
    return {"phase": picker, "form_observed": False, "ready": False}


def validate_state(value: object, url: str, committed: frozenset[str], *,
                   search: bool, continuing: bool, trip_menu_expected: bool = False,
                   picker_expected: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PilotStateError("state_not_object")
    snap = _snapshot(value.get("evidence"))
    if snap is None:
        raise PilotStateError("snapshot_invalid")
    if snap.url != url:
        raise PilotStateError("url_mismatch")
    if not allowed_url(url, results=search):
        raise PilotStateError("url_not_allowed")
    if snap.truncated:
        raise PilotStateError("evidence_truncated")
    if snap.hazards.captcha:
        raise PilotStateError("captcha")
    if value.get("password") is not False:
        raise PilotStateError("password_or_signin")
    form = snap.form
    # Google hides the entire underlying form while this exact menu is open.
    # The host supplies the completed-action/Document binding; hidden values
    # are neither read here nor projected as fresh form evidence.
    if (trip_menu_expected is True and value.get("trip_menu") is True
            and value.get("trip_picker") is True and value.get("calendar") is False
            and value.get("dialogs") == [] and not snap.hazards.dialog
            and not search and not continuing and "One way" not in committed
            and not any((form.origin, form.destination, form.departure, form.controls))
            and not any((snap.results.headings, snap.results.query_summaries,
                         snap.results.adult_summaries, snap.results.displayed_row_count))):
        return {"phase": "trip", "form_observed": False, "ready": False}
    controls = [_normal(name) for name in form.controls]
    # A picker opened by the host-confirmed click hides cabin and passengers with the form.
    if (picker_expected in PICKERS and not search and not continuing
            and isinstance(value.get("dialogs"), list) and len(value["dialogs"]) == 1
            and snap.hazards.dialog and value.get("trip_menu") is False and value.get("trip_picker") is False
            and not any(re.fullmatch(r"economy(?:\s*\(include(?:s)? basic\))?", name)
                        or re.fullmatch(r"1 (?:passenger|adult)(?: 1)?", name)
                        or name == "1 passenger, change number of passengers. 1" for name in controls)
            and not any((snap.results.headings, snap.results.query_summaries,
                         snap.results.adult_summaries, snap.results.displayed_row_count))):
        return _hidden_picker(value, form, picker_expected, committed)
    trips = [name for name in controls if name in {"one way", "round trip"}]
    cabin = [name for name in controls if re.fullmatch(r"economy(?:\s*\(include(?:s)? basic\))?", name)]
    passengers = [name for name in controls if re.fullmatch(r"1 (?:passenger|adult)(?: 1)?", name)
                  or name == "1 passenger, change number of passengers. 1"]
    for reason, items in (("trip_control_count", trips), ("cabin_control_count", cabin),
                          ("passenger_control_count", passengers), ("departure_field_count", form.departure)):
        if len(items) != 1:
            raise PilotStateError(reason)
    opened = []
    for kind, inputs, prefix in (("origin", form.origin, "Where from?"),
                                  ("destination", form.destination, "Where to?")):
        expanded = [item for item in inputs if item.expanded == "true"]
        if not 1 <= len(inputs) <= 2:
            raise PilotStateError(f"{kind}_field_count")
        if len(expanded) > 1 or (len(inputs) == 2 and len(expanded) != 1):
            raise PilotStateError(f"{kind}_expanded_count")
        canonical_labels = ({"where from?", "where from? new york jfk", "where from? john f. kennedy international airport jfk"}
                            if kind == "origin" else {"where to?", "where to? los angeles lax", "where to? los angeles international airport lax"})
        if any(_normal(item.label) not in canonical_labels for item in inputs):
            raise PilotStateError(f"{kind}_label_noncanonical")
        if expanded:
            if _normal(expanded[0].label) != _normal(prefix):
                raise PilotStateError(f"{kind}_expanded_label")
            opened.append(kind)
    if value.get("calendar") is True:
        opened.append("calendar")
    if value.get("trip_picker") is True:
        if "One way" in committed:
            raise PilotStateError("trip_picker_after_one_way")
        opened.append("trip")
    dialogs = value.get("dialogs")
    if not isinstance(dialogs, list):
        raise PilotStateError("dialogs_not_list")
    if len(dialogs) > 1:
        raise PilotStateError("multiple_dialogs")
    if len(opened) > 1:
        raise PilotStateError("multiple_open_pickers")
    if snap.hazards.dialog != bool(dialogs):
        raise PilotStateError("dialog_hazard_mismatch")
    if dialogs and (not opened or dialogs[0] != opened[0]):
        raise PilotStateError("dialog_kind_mismatch")
    origin = _airport(form.origin, "Where from?", "JFK")
    destination = _airport(form.destination, "Where to?", "LAX")
    date_selected = bool(re.fullmatch(_DEPARTURE, " ".join(form.departure[0].value.split()), re.IGNORECASE))
    if _normal(form.departure[0].label) not in {"departure", "departure date"}:
        raise PilotStateError("departure_label")
    for reason, lost in (("committed_trip_lost", "One way" in committed and trips != ["one way"]),
                         ("committed_origin_lost", JFK in committed and not origin),
                         ("committed_destination_lost", LAX in committed and not destination),
                         ("committed_date_lost", DATE_DONE in committed and not date_selected)):
        if lost:
            raise PilotStateError(reason)
    ready = COMMIT_QUERY <= committed and origin and destination and date_selected and trips == ["one way"]
    if search and not ready:
        raise PilotStateError("search_not_ready")
    if search and opened:
        raise PilotStateError("search_picker_open")
    if continuing and opened:
        raise PilotStateError("continuation_picker_open")
    if continuing and trips != ["one way"]:
        raise PilotStateError("continuation_not_one_way")
    return {"phase": "results" if search else (opened[0] if opened else "form"),
            "trip": trips[0], "origin_selected": origin, "destination_selected": destination,
            "date_selected": DATE_DONE in committed and date_selected, "ready": ready,
            "loading": snap.hazards.loading, "adults": 1, "cabin": "economy including Basic"}


COMMIT_QUERY = frozenset({"One way", JFK, LAX, DATE_DONE})

# Google names a calendar day button by its number; its one role-less labelled
# descendant whose text is that number carries the full date (a sibling price
# label is ignored). Read per bound token in the isolated world.
DAY_LABEL_SCRIPT = r"""el => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  if (el.hasAttribute('aria-label') || el.hasAttribute('aria-labelledby')) return null;
  const days = Array.from(el.querySelectorAll('[aria-label]'))
    .filter(e => !e.hasAttribute('role') && /^[0-9]{1,2}$/.test(norm(e.innerText)));
  if (days.length !== 1) return null;
  const day = norm(days[0].innerText);
  const own = norm(el.innerText);
  if (!(own === day || own.startsWith(day + ' '))) return null;
  return norm(days[0].getAttribute('aria-label')).slice(0, 80);
}"""


def day_label(number: object, label: object) -> str | None:
    """Accept the descendant label only when its day agrees with the button's own number."""
    if not isinstance(number, str) or not isinstance(label, str):
        return None
    day = number.split(" ", 1)[0]
    found = re.fullmatch(r"(?:[A-Za-z]+, )?[A-Za-z]+ ([0-9]{1,2}), [0-9]{4}", label)
    return label if re.fullmatch(r"[0-9]{1,2}", day) and found and found.group(1) == day else None


def route(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parts = urlsplit(value)
    # TaskResult already removes the query. This is export formatting, not admission.
    candidate = value if parts.query else value + "?hl=en"
    return ORIGIN + parts.path if allowed_url(candidate, results=True) else None


def _history(rows: list[dict[str, Any]], *, limit: int = 10) -> list[dict[str, Any]]:
    result = []
    for row in rows[-limit:]:
        name = row.get("name")
        if name not in {"Round trip", "One way", "Where from?", "Where to?", JFK, LAX,
                        "Departure", DATE_LABEL, DATE_DONE, "Next month", "Previous month",
                        "Search", "Search for flights", ""}:
            continue
        record = {"operation": row.get("operation") if row.get("operation") in {
                      "click", "fill", "submit", "scroll", "wait"} else None, "name": name,
                       "value": row.get("value") if row.get("value") in {"JFK", "LAX"} else None,
                       "url": route(row.get("url")),
                       "page_changed": row.get("page_changed") if type(row.get("page_changed")) is bool else None}
        if type(row.get("acknowledged")) is bool:
            record["acknowledged"] = row["acknowledged"]
        if isinstance(row.get("post_observation"), dict):
            record["post_observation"] = {key: row["post_observation"].get(key)
                if type(row["post_observation"].get(key)) is bool else None for key in (
                    "same_document", "same_origin", "url_changed", "live_url_matches")}
        for field in ("dispatched_at_ms", "acknowledged_at_ms"):
            value = row.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 1e15:
                record[field] = value
        for field, pattern in (("attempt_id", r"[a-f0-9]{32}"), ("pre_search_digest", r"[a-f0-9]{64}")):
            value = row.get(field)
            if isinstance(value, str) and re.fullmatch(pattern, value):
                record[field] = value
        if row.get("outcome") in {"prepared", "acknowledged", "outcome_unknown", "not_dispatched"}:
            record["outcome"] = row["outcome"]
        result.append(record)
    return result


def project_input(input_text: str, state: dict[str, Any]) -> str:
    raw = json.loads(input_text)
    candidates = {}
    for group, bag in raw["candidates"].items():
        offered = {}
        for key, item in bag.items():
            node = item.get("node") or {}
            canonical = canonical_control(item["operation"], node, state.get("phase"))
            if item["operation"] in {"wait", "scroll"}:
                canonical = item["name"]
            if canonical is None:
                raise ProviderError("invalid_pilot_context")
            aria = (node.get("state") or {}).get("aria") or {}
            offered[key] = {"name": canonical, "operation": item["operation"],
                            "url": route(item.get("url")), "node": {
                                "role": node.get("role", "document"), "name": canonical,
                                "value_matches_request": node.get("value") == {
                                    "Where from?": "JFK", "Where to?": "LAX"}.get(canonical, object()),
                                "expanded": aria.get("expanded") == "true",
                                "selected": aria.get("selected") == "true"},
                            "form": {"action": route((item.get("form") or {}).get("action"))}}
        candidates[group] = offered
    return json.dumps({"goal": raw["goal"], "url": route(raw.get("url")),
                       "state": state, "candidates": candidates,
                       "recent_actions": _history(raw.get("recent_actions", []))}, separators=(",", ":"))


class ProjectedProvider:
    """Initial wrapper around exactly the supplied provider; no recovery factory."""
    def __init__(self, provider: Any, project: Callable[[str], str]) -> None:
        self.provider, self.project = provider, project

    async def decide(self, questions: Sequence[ChoiceQuestion], input_text: str, **kwargs: Any) -> Decision:
        return await self.provider.decide(questions, self.project(input_text), **kwargs)

    async def generate_text(self, *_args: Any, **_kwargs: Any) -> str:
        raise ProviderError("flights_missing_bound_value")

    async def inspect_visual(self, *_args: Any, **_kwargs: Any) -> Any:
        raise ProviderError("flights_decisions_only")

    async def aclose(self) -> None:
        closer = getattr(self.provider, "aclose", None)
        if callable(closer):
            await closer()


def project_report(report: dict[str, Any]) -> dict[str, Any]:
    """Project export copies AFTER oracle verification; never change guard data."""
    output = copy.deepcopy(report)
    if isinstance(output.get("result"), dict):
        output["result"]["last_url"] = route(output["result"].get("last_url"))
    output["actions"] = _history(output.get("actions", []), limit=40)
    evidence = output.get("evidence", {})
    output["evidence"] = {key: evidence[key] for key in ("status", "reasons", "query", "hashes") if key in evidence}
    # A raw snapshot projection is explicitly hash-only; old raw reports remain intact.
    output["evidence"]["raw_projection"] = "omitted; original internal digests retained"
    if evidence.get("row") is not None:
        output["evidence"]["row_sha256"] = hashlib.sha256(
            json.dumps(evidence["row"], sort_keys=True).encode()).hexdigest()
    events = []
    for event in output.get("journal", []):
        row = {key: event[key] for key in ("event", "run_id", "attempt_id", "status", "admission_class",
                                          "action_digest", "proposal_id", "digest", "url_changed", "continuations", "policy") if key in event}
        if "semantic_fingerprint" in event:
            row["semantic_fingerprint_sha256"] = hashlib.sha256(event["semantic_fingerprint"].encode()).hexdigest()
        events.append(row)
    output["journal"] = events
    output["export_projection"] = True
    return output
