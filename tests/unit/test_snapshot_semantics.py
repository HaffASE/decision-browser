"""Execute the shipped snapshot in a small DOM double, without a browser.

These tests cover serialization and policy wiring, not native activation.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from decision_browser.dom import Action
from decision_browser.policy import classify
from decision_browser.runner import RunManager

_SNAPSHOT = Path(__file__).resolve().parents[2] / "src/decision_browser/snapshot.js"
_SCRIPT = r"""
const fs = require('fs');
const snapshot = eval('(' + fs.readFileSync(process.argv[1], 'utf8') + ')');
const spec = JSON.parse(process.argv[2]);
// Model native prototype getters/methods separately from instance fixture data.
const intrinsicPrototype = new Proxy(Object.prototype, {get(target, key, receiver) {
 const own = Object.getOwnPropertyDescriptor(receiver, key);
 if (own) return own.value;
 if (key === 'matches') return () => false;
 return Reflect.get(target, key, receiver);
}});
const intrinsicObject = value => Object.setPrototypeOf(value, intrinsicPrototype);
const doc = {documentElement:{clientWidth:spec.viewportWidth || 800,clientHeight:600},baseURI: spec.baseURI || 'https://example.test/settings',
 defaultView: {innerWidth:spec.innerWidth || 800,innerHeight:600,location: {origin:'https://example.test', href:'https://example.test/settings'},
 performance:{timeOrigin:1}, ShadowRoot:class {}, URL, URLSearchParams,
 HTMLFormElement:class {get elements(){return []; }},
 HTMLButtonElement:class {get type(){const value=(this.getAttribute('type') || '').toLowerCase(); return ['submit','button','reset'].includes(value) ? value : 'submit';}},
 HTMLInputElement:class {get type(){return (this.getAttribute('type') || 'text').toLowerCase();}},
 getComputedStyle:()=>({display:'block',visibility:'visible',opacity:'1'})},
 getElementById:()=>null, elementFromPoint:()=>el,
 querySelector:()=>spec.baseTarget == null ? null : intrinsicObject({getAttribute:()=>spec.baseTarget})};
const el = {ownerDocument:doc, tagName:spec.tag || 'INPUT', value:spec.caption,
 getAttribute:(key)=>({...spec.attrs, type:spec.type, 'aria-label':spec.aria}[key] ?? null),
 hasAttribute:key=>key in (spec.attrs || {}), getRootNode:()=>doc, closest:()=>null,
 isConnected:true, contains:()=>false, elements:[],
 getBoundingClientRect:()=>({x:0,y:0,left:0,top:0,width:100,height:20,...spec.rect})};
