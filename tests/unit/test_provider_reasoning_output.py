import pytest

from decision_browser.provider import ProviderError, _text_output


def _msg(text="ok", **kw):
    item = {
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }
    item.update(kw)
    return item


def _env(output, **kw):
    raw = {"status": "completed", "output": output}
    raw.update(kw)
    return raw


def _code(raw):
    with pytest.raises(ProviderError) as exc:
        _text_output(raw)
    return exc.value.code


def test_reasoning_plus_message_preserves_text():
    text = "  keep\nme"
    raw = _env(
        [
            {
                "type": "reasoning",
                "status": "completed",
                "summary": "!!!refusal",
                "content": "hidden",
            },
            _msg(text),
            {"type": "reasoning", "summary": ["ignored", {"x": 1}]},
        ]
    )
    assert _text_output(raw) == text


def test_ordinary_and_optional_role_status():
    assert _text_output(_env([_msg("plain")])) == "plain"
    bare = _msg("bare")
    bare.pop("role")
    bare.pop("status")
    assert _text_output(_env([bare])) == "bare"


@pytest.mark.parametrize(
    "st", ["incomplete", "failed", "cancelled", "canceled", "queued", "in_progress"]
)
def test_reasoning_incomplete(st):
    assert _code(_env([{"type": "reasoning", "status": st}, _msg()])) == "incomplete"


def test_reasoning_bad_status():
    assert (
        _code(_env([{"type": "reasoning", "status": "weird"}, _msg()])) == "malformed"
    )


def test_duplicate_messages():
    assert _code(_env([_msg("a"), {"type": "reasoning"}, _msg("b")])) == "malformed"


def test_no_message():
    assert _code(_env([{"type": "reasoning", "status": "completed"}])) == "malformed"
    assert _code(_env([])) == "malformed"


def test_unknown_and_tool_items():
    assert _code(_env([_msg(), {"type": "function_call"}])) == "malformed"
    assert _code(_env([{"type": "action"}, _msg()])) == "malformed"
    assert _code(_env(["nope"])) == "malformed"


def test_role_user():
    assert _code(_env([_msg(role="user")])) == "malformed"


def test_refusal():
    item = _msg()
    item["content"] = [{"type": "refusal", "refusal": "no"}]
    assert _code(_env([item])) == "refusal"
    marked = _msg()
    marked["content"] = [{"type": "output_text", "text": "x", "refusal": "no"}]
    assert _code(_env([marked])) == "refusal"


def test_incomplete_outer_and_message():
    assert _code(_env([_msg()], status="failed")) == "incomplete"
    assert _code(_env([_msg()], incomplete_details={"reason": "max"})) == "incomplete"
    assert _code(_env([_msg(status="in_progress")])) == "incomplete"


def test_malformed_content_and_shape():
    multi = _msg()
    multi["content"] = [
        {"type": "output_text", "text": "a"},
        {"type": "output_text", "text": "b"},
    ]
    assert _code(_env([multi])) == "malformed"
    bad = _msg()
    bad["content"] = [{"type": "output_text", "text": 1}]
    assert _code(_env([bad])) == "malformed"
    assert _code(_env([_msg(status="weird")])) == "malformed"
    assert _code(_env([_msg()], error={"message": "x"})) == "malformed"
    assert _code("no") == "malformed"
