"""Finite decision feedback survives compaction without widening model authority."""

from __future__ import annotations

import json
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from test_model_context import TOKEN, context, descriptor, prepare, questions
from test_provider import _budget, _gate, _wire_answers
from test_systemone_provider import reply

from decision_browser.action_context import NEXT_ACTION, PageHistory, context_payload
from decision_browser.decision_codec import CodecError, Decision
from decision_browser.dom import Observation
from decision_browser.privacy import PrivacyError
from decision_browser.provider import OpenAIProvider, ProviderError
from decision_browser.systemone_provider import SystemOneProvider


def feedback_source():
    item = descriptor(operation="fill")
    item["fill_state"] = "differs"
    return context({"fill": {TOKEN: item}}, completion={"total": 2, "pending": 1},
                   scroll={"up": True, "down": True})


def test_page_history_is_bounded_private_and_confined_to_the_current_origin():
    history = PageHistory()
    origin = "https://rail.test"
    after = Observation("o", origin, "new-doc", "", {}, ())
    for n in range(6):
        before = Observation("o", origin, str(n), str(n) + "界" * 700, {}, ())
        history.departed(before, after, _gate())
    excerpts = history.for_origin(origin)
    assert [text[0] for text in excerpts] == list("2345")
    assert all(len(text.encode()) == 1024 for text in excerpts)
    private = Observation("o", origin, "doc", "x" * 1022 + "secret", {}, ())
    history.departed(private, after, _gate("secret"))
    assert history.for_origin(origin) == excerpts
    # An approved cross-origin navigation must not expose text in either direction.
    foreign = Observation("f", "https://elsewhere.test", "doc", "foreign", {}, ())
    history.departed(after, foreign, _gate())
    assert history.for_origin(foreign.origin) == []
    assert history.for_origin(origin) == []


def test_feedback_remains_bounded_and_reversible_after_pooling():
    source = feedback_source()
    before = deepcopy(source)
    original_questions, heads = questions(source["candidates"])
    wire, packed, aliases = prepare(source, original_questions, heads)
    assert source == before
    assert packed["completion"] == {"total": 2, "pending": 1}
    assert packed["scroll"] == {"up": True, "down": True}
    assert "observation_diagnostics" not in packed
    assert packed["candidates"]["fill"]["1"]["fill_state"] == "differs"
    assert aliases == {"fill": {"1": TOKEN}}
    assert wire[0].choices == original_questions[0].choices == ("DONE", "fill")
    assert set(packed["completion"]) == {"total", "pending"}


def test_context_sends_only_scroll_flags_and_completion_counts():
    obs = Observation("o", "https://example.test", "doc", "", {}, (), diagnostics={
        "viewport": {"x": 0, "y": 720, "width": 1280, "height": 720,
                     "can_scroll_up": True, "can_scroll_down": True,
                     "private_detail": "PRIVATE_DIAGNOSTIC"},
    })
    raw = context_payload("Continue", obs, {}, [], completion={"total": 3, "pending": 2})
    body = json.loads(raw)
    assert body["scroll"] == {"up": True, "down": True}
    assert body["completion"] == {"total": 3, "pending": 2}
    q, heads = questions(body["candidates"])
    _, packed, _ = prepare(body, q, heads)
    assert "PRIVATE_DIAGNOSTIC" not in json.dumps(packed)


@pytest.mark.parametrize("viewport", [None, {}, {"can_scroll_up": 1, "can_scroll_down": False},
                                      {"can_scroll_up": False, "can_scroll_down": "false"}])
def test_invalid_or_absent_scroll_diagnostics_are_not_model_feedback(viewport):
    obs = Observation("o", "https://example.test", "doc", "", {}, (),
                      diagnostics={"viewport": viewport})
    assert "scroll" not in json.loads(context_payload("Continue", obs, {}, []))


@pytest.mark.parametrize("field,value", [
    ("previous_pages", {"check": "not text"}),
    ("previous_pages", ["p"] * 5),
    ("previous_pages", ["界" * 342]),
    ("completion", {"total": True, "pending": 0}),
    ("completion", {"total": 2, "pending": -1}),
    ("completion", {"total": 1, "pending": 2}),
    ("completion", {"total": 33, "pending": 1}),
    ("completion", {"total": 2, "pending": 1, "expected": "not allowed"}),
    ("completion", {"pending": 1}),
    ("completion", None),
    ("scroll", {"up": 1, "down": False}),
    ("scroll", {"up": False, "down": "false"}),
    ("scroll", {"up": False, "down": True, "url": "not allowed"}),
    ("scroll", {}),
])
def test_invalid_feedback_is_rejected_not_forwarded(field, value):
    source = feedback_source()
    source[field] = value
    with pytest.raises(CodecError, match="invalid_context"):
        prepare(source)