intrinsicObject(doc); intrinsicObject(el);
if(spec.form) el.form = intrinsicObject({getAttribute:k=>spec.form[k] ?? null, elements:[]});
if(spec.submitter) {
 const sub = {...el, tagName:'INPUT', value:spec.submitter.caption,
 getAttribute:key=>({type:'submit', 'aria-label':'Review', ...spec.submitter.attrs}[key] ?? null)};
 intrinsicObject(sub);
 doc.documentElement = sub;
 doc.createTreeWalker = ()=>({currentNode:sub,nextNode:()=>null});
}
process.stdout.write(JSON.stringify(snapshot(el, spec.operation || 'click')));
"""


def snapshot(caption: str, type_: str, aria: str | None = None) -> dict:
    return snapshot_spec({"caption": caption, "type": type_, "aria": aria})


def snapshot_spec(spec: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser-free snapshot regression")
    result = subprocess.run(
        [node, "-e", _SCRIPT, str(_SNAPSHOT), json.dumps(spec)], check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("goal", [
    "Найти рейсы. Не переходить к покупке или бронированию.",
    "Find flights. Do not purchase or book.",
    "purchase",
])
@pytest.mark.parametrize("spec,expected", [
    ({"type": "button", "caption": "Search"}, "clear_non_match"),
    ({"type": "button", "caption": "Delete account"}, "match"),
    ({"type": "submit", "caption": "Purchase"}, "match"),
    ({"type": "text", "operation": "enter", "form": {},
      "submitter": {"caption": "Purchase"}}, "match"),
    ({"tag": "A", "aria": "Review", "attrs": {"href": "/%63heckout"}}, "match"),
    ({"tag": "A", "aria": "Review", "attrs": {"href": "/safe?operation=pur%63hase"}}, "match"),
])
def test_goal_is_not_action_signal_but_snapshot_consequences_remain_guarded(goal, spec, expected):
    host = RunManager(lambda: None)
    run = SimpleNamespace(request=SimpleNamespace(goal=goal),
                          result=SimpleNamespace(run_id="run"))
    action = Action(spec.get("operation", "click"), "node", None, None, "obs",
                    json.dumps(snapshot_spec(spec)))
    scope = host._scope(run, SimpleNamespace(origin="https://example.test"), action)
    assert goal not in scope.signals
    assert classify(scope) == expected


@pytest.mark.parametrize("type_", ["submit", "button", "reset"])
@pytest.mark.parametrize("aria", [None, "Review"])
def test_input_caption_reaches_policy_and_fingerprint(type_, aria):
    before = snapshot("Review", type_, aria)
    after = snapshot("Delete account", type_, aria)
    assert before["semantic"] != after["semantic"]
    host = RunManager(lambda: None)
    run = SimpleNamespace(
        request=SimpleNamespace(goal="Review settings"),
        result=SimpleNamespace(run_id="run"),
    )
    action = Action("click", "node", None, None, "obs", json.dumps(after))
    scope = host._scope(run, SimpleNamespace(origin="https://example.test"), action)
    assert classify(scope) == "match"


@pytest.mark.parametrize("type_", ["submit", "button", "reset"])
def test_input_caption_supplies_missing_accessible_name(type_):
    assert snapshot("Review", type_)["semantic"]["node"]["name"] == "Review"


def test_input_caption_full_text_is_classified_without_textbox_value_leak():
    caption = "Review " + "x " * 100 + "Delete account"
    data = snapshot(caption, "submit", "Review")
    node = data["semantic"]["node"]
    assert node["fullCaption"] == caption
    assert "caption" not in snapshot("private value", "text")["semantic"]["node"]
    assert snapshot("x" * 4097, "submit")["semantic"]["unsupported"] == "oversized"


@pytest.mark.parametrize("target,reason", [("_blank", "targetblank"), ("existing", "namedtarget")])
def test_inherited_base_target_is_unsupported_and_changes_semantics(target, reason):
    spec = {"tag": "A", "attrs": {"href": "/safe"}, "baseTarget": target}
    assert snapshot_spec(spec)["semantic"]["unsupported"] == reason
    spec["baseTarget"] = "_self"
    assert snapshot_spec(spec)["semantic"]["unsupported"] is None


def test_anchor_in_form_inherits_base_independently():
    result = snapshot_spec({"tag": "A", "attrs": {"href": "/safe"},
                            "baseTarget": "_blank", "form": {"target": "_self"}})
    assert result["semantic"]["unsupported"] == "targetblank"


@pytest.mark.parametrize("target", ["", "_self", "_top"])
def test_explicit_anchor_target_overrides_base(target):
    result = snapshot_spec({"tag": "A", "attrs": {"target": target}, "baseTarget": "_blank"})
    assert result["semantic"]["unsupported"] is None


def test_target_whitespace_is_not_a_reserved_self_keyword():
    result = snapshot_spec({"tag": "A", "attrs": {"target": " _self "}})
    assert result["semantic"]["unsupported"] == "namedtarget"


def test_empty_submitter_overrides_are_not_absent():
    result = snapshot_spec({"type": "submit", "attrs": {
        "formaction": "", "formmethod": "", "formtarget": ""},
        "baseURI": "https://example.test/base/", "baseTarget": "_blank",
        "form": {"action": "/safe", "method": "post", "target": "existing"}})
    form = result["semantic"]["form"]
    assert form["action"] == "https://example.test/settings"
    assert form["method"] == "get"
    assert form["formAction"] == ""
    assert form["formTarget"] == ""
    assert result["semantic"]["unsupported"] is None


def test_non_submit_control_cannot_override_form_target():
    result = snapshot_spec({"type": "button", "attrs": {"formtarget": "_self"},
                            "baseTarget": "_blank", "form": {}})
    assert result["semantic"]["unsupported"] == "targetblank"


def test_default_submitter_full_caption_reaches_enter_policy():
    caption = "Review " + "x " * 100 + "Delete account"
    data = snapshot_spec({"type": "text", "operation": "enter", "form": {},
                          "submitter": {"caption": caption}})
    assert data["semantic"]["form"]["defaultSubmitter"]["fullCaption"] == caption
    host = RunManager(lambda: None)
    run = SimpleNamespace(request=SimpleNamespace(goal="Review"),
                          result=SimpleNamespace(run_id="run"))
    action = Action("enter", "node", None, None, "obs", json.dumps(data))
    scope = host._scope(run, SimpleNamespace(origin="https://example.test"), action)
    assert classify(scope) == "match"


def test_form_itself_cannot_supply_submitter_override():
    result = snapshot_spec({"tag": "FORM", "operation": "submit",
                            "attrs": {"formtarget": "_self"}, "baseTarget": "_blank"})
    assert result["semantic"]["unsupported"] == "targetblank"


def test_enter_inherits_target_and_empty_submitter_target_suppresses_it():
    spec = {"type": "text", "operation": "enter", "form": {}, "baseTarget": "_blank",
            "submitter": {"caption": "Review"}}
    assert snapshot_spec(spec)["semantic"]["unsupported"] == "targetblank"
    spec["submitter"]["attrs"] = {"formtarget": ""}
    assert snapshot_spec(spec)["semantic"]["unsupported"] is None


@pytest.mark.parametrize("tag,type_,operation", [
    ("INPUT", "text", "fill"),
    ("SELECT", "", "select"),
    ("BUTTON", "button", "click"),
])
@pytest.mark.parametrize("attribute,value", [
    ("formaction", "https://foreign.test/two"),
    ("formmethod", "delete"),
    ("formtarget", "existing"),
])
def test_non_submit_route_attributes_change_fingerprint_not_effective_route(
    tag, type_, operation, attribute, value,
):
    spec = {"tag": tag, "type": type_, "operation": operation,
            "form": {"action": "/safe", "method": "post", "target": "_self"}}
    before = snapshot_spec(spec)["semantic"]
    spec["attrs"] = {attribute: value}
    after = snapshot_spec(spec)["semantic"]
    assert before != after
    assert before["form"] == after["form"]
    assert after["form"]["action"] == "https://example.test/safe"
    assert after["form"]["method"] == "post"
    assert after["unsupported"] is None


def test_equivalent_native_methods_keep_distinct_raw_fingerprints():
    spec = {"tag": "FORM", "operation": "submit", "attrs": {"method": "delete"}}
    before = snapshot_spec(spec)["semantic"]
    spec["attrs"]["method"] = "get"
    after = snapshot_spec(spec)["semantic"]
    assert before != after
    assert before["form"]["method"] == after["form"]["method"] == "get"


def test_absent_to_empty_raw_form_action_changes_fingerprint():
    spec = {"tag": "FORM", "operation": "submit", "attrs": {}}
    before = snapshot_spec(spec)["semantic"]
    spec["attrs"]["action"] = ""
    after = snapshot_spec(spec)["semantic"]
    assert before != after
    assert before["form"]["action"] == after["form"]["action"]


@pytest.mark.parametrize("value", ["x" * 4097, "é" * 2049])
def test_raw_route_attributes_are_bounded_and_fail_closed(value):
    result = snapshot_spec({"tag": "INPUT", "type": "text", "operation": "fill",
                            "attrs": {"formmethod": value}, "form": {"action": "/safe"}})
    assert result["semantic"]["unsupported"] == "oversized"
    assert result["semantic"]["routing"]["element"]["formmethod"] is None
    assert value not in json.dumps(result, ensure_ascii=False)
