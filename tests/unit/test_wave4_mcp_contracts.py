"""Pinned MCP contracts with real manager/store and bounded synthetic artifacts."""

import base64
import hashlib
import json
from types import SimpleNamespace

import pytest
from mcp import types
from mcp.server.connection import Connection
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.session import ServerSession

from decision_browser import __version__
from decision_browser.artifacts import ArtifactError
from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.decision_codec import Decision
from decision_browser.dom import Observation
from decision_browser.runner import RunManager
from decision_browser.server import StandardFormDesk, _mcp_ctx, create_server

CAP = 65_536


def opened():
    manager = RunManager(lambda: None)
    run = manager._open(request())
    manager._run = run
    return manager, run


def request():
    return TaskRequest(goal="Read synthetic content", start_url="https://example.test/",
                       values={}, checks=[], limits=Limits(), privacy=PrivacyPolicy())


@pytest.mark.parametrize("tool,args", [
    ("get_task", {}),
    ("cancel_task", {"run_id": "missing"}),
    ("read_artifact", {"run_id": "missing", "artifact_id": "missing"}),
    ("present_admission", {"run_id": "missing", "proposal_id": "missing", "digest": "a" * 64}),
])
async def test_domain_errors_signal_mcp_error_with_fixed_code(tool, args):
    result = await create_server().call_tool(tool, args)
    assert result.is_error is True
    assert result.structured_content == {"error": "unknown"}
    assert json.loads(result.content[0].text) == result.structured_content


@pytest.mark.parametrize("literal", ["null", "true", "false"])
async def test_literal_ids_do_not_select_current_run(literal):
    manager, run = opened()
    response = await create_server(manager).call_tool("get_task", {"run_id": literal})
    assert response.is_error is True
    assert response.structured_content == {"error": "unknown"}
    assert manager.get_task().run_id == run.result.run_id


@pytest.mark.parametrize("args", [{}, {"run_id": None}])
async def test_missing_and_json_null_retain_current_run_read(args):
    manager, run = opened()
    response = await create_server(manager).call_tool("get_task", args)
    assert response.is_error is False
    assert response.structured_content["result"]["run_id"] == run.result.run_id


async def test_legacy_small_artifact_preserves_both_original_envelopes():
    manager, run = opened()
    aid = run.store.put_bytes(b"synthetic small content", "application/octet-stream")
    result = await create_server(manager).call_tool("read_artifact", {
        "run_id": run.result.run_id, "artifact_id": aid,
    })
    body = result.structured_content
    assert set(body) == {"id", "media_type", "content", "sha256"}
    assert json.loads(result.content[0].text) == body
    assert base64.b64decode(body["content"]) == b"synthetic small content"
    assert hashlib.sha256(b"synthetic small content").hexdigest() == body["sha256"]


async def test_legacy_large_artifact_requires_explicit_range_channel():
    manager, run = opened()
    aid = run.store.put_bytes(b"x" * (CAP + 1), "application/octet-stream")
    result = await create_server(manager).call_tool("read_artifact", {
        "run_id": run.result.run_id, "artifact_id": aid,
    })
    assert result.is_error is True
    assert result.structured_content == {"error": "artifact_range_required"}
    assert len(result.model_dump_json()) < 1000


@pytest.mark.parametrize("raw,offset", [(b"", 0), (b"content", 7)])
async def test_explicit_eof_is_bounded_and_hash_bound(raw, offset):
    manager, run = opened()
    aid = run.store.put_bytes(raw, "application/octet-stream")
    response = await create_server(manager).call_tool("read_artifact", {
        "run_id": run.result.run_id, "artifact_id": aid, "channel": "structured", "offset": offset,
    })
    body = response.structured_content
    assert response.is_error is False
    assert body["content"] == "" and body["eof"] is True and body["next_offset"] is None
    assert body["sha256"] == hashlib.sha256(raw).hexdigest()
    assert body["chunk_sha256"] == hashlib.sha256(b"").hexdigest()


