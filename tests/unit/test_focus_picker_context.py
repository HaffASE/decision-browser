"""Boolean picker hints preserve attribute privacy and plain-field context."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_snapshot_semantics import snapshot_spec

from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.dom import Action, Candidate, Observation
from decision_browser.runner import RunManager


@pytest.mark.parametrize("attribute", ["list", "aria-autocomplete", "aria-controls", "aria-haspopup"])
def test_picker_association_excludes_plain_focus_recovery_without_disclosing_attribute(attribute):
    private = "PRIVATE_PICKER_ASSOCIATION"
    data = snapshot_spec({"type": "text", "attrs": {attribute: private}})
    assert data["context"]["picker"] is True
    assert private not in json.dumps(data["context"])


def test_plain_input_keeps_existing_context_shape():
    assert snapshot_spec({"type": "text"})["context"] == {"focused": False}


def recognition(*, admission_mode="task_scope", browser_mode="isolated"):
    host = RunManager(lambda: None, admission_mode=admission_mode)
    run = host._open(TaskRequest(goal="Review", start_url="https://example.test/", values={"City": "London"},
                                 checks=[], limits=Limits(), privacy=PrivacyPolicy()))
    run.request = run.request.model_copy(update={"mode": browser_mode})
    run.completion = {"total": 1, "pending": 1}
    run.dom = SimpleNamespace(_target_identity=lambda _token: ("doc", 7))
    semantic = {"node": {"name": "City", "role": "textbox", "value": "London", "state": {
        "tag": "input", "type": "text", "disabled": False, "readonly": False, "aria": {"expanded": None},
    }}}
    item = Candidate("field", ("click", "fill"), {"click": semantic}, None, {"focused": True})
    obs = Observation("obs", "https://example.test", "doc", "", {"field": item}, (), url="https://example.test/")
    action = Action("click", "field", None, None, "obs", "{}")
    return host, run, obs, action


@pytest.mark.parametrize("admission,browser", [("standard_form", "isolated"), ("task_scope", "existing_chrome")])
def test_recovery_does_not_change_other_admission_or_browser_modes(admission, browser):
    host, run, obs, action = recognition(admission_mode=admission, browser_mode=browser)
    assert host._focus_click_identity(run, obs, action) is None


@pytest.mark.parametrize("kind", ["date", "time", "datetime-local", "month", "week", "color", "number"])
def test_native_picker_input_types_are_never_plain_focus_recovery(kind):
    host, run, obs, action = recognition()
    obs.candidates["field"].semantic_by_operation["click"]["node"]["state"]["type"] = kind
    assert host._focus_click_identity(run, obs, action) is None


@pytest.mark.parametrize("location", ["operations", "semantic", "node", "state", "aria", "tag", "type", "context"])
def test_malformed_recognition_evidence_returns_no_identity(location):
    host, run, obs, action = recognition()
    item = obs.candidates["field"]
    if location == "operations":
        item = replace(item, semantic_by_operation=None)
    elif location == "semantic":
        item.semantic_by_operation["click"] = None
    elif location == "node":
        item.semantic_by_operation["click"]["node"] = None
    elif location == "state":
        item.semantic_by_operation["click"]["node"]["state"] = []
    elif location in {"aria", "tag", "type"}:
        item.semantic_by_operation["click"]["node"]["state"][location] = []
    else:
        item = replace(item, context=None)
    obs = replace(obs, candidates={"field": item})
    assert host._focus_click_identity(run, obs, action) is None
