"""OpenAI Decisions codec. Results are evidence and confer no authority."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

TERMINAL_OPERATIONS = frozenset({"DONE", "UNABLE"})

_MODEL = "gpt-6-luna"
_ANSWER_KEYS = frozenset({"choice", "confidence", "name", "probabilities", "type"})
_PROB_KEYS = frozenset({"probability", "value"})
_TOP_KEYS = frozenset({"answers", "model", "usage"})


class CodecError(Exception):
    """Stable safe reason, with no response payload attached."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _eq(left: object, right: object) -> bool:
    if type(left) is bool and type(right) is bool:
        return left == right
    return type(left) is str and type(right) is str and left == right


def _known(value: object, choices: tuple[str | bool, ...]) -> bool:
    return any(_eq(value, item) for item in choices)


def _wire_choices(choices: tuple[str | bool, ...]) -> tuple[str | bool, ...]:
    """Official Decisions requests require 2-255 choices.

    A singleton source domain stays unchanged. The wire payload adds one
    rejected no-action sentinel: bool False for a string, or the opposite
    bool. False cannot collide with a typed string.
    """
    if len(choices) > 255:
        raise CodecError("choice_count")
    if len(choices) != 1:
        return choices
    only = choices[0]
    sentinel: str | bool = (not only) if type(only) is bool else False
    return (only, sentinel)


def _literal(value: object) -> str | bool:
    if type(value) is str or type(value) is bool:
        return value
    raise CodecError("type_mismatch")


@dataclass(frozen=True, slots=True)
class ChoiceQuestion:
    name: str
    instructions: str
    choices: tuple[str | bool, ...]
    descriptions: tuple[str | None, ...] = ()

    def __post_init__(self) -> None:
        if type(self.name) is not str or self.name == "":
            raise CodecError("invalid_name")
        if type(self.instructions) is not str:
            raise CodecError("invalid_instructions")
        if type(self.choices) is not tuple:
            raise CodecError("invalid_choices")
        if len(self.choices) == 0:
            raise CodecError("empty_choices")
        if (
            type(self.descriptions) is not tuple
            or self.descriptions and len(self.descriptions) != len(self.choices)
            or any(item is not None and type(item) is not str for item in self.descriptions)
        ):
            raise CodecError("invalid_descriptions")
        seen: list[str | bool] = []
        for choice in self.choices:
            bad_type = type(choice) is not str and type(choice) is not bool
            if type(choice) is int or bad_type:
                raise CodecError("invalid_choice")
            if any(_eq(choice, prev) for prev in seen):
                raise CodecError("duplicate_choices")
            seen.append(choice)


@dataclass(frozen=True, slots=True)
class Decision:
    operation: str
    target: str | bool | None
    confidence: float


def _bundle(questions: Sequence[ChoiceQuestion]) -> tuple[ChoiceQuestion, ...]:
    names: list[str] = []
    ready: list[ChoiceQuestion] = []
    for question in questions:
        if type(question) is not ChoiceQuestion:
            raise CodecError("unsupported_question")
        if question.name in names:
            raise CodecError("duplicate_question_names")
        names.append(question.name)
        ready.append(question)
    return tuple(ready)


def compile_questions(
    questions: Sequence[ChoiceQuestion], input_text: str
) -> dict[str, object]:
    if type(input_text) is not str:
        raise CodecError("invalid_input")
    body: list[object] = []
    for question in _bundle(questions):
        choices: list[object] = []
        for index, choice in enumerate(_wire_choices(question.choices)):
            option: dict[str, object] = {"value": choice}
            if question.descriptions and index < len(question.descriptions):
                description = question.descriptions[index]
                if description is not None:
                    option["description"] = description
            choices.append(option)
        body.append(
            {
                "type": "choice",
                "name": question.name,
                "instructions": question.instructions,
                "choices": choices,
            }
        )
    return {"model": _MODEL, "input": input_text, "questions": body}


def _obj(value: object, reason: str) -> dict[str, object]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise CodecError(reason)
    return cast(dict[str, object], value)


def _unit(value: object, reason: str) -> float:
    if type(value) is float:
        if not math.isfinite(value) or value < 0.0 or value > 1.0:
            raise CodecError(reason)
        return value
    if type(value) is int and 0 <= value <= 1:
        return float(value)
    raise CodecError(reason)


