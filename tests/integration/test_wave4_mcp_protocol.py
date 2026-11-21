"""Actual pinned stdio clients; synthetic stores/consent and owned Chromium only."""

import asyncio
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client

from decision_browser import __version__

ROOT = Path(__file__).resolve().parents[2]
CAP = 65_536
RAW = bytes(range(256)) * 512 + b"tail"
REQUEST = {
    "goal": "Read synthetic page", "start_url": "https://example.test/",
    "values": {}, "checks": [], "limits": {}, "privacy": {},
}
_STORE_SERVER = """
import sys
from decision_browser.runner import RunManager
from decision_browser.contracts import TaskRequest
from decision_browser.server import create_server
manager = RunManager(lambda: None)
request = TaskRequest(goal='Read synthetic page', start_url='https://example.test/',
                      values={}, checks=[], limits={}, privacy={})
run = manager._open(request)
manager._run = run
raw = b'small artifact' if sys.argv[1] == 'small' else bytes(range(256))*512+b'tail'
aid = run.store.put_bytes(raw, 'application/octet-stream')
manager._stamp(run, aid)
create_server(manager).run(transport='stdio')
"""


def params(tmp_path, *, store=None):
    env = {key: os.environ[key] for key in ("PATH", "HOME", "SYSTEMROOT", "WINDIR") if key in os.environ}
    env["DECISION_BROWSER_CONFIG_FILE"] = str(tmp_path / "no-user-profile.json")
    args = ["-c", _STORE_SERVER, store] if store else ["-m", "decision_browser.cli", "serve"]
    return StdioServerParameters(command=sys.executable, args=args, cwd=str(ROOT), env=env)


_STOP_SERVER = """
import atexit, json, sys
from pathlib import Path
from test_runner import manager, RecordingDriver, ScriptedProvider
from decision_browser.decision_codec import Decision
from decision_browser.server import create_server
class StopProvider(ScriptedProvider):
    async def decide(self, questions, input_text, *, budget, privacy, **kwargs):
        privacy.check_payload(input_text.encode())
        budget.reserve_provider(len(input_text.encode()))
        budget.consume_output(1)
        self.calls += 1
        assert sys.argv[2] in questions[0].choices
        return Decision(sys.argv[2], None, 1.0)
provider = StopProvider([])
host = manager(sys.argv[1], provider)
@atexit.register
def receipt():
    Path(sys.argv[3]).write_text(json.dumps({
        'counts':[driver.observed_count for driver in RecordingDriver.seen],
        'calls':provider.calls,
        'clean':all(not driver.cleanup_errors and not driver.residual_owned and
                    not driver.foreign_mismatch for driver in RecordingDriver.seen),
    }))
create_server(host).run(transport='stdio')
"""


@pytest.mark.parametrize("operation,expected,terminal,functional,calls", [
    ("DONE", None, "completed_unverified", "unverified", 1),
    ("UNABLE", None, "blocked", "unverified", 1),
    ("UNABLE", "1", "verification_failed", "failed", 1),
    ("UNABLE", "0", "verified", "verified", 0),
])
async def test_stdio_model_stop_retains_independent_oracle_and_no_click(
    tmp_path, counter_page_url, operation, expected, terminal, functional, calls,
):
    descriptor = params(tmp_path)
    descriptor.env["PYTHONPATH"] = str(ROOT / "tests/integration")
    descriptor.env["PLAYWRIGHT_BROWSERS_PATH"] = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
    closed = tmp_path / "owned-close.json"
    descriptor.args = ["-c", _STOP_SERVER, counter_page_url, operation, str(closed)]
    async with stdio_client(descriptor) as (read, write):  # noqa: SIM117
        async with ClientSession(read, write, read_timeout_seconds=20) as session:
            await session.initialize()
            request = {**REQUEST, "start_url": counter_page_url}
            if expected is not None:
                request["checks"] = [{"kind": "text", "target": "#count", "expected": expected}]
            response = await session.call_tool("start_task", {"request": request})
            assert response.is_error is False
            run_id = response.structured_content["result"]["run_id"]
            async with asyncio.timeout(20):
                while True:
                    result = (await session.call_tool("get_task", {"run_id": run_id})).structured_content["result"]
                    if result["terminal"] not in {"queued", "awaiting_human_admission", "running"}:
                        break
                    await asyncio.sleep(0.01)
            assert result["terminal"] == terminal and result["functional"] == functional
            assert result["usage"]["steps"] == 1 and result["usage"]["calls"] == calls
            assert result["effect_guarantee"] == "none" and result["development_only"] is True
            if operation == "UNABLE" and calls:
                assert "model:unable" in result["evidence"]
            if expected is None and operation == "UNABLE":
                assert result["reason"] == "model_unable"
            events = []
            for aid in result["artifact_ids"]:
                body = (await session.call_tool("read_artifact", {
                    "run_id": run_id, "artifact_id": aid,
                })).structured_content
                raw = base64.b64decode(body["content"])
                assert hashlib.sha256(raw).hexdigest() == body["sha256"]
                events.append(json.loads(raw))
            # Actual Policy/Budget/Journal run: only the admitted owned bootstrap
            # was dispatched, and its mandatory evidence survived finalization.
            assert sum(event.get("event") == "before_dispatch" for event in events) == 1
            assert sum(event.get("event") == "outcome" and event.get("status") == "acknowledged" for event in events) == 1
    lifecycle = json.loads(closed.read_text())
    assert lifecycle["calls"] == calls and lifecycle["clean"] is True
    assert lifecycle["counts"] and set(lifecycle["counts"]) == {0}


