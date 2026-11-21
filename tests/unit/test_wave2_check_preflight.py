"""F07: check preflight and direct fixture maps fail before resource work."""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from decision_browser.contracts import Check, Limits, PrivacyPolicy, TaskRequest
from decision_browser.runner import RunManager
from decision_browser.server import create_server
from decision_browser.verification import verify_functional


def request(checks):
    return TaskRequest(
        goal="Read an owned page", start_url="https://example.test/", values={},
        checks=checks, limits=Limits(max_seconds=10), privacy=PrivacyPolicy(),
    )


@pytest.mark.parametrize("payload", [
    {"kind": "route", "target": "#route", "expected": "https://example.test/"},
    {"kind": "route", "expected": 1},
    {"kind": "text", "target": "#note", "expected": True},
    {"kind": "value", "target": "#field", "expected": 1},
    {"kind": "visible", "target": "#go", "expected": "true"},
    {"kind": "count", "target": "button", "expected": True},
    {"kind": "count", "target": "button", "expected": "1"},
    {"kind": "count", "target": "button", "expected": -1},
    {"kind": "text", "target": "", "expected": "ready"},
    {"kind": "text", "target": "界" * 171, "expected": "ready"},
    {"kind": "text", "target": "#note", "expected": "界" * 1366},
    {"kind": "text", "target": "\ud800", "expected": "ready"},
    {"kind": "text", "target": "#note", "expected": "\ud800"},
    {"kind": "text", "target": "xpath=//p", "expected": "ready"},
    {"kind": "text", "target": "#note >> visible=true", "expected": "ready"},
])
def test_check_preflight_rejects_semantically_impossible_or_oversized_checks(payload):
    with pytest.raises(ValidationError):
        Check.model_validate(payload)


@pytest.mark.parametrize("payload", [
    {"kind": "route", "expected": "https://example.test/"},
    {"kind": "text", "target": "界" * 170, "expected": "界" * 1365},
    {"kind": "value", "target": "#field", "expected": "v" * 4000},
    {"kind": "visible", "target": "#go", "expected": False},
    {"kind": "count", "target": "button", "expected": 0},
])
def test_check_preflight_preserves_valid_boundaries(payload):
    assert Check.model_validate(payload).model_dump() == {"target": "", **payload}


def test_task_request_rejects_fixture_state_without_a_trusted_adapter():
    check = Check(kind="fixture_state", target="ready", expected=True)
    with pytest.raises(ValidationError):
        request([check])


def test_mutated_fixture_state_request_fails_before_real_manager_resources():
    calls = []

    def forbidden_factory(*args):
        calls.append(args)
        raise AssertionError("resource construction must not start")

    manager = RunManager(forbidden_factory, forbidden_factory)
    mutated = request([])
    mutated.checks = [Check(kind="fixture_state", target="ready", expected=True)]
    with pytest.raises(ValidationError):
        manager.start_task(mutated)
    assert calls == []
    assert manager._run is None


async def test_mcp_fixture_state_fails_before_manager_body():
    class NeverManager:
        calls = 0

        def start_task(self, value):
            self.calls += 1
            raise AssertionError("unsupported checks must not start a run")

    manager = NeverManager()
    payload = request([]).model_dump()
    payload["checks"] = [{"kind": "fixture_state", "target": "ready", "expected": True}]
    with pytest.raises(ToolError):
        await create_server(manager=manager).call_tool("start_task", {"request": payload})
    assert manager.calls == 0


@pytest.mark.parametrize("state", [
    4, [], "ready", {"ready": 1.0}, {"ready": None}, {1: True},
    {"key" * 1400: True}, {"ready": "界" * 1366},
    {f"key{i}": True for i in range(65)},
])
async def test_direct_fixture_state_mapping_is_bounded_and_validated(state):
    report = await verify_functional(
        object(), [Check(kind="fixture_state", target="ready", expected=True)],
        fixture_state=state,
    )
    assert report.terminal == "verification_failed"
    assert [row.reason for row in report.evidence] == ["fixture_state_invalid"]


@pytest.mark.parametrize("state,target,expected", [
    ({"ready": True}, "ready", True),
    ({"count": 2}, "count", 2),
    ({"phase": "open"}, "phase", "open"),
])
async def test_direct_fixture_state_supported_scalars_remain_valid(state, target, expected):
    report = await verify_functional(
        object(), [Check(kind="fixture_state", target=target, expected=expected)],
        fixture_state=state,
    )
    assert report.terminal == "verified"
