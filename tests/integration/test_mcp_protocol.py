"""Python stdio MCP tests. Strict form accept can record; a host response is not a native human. Not Codex native."""

import asyncio
import copy
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[2]
REQ = {
    "goal": "Read artificial page",
    "start_url": "https://example.test/",
    "mode": "isolated",
    "values": {},
    "checks": [],
    "viewports": [{"width": 390, "height": 844}],
    "limits": {
        "max_steps": 12,
        "max_seconds": 120.0,
        "max_provider_calls": 12,
        "max_input_bytes": 262144,
        "max_output_bytes": 262144,
        "max_stored_bytes": 10485760,
    },
    "privacy": {"blocked_literals": [], "send_images": False},
    "visual": False,
}
NAMES = {
    "start_task",
    "get_task",
    "cancel_task",
    "read_artifact",
    "capabilities",
    "present_admission",
}


def _env():
    env = copy.deepcopy(dict(os.environ))
    env.pop("OPENAI_API_KEY", None)
    env["PYTHONPATH"] = str(ROOT / "src")
    return env


def _params():
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "decision_browser.cli", "serve", "--admission", "standard_form"],
        cwd=str(ROOT),
        env=_env(),
    )


def _zeros(result):
    assert result["usage"]["steps"] == 0
    assert result["usage"]["calls"] == 0


@pytest.mark.parametrize(
    "action,content,kind",
    [
        ("accept", {"reviewed": True}, "accept"),
        ("decline", None, "decline"),
        ("accept", {"reviewed": False}, "reviewed_false"),
        ("cancel", None, "cancel"),
    ],
)
@pytest.mark.asyncio
async def test_scripted_form_ordinary_record_is_not_native_human_proof(action, content, kind):
    """Stdio form callback is a scripted host response, not a native human click."""
    seen = []

    async def answer(ctx, params):
        seen.append(params.message)
        schema = params.requested_schema
        assert set(schema.keys()) == {"type", "properties", "required", "additionalProperties"}
        assert schema["additionalProperties"] is False
        assert schema["properties"]["reviewed"]["type"] == "boolean"
        return types.ElicitResult(action=action, content=content)

    async with stdio_client(_params()) as (read, write):  # noqa: SIM117 - dependent streams
        async with ClientSession(
            read, write, elicitation_callback=answer, read_timeout_seconds=5.0
        ) as session:
            await session.initialize()  # parsed stdio is the transport proof
            caps = (await session.call_tool("capabilities", {})).structured_content
            assert caps["native_human"] == "NOT_RUN"
            assert caps["human_grant_channel"] == "standard_mcp_form"
            assert caps["production_ready"] is False
            assert caps["form_candidate_unverified"] is True
            tools = (await session.list_tools()).tools
            assert {t.name for t in tools} == NAMES and len(tools) == 6
            assert all(
                t.input_schema.get("additionalProperties") is False for t in tools
            )
            raw = await session.call_tool(
                "start_task", {"request": REQ, "approval": True}
            )
            assert raw.is_error is True
            started = await asyncio.wait_for(
                session.call_tool("start_task", {"request": REQ}), timeout=1
            )
            assert started.is_error is False
            result = started.structured_content["result"]
            if result["terminal"] == "queued":
                assert result["reason"] == "preparing_admission"
                assert started.structured_content["pending_proposal"] is None
            else:
                assert result["terminal"] == "awaiting_human_admission"
                assert started.structured_content["pending_proposal"] is not None
            rid = result["run_id"]
            assert rid and "run_id" not in REQ
            _zeros(result)
            pending = None
            for _ in range(50):
                pending = (
                    await session.call_tool("get_task", {})
                ).structured_content.get("pending_proposal")
                if pending:
                    break
                await asyncio.sleep(0.01)
            assert pending and pending["run_id"] == rid
            assert "https://example.test/" in pending["canonical_urls"]
            assert pending["digest"] and "deadline" in pending and "budgets" in pending
            dup = await session.call_tool("start_task", {"request": REQ})
            assert dup.structured_content["error"] == "busy"
            pid = pending["proposal_id"]
            bad = await session.call_tool(
                "present_admission",
                {"run_id": rid, "proposal_id": pid, "digest": "f" * 64},
            )
            assert bad.structured_content["error"] == "unknown" and seen == []
            shown = await session.call_tool(
                "present_admission",
                {"run_id": rid, "proposal_id": pid, "digest": pending["digest"]},
            )
            msg = seen[-1]
            assert (
                pending["digest"] in msg
                and "https://example.test/" in msg
                and "residual" in msg
            )
            assert REQ["goal"] not in msg
            shown_result = shown.structured_content["result"]
            assert "candidate grants nothing" not in msg
            assert "standard MCP form" in msg
            assert "host accept is not proof of a native human click" in msg
            assert shown_result.get("reason") != "no_supported_human_grant_channel"
            if kind == "accept":
                async with asyncio.timeout(5):
                    while (
                        progressed := (
                            await session.call_tool("get_task", {"run_id": rid})
                        ).structured_content
                    )["result"]["terminal"] in _OPEN:
                        await asyncio.sleep(0.01)
                assert progressed["pending_proposal"] is None
                assert progressed["result"]["terminal"] == "blocked"
                assert progressed["result"]["reason"] == "missing_api_key"
                assert progressed["result"]["development_only"] is False
                assert progressed["result"]["effect_guarantee"] == "none"
                _zeros(progressed["result"])
                repeated = await session.call_tool(
                    "present_admission",
                    {"run_id": rid, "proposal_id": pid, "digest": pending["digest"]},
                )
                assert repeated.structured_content["error"] == "presentation_resolved"
                stable = (
                    await session.call_tool("get_task", {"run_id": rid})
                ).structured_content
                assert stable["result"] == progressed["result"]
                assert stable["pending_proposal"] is None
                held = stable
            else:
                assert shown_result["terminal"] in {"running", "blocked"}
                assert shown_result["reason"] == (
                    "finalizing" if shown_result["terminal"] == "running" else "blocked"
                )
                _zeros(shown_result)
                async with asyncio.timeout(5):
                    while True:
                        held = (
                            await session.call_tool("get_task", {"run_id": rid})
                        ).structured_content
                        if held["result"]["terminal"] not in _OPEN:
                            break
                        await asyncio.sleep(0.01)
                assert held["result"]["run_id"] == rid
                assert held["result"]["terminal"] == "blocked"
                assert held["result"]["reason"] == "blocked"
                assert held["pending_proposal"] is None
                _zeros(held["result"])
                repeated = await session.call_tool(
                    "present_admission",
                    {"run_id": rid, "proposal_id": pid, "digest": pending["digest"]},
                )
                assert repeated.structured_content["error"] == "presentation_resolved"
                again = (
                    await session.call_tool("get_task", {"run_id": rid})
                ).structured_content
                assert again["result"] == held["result"]
                assert again["pending_proposal"] is None
            cancelled = (
                await session.call_tool("cancel_task", {"run_id": rid})
            ).structured_content
            assert cancelled["result"]["run_id"] == rid
            if kind != "accept":
                _zeros(cancelled["result"])
            missing = await session.call_tool(
                "read_artifact", {"run_id": rid, "artifact_id": "/tmp/notartifact"}
            )
            assert missing.is_error is True