async def test_range_bounds_and_legacy_range_failure_do_not_return_partial_whole():
    manager, run = opened()
    aid = run.store.put_bytes(b"content", "application/octet-stream")
    server = create_server(manager)
    for args, code in [({"offset": 1}, "artifact_range_required"),
                       ({"limit": 2}, "artifact_range_required"),
                       ({"channel": "structured", "offset": 8}, "bad_range")]:
        response = await server.call_tool("read_artifact", {
            "run_id": run.result.run_id, "artifact_id": aid, **args,
        })
        assert response.is_error is True and response.structured_content == {"error": code}
    assert manager.read_artifact(run.result.run_id, aid).content == b"content"


@pytest.mark.parametrize("offset,limit", [(True, 1), (-1, 1), (0, True), (0, 0), (0, CAP + 1)])
def test_store_range_has_its_own_exact_integer_boundary(offset, limit):
    manager, run = opened()
    aid = run.store.put_bytes(b"safe", "application/octet-stream")
    with pytest.raises(ArtifactError, match="bad_range"):
        manager.read_artifact_range(run.result.run_id, aid, offset=offset, limit=limit)


async def test_artifact_range_keeps_run_ownership_and_original_store_error_codes():
    manager, run = opened()
    aid = run.store.put_bytes(b"safe", "application/octet-stream")
    server = create_server(manager)
    for rid, art, code in [("foreign", aid, "unknown"), (run.result.run_id, "0" * 32, "failed")]:
        result = await server.call_tool("read_artifact", {
            "run_id": rid, "artifact_id": art, "channel": "structured",
        })
        assert result.is_error is True and result.structured_content == {"error": code}


@pytest.mark.parametrize("channel", ["structured", "resource"])
async def test_explicit_channels_page_and_encode_each_chunk_once(channel):
    manager, run = opened()
    raw = b"abcd" * CAP + b"tail"
    aid = run.store.put_bytes(raw, "application/octet-stream")
    server = create_server(manager)
    chunks = []
    offset = 0
    while True:
        result = await server.call_tool("read_artifact", {
            "run_id": run.result.run_id, "artifact_id": aid,
            "channel": channel, "offset": offset, "limit": CAP,
        })
        body = result.structured_content
        assert result.is_error is False
        assert body["format"] == "decision-browser-artifact-range-v1"
        assert body["id"] == aid and body["total_bytes"] == len(raw)
        assert body["sha256"] == hashlib.sha256(raw).hexdigest()
        assert body["offset"] == offset
        if channel == "structured":
            encoded = body["content"]
            assert "content" not in json.loads(result.content[0].text)
        else:
            assert "content" not in body
            assert result.content[0].type == "resource"
            encoded = result.content[0].resource.blob
            assert result.content[0].resource.mime_type == "application/octet-stream"
        chunk = base64.b64decode(encoded)
        assert 0 < len(chunk) <= CAP
        assert hashlib.sha256(chunk).hexdigest() == body["chunk_sha256"]
        assert result.model_dump_json().count(encoded) == 1
        chunks.append(chunk)
        offset += len(chunk)
        assert body["next_offset"] == (None if offset == len(raw) else offset)
        if body["eof"]:
            break
    assert b"".join(chunks) == raw


@pytest.mark.parametrize("field,value", [
    ("offset", True), ("offset", -1), ("offset", 1.0), ("offset", "1"),
    ("limit", True), ("limit", 0), ("limit", CAP + 1), ("limit", "10"),
    ("channel", "unknown"),
])
async def test_range_argument_types_rejected_before_read(field, value):
    manager, run = opened()
    aid = run.store.put_bytes(b"safe", "application/octet-stream")
    with pytest.raises(ToolError):
        await create_server(manager).call_tool("read_artifact", {
            "run_id": run.result.run_id, "artifact_id": aid,
            "channel": "structured", field: value,
        })


