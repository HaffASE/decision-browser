"""Decisions-only, cumulative-budget provider for the approved public pilot."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

from .budget import Budget
from .decision_codec import ChoiceQuestion, Decision
from .experiment_budget import ExperimentBudgetError, ExperimentLedger
from .flights_context import FLIGHTS_FORMAT, read_context, validate_context
from .privacy import CaptureEvidence, PrivacyGate
from .provider import OpenAIProvider, ProviderError


class FlightsProvider(OpenAIProvider):
    def __init__(self, api_key: str, ledger: ExperimentLedger, **kwargs: Any) -> None:
        super().__init__(api_key, **kwargs)
        self.ledger = ledger

    async def decide(self, questions: Sequence[ChoiceQuestion], input_text: str, **kwargs: Any) -> Decision:
        return await super().decide(questions, compact_input(input_text), **kwargs)

    async def _post(
        self, path: str, payload: object, *, budget: Budget,
        privacy: PrivacyGate, capture: CaptureEvidence | None = None,
        contains_image: bool = False,
    ) -> dict[str, object]:
        if (path != "/v1/decisions" or contains_image or capture is not None
                or not isinstance(payload, dict) or payload.get("model") != "gpt-6-luna"):
            raise ProviderError("flights_decisions_only")
        try:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError):
            raise ProviderError("invalid_request") from None
        privacy.check_payload(body, None, False)
        try:
            self.ledger.reserve_call(len(body))
            async with asyncio.timeout(self.ledger.remaining_seconds):
                response = await super()._post(path, payload, budget=budget, privacy=privacy)
            usage = response.get("usage")
            if isinstance(usage, dict) and type(usage.get("input_tokens")) is int:
                clean = {k: v for k, v in usage.items() if k in {
                    "input_tokens", "output_tokens", "total_tokens", "cached_tokens"
                } and type(v) is int and v >= 0}
                self.ledger.record_usage(clean)
            return response
        except ExperimentBudgetError as exc:
            raise ProviderError(exc.code) from None
        except TimeoutError:
            raise ProviderError("experiment_deadline") from None

    async def generate_text(self, *_args: Any, **_kwargs: Any) -> str:
        raise ProviderError("flights_missing_bound_value")

    async def inspect_visual(self, *_args: Any, **_kwargs: Any) -> Any:
        raise ProviderError("flights_decisions_only")


def compact_input(input_text: str) -> str:
    """Lossless references for repeated candidate descriptions, no choice pruning."""
    try:
        data = read_context(input_text)
        validate_context(data)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProviderError("invalid_pilot_context") from None
    elements: dict[str, Any] = {}
    seen: dict[str, str] = {}
    def compact(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: compact(item) for key, item in value.items()
                    if item is not None and item != {} and item != [] and item is not False}
        if isinstance(value, list):
            return [compact(item) for item in value]
        return value
    for group in data["candidates"].values():
        for key, entry in group.items():
            node = compact(entry.get("node", {}))
            packed = json.dumps(node, sort_keys=True, ensure_ascii=False)
            if packed not in seen:
                seen[packed] = f"e{len(seen)}"
                elements[seen[packed]] = node
            group[key] = compact({"element": seen[packed],
                "name": entry.get("name"), "destination": entry.get("destination"),
                "form": entry.get("form"), "context": entry.get("context")})
    data["elements"] = elements
    data["candidate_format"] = FLIGHTS_FORMAT
    try:
        validate_context(data, compact=True)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProviderError("invalid_pilot_context") from None
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
