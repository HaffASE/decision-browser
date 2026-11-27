"""Exercise discoverability on the real CLI and preserve explicit result envelopes.

No provider calls, browser launches, user profile reads or human grants occur.
"""

import base64
import json
import os
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.runner import RunManager
from decision_browser.server import create_server

ROOT = Path(__file__).resolve().parents[2]
NAMES = {"start_task", "get_task", "cancel_task", "read_artifact", "capabilities", "present_admission"}
READS = {"get_task", "read_artifact", "capabilities"}


def assert_definitions(tools):
    by_name = {tool.name: tool for tool in tools}
    assert set(by_name) == NAMES
    for name, tool in by_name.items():
        assert tool.description and len(tool.description) > 150
        assert tool.input_schema["additionalProperties"] is False
        for prop in tool.input_schema["properties"].values():
            assert prop.get("description")
        assert tool.output_schema["type"] == "object"
        assert tool.output_schema["properties"]
        Draft202012Validator.check_schema(tool.output_schema)
        assert tool.annotations.read_only_hint is (name in READS)
        assert tool.annotations.open_world_hint is (name not in READS)
        if name not in READS:
            assert tool.annotations.destructive_hint is True
            assert tool.annotations.idempotent_hint is (name == "cancel_task")
    task_schema = by_name["start_task"].input_schema
    for model in ("TaskRequest", "Check", "Limits", "PrivacyPolicy", "Viewport"):
        assert task_schema["$defs"][model]["additionalProperties"] is False
        assert all(p.get("description") for p in task_schema["$defs"][model]["properties"].values())
    assert "without run_id" in by_name["start_task"].description
    assert "Never retry start_task" in by_name["start_task"].description
    assert "pending_proposal" in by_name["present_admission"].description
    assert "does not itself grant" in by_name["present_admission"].description
    assert "cleanup" in by_name["cancel_task"].description
    assert "next_offset" in by_name["read_artifact"].description
    assert "production_ready" in by_name["capabilities"].description
    return by_name


@pytest.mark.parametrize("mode", ["standard_form", "task_scope"])
async def test_real_cli_lists_actionable_schemas_and_preserves_errors(tmp_path, mode):
    env = {key: os.environ[key] for key in ("PATH", "HOME", "SYSTEMROOT", "WINDIR") if key in os.environ}
    env.update(PYTHONPATH=str(ROOT / "src"),
               DECISION_BROWSER_CONFIG_FILE=str(tmp_path / "absent-provider.json"))
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "decision_browser.cli", "serve", "--admission", mode],
        cwd=str(tmp_path), env=env,
    )
    async with stdio_client(params) as (read, write):  # noqa: SIM117 - dependent streams
        async with ClientSession(read, write, read_timeout_seconds=20) as session:
            await session.initialize()
            listed = assert_definitions((await session.list_tools()).tools)
            caps = await session.call_tool("capabilities", {})
            assert not caps.is_error
            body = caps.structured_content
            Draft202012Validator(listed["capabilities"].output_schema).validate(body)
            assert json.loads(caps.content[0].text) == body
            assert body["production_ready"] is False
            assert body["provider_ready"] is False
            assert body["admission_mode"] == mode
            # Domain errors remain error envelopes even with a success outputSchema.
            for name, args in [
                ("get_task", {}), ("get_task", {"run_id": "null"}),
                ("cancel_task", {"run_id": "absent"}),
                ("present_admission", {"run_id": "absent", "proposal_id": "absent", "digest": "a" * 64}),
                ("read_artifact", {"run_id": "absent", "artifact_id": "absent"}),
            ]:
                result = await session.call_tool(name, args)
                assert result.is_error
                assert result.structured_content == {"error": "unknown"}
                assert json.loads(result.content[0].text) == {"error": "unknown"}
            invalid = await session.call_tool("get_task", {"approval": True})
            assert invalid.is_error


@pytest.mark.parametrize("channel", ["legacy", "structured", "resource"])
async def test_output_schema_accepts_channels_without_rewrapping(channel):
    manager = RunManager(lambda: None)
    request = TaskRequest(goal="Read synthetic data", start_url="https://example.test/",
                          values={}, checks=[], limits=Limits(), privacy=PrivacyPolicy())
    run = manager._open(request)
    manager._run = run
    aid = run.store.put_bytes(b"synthetic evidence", "application/octet-stream")
    server = create_server(manager)
    listed = {tool.name: tool for tool in await server.list_tools()}
    try:
        result = await server.call_tool("read_artifact", {
            "run_id": run.result.run_id, "artifact_id": aid, "channel": channel,
        })
        body = result.structured_content
        assert not result.is_error
        Draft202012Validator(listed["read_artifact"].output_schema).validate(body)
        assert "result" not in body
        if channel == "legacy":
            assert set(body) == {"id", "media_type", "content", "sha256"}
            assert json.loads(result.content[0].text) == body
        elif channel == "resource":
            assert "content" not in body
            assert base64.b64decode(result.content[0].resource.blob) == b"synthetic evidence"
        else:
            assert base64.b64decode(body["content"]) == b"synthetic evidence"
            assert "content" not in json.loads(result.content[0].text)
        # A lost start response is recoverable without launching another task.
        snapshot = await server.call_tool("get_task", {})
        assert snapshot.structured_content["result"]["run_id"] == run.result.run_id
        Draft202012Validator(listed["get_task"].output_schema).validate(snapshot.structured_content)
    finally:
        await manager.aclose()