import base64
import hashlib
import json

_CODE = (
    "import sys\nfrom test_runner import manager, ScriptedProvider\n"
    "from decision_browser.server import create_server\n"
    "create_server(manager(sys.argv[1], ScriptedProvider(['click']))).run(transport='stdio')\n"
)
_OPEN = {"queued", "awaiting_human_admission", "running"}


def _fixture_params(url):
    env = _env()
    env["PYTHONPATH"] = os.pathsep.join(
        (str(ROOT / "src"), str(ROOT / "tests" / "integration"))
    )
    return StdioServerParameters(
        command=sys.executable, args=["-c", _CODE, url], cwd=str(ROOT), env=env
    )


@pytest.mark.asyncio
async def test_development_fixture_stdio_verification_artifact_and_restart(
    counter_page_url,
):
    req = copy.deepcopy(REQ)
    req.update(
        start_url=counter_page_url,
        goal="Update counter",
        checks=[{"kind": "text", "target": "#count", "expected": "1"}],
    )
    req["limits"]["max_seconds"] = 15.0
    async with stdio_client(_fixture_params(counter_page_url)) as (read, write):  # noqa: SIM117
        async with ClientSession(read, write, read_timeout_seconds=20.0) as session:
            await session.initialize()
            started = await session.call_tool("start_task", {"request": req})
            assert started.is_error is False
            run_id = started.structured_content["result"]["run_id"]
            async with asyncio.timeout(20):
                while (
                    result := (
                        await session.call_tool("get_task", {})
                    ).structured_content["result"]
                )["terminal"] in _OPEN:
                    await asyncio.sleep(0.01)
            assert result["terminal"] == "verified" and result["run_id"] == run_id
            assert result["functional"] == "verified"
            assert result["development_only"] is True
            assert result["effect_guarantee"] == "none"
            assert result["usage"]["calls"] == 1 and result["usage"]["steps"] == 2
            artifact_id = result["artifact_ids"][-1]
            body = (
                await session.call_tool(
                    "read_artifact", {"run_id": run_id, "artifact_id": artifact_id}
                )
            ).structured_content
            artifact = body
            raw = base64.b64decode(artifact["content"])
            assert isinstance(json.loads(raw), dict)
            assert hashlib.sha256(raw).hexdigest() == artifact["sha256"]
            assert artifact["id"] == artifact_id and artifact["media_type"]
    async with stdio_client(_fixture_params(counter_page_url)) as (read, write):  # noqa: SIM117
        async with ClientSession(read, write, read_timeout_seconds=20.0) as session:
            await session.initialize()
            stale = await session.call_tool("get_task", {"run_id": run_id})
            assert stale.structured_content["error"] == "unknown"
            fresh = (
                await session.call_tool("start_task", {"request": req})
            ).structured_content["result"]
            cancelled = (
                await session.call_tool("cancel_task", {"run_id": fresh["run_id"]})
            ).structured_content["result"]
            assert cancelled["run_id"] == fresh["run_id"]
            assert cancelled["terminal"] in {"running", "cancelled", "outcome_unknown"}
            async with asyncio.timeout(5):
                while cancelled["terminal"] in _OPEN:
                    await asyncio.sleep(0.01)
                    cancelled = (await session.call_tool("get_task", {"run_id": fresh["run_id"]})).structured_content["result"]
            assert cancelled["terminal"] in {"cancelled", "outcome_unknown"}