@pytest.mark.parametrize("channel", ["structured", "resource"])
async def test_stdio_domain_errors_literal_ids_and_bounded_ranges(tmp_path, channel):
    async with stdio_client(params(tmp_path, store="large")) as (read, write):  # noqa: SIM117
        async with ClientSession(read, write, read_timeout_seconds=10) as session:
            init = await session.initialize()
            assert init.server_info.version == __version__
            current = (await session.call_tool("get_task", {})).structured_content
            run_id = current["result"]["run_id"]
            artifact_id = current["result"]["artifact_ids"][0]
            for arguments in [{"run_id": "null"}, {"run_id": "missing"}]:
                error = await session.call_tool("get_task", arguments)
                assert error.is_error is True and error.structured_content == {"error": "unknown"}
                assert json.loads(error.content[0].text) == error.structured_content
            assert (await session.call_tool("get_task", {"run_id": None})).structured_content == current
            busy = await session.call_tool("start_task", {"request": REQUEST})
            assert busy.is_error is True and busy.structured_content == {"error": "busy"}
            missing = await session.call_tool("read_artifact", {"run_id": run_id, "artifact_id": "0" * 32})
            assert missing.is_error is True and missing.structured_content == {"error": "failed"}
            foreign = await session.call_tool("read_artifact", {"run_id": "foreign", "artifact_id": artifact_id})
            assert foreign.is_error is True and foreign.structured_content == {"error": "unknown"}
            legacy = await session.call_tool("read_artifact", {"run_id": run_id, "artifact_id": artifact_id})
            assert legacy.is_error is True and legacy.structured_content == {"error": "artifact_range_required"}
            assert len(legacy.model_dump_json()) < 1000
            offset = 0
            chunks = []
            while True:
                response = await session.call_tool("read_artifact", {
                    "run_id": run_id, "artifact_id": artifact_id,
                    "channel": channel, "offset": offset, "limit": CAP,
                })
                assert response.is_error is False
                body = response.structured_content
                assert body["format"] == "decision-browser-artifact-range-v1"
                assert body["id"] == artifact_id and body["offset"] == offset
                assert body["sha256"] == hashlib.sha256(RAW).hexdigest()
                assert body["total_bytes"] == len(RAW)
                encoded = body["content"] if channel == "structured" else response.content[0].resource.blob
                assert response.model_dump_json().count(encoded) == 1
                chunk = base64.b64decode(encoded)
                assert len(chunk) <= CAP and hashlib.sha256(chunk).hexdigest() == body["chunk_sha256"]
                chunks.append(chunk)
                offset += len(chunk)
                assert body["next_offset"] == (None if offset == len(RAW) else offset)
                if body["eof"]:
                    break
            assert b"".join(chunks) == RAW


async def test_stdio_small_default_artifact_is_backwards_compatible(tmp_path):
    async with stdio_client(params(tmp_path, store="small")) as (read, write):  # noqa: SIM117
        async with ClientSession(read, write, read_timeout_seconds=10) as session:
            await session.initialize()
            current = (await session.call_tool("get_task", {})).structured_content["result"]
            response = await session.call_tool("read_artifact", {
                "run_id": current["run_id"], "artifact_id": current["artifact_ids"][0],
            })
            body = response.structured_content
            assert response.is_error is False
            assert set(body) == {"id", "media_type", "content", "sha256"}
            assert json.loads(response.content[0].text) == body
            assert base64.b64decode(body["content"]) == b"small artifact"
            assert response.model_dump_json().count(body["content"]) == 2  # Explicit bounded legacy compatibility.


@pytest.mark.parametrize("form", [False, True])
async def test_stdio_url_only_refused_but_legacy_empty_form_retained(tmp_path, form):
    shown = []

    async def answer(_context, request):
        shown.append(request)
        assert request.requested_schema["additionalProperties"] is False
        return types.ElicitResult(action="accept", content={"reviewed": True})

    class DeclaredSession(ClientSession):
        def _build_capabilities(self, protocol_version):
            # Test-only handshake fixture: the production guard reads the
            # ServerSession's public client_capabilities after actual stdio init.
            return types.ClientCapabilities(elicitation=(
                types.ElicitationCapability() if form else
                types.ElicitationCapability(url=types.UrlElicitationCapability())
            ))

    async with stdio_client(params(tmp_path)) as (read, write):  # noqa: SIM117
        async with DeclaredSession(read, write, elicitation_callback=answer, read_timeout_seconds=10) as session:
            await session.initialize()
            started = (await session.call_tool("start_task", {"request": REQUEST})).structured_content
            run_id = started["result"]["run_id"]
            async with asyncio.timeout(5):
                while True:
                    current = (await session.call_tool("get_task", {"run_id": run_id})).structured_content
                    if current["pending_proposal"]:
                        break
                    assert current["result"]["terminal"] == "queued"
                    assert current["result"]["reason"] == "preparing_admission"
                    await asyncio.sleep(0.01)
            pending = current["pending_proposal"]
            assert current["result"]["terminal"] == "awaiting_human_admission"
            response = await session.call_tool("present_admission", {
                "run_id": run_id, "proposal_id": pending["proposal_id"], "digest": pending["digest"],
            })
            async with asyncio.timeout(5):
                while True:
                    result = (await session.call_tool("get_task", {"run_id": run_id})).structured_content["result"]
                    if result["terminal"] not in {"queued", "running", "awaiting_human_admission"}:
                        break
                    await asyncio.sleep(0.01)
            assert response.is_error is False
            assert result["terminal"] == "blocked" and result["functional"] == "not_run"
            assert result["reason"] == ("missing_api_key" if form else "no_supported_human_grant_channel")
            assert len(shown) == int(form)
            assert result["usage"]["calls"] == result["usage"]["steps"] == 0
            assert result["effect_guarantee"] == "none" and result["development_only"] is False