async def test_initial_admission_is_explicitly_preparing_until_proposal_exists():
    manager = RunManager(lambda: None)
    try:
        result = await create_server(manager).call_tool("start_task", {"request": request().model_dump()})
        body = result.structured_content
        assert body["result"]["terminal"] == "queued"
        assert body["result"]["reason"] == "preparing_admission"
        assert body["pending_proposal"] is None
        assert body["result"]["usage"]["steps"] == 0
        assert body["result"]["usage"]["calls"] == 0
    finally:
        await manager.aclose()


async def test_development_pending_display_matches_task_result():
    manager, run = opened()
    run.result.development_only = True
    run.pending = manager._admission.propose_alpha(
        run.result.run_id, ["https://example.test/"], ["click"], {"steps": 1},
        run.budget.original_deadline, development_only=True,
    )
    body = (await create_server(manager).call_tool("get_task", {})).structured_content
    assert body["result"]["development_only"] is True
    assert body["pending_proposal"]["development_only"] is True


async def test_cancel_wrapper_uses_returned_public_result_without_second_get():
    manager, run = opened()
    reads = []

    def trap_get(*args):
        reads.append(args)
        raise AssertionError("redundant get_task")

    manager.get_task = trap_get
    result = await create_server(manager).call_tool("cancel_task", {"run_id": run.result.run_id})
    assert result.is_error is False and reads == []
    assert result.structured_content["result"]["terminal"] == "cancelled"
    assert result.structured_content["pending_proposal"] is None


@pytest.mark.parametrize("capability", [None, types.ElicitationCapability(url=types.UrlElicitationCapability())])
async def test_actual_pinned_capability_predicate_cannot_substitute_for_form(capability):
    from decision_browser.admission import AdmissionStore
    from decision_browser.desk import DeskUnavailable

    store = AdmissionStore(clock=lambda: 1.0, cleanup_grace=2.0)
    proposal = store.propose_alpha("run-1", ["https://example.test/"], ["click"], {}, 20.0)
    connection = Connection(SimpleNamespace(), protocol_version="2025-11-25")
    connection.client_capabilities = types.ClientCapabilities(elicitation=capability)
    session = object.__new__(ServerSession)
    session._connection = connection
    calls = []

    async def elicit_form(**kwargs):
        calls.append(kwargs)
        return types.ElicitResult(action="accept", content={"reviewed": True})

    session.elicit_form = elicit_form
    token = _mcp_ctx.set(SimpleNamespace(request_context=SimpleNamespace(session=session), request_id="req"))
    try:
        with pytest.raises(DeskUnavailable, match="no_supported_human_grant_channel"):
            await StandardFormDesk(store).present(proposal)
    finally:
        _mcp_ctx.reset(token)
    assert calls == [] and not store.is_live(proposal.proposal_id)


async def test_no_check_choices_offer_done_and_unable_without_verification_claim():
    manager, run = opened()
    obs = Observation("obs", "https://example.test", "doc", "", {}, ())
    questions, _heads, _bindings = manager._questions(run, obs)
    assert {"DONE", "UNABLE"} <= set(questions[0].choices)

    class UnableProvider:
        async def decide(self, *args, **kwargs):
            return Decision("UNABLE", None, 1.0)

    run.dom, run.provider = object(), UnableProvider()
    assert await manager._choose(run, obs) is None
    assert run.stop_choice == "UNABLE"


async def test_capabilities_version_and_range_contract_are_code_owned():
    server = create_server()
    assert server._lowlevel_server.version == __version__
    body = (await server.call_tool("capabilities", {})).structured_content
    assert body["artifact_read"] == {
        "format": "decision-browser-artifact-range-v1", "max_chunk_bytes": CAP,
        "default_channel": "legacy", "channels": ["legacy", "structured", "resource"],
    }