@pytest.mark.parametrize("state", ["matches", "differs", "unbound"])
def test_fill_feedback_does_not_duplicate_element_or_prune_choices(state):
    click = descriptor(operation="click")
    fill = descriptor(operation="fill")
    fill["fill_state"] = state
    source = context({"click": {TOKEN: click}, "fill": {TOKEN: fill}})
    wire, packed, aliases = prepare(source)
    assert len(packed["elements"]) == 1
    assert packed["candidates"]["fill"]["1"]["fill_state"] == state
    assert set(aliases) == {"click", "fill"} and "DONE" in wire[0].choices
    assert "fill_state" not in packed["elements"]["e1"]


@pytest.mark.parametrize("operation,state", [("fill", "invented"), ("fill", True),
                                           ("fill", None), ("click", "matches")])
def test_fill_feedback_requires_valid_enum_on_a_fill_descriptor(operation, state):
    item = descriptor(operation=operation)
    item["fill_state"] = state
    with pytest.raises(CodecError, match="invalid_context"):
        prepare(context({operation: {TOKEN: item}}))


@pytest.mark.parametrize("provider_kind", ["openai", "systemone"])
async def test_real_provider_wire_keeps_feedback_and_restores_the_target(provider_kind):
    source = feedback_source()
    earlier = Observation("old", source["origin"], "timetable", "Express 09:20 is direct, platform 4.", {}, ())
    current = Observation("new", earlier.origin, "route", source["text"], {}, ())
    history = PageHistory()
    history.departed(earlier, current, _gate())
    source.update(json.loads(context_payload(source["goal"], current, source["candidates"], [],
                                            completion=source["completion"],
                                            previous_pages=history.for_origin(current.origin))))

    q, heads = questions(source["candidates"])
    seen = []

    def handler(request):
        body = json.loads(request.content)
        packed = json.loads(body["input" if provider_kind == "openai" else "state"])
        seen.append(packed)
        assert packed["previous_pages"] == [earlier.text] and earlier.text not in packed["text"]
        assert packed["completion"] == {"total": 2, "pending": 1}
        assert packed["scroll"]["down"] is True
        assert packed["candidates"]["fill"]["1"]["fill_state"] == "differs"
        picks = {"operation": "fill", "target_fill": "1"}
        return (httpx.Response(200, content=_wire_answers(body, picks)) if provider_kind == "openai"
                else httpx.Response(200, json=reply(body, picks)))

    active = (OpenAIProvider("test-key", transport=httpx.MockTransport(handler)) if provider_kind == "openai"
              else SystemOneProvider(endpoint="http://127.0.0.1:8000/v1/systemone", model="fixture",
                                     transport=httpx.MockTransport(handler)))
    budget = _budget()
    try:
        decision = await active.decide(q, json.dumps(source), target_heads=heads, budget=budget, privacy=_gate())
        assert decision.operation == "fill" and decision.target == TOKEN
        assert isinstance(decision, Decision)
    finally:
        await active.aclose()
    assert len(seen) == budget.usage().calls == 1


@pytest.mark.parametrize("provider_kind", ["openai", "systemone"])
@pytest.mark.parametrize("privacy", [False, True])
async def test_bad_feedback_never_reaches_http_or_spends_call_budget(provider_kind, privacy):
    source = feedback_source()
    source["completion"]["expected"] = "PRIVATE_CHECK_VALUE"
    q, heads = questions(source["candidates"])
    seen = []

    def forbidden(request):
        seen.append(request)
        raise AssertionError("invalid or private feedback must not be transmitted")

    active = (OpenAIProvider("test-key", transport=httpx.MockTransport(forbidden)) if provider_kind == "openai"
              else SystemOneProvider(endpoint="http://127.0.0.1:8000/v1/systemone", model="fixture",
                                     transport=httpx.MockTransport(forbidden)))
    budget = _budget()
    try:
        with pytest.raises(PrivacyError if privacy else ProviderError):
            await active.decide(q, json.dumps(source), target_heads=heads, budget=budget,
                                privacy=_gate("PRIVATE_CHECK_VALUE") if privacy else _gate())
    finally:
        await active.aclose()
    assert not seen and budget.usage().calls == 0


