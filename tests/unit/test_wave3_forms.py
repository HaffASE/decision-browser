"""F09 form bindings: real scope/classifier, no browser activation claim."""
import json

import pytest
from test_runner_control_bindings import candidate, manager, observation

from decision_browser.dom import Action, _el_value
from decision_browser.policy import classify
from decision_browser.runner import RunnerError


@pytest.mark.parametrize("supplied,options,note", [
    ("missing", [["a", "Alpha", False]], "select_input:value_mismatch"),
    ("Requested", [["x", "Allowed", False], ["x", "Requested", False]], "select_input:ambiguous_value"),
    ("Same", [["a", "Same", False], ["b", "Same", False]], "select_input:ambiguous_value"),
])
def test_supplied_select_mismatch_and_ambiguity_are_explicit(supplied, options, note):
    host, run = manager({"Menu": supplied})
    item = candidate("menu", "select", "Menu", options=options)
    with pytest.raises(RunnerError, match="blocked"):
        host._questions(run, observation({"menu": item}))
    assert note in run.result.evidence


def test_duplicate_value_is_refused_even_when_only_one_option_enabled():
    with pytest.raises(ValueError, match="select_ambiguous"):
        _el_value("select", "x", {"node": {"options": [["x", "A", True], ["x", "B", False]]}})
    assert _el_value("select", "b", {"node": {"options": [["a", "A", False], ["b", "B", False]]}}) == "b"


@pytest.mark.parametrize("operation", ["fill", "select", "click"])
@pytest.mark.parametrize("action_route,expected", [("https://other.test/search", "clear_non_match"), ("https://other.test/%70urchase", "match")])
def test_mutation_recipient_is_document_but_future_route_remains_signal(operation, action_route, expected):
    host, run = manager()
    semantic = {"url": "http://127.0.0.1:12345/", "node": {"name": "Query", "state": {"tag": "input", "type": "text"}}, "form": {"action": action_route, "submits": False}}
    action = Action(operation, "query", "x" if operation != "click" else None, None, "obs", json.dumps({"semantic": semantic}))
    scope = host._scope(run, observation({}), action)
    assert scope.destination_origin == "http://127.0.0.1:12345"
    assert scope.destination_url == semantic["url"]
    assert scope.signal_conflict is False
    assert classify(scope) == expected


async def test_form_serialization_unsupported_uses_fixed_public_code():
    from test_unsupported_diagnostics import _unsupported

    result = await _unsupported(("formserialization",))
    assert result.evidence == ["unsupported:formserialization"]
