"""Contract bounds."""

import math
from typing import get_args

import pytest
from pydantic import ValidationError

from decision_browser.contracts import (
    AdmissionClass,
    Check,
    Limits,
    PrivacyPolicy,
    TaskRequest,
    TaskResult,
    UsageCounters,
)


def _req(**k):
    d = {
        "goal": "ship",
        "start_url": "https://example.com/a",
        "values": {"k": "v"},
        "checks": [],
        "limits": Limits(),
        "privacy": PrivacyPolicy(),
    }
    d.update(k)
    return TaskRequest(**d)


def _res(**k):
    d = {
        "run_id": "r",
        "terminal": "verified",
        "usage": UsageCounters(
            calls=0, input_bytes=0, output_bytes=0, steps=0, stored_bytes=0
        ),
        "timings": {},
        "artifact_ids": ["ok"],
    }
    d.update(k)
    return TaskResult(**d)


def _bad(fn, **k):
    with pytest.raises(ValidationError):
        fn(**k)


@pytest.mark.parametrize(
    "field", ["authorization", "grant", "approved", "effect_scope", "allow_effect"]
)
@pytest.mark.parametrize("fn", [_req, _res])
def test_grants(fn, field):
    _bad(fn, **{field: True})


def test_accepts():
    assert (Limits().max_steps, Limits().max_seconds, Limits().max_provider_calls) == (
        12,
        120.0,
        12,
    )
    Limits(
        max_steps=1000,
        max_provider_calls=1000,
        max_seconds=600.0,
        max_input_bytes=16777216,
        max_output_bytes=16777216,
        max_stored_bytes=104857600,
    )
    assert _req(goal="  go  ", start_url="http://localhost:1/a#b").goal == "go"
    _req(start_url="https://[::1]:65535/x?y=1#z")
    row = _res(
        terminal="refused",
        development_only=True,
        last_url="https://example.com/p",
        evidence=["redacted"],
        timings={"n" * 64: 0.0},
    )
    assert (
        row.effect_guarantee == "none"
        and row.development_only is True
        and _res(terminal="unsupported", last_url=None).last_url is None
    )
    assert _res(timings={"t": 1}).timings["t"] == 1.0
    Check(kind="visible", target="#go", expected=True)
    Check(kind="count", target="button", expected=0)
    Check(kind="text", target="t" * 512, expected="e" * 4000)


@pytest.mark.parametrize(
    "k",
    [
        {"max_steps": True},
        {"max_steps": 1001},
        {"max_provider_calls": 1001},
        {"max_seconds": math.nan},
        {"max_seconds": math.inf},
        {"max_seconds": 600.1},
        {"max_input_bytes": 16777217},
        {"max_output_bytes": 16777217},
        {"max_stored_bytes": 104857601},
    ],
)
def test_limits(k):
    _bad(Limits, **k)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://e",
        "http://",
        "http://e:0/",
        "http://e:65536/",
        "http://u:p@e/",
        "http://e/a b",
        "http://e/\n",
        "http://e/\x7f",
    ],
)
def test_url(url):
    _bad(_req, start_url=url)


@pytest.mark.parametrize(
    "k",
    [
        {"goal": "  "},
        {"viewports": []},
        {"visual": 1},
        {"values": {str(i): "x" for i in range(33)}},
        {"values": {"k" * 129: "x"}},
        {"values": {"k": "v" * 4001}},
        {"checks": [{"kind": "text", "expected": 1.5}]},
        {"checks": [{"kind": "text", "target": "t" * 4097, "expected": "ok"}]},
        {"checks": [{"kind": "text", "expected": "e" * 4001}]},
        {"checks": [{"kind": "count", "expected": 1}] * 33},
    ],
)
def test_request(k):
    _bad(_req, **k)


@pytest.mark.parametrize(
    "k",
    [
        {"artifact_ids": ["../x"]},
        {"artifact_ids": ["a/b"]},
        {"artifact_ids": [""]},
        {"development_only": 1},
        {"effect_guarantee": "scope"},
        {"timings": {str(i): 1.0 for i in range(17)}},
        {"timings": {"n" * 65: 1.0}},
        {"timings": {"t": math.nan}},
        {"timings": {"t": math.inf}},
        {"timings": {"t": -0.1}},
        {"evidence": ["e"] * 33},
        {"evidence": ["x" * 4097]},
        {"last_url": "https://u:p@h/a"},
        {"last_url": "https://h/a?q=1"},
        {"last_url": "https://h/a#f"},
        {"last_url": "http://"},
        {"last_url": "https://h/\n"},
    ],
)
def test_result(k):
    _bad(_res, **k)


def test_admission_class_output_vocabulary_is_not_a_grant():
    """Admission labels are closed output data. Default stays unresolved. No grant field."""
    names = get_args(AdmissionClass)
    assert names == (
        "fixture_mapped",
        "observe_driver",
        "ordinary_admitted",
        "ordinary_task_scoped",
        "ordinary_beta_admitted",
        "consequential",
        "unresolved",
        "owned_loopback_document",
    )
    assert _res().admission_class == "unresolved"
    assert _res().effect_guarantee == "none"
    for name in names:
        row = _res(admission_class=name)
        dumped = row.model_dump()
        assert row.admission_class == name
        assert row.effect_guarantee == "none"
        assert dumped["admission_class"] == name
        assert "content_id" not in dumped
        assert "approval" not in dumped
        assert "grant" not in dumped
    _bad(_res, admission_class="not_a_class")
    _bad(_res, admission_class="")
    _bad(_res, content_id="owned")
    _bad(_req, content_id="owned")
    _bad(_req, approval=True)
    _bad(_req, grant=True)


@pytest.mark.parametrize("as_json", [False, True])
def test_unused_alias_is_extra_in_python_and_json(as_json):
    """Pydantic 2.14.2: unused field-name aliases must not silently disappear."""
    import json

    from pydantic import ConfigDict
    from pydantic.alias_generators import to_camel

    from decision_browser.contracts import StrictModel

    class AliasedLimits(StrictModel):
        model_config = ConfigDict(extra="forbid", strict=True, alias_generator=to_camel)
        max_steps: int = 12

    def validate(payload):
        if as_json:
            return AliasedLimits.model_validate_json(json.dumps(payload))
        return AliasedLimits.model_validate(payload)
    assert validate({"maxSteps": 4}).max_steps == 4
    for payload in ({"max_steps": 4}, {"maxSteps": 4, "max_steps": 1000}):
        with pytest.raises(ValidationError) as exc:
            validate(payload)
        assert "extra_forbidden" in {error["type"] for error in exc.value.errors()}


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("field", ["approval", "grant", "maxSteps"])
def test_nested_limits_extras_stay_forbidden(as_json, field):
    import json

    payload = _req().model_dump(mode="json", by_alias=True)
    payload["limits"][field] = True
    with pytest.raises(ValidationError) as exc:
        if as_json:
            TaskRequest.model_validate_json(json.dumps(payload))
        else:
            TaskRequest.model_validate(payload)
    assert any(error["type"] == "extra_forbidden" and error["loc"][0] == "limits"
               for error in exc.value.errors())


def test_task_request_json_roundtrip_preserves_public_field_names():
    request = _req()
    payload = request.model_dump_json(by_alias=True)
    assert TaskRequest.model_validate_json(payload) == request
    assert '"start_url"' in payload and '"max_steps"' in payload
