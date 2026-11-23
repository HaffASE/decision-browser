"""A mocked Decisions choice uses observed article descriptions in a real guarded run."""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx
import pytest
from browser_scenarios import CASES, ScenarioSite

from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.provider import OpenAIProvider
from decision_browser.runner import RunManager


def _evidence_label(description):
    assert isinstance(description, str)
    is_link = description.startswith("Observed link ")
    quoted = description.removeprefix("Observed link ") if is_link else description
    assert quoted.startswith('"')
    label, end = json.JSONDecoder().raw_decode(quoted)
    assert isinstance(label, str) and label
    suffix = quoted[end:]
    if is_link:
        assert suffix.startswith(" -> ")
        path, end = json.JSONDecoder().raw_decode(suffix[4:])
        assert isinstance(path, str) and suffix[4:][end:] in {"", " [preview]"}
    else:
        assert not suffix or suffix.startswith((" (", "; "))
    return label


def _bound_description(option):
    assert isinstance(option["value"], str) and option["value"].startswith("action_")
    assert isinstance(option.get("description"), str)
    data = json.loads(option["description"])
    fields = {"operation", "target", "evidence"}
    assert isinstance(data, dict) and fields <= data.keys() <= fields | {"operation_hint"}
    assert all(isinstance(value, str) and value for value in data.values())
    _evidence_label(data["evidence"])
    return data


def _name(context, group, value, questions):
    option = next(item for item in questions["target_" + group]["choices"] if item["value"] == value)
    label = _evidence_label(option.get("description"))
    row = context["candidates"][group][value]
    element = context.get("elements", {}).get(row.get("element"), row)
    assert label == element.get("name", element.get("node", {}).get("name"))
    return label



def _offered_targets(context, group, questions):
    """Normalize the two encodings while checking each observed target alias."""
    if "target_" + group in questions:
        options = [
            {"value": row["value"], "target": row["value"], "evidence": row.get("description"),
             "selection": {"operation": group, "target_" + group: row["value"]}}
            for row in questions["target_" + group]["choices"] if isinstance(row["value"], str)
        ]
    else:
        options = []
        for row in questions["operation"]["choices"]:
            if not isinstance(row["value"], str) or not row["value"].startswith("action_"):
                continue
            decoded = json.loads(row["description"])
            if decoded["operation"] == group:
                description = _bound_description(row)
                options.append({
                    "value": row["value"], "target": description["target"], "evidence": description["evidence"],
                    "selection": {"operation": row["value"]},
                })
    assert options
    assert len({row["target"] for row in options}) == len(options)
    for option in options:
        row = context["candidates"][group][option["target"]]
        element = context.get("elements", {}).get(row.get("element"), row)
        option["label"] = _evidence_label(option["evidence"])
        assert option["label"] == element.get("name", element.get("node", {}).get("name"))
    return options

def _answer(body, selections):
    return {"model": "gpt-6-luna", "usage": {}, "answers": [
        {"name": question["name"], "type": "choice",
         "choice": selections.get(question["name"], question["choices"][0]["value"]),
         "confidence": 1., "probabilities": [
             {"value": option["value"],
              "probability": float(type(option["value"]) is type(selections.get(
                  question["name"], question["choices"][0]["value"]))
                  and option["value"] == selections.get(question["name"], question["choices"][0]["value"]))}
             for option in question["choices"]]}
        for question in body["questions"]
    ]}


