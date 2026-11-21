"""Admission consumes snapshot's native submit decision, not raw button type."""
import json
from types import SimpleNamespace

import pytest

from decision_browser.admission import canonical_origin
from decision_browser.dom import Action
from decision_browser.runner import RunManager, RunnerError


@pytest.mark.parametrize("raw", [None, "", "invalid", "SUBMIT", "button", " reset "])
@pytest.mark.parametrize("submits", [False, True])
def test_scope_uses_code_owned_submission_semantics(raw, submits):
    semantic = {"url": "https://example.test/same", "node": {"state": {"tag": "button", "type": raw}}, "form": {"action": "https://other.test/search", "destination": "https://other.test/search?q=fixture", "submits": submits}}
    action = Action("click", "button", None, None, "obs", json.dumps({"semantic": semantic}))
    host = RunManager(lambda: None)
    scope = host._scope(SimpleNamespace(result=SimpleNamespace(run_id="run")), SimpleNamespace(origin="https://example.test"), action)
    assert scope.destination_origin == canonical_origin("https://other.test" if submits else "https://example.test")
    assert scope.destination_url == (semantic["form"]["destination"] if submits else semantic["url"])


@pytest.mark.parametrize("metadata", [None, {}, {"submits": None}, {"submits": 0}, {"submits": 1}, {"submits": "false"}, {"submits": "true"}, {"submits": []}, {"submits": {}}])
@pytest.mark.parametrize("operation", ["click", "fill", "select", "enter", "submit"])
def test_missing_or_nonboolean_submit_metadata_never_falls_back_to_local_admission(metadata, operation):
    form = {"action": "https://other.test/search"}
    if metadata is not None:
        form.update(metadata)
    semantic = {"url": "https://example.test/same", "node": {"state": {"tag": "button", "type": "button"}}, "form": form}
    action = Action(operation, "button", None, None, "obs", json.dumps({"semantic": semantic}))
    host = RunManager(lambda: None)
    with pytest.raises(RunnerError, match="unknown"):
        host._scope(SimpleNamespace(result=SimpleNamespace(run_id="run")), SimpleNamespace(origin="https://example.test"), action)
