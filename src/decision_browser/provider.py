"""OpenAI HTTPS provider. Privacy and budget run before any socket."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace

import httpx

from .budget import Budget, BudgetExceeded
from .decision_codec import (
    ChoiceQuestion,
    CodecError,
    Decision,
    _bundle,
    _operation,
    _parse_decision,
    _terminal_choice,
    compile_questions,
)
from .model_context import bind_openai_singletons, prepare_context
from .openai_actions import bind_complete_actions
from .openai_context import action_evidence
from .privacy import CaptureEvidence, PrivacyError, PrivacyGate
from .provider_config import ProviderConfigError, validate_api_key
from .provider_http import ProviderError, post_json
from .provider_http import load_json as _loads
from .verification import ModelVisualReport, validate_visual_response

__all__ = ["OpenAIProvider", "ProviderError"]

_ORIGIN = "https://api.openai.com"
_PATHS = frozenset({"/v1/decisions", "/v1/responses"})


_NATIVE_OPERATIONS = {
    "UNABLE": (
        "Stop only when no offered safe operation and target can advance the remaining goal. "
        "A long goal need not be completed in one action or on the current page. "
        "An observed link or editable control supporting the next step remains useful."
    ),
    "click": (
        "Click an observed control or link that advances the next unfinished requirement, "
        "including opening the requested result or the next page of a multi-step workflow. "
        "Use fill for an editable text field. A click only focuses that field. "
        "Use acknowledged history: change the target or operation after a click leaves the page unchanged."
    ),
    "fill": (
        "Fill the editable field whose requested value is still missing or differs. "
        "A matching supplied value needs no further fill or focus click."
    ),
    "navigate": (
        "Follow the offered destination that advances the next unfinished requirement. "
        "Use the goal, visible link details and previous_pages to select the exact requested result."
    ),
}


# JSON Schema maxLength counts Unicode characters, matching ModelFinding.
_TEXT = {"type": "string", "minLength": 1, "maxLength": 1024}
_DIM = {"type": "integer", "minimum": 1, "maximum": 20000}
_SPAN = {"type": "number", "exclusiveMinimum": 0}
_VISUAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "maxItems": 128,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "origin",
                    "evidence",
                    "element",
                    "viewport",
                    "region",
                    "severity",
                    "uncertainty",
                ],
                "properties": {
                    "origin": {"type": "string", "enum": ["model"]},
                    "evidence": _TEXT,
                    "element": _TEXT,
                    "viewport": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["width", "height"],
                        "properties": {"width": _DIM, "height": _DIM},
                    },
                    "region": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["x", "y", "width", "height"],
                        "properties": {
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                            "width": _SPAN,
                            "height": _SPAN,
                        },
                    },
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "uncertainty": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        },
    },
}

_TEXT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}
_INCOMPLETE = ("incomplete", "failed", "cancelled", "canceled", "queued", "in_progress")


def _token(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProviderError("malformed")
    return value


def _text_output(raw: dict[str, object]) -> str:
    if not isinstance(raw, dict):
        raise ProviderError("malformed")
    status = _token(raw.get("status"))
    if status in _INCOMPLETE or raw.get("incomplete_details"):
        raise ProviderError("incomplete")
    if status != "completed" or raw.get("error"):
        raise ProviderError("malformed")
    output = raw.get("output")
    if not isinstance(output, list):
        raise ProviderError("malformed")
    message = None
    for item in output:
        if not isinstance(item, dict):
            raise ProviderError("malformed")
        kind = _token(item.get("type"))
        if kind == "reasoning":
            item_status = _token(item.get("status"))
            if item_status in _INCOMPLETE:
                raise ProviderError("incomplete")
            if item_status not in (None, "completed"):
                raise ProviderError("malformed")
            continue
        if kind != "message":
            raise ProviderError("malformed")
        if message is not None:
            raise ProviderError("malformed")
        message = item
    if message is None:
        raise ProviderError("malformed")
    if _token(message.get("role")) not in (None, "assistant"):
        raise ProviderError("malformed")
    message_status = _token(message.get("status"))
    if message_status in _INCOMPLETE:
        raise ProviderError("incomplete")
    if message_status not in (None, "completed"):
        raise ProviderError("malformed")
    content = message.get("content")
    if (
        not isinstance(content, list)
        or len(content) != 1
        or not isinstance(content[0], dict)
    ):
        raise ProviderError("malformed")
    part = content[0]
    part_type = _token(part.get("type"))
    if part_type == "refusal" or part.get("refusal"):
        raise ProviderError("refusal")
    text = part.get("text")
    if part_type != "output_text" or not isinstance(text, str):
        raise ProviderError("malformed")
    return text


class OpenAIProvider:
    __slots__ = ("_api_key", "_client", "_owned")

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ProviderError("invalid_client")
        try:
            key = validate_api_key(api_key)
        except ProviderConfigError:
            raise ProviderError("invalid_api_key") from None
        if key is None:
            raise ProviderError("invalid_api_key")
        self._api_key = key
        self._owned = client is None
        if client is None:
            self._client = httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                timeout=None,
                trust_env=False,
            )
        else:
            self._client = client

    async def aclose(self) -> None:
        if self._owned:
            self._owned = False
            await self._client.aclose()

    async def decide(
        self,
        questions: Sequence[ChoiceQuestion],
        input_text: str,
        *,
        budget: Budget,
        privacy: PrivacyGate,
        operation_head: str = "operation",
        target_heads: Mapping[str, str] | None = None,
    ) -> Decision:
        try:
            original = _bundle(questions)
            operation = _operation(original, operation_head, target_heads)
            for choice in operation.choices:
                if type(choice) is not str:
                    raise CodecError("invalid_operation")
                if not _terminal_choice(choice, target_heads) and (
                    target_heads is None or choice not in target_heads
                ):
                    raise CodecError("missing_target")
            wire_questions, wire_input, aliases = prepare_context(
                original, input_text,
                operation_head=operation_head, target_heads=target_heads,
                privacy=privacy, compact=True, lean=True, provider="openai",
            )
            if (aliases and not any(question.descriptions for question in original)
                    and _loads(wire_input.encode()).get("context_format") == "decision-browser-choice-table-v3"):
                wire_questions = tuple(
                    replace(question, descriptions=tuple(
                        existing if existing is not None else _NATIVE_OPERATIONS.get(str(choice).split("_group", 1)[0])
                        for choice, existing in zip(
                            question.choices, question.descriptions or (None,) * len(question.choices), strict=True,
                        )
                    )) if question.name == operation_head else question
                    for question in wire_questions
                )
                wire_questions = action_evidence(
                    wire_questions, _loads(input_text.encode()), aliases,
                    operation_head=operation_head, target_heads=target_heads or {},
                )
            complete = bind_complete_actions(
                original, wire_questions, wire_input, aliases,
                operation_head=operation_head, target_heads=target_heads,
            )
            if complete is None:
                wire_questions, wire_heads, bound_targets = bind_openai_singletons(
                    wire_questions, wire_input, aliases, operation_head=operation_head, target_heads=target_heads,
                )
            else:
                wire_questions, wire_heads, bound_targets, wire_input = complete
            payload = compile_questions(wire_questions, wire_input)
        except CodecError as exc:
            raise ProviderError(exc.reason) from None
        parsed = await self._post(
            "/v1/decisions",
            payload,
            budget=budget,
            privacy=privacy,
        )
        try:
            decision = _parse_decision(
                wire_questions,
                parsed,
                operation_head=operation_head,
                target_heads=wire_heads, bound_targets=bound_targets,
            )
            targets = aliases.get(decision.operation)
            if targets is not None:
                if type(decision.target) is not str or decision.target not in targets:
                    raise CodecError("invalid_context")
                decision = Decision(
                    decision.operation, targets[decision.target], decision.confidence
                )
        except CodecError as exc:
            raise ProviderError(exc.reason) from None
        budget.remaining()
        return decision

    async def generate_text(
        self,
        prompt: str,
        *,
        budget: Budget,
        privacy: PrivacyGate,
        max_text_chars: int = 4096,
    ) -> str:
        if type(max_text_chars) is not int or not 1 <= max_text_chars <= 65536:
            raise ProviderError("max_text_chars")
        if not isinstance(prompt, str):
            raise ProviderError("malformed")
        payload = {
            "model": "gpt-6-luna",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": prompt}]}
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "text_helper",
                    "strict": True,
                    "schema": _TEXT_SCHEMA,
                }
            },
        }
        response = await self._post(
            "/v1/responses", payload, budget=budget, privacy=privacy
        )
        if not isinstance(response, dict):
            raise ProviderError("malformed")
        try:
            parsed = _loads(_text_output(response).encode())
        except UnicodeError:
            raise ProviderError("schema") from None
        if not isinstance(parsed, dict) or len(parsed) != 1 or "text" not in parsed:
            raise ProviderError("schema")
        value = parsed["text"]
        if not isinstance(value, str):
            raise ProviderError("schema")
        if len(value) > max_text_chars:
            raise ProviderError("max_text_chars")
        budget.remaining()
        return value

    async def inspect_visual(
        self,
        prompt: str,
        image: bytes,
        capture: CaptureEvidence,
        *,
        budget: Budget,
        privacy: PrivacyGate,
    ) -> ModelVisualReport:
        try:
            image_url = privacy.prepare_image(image, capture)
        except PrivacyError:
            return ModelVisualReport(
                diagnostic="incomplete", reason="unsafe_capture", findings=[]
            )
        payload = {
            "model": "gpt-6-luna",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": image_url},
                    ],
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "visual_findings",
                    "strict": True,
                    "schema": _VISUAL_SCHEMA,
                }
            },
        }
        try:
            raw = await self._post(
                "/v1/responses",
                payload,
                budget=budget,
                privacy=privacy,
                capture=capture,
                contains_image=True,
            )
            parsed = _loads(_text_output(raw).encode())
        except BudgetExceeded:
            raise
        except PrivacyError:
            return ModelVisualReport(
                diagnostic="incomplete", reason="unsafe_capture", findings=[]
            )
        except UnicodeError:
            return ModelVisualReport(
                diagnostic="failed", reason="malformed_response", findings=[]
            )
        except ProviderError as exc:
            incomplete = exc.code in ("refusal", "incomplete")
            return ModelVisualReport(
                diagnostic="incomplete" if incomplete else "failed",
                reason=exc.code or "provider_error",
                findings=[],
            )
        report = validate_visual_response(parsed, safe_capture=True)
        budget.remaining()
        return report

    async def _post(
        self,
        path: str,
        payload: object,
        *,
        budget: Budget,
        privacy: PrivacyGate,
        capture: CaptureEvidence | None = None,
        contains_image: bool = False,
    ) -> dict[str, object]:
        if path not in _PATHS:
            raise ProviderError("invalid_path")
        for attempt in range(2):
            try:
                return await post_json(
                    self._client, _ORIGIN + path, self._api_key, payload,
                    budget=budget, privacy=privacy, capture=capture,
                    contains_image=contains_image,
                )
            except ProviderError as error:
                if (attempt or path != "/v1/decisions" or error.code != "http_error"
                        or error.http_status != 503
                        or error.diagnostics != ("http_status:server_error",)
                        or error.__dict__.get("residual_tasks")
                        or not budget.claim_provider_retry()):
                    raise
                # Repeat inference before any browser action, using the same
                # budget and individually metered HTTP boundary for each POST.
                try:
                    async with asyncio.timeout(budget.remaining()):
                        await asyncio.sleep(0.25)
                except TimeoutError:
                    raise BudgetExceeded("deadline_expired") from None
        raise AssertionError("unreachable")
