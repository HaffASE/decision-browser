"""Unsupported DOM observations expose bounded, code-owned mechanism tokens."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.dom import Observation
from decision_browser.runner import RunManager


async def _unsupported(reasons: tuple[str, ...], evidence: list[str] | None = None):
    host = RunManager(lambda: None)
    run = host._open(
        TaskRequest(
            goal="Find flights",
            start_url="https://example.test/",
            values={},
            checks=[],
            privacy=PrivacyPolicy(),
            limits=Limits(max_seconds=10),
        )
    )
    host._run = run
    if evidence is not None:
        host._set(run, evidence=evidence)
    choose = AsyncMock(side_effect=AssertionError("provider decision attempted"))
    dispatch = AsyncMock(side_effect=AssertionError("effect attempted"))
    verify = AsyncMock(side_effect=AssertionError("verification attempted"))
    host._choose, host._dispatch_one, host._verify = choose, dispatch, verify
    obs = Observation("obs", "https://example.test", "doc", "", {}, reasons)
    await host._loop(run, obs)
    choose.assert_not_awaited()
    dispatch.assert_not_awaited()
    verify.assert_not_awaited()
    result = host.get_task(run.result.run_id)
    assert result.terminal == "unsupported"
    assert result.reason == "unsupported"
    assert result.usage.calls == 0
    assert result.usage.steps == 0
    assert run.current_observation is obs
    return result


@pytest.mark.parametrize(
    "mechanism",
    [
        "capacity",
        "canvas",
        "iframe",
        "shadow",
        "file",
        "password",
        "download",
        "newwindow",
        "namedtarget",
        "oversized",
    ],
)
async def test_exact_code_owned_mechanism_is_public(mechanism: str):
    result = await _unsupported((mechanism,))
    assert result.evidence == [f"unsupported:{mechanism}"]


@pytest.mark.parametrize("count", ["1", "2", "9999999999"])
async def test_popup_count_is_not_echoed(count: str):
    result = await _unsupported((f"popup:{count}",))
    assert result.evidence == ["unsupported:popup"]


@pytest.mark.parametrize(
    "reason",
    [
        "capacity:https://secret.test/?token=private",
        "canvas private-value",
        "https://secret.test/private",
        "popup:private-value",
        "popup:1:private-value",
        "popup:0",
        "popup:-1",
        "popup:１２",
        "popup:99999999999",
        "popup",
        "",
    ],
)
async def test_unknown_and_malformed_reasons_are_never_retained(reason: str):
    result = await _unsupported((reason,))
    assert result.evidence == ["unsupported:unknown"]
    assert "private" not in result.model_dump_json()


async def test_diagnostics_are_deduplicated_capped_and_keep_existing_evidence():
    result = await _unsupported(
        ("capacity", "capacity", "canvas", "popup:1", "popup:2", "attacker") * 100,
        [f"prior:{i}" for i in range(32)],
    )
    assert result.evidence[:4] == [
        "unsupported:capacity",
        "unsupported:canvas",
        "unsupported:popup",
        "unsupported:unknown",
    ]
    assert result.evidence[4:] == [f"prior:{i}" for i in range(28)]
    assert len(result.evidence) == 32
