"""Optional, independent OpenAI-compatible chat helper for unbound field text."""

from __future__ import annotations

import httpx

from .budget import Budget
from .privacy import PrivacyGate
from .provider_config import ProviderConfigError, TextSettings, validate_text_settings
from .provider_http import ProviderError, post_json


def _content(response: dict[str, object]) -> str:
    if response.get("error"):
        raise ProviderError("malformed")
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ProviderError("malformed")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ProviderError("malformed")
    finish = choice.get("finish_reason")
    if finish == "content_filter":
        raise ProviderError("refusal")
    if finish in (None, "length"):
        raise ProviderError("incomplete")
    if finish != "stop":
        raise ProviderError("malformed")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ProviderError("malformed")
    if message.get("refusal"):
        raise ProviderError("refusal")
    if (
        message.get("role") != "assistant"
        or message.get("tool_calls") or message.get("function_call")
        or not isinstance(message.get("content"), str)
    ):
        raise ProviderError("malformed")
    return message["content"]


async def generate_chat_text(
    client: httpx.AsyncClient,
    settings: TextSettings,
    prompt: str,
    *,
    budget: Budget,
    privacy: PrivacyGate,
    max_text_chars: int = 4096,
) -> str:
    """Return one complete assistant text; responses cannot introduce tool calls.

    The helper's endpoint, model and key are independent of the decision route.
    The same original task budget and privacy gate cover this request.
    """
    if type(max_text_chars) is not int or not 1 <= max_text_chars <= 65536:
        raise ProviderError("max_text_chars")
    if type(prompt) is not str:
        raise ProviderError("malformed")
    try:
        settings = validate_text_settings(settings)
    except ProviderConfigError:
        raise ProviderError("invalid_provider_config") from None
    response = await post_json(
        client, settings.endpoint, settings.api_key,
        {"model": settings.model, "stream": False,
         "messages": [{"role": "user", "content": prompt}]},
        budget=budget, privacy=privacy,
    )
    value = _content(response)
    if len(value) > max_text_chars:
        raise ProviderError("max_text_chars")
    budget.remaining()
    return value
