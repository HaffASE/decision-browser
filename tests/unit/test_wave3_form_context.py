"""Shipped form metadata stays native; unfamiliar evidence stays lossless."""
import json
from copy import deepcopy

import pytest
from test_compact_context_v2 import standard_questions
from test_lean_context import native
from test_model_context import TOKEN, context

from decision_browser.contracts import PrivacyPolicy
from decision_browser.model_context import bind_openai_singletons, prepare_context
from decision_browser.privacy import PrivacyError, PrivacyGate

DESTINATION = 'https://example.test/search?q=%22quoted%22+%CE%A9&order=first#exact'


def source(extras):
    row = native()
    row["form"] = {"path": "0/1", "id": "search", "name": "Search Ω", "action": "https://example.test/search",
                   "method": "get", "formAction": "", "formMethod": "GET", "formTarget": "_self",
                   "defaultSubmitter": {"path": "0/1/2", "role": "button", "name": "Search", "type": "submit", "caption": 'Go "Ω"'},
                   **extras}
    return context({"fill": {TOKEN: row}})


def project(data, provider, *, lean=True, privacy=None):
    questions, heads = standard_questions(data["candidates"])
    return prepare_context(questions, json.dumps(data, ensure_ascii=False), target_heads=heads,
                           compact=True, lean=lean, provider=provider, privacy=privacy)


@pytest.mark.parametrize("provider", ["openai", "systemone"])
@pytest.mark.parametrize("extras", [
    {}, {"submits": False}, {"submits": True},
    {"destination": DESTINATION}, {"submits": True, "destination": DESTINATION},
    {"submits": False, "destination": DESTINATION},
])
def test_known_form_fields_use_native_v3_and_keep_every_string_and_flag(provider, extras):
    data = source(extras)
    original = deepcopy(data)
    questions, raw, aliases = project(data, provider)
    result = json.loads(raw)
    assert result["context_format"] == "decision-browser-choice-table-v3"
    assert data == original
    alias = next(alias for alias, token in aliases["fill"].items() if token == TOKEN)
    form = result["forms"][result["candidates"]["fill"][alias]["form"]]
    expected = {key: value for key, value in original["candidates"]["fill"][TOKEN]["form"].items() if key not in {"path", "defaultSubmitter"}}
    expected["defaultSubmitter"] = {"role": "button", "name": "Search", "type": "submit", "caption": 'Go "Ω"'}
    assert form == expected
    if "submits" in extras:
        assert form["submits"] is extras["submits"]
    assert questions[1].choices == (alias,)


@pytest.mark.parametrize("provider", ["openai", "systemone"])
@pytest.mark.parametrize("extras", [
    {"submits": None}, {"submits": 0}, {"submits": 1},
    {"submits": "false"}, {"submits": "true"}, {"submits": []}, {"submits": {}},
    {"destination": None}, {"destination": False}, {"destination": 1},
    {"destination": []}, {"destination": {}},
    {"submits": True, "destination": DESTINATION, "future": False},
    {"submits": False, "future": "unfamiliar evidence"},
])
def test_unknown_or_badly_typed_form_metadata_uses_exact_lossless_fallback(provider, extras):
    data = source(extras)
    before = deepcopy(data)
    assert project(data, provider) == project(data, provider, lean=False)
    assert json.loads(project(data, provider)[1])["context_format"] == "decision-browser-choice-table-v2"
    assert data == before


def test_openai_singleton_and_systemone_keep_identical_native_form_evidence():
    data = source({"submits": False, "destination": DESTINATION})
    questions, raw, aliases = project(data, "openai")
    systemone_questions, systemone_raw, systemone_aliases = project(data, "systemone")
    assert json.loads(raw)["context_format"] == "decision-browser-choice-table-v3"
    assert json.loads(raw)["forms"] == json.loads(systemone_raw)["forms"]
    assert aliases == systemone_aliases
    _, heads = standard_questions(data["candidates"])
    bound, remaining, singletons = bind_openai_singletons(questions, raw, aliases, operation_head="operation", target_heads=heads)
    assert list(singletons.values()) == [("fill", "1")]
    assert aliases["fill"]["1"] == TOKEN
    assert remaining == {} and len(bound) == 1
    assert systemone_questions[1].choices == ("1",)


@pytest.mark.parametrize("field", ["destination", "submits"])
def test_source_privacy_precedes_native_projection_or_bad_type_fallback(field):
    data = source({"submits": False, field: "blocked-form-evidence"})
    privacy = PrivacyGate(PrivacyPolicy(blocked_literals=["blocked-form-evidence"]))
    for provider in ("openai", "systemone"):
        with pytest.raises(PrivacyError):
            project(data, provider, privacy=privacy)