def _read(question: ChoiceQuestion, answer: object) -> tuple[str | bool, float]:
    payload = _obj(answer, "invalid_answer")
    if payload.get("type") == "refusal":
        raise CodecError("refusal")
    if set(payload) != _ANSWER_KEYS or payload.get("type") != "choice":
        raise CodecError("invalid_answer")
    if payload.get("name") != question.name:
        raise CodecError("answer_name")
    wire = _wire_choices(question.choices)
    choice = _literal(payload.get("choice"))
    if not _known(choice, wire):
        raise CodecError("unknown_choice")
    confidence = _unit(payload.get("confidence"), "confidence")
    raw = payload.get("probabilities")
    if type(raw) is not list:
        raise CodecError("probabilities")
    found: list[tuple[str | bool, float]] = []
    for item in cast(list[object], raw):
        entry = _obj(item, "probabilities")
        if set(entry) != _PROB_KEYS:
            raise CodecError("probabilities")
        value = _literal(entry.get("value"))
        if not _known(value, wire):
            raise CodecError("unknown_choice")
        if any(_eq(value, seen) for seen, _prob in found):
            raise CodecError("duplicates")
        found.append((value, _unit(entry.get("probability"), "probability_range")))
    if len(found) != len(wire):
        raise CodecError("incomplete_distribution")
    # The API returns choice separately from probabilities/confidence. Do not
    # infer argmax or an undocumented normalization tolerance from those fields.
    if not _known(choice, question.choices):
        raise CodecError("rejected_sentinel")
    return choice, confidence


def _rows(questions: Sequence[ChoiceQuestion], raw: object) -> list[object]:
    if type(raw) is not list:
        raise CodecError("invalid_answers")
    answers = cast(list[object], raw)
    if len(answers) > len(questions):
        raise CodecError("extra_answer")
    if len(answers) < len(questions):
        raise CodecError("missing_answer")
    for question, answer in zip(questions, answers, strict=True):
        if _obj(answer, "answer_name").get("name") != question.name:
            raise CodecError("answer_name")
    return answers


def _operation(
    questions: tuple[ChoiceQuestion, ...],
    operation_head: str,
    target_heads: Mapping[str, str] | None,
) -> ChoiceQuestion:
    names = {question.name: question for question in questions}
    if type(operation_head) is not str or operation_head not in names:
        raise CodecError("invalid_mapping")
    operation = names[operation_head]
    if target_heads is None:
        return operation
    if not isinstance(target_heads, Mapping):
        raise CodecError("invalid_mapping")
    for key, head in target_heads.items():
        key_known = type(key) is str and any(
            type(item) is str and item == key for item in operation.choices
        )
        head_known = type(head) is str and head in names and head != operation_head
        if not key_known or not head_known:
            raise CodecError("invalid_mapping")
    return operation


def _terminal_choice(choice: str, target_heads: Mapping[str, str] | None) -> bool:
    # DONE has always been reserved. Generic callers can define their own UNABLE target.
    return choice == "DONE" or (choice == "UNABLE" and (target_heads is None or choice not in target_heads))


def parse_decision(
    questions: Sequence[ChoiceQuestion],
    raw_response: object,
    *,
    operation_head: str = "operation",
    target_heads: Mapping[str, str] | None = None,
) -> Decision:
    return _parse_decision(questions, raw_response, operation_head=operation_head, target_heads=target_heads,
                           bound_targets={})


def _parse_decision(
    questions: Sequence[ChoiceQuestion], raw_response: object, *,
    operation_head: str, target_heads: Mapping[str, str] | None, bound_targets: Mapping[str, tuple[str, str]],
) -> Decision:
    prepared = _bundle(questions)
    operation = _operation(prepared, operation_head, target_heads)
    response = _obj(raw_response, "invalid_response")
    if set(response) != _TOP_KEYS:
        raise CodecError("invalid_response")
    if response.get("model") != _MODEL:
        raise CodecError("invalid_model")
    if type(response.get("usage")) is not dict:
        raise CodecError("invalid_usage")
    answers = _rows(prepared, response.get("answers"))
    choice, confidence = _read(operation, answers[prepared.index(operation)])
    if type(choice) is not str:
        raise CodecError("invalid_operation")
    if choice in bound_targets:
        group, bound_target = bound_targets[choice]
        return Decision(group, bound_target, confidence)
    if _terminal_choice(choice, target_heads):
        return Decision(choice, None, confidence)
    if target_heads is None or choice not in target_heads:
        raise CodecError("missing_target")
    target_name = target_heads[choice]
    target_q = next(question for question in prepared if question.name == target_name)
    target, target_confidence = _read(target_q, answers[prepared.index(target_q)])
    return Decision(choice, target, min(confidence, target_confidence))
