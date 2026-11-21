"""Benchmark-only transport for unchanged jev-ultrafast policy and text helper.

The TypeSafe wire format is translated to OpenAI Decisions. Singleton heads are
resolved locally, since their sole offered answer carries no model decision.
Only the upstream selected-head validator determines which target is usable.
"""

import json

import httpx

MODEL = "gpt-6-luna"
ORIGIN = "https://api.openai.com"
_DECISION_SOURCE = "https://api.typesafe.ai/v1/systemone"
_TEXT_SOURCE = ORIGIN + "/v1/chat/completions"


class TransportError(RuntimeError):
    """Stable error code without page data, credentials, or raw responses."""


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _request(client, key, path, payload):
    try:
        response = client.post(
            ORIGIN + path,
            headers={"Authorization": "Bearer " + key},
            json=payload,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        raise TransportError("connection_failed") from None
    if not 200 <= response.status_code < 300:
        raise TransportError("http_" + str(response.status_code))
    try:
        raw = response.json()
    except ValueError:
        raise TransportError("malformed_json") from None
    if not isinstance(raw, dict):
        raise TransportError("malformed_response")
    return raw


def _decision_request(body):
    questions = body.get("questions")
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 400:
        raise TransportError("question_count")
    if "operation" not in questions:
        raise TransportError("missing_operation")
    fixed, asked, wire = {}, {}, []
    for name, question in questions.items():
        if not isinstance(name, str) or not isinstance(question, dict):
            raise TransportError("invalid_question")
        criteria = question.get("criteria")
        if (
            question.get("type") != "choice"
            or not isinstance(criteria, dict)
            or not 1 <= len(criteria) <= 255
            or not all(isinstance(choice, str) for choice in criteria)
            or "instructions" not in question
        ):
            raise TransportError("invalid_choice_domain")
        if len(criteria) == 1:
            only = next(iter(criteria))
            fixed[name] = {"choice": only, "confidence": 1.0, "probabilities": {only: 1.0}}
            continue
        asked[name] = question
        wire.append({
            "name": name,
            "type": "choice",
            "instructions": _text(question["instructions"]),
            "choices": [{"value": choice, "description": _text(description)}
                        for choice, description in criteria.items()],
        })
    return {"model": MODEL, "input": _text(body.get("state")), "questions": wire}, asked, fixed


def _choice_answer(item):
    # Refusals and malformed target heads remain invalid dictionaries. The
    # upstream policy deliberately validates only its selected operation/head.
    if item.get("type") != "choice" or not isinstance(item.get("probabilities"), list):
        return {}
    probabilities = {}
    for entry in item["probabilities"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("value"), str):
            return {}
        value = entry["value"]
        if value in probabilities:
            return {}
        probabilities[value] = entry.get("probability")
    return {
        "choice": item.get("choice"),
        "confidence": item.get("confidence"),
        "probabilities": probabilities,
    }


def _decision_response(raw, asked, fixed):
    if not isinstance(raw.get("answers"), list):
        raise TransportError("malformed_answers")
    answers, seen = dict(fixed), set()
    for item in raw["answers"]:
        if not isinstance(item, dict):
            raise TransportError("malformed_answer")
        name = item.get("name")
        if not isinstance(name, str) or name not in asked or name in seen:
            raise TransportError("unknown_or_duplicate_head")
        seen.add(name)
        answers[name] = _choice_answer(item)
    if "operation" not in answers:
        raise TransportError("missing_operation")
    model = raw.get("model")
    if not isinstance(model, str) or not model:
        raise TransportError("missing_model")
    return {"model": model, "answers": answers, "usage": raw.get("usage", {})}


def _text_request(body):
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise TransportError("invalid_text_messages")
    inputs = []
    for message in messages:
        if (
            not isinstance(message, dict)
            or message.get("role") not in ("system", "user")
            or not isinstance(message.get("content"), str)
        ):
            raise TransportError("invalid_text_messages")
        inputs.append({"role": message["role"], "content": [
            {"type": "input_text", "text": message["content"]}
        ]})
    maximum = body.get("max_tokens", 1024)
    if type(maximum) is not int or maximum <= 0:
        raise TransportError("invalid_text_limit")
    return {
        "model": MODEL,
        "input": inputs,
        "max_output_tokens": maximum,
        "text": {"format": {
            "type": "json_schema", "name": "field_text", "strict": True,
            "schema": {
                "type": "object", "additionalProperties": False,
                "properties": {"text": {"type": ["string", "null"]}},
                "required": ["text"],
            },
        }},
    }


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _text_response(raw):
    if raw.get("status") != "completed" or raw.get("error") or raw.get("incomplete_details"):
        raise TransportError("text_incomplete")
    if not isinstance(raw.get("output"), list):
        raise TransportError("text_malformed")
    messages = []
    for item in raw["output"]:
        if not isinstance(item, dict):
            raise TransportError("text_malformed")
        if item.get("type") == "reasoning" and item.get("status") in (None, "completed"):
            continue
        if item.get("type") != "message" or item.get("status") not in (None, "completed"):
            raise TransportError("text_malformed")
        if item.get("role") not in (None, "assistant"):
            raise TransportError("text_malformed")
        messages.append(item)
    if len(messages) != 1:
        raise TransportError("text_malformed")
    content = messages[0].get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
        raise TransportError("text_malformed")
    part = content[0]
    if part.get("type") == "refusal" or part.get("refusal"):
        raise TransportError("text_refusal")
    if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
        raise TransportError("text_malformed")
    try:
        value = json.loads(part["text"], object_pairs_hook=_unique_pairs)
    except (ValueError, TypeError, RecursionError):
        raise TransportError("text_schema") from None
    if (
        not isinstance(value, dict) or set(value) != {"text"}
        or (value["text"] is not None and not isinstance(value["text"], str))
    ):
        raise TransportError("text_schema")
    model = raw.get("model")
    if not isinstance(model, str) or not model:
        raise TransportError("missing_model")
    return {
        "model": model,
        "choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}],
        "usage": raw.get("usage", {}),
    }


def make_post_json(key, client):
    """Return the upstream ``post_json(url, key, body)`` transport replacement."""
    if not isinstance(key, str) or not key:
        raise TransportError("missing_key")

    def post_json(url, _source_key, body):
        if not isinstance(body, dict):
            raise TransportError("invalid_request")
        if url == _DECISION_SOURCE:
            payload, asked, fixed = _decision_request(body)
            if not asked:
                return {"model": MODEL, "answers": fixed, "usage": {}}
            return _decision_response(_request(client, key, "/v1/decisions", payload), asked, fixed)
        if url == _TEXT_SOURCE:
            return _text_response(_request(client, key, "/v1/responses", _text_request(body)))
        raise TransportError("unsupported_source_endpoint")

    return post_json
