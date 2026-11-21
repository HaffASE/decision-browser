"""Offline tests for the public Decisions codec."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import cast

import pytest

from decision_browser.decision_codec import (
    ChoiceQuestion,
    CodecError,
    Decision,
    compile_questions,
    parse_decision,
)

OPS = ChoiceQuestion("operation", "Choose operation", ("launch", "DONE"))
TARGET = ChoiceQuestion("target", "Choose target", ("moon", "mars"))
FLAG = ChoiceQuestion("flag", "Choose flag", (True, False))
WORD = ChoiceQuestion("word", "Choose word", ("true", True))
QUESTIONS = (OPS, TARGET, FLAG)
HEADS = {"launch": "target"}


def _answer(
    name: str,
    choice: object,
    confidence: object,
    rows: list[tuple[object, float]],
) -> dict[str, object]:
    return {
        "type": "choice",
        "name": name,
        "choice": choice,
        "confidence": confidence,
        "probabilities": [
            {"value": value, "probability": prob} for value, prob in rows
        ],
    }


def _ok(answers: Sequence[object]) -> dict[str, object]:
    usage: dict[str, object] = {"total_tokens": 3, "detail": {"cached": False}}
    return {"model": "gpt-6-luna", "answers": list(answers), "usage": usage}


def _ops(choice: str = "launch", confidence: float = 0.75) -> dict[str, object]:
    other = "DONE" if choice == "launch" else "launch"
    return _answer(
        "operation",
        choice,
        confidence,
        [(choice, confidence), (other, 1.0 - confidence)],
    )


def _target(choice: str = "moon", confidence: float = 0.6) -> dict[str, object]:
    other = "mars" if choice == "moon" else "moon"
    return _answer(
        "target",
        choice,
        confidence,
        [(choice, confidence), (other, 1.0 - confidence)],
    )


def _flag() -> dict[str, object]:
    return _answer("flag", True, 0.9, [(True, 0.9), (False, 0.1)])


def _reason(call: Callable[[], object]) -> str:
    with pytest.raises(CodecError) as caught:
        call()
    return caught.value.reason


def test_compile_official_shape() -> None:
    body = compile_questions((OPS, FLAG), "flight plan")
    assert body == {
        "model": "gpt-6-luna",
        "input": "flight plan",
        "questions": [
            {
                "type": "choice",
                "name": "operation",
                "instructions": "Choose operation",
                "choices": [{"value": "launch"}, {"value": "DONE"}],
            },
            {
                "type": "choice",
                "name": "flag",
                "instructions": "Choose flag",
                "choices": [{"value": True}, {"value": False}],
            },
        ],
    }
    questions = cast(list[dict[str, object]], body["questions"])
    choices = cast(list[dict[str, object]], questions[1]["choices"])
    assert choices[0]["value"] is True


def test_constructors_and_request_heads() -> None:
    assert _reason(lambda: ChoiceQuestion("", "x", ("a",))) == "invalid_name"
    assert _reason(lambda: ChoiceQuestion(cast(str, 1), "x", ("a",))) == "invalid_name"
    bad_notes = cast(str, None)
    assert (
        _reason(lambda: ChoiceQuestion("n", bad_notes, ("a",)))
        == "invalid_instructions"
    )
    assert _reason(lambda: ChoiceQuestion("n", "x", ())) == "empty_choices"
    assert _reason(lambda: ChoiceQuestion("n", "x", ("a", "a"))) == "duplicate_choices"
    bad_int = cast(tuple[str | bool, ...], (1, False))
    assert _reason(lambda: ChoiceQuestion("n", "x", bad_int)) == "invalid_choice"
    bad_seq = cast(tuple[str | bool, ...], ["a"])
    assert _reason(lambda: ChoiceQuestion("n", "x", bad_seq)) == "invalid_choices"
    assert (
        _reason(lambda: compile_questions((OPS, OPS), "i"))
        == "duplicate_question_names"
    )
    alien = cast(Sequence[ChoiceQuestion], (OPS, "nope"))
    assert _reason(lambda: compile_questions(alien, "i")) == "unsupported_question"
    assert _reason(lambda: compile_questions((OPS,), cast(str, 4))) == "invalid_input"


def test_relevant_decision_refusal_and_done() -> None:
    parsed = parse_decision(
        QUESTIONS, _ok([_ops(), _target(), _flag()]), target_heads=HEADS
    )
    assert parsed == Decision("launch", "moon", 0.6)
    done_answers: list[object] = [
        _ops("DONE", 0.8),
        {"name": "target", "choice": 1},
        {"name": "flag"},
    ]
    done = parse_decision(QUESTIONS, _ok(done_answers), target_heads=HEADS)
    assert done == Decision("DONE", None, 0.8)
    assert parse_decision(QUESTIONS, _ok(done_answers)).target is None
    plain = _ok([_ops(), _target(), _flag()])
    assert _reason(lambda: parse_decision(QUESTIONS, plain)) == "missing_target"
    refused = _ok([{"type": "refusal", "name": "operation"}, _target(), _flag()])
    assert (
        _reason(lambda: parse_decision(QUESTIONS, refused, target_heads=HEADS))
        == "refusal"
    )


def test_names_order_unused_and_bool_string() -> None:
    swapped = _ok([_target(), _ops(), _flag()])
    assert (
        _reason(lambda: parse_decision(QUESTIONS, swapped, target_heads=HEADS))
        == "answer_name"
    )
    unused = _ok([_ops(), _target(), {"name": "flag", "probabilities": "bad"}])
    assert parse_decision(QUESTIONS, unused, target_heads=HEADS).target == "moon"
    short = _ok([_ops(), _target()])
    assert (
        _reason(lambda: parse_decision(QUESTIONS, short, target_heads=HEADS))
        == "missing_answer"
    )
    long = _ok([_ops(), _target(), _flag(), {"name": "extra"}])
    assert (
        _reason(lambda: parse_decision(QUESTIONS, long, target_heads=HEADS))
        == "extra_answer"
    )
    heads = {"launch": "word"}
    text = _answer("word", "true", 0.7, [("true", 0.7), (True, 0.3)])
    flag = _answer("word", True, 0.7, [("true", 0.3), (True, 0.7)])
    assert (
        parse_decision((OPS, WORD), _ok([_ops(), text]), target_heads=heads).target
        == "true"
    )
    assert (
        parse_decision((OPS, WORD), _ok([_ops(), flag]), target_heads=heads).target
        is True
    )
    numeric = _answer("word", 1, 1.0, [(1, 1.0)])
    parsed = lambda: parse_decision(
        (OPS, WORD), _ok([_ops(), numeric]), target_heads=heads
    )
    assert _reason(parsed) == "type_mismatch"


def _bad_target(answer: dict[str, object]) -> str:
    payload = _ok([_ops(), answer, _flag()])
    return _reason(lambda: parse_decision(QUESTIONS, payload, target_heads=HEADS))


def test_distribution_duplicates_and_numeric_bounds() -> None:
    assert (
        _bad_target(_answer("target", "moon", 0.5, [("moon", 0.5)]))
        == "incomplete_distribution"
    )
    dup = _answer("target", "moon", 0.5, [("moon", 0.5), ("moon", 0.5)])
    assert _bad_target(dup) == "duplicates"
    assert (
        _bad_target(_answer("target", "moon", math.nan, [("moon", 0.5), ("mars", 0.5)]))
        == "confidence"
    )
    assert (
        _bad_target(_answer("target", "moon", math.inf, [("moon", 0.5), ("mars", 0.5)]))
        == "confidence"
    )
    assert (
        _bad_target(_answer("target", "moon", -0.1, [("moon", 0.5), ("mars", 0.5)]))
        == "confidence"
    )
    assert (
        _bad_target(_answer("target", "moon", 1.2, [("moon", 0.5), ("mars", 0.5)]))
        == "confidence"
    )
    assert (
        _bad_target(_answer("target", "moon", True, [("moon", 0.5), ("mars", 0.5)]))
        == "confidence"
    )
    nan_row = _answer("target", "moon", 0.5, [("moon", math.nan), ("mars", 0.5)])
    assert _bad_target(nan_row) == "probability_range"
    inf_row = _answer("target", "moon", 0.5, [("moon", math.inf), ("mars", 0.5)])
    assert _bad_target(inf_row) == "probability_range"
    neg_row = _answer("target", "moon", 0.5, [("moon", -0.1), ("mars", 0.5)])
    assert _bad_target(neg_row) == "probability_range"
    high_row = _answer("target", "moon", 0.5, [("moon", 1.1), ("mars", 0.5)])
    assert _bad_target(high_row) == "probability_range"
    low = _answer("target", "moon", 0.4, [("moon", 0.4), ("mars", 0.6)])
    assert parse_decision(
        QUESTIONS, _ok([_ops(), low, _flag()]), target_heads=HEADS
    ) == Decision("launch", "moon", 0.4)
    off = _answer("target", "moon", 0.5, [("moon", 0.7), ("mars", 0.3)])
    raw_ops = _answer(
        "operation", "launch", 0.5, [("launch", 0.7), ("DONE", 0.3)]
    )
    accepted = parse_decision(
        QUESTIONS, _ok([raw_ops, off, _flag()]), target_heads=HEADS
    )
    assert accepted == Decision("launch", "moon", 0.5)
    for edge in (0.0, 1.0):
        edged = _answer(
            "operation", "launch", edge, [("launch", 0.7), ("DONE", 0.3)]
        )
        assert parse_decision(
            QUESTIONS, _ok([edged, off, _flag()]), target_heads=HEADS
        ) == Decision("launch", "moon", min(edge, 0.5))
    reason = _bad_target(
        _answer("target", "pluto", 1.0, [("moon", 0.0), ("mars", 1.0)])
    )
    assert reason == "unknown_choice"
    assert "pluto" not in reason


@pytest.mark.parametrize("probability", [0.333, 0.333334])
def test_documented_choice_fields_with_rounded_statistics(probability) -> None:
    target = ChoiceQuestion("target", "Choose target", ("moon", "mars", "venus"))
    operation = _answer("operation", "launch", 0.93, [("launch", 0.1), ("DONE", 0.9)])
    answer = _answer(
        "target", "moon", 0.55,
        [(value, probability) for value in target.choices],
    )
    # Contract-shaped synthetic responses; no claim the service emits these values.
    response = _ok([operation, answer])
    response["usage"] = {
        "input_tokens": 42,
        "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
        "output_tokens": 0, "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 42,
    }
    assert parse_decision((OPS, target), response, target_heads=HEADS) == Decision(
        "launch", "moon", 0.55
    )


def test_typed_choice_is_not_replaced_by_probability_winner() -> None:
    operation = _answer("operation", "launch", 0.0, [("launch", 0.1), ("DONE", 0.9)])
    target = _answer("word", "true", 1.0, [("true", 0.2), (True, 0.8)])
    assert parse_decision(
        (OPS, WORD), _ok([operation, target]), target_heads={"launch": "word"}
    ) == Decision("launch", "true", 0.0)


@pytest.mark.parametrize("field,value,reason", [
    ("choice", "CANARY_UNKNOWN", "unknown_choice"),
    ("choice", 1, "type_mismatch"),
    ("confidence", True, "confidence"),
    ("confidence", math.nan, "confidence"),
    ("probabilities", [], "incomplete_distribution"),
    ("type", "score", "invalid_answer"),
    ("type", "refusal", "refusal"),
    ("name", "other_operation_target", "answer_name"),
    ("unrecognized", "CANARY_UNKNOWN", "invalid_answer"),
])
def test_nonmax_statistics_do_not_bypass_selected_answer_guards(field, value, reason) -> None:
    target = _answer("target", "moon", 0.8, [("moon", 0.1), ("mars", 0.9)])
    target[field] = value
    assert _bad_target(target) == reason


@pytest.mark.parametrize("value,reason", [
    (True, "probability_range"), (-0.1, "probability_range"),
    (1.1, "probability_range"), (math.inf, "probability_range"),
    (10**1000, "probability_range"),
])
def test_rounded_statistics_still_require_each_valid_probability(value, reason) -> None:
    answer = _answer("target", "moon", 0.8, [("moon", value), ("mars", 0.333)])
    assert _bad_target(answer) == reason


def test_mapping_and_schema_heads() -> None:
    ok = _ok([_ops(), _target(), _flag()])
    assert (
        _reason(lambda: parse_decision(QUESTIONS, ok, operation_head="missing"))
        == "invalid_mapping"
    )
    missing_head = {"launch": "missing"}
    assert (
        _reason(lambda: parse_decision(QUESTIONS, ok, target_heads=missing_head))
        == "invalid_mapping"
    )
    assert (
        _reason(lambda: parse_decision(QUESTIONS, ok, target_heads={"nope": "target"}))
        == "invalid_mapping"
    )
    same = {"launch": "operation"}
    assert (
        _reason(lambda: parse_decision(QUESTIONS, ok, target_heads=same))
        == "invalid_mapping"
    )
    missing = {"model": "gpt-6-luna", "answers": []}
    assert (
        _reason(lambda: parse_decision(QUESTIONS, missing, target_heads=HEADS))
        == "invalid_response"
    )
    extra = dict(ok)
    extra["note"] = "leak"
    assert (
        _reason(lambda: parse_decision(QUESTIONS, extra, target_heads=HEADS))
        == "invalid_response"
    )
    renamed = dict(ok)
    renamed["model"] = "gpt-other"
    assert (
        _reason(lambda: parse_decision(QUESTIONS, renamed, target_heads=HEADS))
        == "invalid_model"
    )
    usage = dict(ok)
    usage["usage"] = ["tokens"]
    assert (
        _reason(lambda: parse_decision(QUESTIONS, usage, target_heads=HEADS))
        == "invalid_usage"
    )
    richer = ChoiceQuestion("operation", "Choose operation", ("launch", "hold", "DONE"))
    hold = _answer(
        "operation", "hold", 0.5, [("hold", 0.5), ("launch", 0.25), ("DONE", 0.25)]
    )
    held = lambda: parse_decision(
        (richer, TARGET), _ok([hold, _target()]), target_heads=HEADS
    )
    assert _reason(held) == "missing_target"
    flagged = ChoiceQuestion("operation", "op", (True, False))
    picked = _answer("operation", True, 0.8, [(True, 0.8), (False, 0.2)])
    assert (
        _reason(lambda: parse_decision((flagged,), _ok([picked])))
        == "invalid_operation"
    )


def test_enormous_number_is_safe():
    import math

    import pytest

    import decision_browser.decision_codec as codec

    invalid = (10**1000, -(10**1000), True, False, math.nan, math.inf, -math.inf)
    for value in invalid:
        with pytest.raises(codec.CodecError) as caught:
            codec._unit(value, "confidence")
        assert caught.value.reason == "confidence"
    assert codec._unit(0, "confidence") == 0.0
    assert codec._unit(1, "confidence") == 1.0
    assert codec._unit(0.0, "confidence") == 0.0
    assert codec._unit(1.0, "confidence") == 1.0


def _wire_values(question: ChoiceQuestion) -> list[object]:
    body = compile_questions((question,), "plan")
    questions = cast(list[dict[str, object]], body["questions"])
    choices = cast(list[dict[str, object]], questions[0]["choices"])
    return [item["value"] for item in choices]


def test_singleton_string_sentinel_and_multi_unchanged() -> None:
    single = ChoiceQuestion("operation", "choose", ("DONE",))
    assert _wire_values(single) == ["DONE", False]
    assert _wire_values(single)[1] is False
    heads = {"launch": "target"}
    for word in ("false", "False", "true", "True"):
        question = ChoiceQuestion("target", "choose", (word,))
        values = _wire_values(question)
        assert values == [word, False]
        assert type(values[1]) is bool and values[1] is False
        parsed = parse_decision(
            (OPS, question),
            _ok(
                [
                    _ops(),
                    _answer("target", word, 1.0, [(word, 1.0), (False, 0.0)]),
                ]
            ),
            target_heads=heads,
        )
        assert parsed.operation == "launch"
        assert parsed.target == word and type(parsed.target) is str
    assert _wire_values(ChoiceQuestion("flag", "f", (True,))) == [True, False]
    assert _wire_values(ChoiceQuestion("flag", "f", (False,))) == [False, True]
    assert compile_questions((OPS, FLAG), "flight plan") == {
        "model": "gpt-6-luna",
        "input": "flight plan",
        "questions": [
            {
                "type": "choice",
                "name": "operation",
                "instructions": "Choose operation",
                "choices": [{"value": "launch"}, {"value": "DONE"}],
            },
            {
                "type": "choice",
                "name": "flag",
                "instructions": "Choose flag",
                "choices": [{"value": True}, {"value": False}],
            },
        ],
    }


def test_sentinel_rejected_after_full_distribution_checks() -> None:
    single = ChoiceQuestion("operation", "choose", ("DONE",))
    picked = _answer("operation", False, 1.0, [("DONE", 0.0), (False, 1.0)])
    assert _reason(lambda: parse_decision((single,), _ok([picked]))) == "rejected_sentinel"
    rounded = _answer("operation", False, 0.4, [("DONE", 0.333), (False, 0.666)])
    assert (
        _reason(lambda: parse_decision((single,), _ok([rounded])))
        == "rejected_sentinel"
    )
    incomplete = _answer("operation", "DONE", 1.0, [("DONE", 1.0)])
    assert (
        _reason(lambda: parse_decision((single,), _ok([incomplete])))
        == "incomplete_distribution"
    )
    nonmax = _answer("operation", "DONE", 0.2, [("DONE", 0.2), (False, 0.8)])
    assert parse_decision((single,), _ok([nonmax])) == Decision("DONE", None, 0.2)
    disagree = _answer("operation", False, 0.2, [("DONE", 0.0), (False, 1.0)])
    assert (
        _reason(lambda: parse_decision((single,), _ok([disagree])))
        == "rejected_sentinel"
    )
    duplicate = _answer(
        "operation", "DONE", 1.0, [("DONE", 1.0), ("DONE", 0.0), (False, 0.0)]
    )
    assert _reason(lambda: parse_decision((single,), _ok([duplicate]))) == "duplicates"
    unknown = _answer("operation", "mars", 1.0, [("DONE", 1.0), (False, 0.0)])
    assert _reason(lambda: parse_decision((single,), _ok([unknown]))) == "unknown_choice"
    refused = _ok([{"type": "refusal", "name": "operation"}])
    assert _reason(lambda: parse_decision((single,), refused)) == "refusal"
    done = parse_decision(
        (single,),
        _ok([_answer("operation", "DONE", 1.0, [("DONE", 1.0), (False, 0.0)])]),
    )
    assert done == Decision("DONE", None, 1.0)


def test_singleton_target_domain_and_choice_bounds() -> None:
    moon = ChoiceQuestion("target", "t", ("moon",))
    ignored = parse_decision(
        (OPS, moon),
        _ok([_ops("DONE", 0.8), {"name": "target", "choice": False}]),
        target_heads=HEADS,
    )
    assert ignored == Decision("DONE", None, 0.8)
    selected = parse_decision(
        (OPS, moon),
        _ok(
            [
                _ops(),
                _answer("target", "moon", 0.7, [("moon", 0.7), (False, 0.3)]),
            ]
        ),
        target_heads=HEADS,
    )
    assert selected == Decision("launch", "moon", 0.7)
    sentinel = _answer("target", False, 1.0, [("moon", 0.0), (False, 1.0)])
    assert (
        _reason(
            lambda: parse_decision(
                (OPS, moon), _ok([_ops(), sentinel]), target_heads=HEADS
            )
        )
        == "rejected_sentinel"
    )
    yes = ChoiceQuestion("flag", "f", (True,))
    no = ChoiceQuestion("flag", "f", (False,))
    heads = {"launch": "flag"}
    assert (
        parse_decision(
            (OPS, yes),
            _ok([_ops(), _answer("flag", True, 1.0, [(True, 1.0), (False, 0.0)])]),
            target_heads=heads,
        ).target
        is True
    )
    assert (
        parse_decision(
            (OPS, no),
            _ok([_ops(), _answer("flag", False, 1.0, [(False, 1.0), (True, 0.0)])]),
            target_heads=heads,
        ).target
        is False
    )
    added = _answer("flag", True, 1.0, [(False, 0.0), (True, 1.0)])
    assert (
        _reason(
            lambda: parse_decision(
                (OPS, no), _ok([_ops(), added]), target_heads=heads
            )
        )
        == "rejected_sentinel"
    )
    wide = ("DONE",) + tuple(f"c{index}" for index in range(254))
    assert len(wide) == 255
    assert len(_wire_values(ChoiceQuestion("operation", "choose", wide))) == 255
    wide_probs = [("DONE", 1.0), *[(name, 0.0) for name in wide[1:]]]
    assert parse_decision(
        (ChoiceQuestion("operation", "choose", wide),),
        _ok([_answer("operation", "DONE", 1.0, wide_probs)]),
    ) == Decision("DONE", None, 1.0)
    too_many = tuple(f"c{index}" for index in range(256))
    assert (
        _reason(
            lambda: compile_questions(
                (ChoiceQuestion("operation", "choose", too_many),), "plan"
            )
        )
        == "choice_count"
    )


def test_explicit_unable_target_is_preserved_for_custom_operation():
    questions = (ChoiceQuestion("operation", "choose", ("UNABLE", "DONE")),
                 ChoiceQuestion("reason", "choose reason", ("captcha", "login")))
    raw = _ok([_answer("operation", "UNABLE", 1., [("UNABLE", 1.), ("DONE", 0.)]),
               _answer("reason", "captcha", 1., [("captcha", 1.), ("login", 0.)])])
    assert parse_decision(questions, raw, target_heads={"UNABLE": "reason"}) == Decision("UNABLE", "captcha", 1.)
