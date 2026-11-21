"""Browser-free scripted binding/sequence evidence; no native or live acceptance."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.decision_codec import Decision, compile_questions
from decision_browser.dom import (
    Action,
    Candidate,
    DispatchResult,
    GuardResult,
    Observation,
)
from decision_browser.runner import DevelopmentConfig, RunManager

ORIGIN = "http://127.0.0.1:12345"
VALUE = "Καλημέρα, 世界 🌱"


def candidate(token, operation, name, **node):
    semantic = {
        "operation": operation,
        "origin": ORIGIN,
        "url": ORIGIN + "/",
        "node": {"name": name, **node},
        "form": None,
    }
    return Candidate(token, (operation,), {operation: semantic}, None)


def observation(candidates, version="initial"):
    return Observation(version, ORIGIN, "document", "", candidates, ())


def manager(values=None):
    host = RunManager(
        lambda: None,
        development_config=DevelopmentConfig(
            ORIGIN,
            frozenset({("click", "Fresh choice")}),
        ),
    )
    run = host._open(
        TaskRequest(
            goal="Exercise the owned control fixture.",
            start_url=ORIGIN + "/",
            values={} if values is None else values,
            checks=[],
            privacy=PrivacyPolicy(),
            limits=Limits(max_seconds=10),
        )
    )
    return host, run


class ChoosingProvider:
    def __init__(self, operation, predicate):
        self.operation, self.predicate = operation, predicate

    async def decide(self, questions, input_text, **kwargs):
        compile_questions(questions, input_text)
        matches = [
            (group, key)
            for group, bag in json.loads(input_text)["candidates"].items()
            for key, value in bag.items()
            if value["operation"] == self.operation and self.predicate(value)
        ]
        assert len(matches) == 1
        group, key = matches[0]
        assert key in next(
            q.choices for q in questions if q.name == kwargs["target_heads"][group]
        )
        return Decision(group, key, 1.0)

    async def generate_text(self, *_args, **_kwargs):
        raise AssertionError("Explicit values must not invoke the text helper")


class RecordingDOM:
    def __init__(self):
        self.prepared, self.guarded = [], []

    async def prepare(self, operation, token, *, value, destination=None):
        action = Action(operation, token, value, destination, "initial", "{}")
        self.prepared.append(action)
        return action

    async def guard(self, action):
        self.guarded.append(action)
        return GuardResult("pass", "")


@pytest.mark.parametrize(
    "operation, predicate, expected",
    [
        ("fill", lambda d: d["name"] == "Unicode field", ("field-b", VALUE, None)),
        (
            "select",
            lambda d: (
                d["name"] == "Target menu"
                and d["node"]["option"]["label"] == "Violet label"
            ),
            ("menu-b", "violet-code", None),
        ),
        (
            "navigate",
            lambda d: d["destination"] == ORIGIN + "/right",
            ("link-b", None, ORIGIN + "/right"),
        ),
        ("scroll", lambda d: d["name"] == "down", (None, "down", None)),
        ("scroll", lambda d: d["name"] == "up", (None, "up", None)),
        ("wait", lambda d: d["name"] == "short", (None, 100, None)),
    ],
)
async def test_semantic_choice_binds_exact_guarded_action(
    operation, predicate, expected
):
    candidates = {
        "field-a": candidate("field-a", "fill", "First field"),
        "field-b": candidate("field-b", "fill", "Unicode field"),
        "menu-a": candidate(
            "menu-a",
            "select",
            "First menu",
            options=[
                ["first", "First label", False],
                ["other", "Other label", False],
            ],
        ),
        "menu-b": candidate(
            "menu-b",
            "select",
            "Target menu",
            options=[
                ["seed-code", "Initial label", False],
                ["violet-code", "Violet label", False],
            ],
        ),
        "link-a": candidate("link-a", "click", "Details", href=ORIGIN + "/left"),
        "link-b": candidate("link-b", "click", "Details", href=ORIGIN + "/right"),
    }
    host, run = manager({"First field": "wrong field", "Unicode field": VALUE})
    run.provider = ChoosingProvider(operation, predicate)
    run.dom = dom = RecordingDOM()
    action = await host._choose(run, observation(candidates))
    assert action.operation == operation
    assert (action.token, action.value, action.destination) == expected
    assert dom.prepared == dom.guarded == [action]


@pytest.mark.parametrize("supplied", ["Violet label", "violet-code"])
async def test_requested_select_label_or_value_binds_underlying_value(supplied):
    host, run = manager({"Target menu": supplied})
    item = candidate(
        "menu",
        "select",
        "Target menu",
        options=[
            ["seed-code", "Initial label", False],
            ["violet-code", "Violet label", False],
        ],
    )
    run.provider = ChoosingProvider("select", lambda d: d["name"] == "Target menu")
    run.dom = RecordingDOM()
    action = await host._choose(run, observation({"menu": item}))
    assert (action.token, action.value) == ("menu", "violet-code")


@pytest.mark.parametrize(
    "key, label",
    [
        ("query", "Search query"),
        ("QUERY", "Search Query"),
        ("first_name", "Your first name"),
        ("имя", "Ваше имя"),
    ],
)
async def test_supplied_value_matches_unique_whole_word_field_label(key, label):
    host, run = manager({key: VALUE})
    item = candidate("field", "fill", label)
    run.provider = ChoosingProvider("fill", lambda d: d["name"] == label)
    run.dom = RecordingDOM()
    action = await host._choose(run, observation({"field": item}))
    assert (action.token, action.value) == ("field", VALUE)
    assert run.dom.prepared == run.dom.guarded == [action]


async def test_exact_supplied_field_label_takes_priority_over_word_match():
    host, run = manager({"query": "wrong", "Search query": VALUE})
    item = candidate("field", "fill", "Search query")
    run.provider = ChoosingProvider("fill", lambda d: True)
    run.dom = RecordingDOM()
    action = await host._choose(run, observation({"field": item}))
    assert action.value == VALUE


@pytest.mark.parametrize(
    "values, labels",
    [
        ({"name": VALUE}, ["First name", "Last name"]),
        ({"query": VALUE, "search": "other"}, ["Search query"]),
        ({"query": VALUE, "QUERY": "other"}, ["Search query"]),
        ({"query": VALUE}, ["Search query", "Search query"]),
        ({"query": VALUE}, ["Inquiry"]),
        ({"unrelated": VALUE}, ["Search query"]),
    ],
)
async def test_ambiguous_or_unrelated_supplied_values_keep_text_helper(values, labels):
    host, run = manager(values)
    items = {str(i): candidate(str(i), "fill", label) for i, label in enumerate(labels)}

    class HelperProvider:
        helper_calls = 0

        async def decide(self, questions, input_text, **kwargs):
            compile_questions(questions, input_text)
            head = kwargs["target_heads"]["fill"]
            assert "0" in next(q.choices for q in questions if q.name == head)
            return Decision("fill", "0", 1.0)

        async def generate_text(self, *_args, **_kwargs):
            self.helper_calls += 1
            return "from goal"

    provider = HelperProvider()
    run.provider = provider
    run.dom = RecordingDOM()
    action = await host._choose(run, observation(items))
    assert provider.helper_calls == 1
    assert action.value == "from goal"


async def test_stale_loop_reobserves_before_fresh_choice_and_never_replays():
    """Real _loop/_choose/_dispatch_one/policy/journal with explicit DOM doubles."""
    host, run = manager()
    host._set(run, terminal="running")
    sequence = []

    class MutableDOM:
        def __init__(self):
            self.version = "old"
            self.effects = 0
            self.dispatched = []

        async def observe(self):
            sequence.append("observe:" + self.version)
            name = "Old choice" if self.version == "old" else "Fresh choice"
            item = candidate(self.version, "click", name)
            self.obs = observation({item.token: item}, self.version)
            return self.obs

        async def prepare(self, operation, token, *, value, destination):
            semantic = self.obs.candidates[token].semantic_by_operation[operation]
            return Action(
                operation,
                token,
                value,
                destination,
                self.obs.id,
                json.dumps({"semantic": semantic}),
            )

        async def guard(self, action):
            status = "pass" if action.observation_id == self.version else "stale"
            sequence.append("guard:" + status)
            return GuardResult(status, "")

        async def dispatch(self, action, before):
            verdict = await self.guard(action)
            if verdict.status != "pass":
                return DispatchResult(verdict.status, "stale")
            before(action)
            sequence.append("dispatch:" + action.token)
            self.effects += 1
            self.dispatched.append(action)
            return DispatchResult("pass", "")

    class MutatingProvider:
        calls = 0

        async def decide(self, questions, input_text, **kwargs):
            kwargs["budget"].reserve_provider(len(input_text.encode()))
            kwargs["budget"].consume_output(1)
            self.calls += 1
            if self.calls == 3:
                return Decision("DONE", None, 1.0)
            expected = "Old choice" if self.calls == 1 else "Fresh choice"
            assert dom.effects == 0  # First choice must have had zero effect.
            sequence.append("decide:" + expected)
            chosen = await ChoosingProvider(
                "click", lambda d: d["name"] == expected
            ).decide(
                questions,
                input_text,
                **kwargs,
            )
            dom.version = "fresh"  # Mutate after choosing against the old observation.
            return chosen

    run.dom = dom = MutableDOM()
    run.provider = provider = MutatingProvider()
    run.driver = SimpleNamespace(page=SimpleNamespace(url=ORIGIN + "/"))
    await host._loop(run, await dom.observe())
    assert sequence == [
        "observe:old",
        "decide:Old choice",
        "guard:stale",
        "observe:fresh",
        "decide:Fresh choice",
        "guard:pass",
        "guard:pass",
        "dispatch:fresh",
        "observe:fresh",
    ]
    assert dom.effects == len(dom.dispatched) == 1
    assert dom.dispatched[0].observation_id == "fresh"
    assert provider.calls == run.result.usage.calls == 3
    assert run.result.usage.steps == 1  # This unit seam starts after bootstrap.
    assert run.result.terminal == "completed_unverified"
    assert run.result.functional == "unverified" and run.result.development_only is True
    events = [
        json.loads(run.store.read(key).content) for key in run.result.artifact_ids
    ]
    assert sum(event.get("event") == "before_dispatch" for event in events) == 1
    assert (
        sum(
            event.get("event") == "outcome" and event["status"] == "acknowledged"
            for event in events
        )
        == 1
    )
