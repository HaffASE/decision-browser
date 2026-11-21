"""OpenAIProvider unit tests. MockTransport only; no live HTTP."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from decision_browser.budget import Budget, BudgetExceeded
from decision_browser.contracts import Limits, PrivacyPolicy
from decision_browser.decision_codec import ChoiceQuestion, Decision
from decision_browser.privacy import PrivacyError, PrivacyGate
from decision_browser.provider import OpenAIProvider, ProviderError

pytestmark = pytest.mark.asyncio
_Q = (ChoiceQuestion("operation", "choose", ("DONE",)),)


def _ok() -> bytes:
    answer = {
        "choice": "DONE",
        "confidence": 1,
        "name": "operation",
        "type": "choice",
        "probabilities": [
            {"probability": 1, "value": "DONE"},
            {"probability": 0, "value": False},
        ],
    }
    return json.dumps(
        {"answers": [answer], "model": "gpt-6-luna", "usage": {}}
    ).encode()


def _open(handler: object) -> OpenAIProvider:
    return OpenAIProvider("test-key", transport=httpx.MockTransport(handler))


def _budget(**limits: object) -> Budget:
    return Budget(Limits(**limits), clock=lambda: 0.0)


def _gate(secret: str | None = None) -> PrivacyGate:
    return PrivacyGate(PrivacyPolicy(blocked_literals=[secret] if secret else []))


async def test_success_one_operation_done_posts_decisions_model() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["body"] = str(request.url), json.loads(request.content)
        return httpx.Response(200, content=_ok())

    budget, provider = _budget(), _open(handler)
    decision = await provider.decide(_Q, "ship", budget=budget, privacy=_gate())
    await provider.aclose()
    assert (
        decision.operation == "DONE"
        and decision.target is None
        and budget.usage().calls == 1
    )
    assert seen["url"] == "https://api.openai.com/v1/decisions"
    assert seen["body"]["model"] == "gpt-6-luna" and seen["body"]["input"] == "ship"


async def test_strict_json_rejects_dupes_nan_overflow_and_list() -> None:
    for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}', b"[]"):

        def handler(request: httpx.Request, raw: bytes = raw) -> httpx.Response:
            return httpx.Response(200, content=raw)

        provider = _open(handler)
        with pytest.raises(ProviderError) as caught:
            await provider.decide(_Q, "ship", budget=_budget(), privacy=_gate())
        await provider.aclose()
        assert caught.value.code == "malformed_response"


async def test_seeded_textsecret_raises_privacy_with_zero_network() -> None:
    hits = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal hits
        hits += 1
        return httpx.Response(200, content=_ok())

    budget, provider = _budget(), _open(handler)
    with pytest.raises(PrivacyError) as caught:
        await provider.decide(
            _Q, "seed textsecret", budget=budget, privacy=_gate("textsecret")
        )
    await provider.aclose()
    assert (
        caught.value.code == "blocked_literal"
        and hits == 0
        and budget.usage().calls == 0
    )


async def test_calls_quota_blocks_next_socket_and_output_byte_boundary() -> None:
    raw, hits = _ok(), 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal hits
        hits += 1
        return httpx.Response(200, content=raw)

    budget, provider = (
        _budget(max_output_bytes=len(raw), max_provider_calls=1),
        _open(handler),
    )
    done = await provider.decide(_Q, "ship", budget=budget, privacy=_gate())
    assert (
        done.operation == "DONE"
        and budget.usage().output_bytes == len(raw)
        and hits == 1
    )
    with pytest.raises(BudgetExceeded) as quota:
        await provider.decide(_Q, "next", budget=budget, privacy=_gate())
    await provider.aclose()
    assert quota.value.code == "provider_calls" and hits == 1
    short, provider = _budget(max_output_bytes=len(raw) - 1), _open(handler)
    with pytest.raises(BudgetExceeded) as edge:
        await provider.decide(_Q, "ship", budget=short, privacy=_gate())
    await provider.aclose()
    assert edge.value.code == "output_bytes" and short.usage().calls == 1


async def test_cancel_and_hanging_headers_preserve_original_deadline() -> None:
    started = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        return httpx.Response(200, content=b"{}")

    cancel_budget, provider = _budget(max_seconds=30.0), _open(hang)
    task = asyncio.create_task(
        provider.decide(_Q, "ship", budget=cancel_budget, privacy=_gate())
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await provider.aclose()
    assert cancel_budget.original_deadline == cancel_budget.deadline == 30.0
    hang_budget, provider = _budget(max_seconds=0.05), _open(hang)
    with pytest.raises(BudgetExceeded) as caught:
        await provider.decide(_Q, "ship", budget=hang_budget, privacy=_gate())
    await provider.aclose()
    assert (
        caught.value.code == "deadline_expired"
        and hang_budget.original_deadline == hang_budget.deadline == 0.05
    )


import base64
import hashlib

import pytest

from decision_browser.privacy import CaptureEvidence

pytestmark = pytest.mark.asyncio
IMG = b"\x89PNG\r\nsafe"
GATE = PrivacyGate(PrivacyPolicy(send_images=True, blocked_literals=["SECRET"]))
FINDING = {
    "origin": "model",
    "evidence": "cropped",
    "element": "button",
    "viewport": {"width": 800, "height": 600},
    "region": {"x": 1, "y": 2, "width": 30, "height": 40},
    "severity": "high",
    "uncertainty": 0.2,
}


def envelope(findings):
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": json.dumps({"findings": findings})}
                ],
            }
        ],
    }


async def drive(
    payload, image=IMG, prompt="look", sanitized=True, sensitive=False, digest=None
):
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=payload)

    provider = OpenAIProvider("fake", transport=httpx.MockTransport(handler))
    bud = Budget(Limits())
    cap = CaptureEvidence(
        digest or hashlib.sha256(image).hexdigest(), sanitized, sensitive
    )
    try:
        report = await provider.inspect_visual(
            prompt, image, cap, budget=bud, privacy=GATE
        )
    finally:
        await provider.aclose()
    return report, seen, bud


async def test_safe_image_exact_outgoing_bytes_and_empty_report():
    report, seen, bud = await drive(envelope([]))
    body = seen["body"]
    url = body["input"][0]["content"][1]["image_url"]
    url = url["url"] if isinstance(url, dict) else url
    assert seen["path"] == "/v1/responses"
    assert base64.b64decode(url.split(",", 1)[1]) == IMG
    nodes = [body["text"]["format"]["schema"]]
    for node in nodes:
        if isinstance(node, dict) and "properties" in node:
            assert set(node["required"]) == set(node["properties"])
            nodes.extend(node["properties"].values())
        if isinstance(node, dict) and isinstance(node.get("items"), dict):
            nodes.append(node["items"])
    assert (report.diagnostic, report.reason, report.findings, bud.usage().calls) == (
        "complete",
        "no_model_findings_not_proof_of_cleanliness",
        [],
        1,
    )


@pytest.mark.parametrize(
    "kw",
    [
        {"sanitized": False},
        {"sensitive": True},
        {"digest": "ab" * 32},
        {"image": b"rawSECRET"},
        {"prompt": "prompt SECRET"},
    ],
)
async def test_unsafe_makes_no_call(kw):
    report, seen, bud = await drive(envelope([]), **kw)
    assert (
        report.diagnostic,
        report.reason,
        report.findings,
        seen["n"],
        bud.usage().calls,
    ) == ("incomplete", "unsafe_capture", [], 0, 0)


async def test_valid_finding_is_exact():
    report, _, _ = await drive(envelope([FINDING]))
    f = report.findings[0]
    vp, rg = f.viewport, f.region
    assert report.diagnostic == "complete" and len(report.findings) == 1
    assert (
        f.origin,
        f.evidence,
        f.element,
        f.severity,
        f.uncertainty,
        vp.width,
        vp.height,
        rg.x,
        rg.y,
        rg.width,
        rg.height,
    ) == ("model", "cropped", "button", "high", 0.2, 800, 600, 1, 2, 30, 40)


@pytest.mark.parametrize(
    "payload",
    [
        envelope([{**FINDING, "uncertainty": True}]),
        {"status": "completed", "output": [{"type": "container", "content": []}]},
        {"status": "completed"},
        {"status": "incomplete", "output": []},
        {
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}
            ],
        },
    ],
)
async def test_invalid_is_failed_or_incomplete(payload):
    report, _, _ = await drive(payload)
    assert report.diagnostic in ("failed", "incomplete") and report.findings == []


async def test_text_helper_contract_and_privacy():
    import json

    import httpx
    import pytest

    from decision_browser.budget import Budget
    from decision_browser.contracts import Limits, PrivacyPolicy
    from decision_browser.privacy import PrivacyError, PrivacyGate
    from decision_browser.provider import OpenAIProvider, ProviderError

    payloads = [
        '{"text":""}',
        '{"text":"ok"}',
        "{}",
        '{"text":1}',
        '{"text":"a","extra":1}',
        '{"text":"a","text":"b"}',
        '{"text":NaN}',
        "[]",
    ]
    state = {"i": 0}

    def handler(request):
        assert request.url == "https://api.openai.com/v1/responses"
        body = json.loads(request.content)
        assert body["text"]["format"]["strict"] is True
        assert body["text"]["format"]["schema"]["required"] == ["text"]
        text = payloads[state["i"]]
        state["i"] += 1
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ],
            },
        )

    p = OpenAIProvider("fake", transport=httpx.MockTransport(handler))
    try:
        gate = PrivacyGate(PrivacyPolicy())
        assert await p.generate_text("p", budget=Budget(Limits()), privacy=gate) == ""
        assert await p.generate_text("p", budget=Budget(Limits()), privacy=gate) == "ok"
        for _ in payloads[2:]:
            with pytest.raises(ProviderError):
                await p.generate_text("p", budget=Budget(Limits()), privacy=gate)
        n = state["i"]
        b = Budget(Limits())
        with pytest.raises(PrivacyError):
            await p.generate_text(
                "SECRET",
                budget=b,
                privacy=PrivacyGate(PrivacyPolicy(blocked_literals=["SECRET"])),
            )
        assert state["i"] == n and b.usage().calls == 0
    finally:
        await p.aclose()


async def test_provider_slow_stream_keeps_original_deadline():
    import asyncio

    import httpx
    import pytest

    from decision_browser.budget import Budget, BudgetExceeded
    from decision_browser.contracts import Limits, PrivacyPolicy
    from decision_browser.privacy import PrivacyGate
    from decision_browser.provider import OpenAIProvider

    hits = closed = 0
    first = b'{"status":'

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield first
            await asyncio.sleep(1)
            yield b'"completed"}'

        async def aclose(self):
            nonlocal closed
            closed += 1

    def handler(request):
        nonlocal hits
        hits += 1
        return httpx.Response(200, stream=Stream())

    p = OpenAIProvider("fake", transport=httpx.MockTransport(handler))
    b = Budget(Limits(max_seconds=0.03))
    original = b.original_deadline
    with pytest.raises(BudgetExceeded, match="deadline_expired"):
        await p.generate_text("x", budget=b, privacy=PrivacyGate(PrivacyPolicy()))
    u = b.usage()
    assert (
        b.original_deadline == original
        and u.calls == 1
        and u.output_bytes == len(first)
        and hits == 1
        and closed == 1
    )
    await p.aclose()


async def test_provider_exact_input_bytes():
    seen = []

    def handler(request):
        seen.append(len(request.content))
        return httpx.Response(200, content=_ok())

    p = OpenAIProvider("fake", transport=httpx.MockTransport(handler))
    await p.decide(_Q, "ship", budget=_budget(), privacy=_gate())
    n = seen[0]
    b = _budget(max_input_bytes=n, max_provider_calls=2)
    await p.decide(_Q, "ship", budget=b, privacy=_gate())
    with pytest.raises(BudgetExceeded, match="input_bytes"):
        await p.decide(_Q, "ship", budget=b, privacy=_gate())
    u = b.usage()
    assert u.calls == 1 and u.input_bytes == n and len(seen) == 2
    await p.aclose()


def _same_choice(value: object, choice: object) -> bool:
    return type(value) is type(choice) and value == choice


def _decision_body(request: httpx.Request) -> dict[str, object]:
    body = json.loads(request.content)
    assert isinstance(body, dict)
    return body


def _wire_answers(body: dict[str, object], selected: dict[str, object] | None = None) -> bytes:
    questions = body["questions"]
    assert isinstance(questions, list)
    answers: list[object] = []
    for question in questions:
        assert isinstance(question, dict)
        raw_choices = question["choices"]
        assert isinstance(raw_choices, list)
        values = [item["value"] for item in raw_choices if isinstance(item, dict)]
        assert 2 <= len(values) <= 255
        name = question["name"]
        assert isinstance(name, str)
        choice = values[0] if selected is None or name not in selected else selected[name]
        answers.append(
            {
                "type": "choice",
                "name": name,
                "choice": choice,
                "confidence": 1,
                "probabilities": [
                    {
                        "value": value,
                        "probability": 1 if _same_choice(value, choice) else 0,
                    }
                    for value in values
                ],
            }
        )
    return json.dumps(
        {"answers": answers, "model": "gpt-6-luna", "usage": {}}
    ).encode()


async def test_provider_singleton_wire_is_two_and_domain_stays_original() -> None:
    ops = ChoiceQuestion("operation", "choose", ("launch", "DONE"))
    heads = {"launch": "target"}
    for word in ("false", "False", "true", "True"):
        seen: dict[str, object] = {}

        def handler(
            request: httpx.Request,
            seen: dict[str, object] = seen,
            word: str = word,
        ) -> httpx.Response:
            body = _decision_body(request)
            seen["body"] = body
            return httpx.Response(
                200, content=_wire_answers(body, {"operation": "launch", "target": word})
            )

        provider = _open(handler)
        decision = await provider.decide(
            (ops, ChoiceQuestion("target", "choose", (word,))),
            "ship",
            budget=_budget(),
            privacy=_gate(),
            target_heads=heads,
        )
        await provider.aclose()
        payload = seen["body"]
        assert isinstance(payload, dict)
        questions = payload["questions"]
        assert isinstance(questions, list)
        assert questions[1]["choices"] == [{"value": word}, {"value": False}]
        assert decision == Decision("launch", word, 1.0)
        assert type(decision.target) is str


async def test_provider_sentinel_raises_before_dispatch_callback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = _decision_body(request)
        return httpx.Response(
            200, content=_wire_answers(body, {"operation": False})
        )

    provider = _open(handler)
    dispatched: list[object] = []
    with pytest.raises(ProviderError) as caught:
        decision = await provider.decide(
            (ChoiceQuestion("operation", "choose", ("DONE",)),),
            "ship",
            budget=_budget(),
            privacy=_gate(),
        )
        dispatched.append(decision)
    await provider.aclose()
    assert caught.value.code == "rejected_sentinel" and dispatched == []


async def test_provider_bool_singleton_target_and_choice_bounds() -> None:
    ops = ChoiceQuestion("operation", "choose", ("launch", "DONE"))
    yes = ChoiceQuestion("flag", "choose", (True,))
    seen: dict[str, object] = {}

    def ok(request: httpx.Request) -> httpx.Response:
        body = _decision_body(request)
        seen["yes"] = body["questions"]
        return httpx.Response(200, content=_wire_answers(body, {"operation": "launch"}))

    provider = _open(ok)
    decision = await provider.decide(
        (ops, yes),
        "ship",
        budget=_budget(),
        privacy=_gate(),
        target_heads={"launch": "flag"},
    )
    await provider.aclose()
    questions = seen["yes"]
    assert isinstance(questions, list)
    assert questions[0]["choices"] == [{"value": "launch"}, {"value": "DONE"}]
    assert questions[1]["choices"] == [{"value": True}, {"value": False}]
    assert decision == Decision("launch", True, 1.0)

    def rejected(request: httpx.Request) -> httpx.Response:
        body = _decision_body(request)
        return httpx.Response(
            200, content=_wire_answers(body, {"operation": "launch", "flag": False})
        )

    provider = _open(rejected)
    dispatched: list[object] = []
    with pytest.raises(ProviderError) as caught:
        decision = await provider.decide(
            (ops, yes),
            "ship",
            budget=_budget(),
            privacy=_gate(),
            target_heads={"launch": "flag"},
        )
        dispatched.append(decision.target)
    await provider.aclose()
    assert caught.value.code == "rejected_sentinel" and dispatched == []
    wide = ("DONE",) + tuple(f"c{index}" for index in range(254))
    wide_target = ChoiceQuestion("wide_target", "choose target", ("first", "last"))
    hits = 0

    def wide_handler(request: httpx.Request) -> httpx.Response:
        nonlocal hits
        hits += 1
        body = _decision_body(request)
        choices = body["questions"]
        assert isinstance(choices, list) and len(choices[0]["choices"]) == 255
        assert choices[1]["name"] == "wide_target" and len(choices[1]["choices"]) == 2
        return httpx.Response(200, content=_wire_answers(body))

    provider = _open(wide_handler)
    decision = await provider.decide(
        (ChoiceQuestion("operation", "choose", wide), wide_target),
        "ship",
        budget=_budget(),
        privacy=_gate(),
        target_heads={choice: "wide_target" for choice in wide if choice != "DONE"},
    )
    await provider.aclose()
    assert decision.operation == "DONE" and decision.target is None and hits == 1
    hits = 0

    def blocked(request: httpx.Request) -> httpx.Response:
        nonlocal hits
        hits += 1
        return httpx.Response(200, content=_ok())

    provider = _open(blocked)
    budget = _budget()
    oversized = tuple(f"c{index}" for index in range(256))
    with pytest.raises(ProviderError) as caught:
        await provider.decide(
            (ChoiceQuestion("operation", "choose", oversized), wide_target),
            "ship",
            budget=budget,
            privacy=_gate(),
            target_heads={choice: "wide_target" for choice in oversized},
        )
    await provider.aclose()
    assert caught.value.code == "choice_count" and hits == 0 and budget.usage().calls == 0


async def test_provider_guide_confidence_independent_of_selected_probability() -> None:
    ops = ChoiceQuestion("operation", "choose", ("launch", "DONE"))
    moon = ChoiceQuestion("target", "choose", ("moon",))

    def handler(request: httpx.Request) -> httpx.Response:
        body = _decision_body(request)
        questions = body["questions"]
        assert isinstance(questions, list)
        assert questions[0]["choices"] == [{"value": "launch"}, {"value": "DONE"}]
        assert questions[1]["choices"] == [{"value": "moon"}, {"value": False}]
        assert questions[1]["choices"][1]["value"] is False
        payload = {
            "answers": [
                {
                    "type": "choice",
                    "name": "operation",
                    "choice": "launch",
                    "confidence": 0.93,
                    "probabilities": [
                        {"value": "launch", "probability": 0.95},
                        {"value": "DONE", "probability": 0.05},
                    ],
                },
                {
                    "type": "choice",
                    "name": "target",
                    "choice": "moon",
                    "confidence": 1.0,
                    "probabilities": [
                        {"value": "moon", "probability": 1.0},
                        {"value": False, "probability": 0.0},
                    ],
                },
            ],
            "model": "gpt-6-luna",
            "usage": {},
        }
        return httpx.Response(200, content=json.dumps(payload).encode())

    provider = _open(handler)
    decision = await provider.decide(
        (ops, moon),
        "flight plan",
        budget=_budget(),
        privacy=_gate(),
        target_heads={"launch": "target"},
    )
    await provider.aclose()
    assert decision == Decision("launch", "moon", 0.93)