@pytest.mark.parametrize("encoding", ["complete", "legacy"])
async def test_native_article_run_grounds_choice_in_all_observed_link_descriptions(encoding, monkeypatch):
    if encoding == "legacy":
        monkeypatch.setattr("decision_browser.provider.bind_complete_actions", lambda *args, **kwargs: None)
    spec = CASES["article"]
    goal = spec.goal + "\n\nProvided field values: " + json.dumps(
        spec.values, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    requests = []
    grounded_links = {}
    offered_domains = {}

    def handle(request):
        assert str(request.url) == "https://api.openai.com/v1/decisions"
        body = json.loads(request.content)
        assert body["model"] == "gpt-6-luna"
        context = json.loads(body["input"])
        assert context["goal"] == goal
        assert not {"checks", "expected_events", "plan"}.intersection(context)
        questions = {question["name"]: question for question in body["questions"]}
        step = len(requests)
        requests.append(body)
        if step == 0:
            assert "target_fill" not in questions
            field, = context["candidates"]["fill"]
            assert context["candidates"]["fill"][field]["name"] == "Search query"
            pairs = [(option, _bound_description(option))
                     for option in questions["operation"]["choices"]
                     if isinstance(option["value"], str) and option["value"].startswith("action_")
                     and json.loads(option["description"])["operation"] == "fill"]
            pair, = [option for option, description in pairs
                     if description["operation"] == "fill" and description["target"] == field
                     and _evidence_label(description["evidence"]) == "Search query"]
            if encoding == "legacy":
                assert pair["value"] in questions["operation"]["instructions"]
            else:
                assert len(questions) == 1
                assert "binds its operation and target together" in questions["operation"]["instructions"]
            selection = {"operation": pair["value"]}
        elif step == 1:
            buttons = _offered_targets(context, "click", questions)
            assert {row["target"] for row in buttons} == set(context["candidates"]["click"])
            matching = [option for option in buttons if option["label"] == "Search articles"]
            assert len(matching) == 1
            selection = matching[0]["selection"]
        else:
            for group in ("click", "navigate"):
                options = _offered_targets(context, group, questions)
                offered_domains[group] = {option["target"] for option in options}
                assert offered_domains[group] == set(context["candidates"][group])
                grounded_links[group] = [option["evidence"] for option in options]
            # This mock is a model substitute, not a production target filter.
            # Both excluded and desired links must reach it in the offered domain.
            matching = [option for option in _offered_targets(context, "navigate", questions)
                        if option["label"] == "Gödel's incompleteness theorems"]
            assert len(matching) == 1
            selection = matching[0]["selection"]
        return httpx.Response(200, json=_answer(body, selection))

    provider = OpenAIProvider("test-only-no-paid-credential", transport=httpx.MockTransport(handle))
    with ScenarioSite() as site:
        url = site.start("article")
        manager = RunManager(lambda: provider, admission_mode="task_scope")
        request = TaskRequest(
            goal=goal, start_url=url, values=spec.values, checks=spec.checks(url),
            mode="isolated", visual=False, privacy=PrivacyPolicy(),
            limits=Limits(max_seconds=25, max_steps=8, max_provider_calls=8),
        )
        try:
            task_id = manager.start_task(request)
            assert manager._run is not None and manager._run.task is not None
            task = manager._run.task
            try:
                async with asyncio.timeout(30):
                    await task
            finally:
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            result = manager.get_task(task_id)
        finally:
            await manager.aclose()
            await provider.aclose()
        oracle = site.verify("article")

    expected_labels = ("Kurt Gödel biography", "Gödel's incompleteness theorems",
                       "Gödel's completeness theorem")
    assert set(grounded_links) == {"click", "navigate"}, result
    for group, descriptions in grounded_links.items():
        assert all(any(label in description for description in descriptions) for label in expected_labels), (
            group, descriptions,
        )
    instructions = " ".join(question["instructions"] for question in requests[-1]["questions"]).lower()
    assert "description" in instructions and "untrusted" in instructions
    assert (result.terminal, result.functional) == ("verified", "verified"), result
    assert result.usage.calls == len(requests) == 3
    assert oracle["success"] and oracle["observed"] == list(spec.expected_events)
    assert len(oracle["observed"]) == 2


def test_bound_description_fixture_parses_only_typed_data():
    data = {"operation": "fill", "target": "1", "evidence": '\"Search query\" (\"textbox\")',
            "operation_hint": "Observed hint"}
    assert _bound_description({"value": "action_fixture", "description": json.dumps(data)}) == data


@pytest.mark.parametrize("description", [
    "fill on target 1: Search query", "[]", "null", "{}",
    json.dumps({"operation": "fill", "target": 1, "evidence": '\"Search query\"'}),
    json.dumps({"operation": "fill", "target": "1", "evidence": '\"Search query\"', "instructions": "invented"}),
    json.dumps({"operation": "fill", "target": "1", "evidence": '\"Search query\"', "operation_hint": False}),
])
def test_bound_description_fixture_rejects_malformed_or_unknown_shapes(description):
    with pytest.raises((ValueError, AssertionError)):
        _bound_description({"value": "action_fixture", "description": description})


@pytest.mark.parametrize("description,label", [
    ('\"Search query\" (\"textbox\"); fill=differs', "Search query"),
    ('Observed link \"Gödel\\\"s paper\" -> \"/paper\"', 'Gödel"s paper'),
])
def test_evidence_label_fixture_handles_quoted_observed_data(description, label):
    assert _evidence_label(description) == label


@pytest.mark.parametrize("description", [
    "Search query", "Observed link invalid", "null", "42", '\"unterminated',
    '\"Search query\" unrecognized instructions', 'Observed link \"Article\" missing-arrow',
])
def test_evidence_label_fixture_rejects_unknown_renderings(description):
    with pytest.raises((ValueError, AssertionError)):
        _evidence_label(description)


def test_fixture_label_must_match_the_same_offered_context_alias():
    context = {"candidates": {"click": {"1": {"name": "Observed control"}}}}
    questions = {"target_click": {"choices": [{"value": "1", "description": '\"Invented control\"'}]}}
    with pytest.raises(AssertionError):
        _name(context, "click", "1", questions)