@pytest.mark.parametrize("scroll_top,client_height,scroll_height,expected", [
    (0, 720, 1607, {"up": False, "down": True}),
    (720, 720, 1607, {"up": True, "down": True}),
    (887, 720, 1607, {"up": True, "down": False}),
    (0, 720, 720, {"up": False, "down": False}),
])
def test_collector_reports_document_scroll_extent(scroll_top, client_height, scroll_height, expected):
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the collector regression"
    source = Path("src/decision_browser/collector.js").read_text()
    js = """
const [source, top, height, extent] = JSON.parse(process.argv[1]);
global.document = {documentElement:{clientWidth:800,clientHeight:600},querySelectorAll:()=>[], scrollingElement:{scrollTop:top,clientHeight:height,scrollHeight:extent}};
global.scrollX=0;global.scrollY=top;global.innerWidth=1280;global.innerHeight=height;
// Model native prototype getters/methods separately from instance fixture data.
const intrinsicPrototype = new Proxy(Object.prototype, {get(target, key, receiver) {
 const own = Object.getOwnPropertyDescriptor(receiver, key);
 if (own) return own.value;
 if (key === 'matches') return () => false;
 return Reflect.get(target, key, receiver);
}});
const intrinsicObject = value => Object.setPrototypeOf(value, intrinsicPrototype);
intrinsicObject(document);
const result=eval('('+source+')')('selector',()=>{});
process.stdout.write(JSON.stringify(result.diagnostics.viewport));
"""
    result = subprocess.run([node, "-e", js, json.dumps([source, scroll_top, client_height, scroll_height])],
                            capture_output=True, text=True, check=True)
    viewport = json.loads(result.stdout)
    assert {"up": viewport["can_scroll_up"], "down": viewport["can_scroll_down"]} == expected


def test_operation_instructions_distinguish_editable_fields_from_picker_triggers():
    assert "CLICK to open its picker, observe, then FILL" not in NEXT_ACTION
    assert "editable" in NEXT_ACTION and "FILL" in NEXT_ACTION
    assert "If the goal requires\nan autocomplete selection" in NEXT_ACTION
    assert "ordinary text/search field" in NEXT_ACTION
    assert "completion.pending" in NEXT_ACTION
    assert "scroll.down" in NEXT_ACTION


@pytest.mark.parametrize('count', [1, 2])
async def test_systemone_repeated_action_has_progress_criteria_and_intact_history(count):
    """A completed click must not be presented as a completed multi-click goal."""
    from test_lean_context import native

    from decision_browser.action_context import TARGET
    from decision_browser.decision_codec import ChoiceQuestion

    row = native('inc', operation='click')
    row['node'].update(role='button', name='inc')
    row['node']['state'].update(tag='button', type='button')
    history = [{'operation': 'click', 'name': 'inc', 'acknowledged': True,
                'page_changed': True, 'value': None, 'url': row['url']} for _ in range(count)]
    source = context({'click': {TOKEN: row}}, goal='Click inc three times until the count is 3.',
                     text=f'{count}\ninc\nreset', recent_actions=history,
                     completion={'total': 1, 'pending': 1})
    q = (ChoiceQuestion('operation', NEXT_ACTION, ('UNABLE', 'click')),
         ChoiceQuestion('target_click', TARGET + ' Operation: click', (TOKEN,)))
    seen = []

    def handler(request):
        body = json.loads(request.content)
        packed = json.loads(body['state'])
        assert packed['recent_actions'] == history
        assert packed['text'] == source['text']
        assert packed['completion'] == {'total': 1, 'pending': 1}
        criteria = [json.loads(value) for value in body['questions']['operation']['criteria'].values()]
        assert all(isinstance(value, dict) for value in criteria)
        descriptions = {value['choice']: value['description'] for value in criteria}
        assert set(descriptions) == {'UNABLE', 'click'}
        assert 'repetitions' in descriptions['click']
        assert 'no offered safe action' in descriptions['UNABLE']
        seen.append(body)
        return httpx.Response(200, json=reply(body, {'operation': 'click', 'target_click': '1'}))

    provider = SystemOneProvider(endpoint='http://127.0.0.1:8000/v1/systemone', model='fixture',
                                 transport=httpx.MockTransport(handler))
    try:
        chosen = await provider.decide(q, json.dumps(source), target_heads={'click': 'target_click'},
                                       budget=_budget(), privacy=_gate())
        assert chosen.operation == 'click' and chosen.target == TOKEN
    finally:
        await provider.aclose()
    assert len(seen) == 1


@pytest.mark.parametrize('existing', ['caller-specific instruction', '', None])
def test_systemone_grouped_operation_preserves_explicit_description(existing):
    from decision_browser.action_context import TARGET
    from decision_browser.decision_codec import ChoiceQuestion
    from decision_browser.model_context import prepare_context

    group = 'click_group1'
    source = context({group: {TOKEN: descriptor(operation='click')}})
    q = (ChoiceQuestion('operation', NEXT_ACTION, ('UNABLE', group), ('custom stop', existing)),
         ChoiceQuestion('target_click', TARGET + ' Operation: ' + group, (TOKEN,)))
    wire, _, aliases = prepare_context(q, json.dumps(source), target_heads={group: 'target_click'},
                                       compact=True, lean=True, provider='systemone')
    assert wire[0].choices == ('UNABLE', group)
    assert wire[0].descriptions[0] == 'custom stop'
    if existing is not None:
        assert wire[0].descriptions[1] == existing
    else:
        assert 'repetitions' in wire[0].descriptions[1]
    assert aliases[group]['1'] == TOKEN
