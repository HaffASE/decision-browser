"""Offline doubles only: no browser, credential, live HTTP provider, or human proof."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from mcp.types import (
    ClientCapabilities,
    ElicitationCapability,
    FormElicitationCapability,
)

import decision_browser.counter_acceptance as counter
from decision_browser.artifacts import Artifact
from decision_browser.browser import BrowserDriver
from decision_browser.contracts import TaskResult, UsageCounters
from decision_browser.decision_codec import ChoiceQuestion, Decision
from decision_browser.model_context import prepare_context
from decision_browser.runner import RunManager, RunnerError
from decision_browser.server import StandardFormDesk, _mcp_ctx, create_server


def artifact(index, body):
    raw = json.dumps(body).encode()
    return Artifact(f"{index:032x}", "application/json", raw, hashlib.sha256(raw).hexdigest())


def evidence(url="http://127.0.0.1:12345/counter.html", clicks=3, operation="click", name="inc"):
    request = counter.counter_request(url)
    store = counter.AdmissionStore(clock=lambda: 10.0)
    proposal = store.propose_alpha("run", [url], ["navigate", "click"],
                                   request.limits.model_dump(), 130.0)
    artifacts = []
    for index in range(clicks + 1):
        if index:
            artifacts.append(artifact(len(artifacts) + 1, {
                "event": "admission_binding", "run_id": "run",
                "proposal_id": proposal.proposal_id, "digest": proposal.digest,
            }))
        semantic = {
            "operation": operation if index else "navigate", "unsupported": None,
            "origin": url.rsplit("/", 1)[0] if index else "null",
            "url": url if index else "about:blank", "form": None,
            "node": {"name": name, "role": "button", "state": {
                "tag": "button", "type": "button", "disabled": False,
            }} if index else None,
        }
        before = artifact(len(artifacts) + 1, {
            "event": "before_dispatch", "run_id": "run", "action_digest": "a" * 64,
            "admission_class": "ordinary_admitted",
            "semantic_fingerprint": json.dumps({
                "semantic": semantic, "document_id": "counter" if index else "blank",
                "token": "inc-token" if index else None, "destination": None if index else url,
            }),
        })
        artifacts.append(before)
        artifacts.append(artifact(len(artifacts) + 1, {
            "event": "outcome", "run_id": "run", "attempt_id": before.id,
            "status": "acknowledged",
        }))
        artifacts.append(artifact(len(artifacts) + 1, {
            "terminal": "verified" if index == 3 else "verification_failed",
            "functional": "verified" if index == 3 else "failed",
            "evidence": [{"kind": "text", "target": "#count", "passed": index == 3,
                          "reason": "ok" if index == 3 else "mismatch"}],
        }))
    result = TaskResult(
        run_id="run", terminal="verified", functional="verified", reason="verified",
        usage=UsageCounters(calls=3, steps=4, input_bytes=5000, output_bytes=1000, stored_bytes=9000),
        timings={}, artifact_ids=[a.id for a in artifacts], last_url=url,
    )
    driver = {"count": "3", "count_url": url, "count_error": None, "close_completed": True,
              "cleanup_errors": [], "residual_owned": False, "foreign_mismatch": None}
    return request, result, artifacts, driver, proposal


def test_fixture_matches_test_and_example():
    from conftest import COUNTER_PAGE_HTML

    assert counter.COUNTER_HTML == COUNTER_PAGE_HTML.encode()


def test_structural_pass_is_not_live_acceptance(tmp_path):
    request, result, artifacts, driver, proposal = evidence()
    assert counter.validate_evidence(request, result, artifacts, driver, True, proposal=proposal) == []
    fixture = counter.CounterFixture()
    fixture.url = request.start_url
    manager = counter.CounterAcceptanceManager(fixture, tmp_path)
    manager._report("PASS", human_confirmed=True, live_confirmed=True)
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["live_acceptance"] == "UNVERIFIED"
    assert report["human_admission"] == "UNVERIFIED"
    assert report["native_host"] == "NOT_RUN"
    assert report["live_provider"] == "UNVERIFIED"


@pytest.mark.parametrize("case", ["one", "four", "reset", "screenshot", "count", "hash", "orphan",
                                  "run", "unpaired", "cleanup", "fixture", "development", "images"])
def test_reject_invalid_evidence(case):
    request, result, artifacts, driver, proposal = evidence(
        clicks={"one": 1, "four": 4}.get(case, 3),
        operation="screenshot" if case == "screenshot" else "click",
        name="reset" if case == "reset" else "inc",
    )
    if case == "count":
        driver["count"] = "2"
    if case == "hash":
        artifacts[0] = replace(artifacts[0], sha256="0" * 64)
    if case in {"orphan", "run"}:
        payload = json.loads(artifacts[1].content)
        payload["attempt_id" if case == "orphan" else "run_id"] = "elsewhere"
        artifacts[1] = artifact(2, payload)
    if case == "unpaired":
        artifacts = artifacts[:-2]
        result.artifact_ids = [a.id for a in artifacts]
    if case == "cleanup":
        driver["cleanup_errors"] = ["failed"]
    if case == "development":
        result.development_only = True
    if case == "images":
        artifacts[0] = replace(artifacts[0], media_type="image/png")
    assert counter.validate_evidence(request, result, artifacts, driver, case != "fixture", proposal=proposal)


async def test_exact_request_actual_server_and_zero_effects_before_admission(tmp_path, monkeypatch):
    fixture = counter.CounterFixture()
    await fixture.start()
    effects = []

    def provider():
        effects.append("provider")
        raise AssertionError("provider must remain unopened")

    async def browser_start(self):
        effects.append("browser")
        raise AssertionError("browser must remain unopened")

    monkeypatch.setattr(counter, "production_factory", provider)
    monkeypatch.setattr(BrowserDriver, "start", browser_start)
    manager = counter.CounterAcceptanceManager(fixture, tmp_path)
    assert isinstance(manager._desk, StandardFormDesk)
    assert manager._desk._store is manager._admission
    assert manager._development is None
    assert Counter_inherits_execution()
    server = create_server(manager)
    request = manager.request
    for altered in (
        request.model_copy(update={"visual": True}),
        request.model_copy(update={"goal": "one click"}),
        request.model_copy(update={"mode": "existing_chrome"}),
    ):
        with pytest.raises(RunnerError, match="counter_request_mismatch"):
            manager.start_task(altered)
    assert manager.observer is None and effects == []
    response = await server.call_tool("start_task", {"request": request.model_dump()})
    run_id = response.structured_content["result"]["run_id"]
    request.limits.max_steps = 900  # Caller cannot mutate the runner's frozen copy.
    async with asyncio.timeout(2):
        while manager.pending_proposal(run_id) is None:
            await asyncio.sleep(0)
    assert manager.get_task(run_id).terminal == "awaiting_human_admission"
    assert effects == []
    assert manager.request.limits.max_steps == 4
    with pytest.raises(RunnerError, match="counter_unavailable"):
        manager.start_task(manager.request)
    await manager.aclose()
    assert effects == [] and fixture.clean and manager.observer.done()
    assert json.loads((tmp_path / "report.json").read_text())["structural_status"] == "FAIL"


def Counter_inherits_execution():
    for method in ("_execute", "_body", "_loop", "present_admission", "_admit_scope"):
        assert getattr(counter.CounterAcceptanceManager, method) is getattr(RunManager, method)
    return True


@pytest.mark.parametrize("event", ["decline", "expiry", "no_start"])
async def test_no_start_denial_and_expiry_stop_fixture(tmp_path, event, monkeypatch):
    fixture = counter.CounterFixture()
    await fixture.start()
    manager = counter.CounterAcceptanceManager(fixture, tmp_path)
    form_calls = []
    if event != "no_start":
        if event == "expiry":
            original = counter.Budget.remaining
            monkeypatch.setattr(counter.Budget, "remaining", lambda self: min(original(self), 0.02))
        run_id = manager.start_task(manager.request)
        async with asyncio.timeout(2):
            while manager.pending_proposal(run_id) is None:
                await asyncio.sleep(0)
        pending = manager.pending_proposal(run_id)
        session = SimpleNamespace(
            check_client_capability=lambda _: True,
            client_capabilities=ClientCapabilities(
                elicitation=ElicitationCapability(form=FormElicitationCapability())
            ),
        )

        async def decline(**kwargs):
            form_calls.append(True)
            return SimpleNamespace(action="decline", content=None)

        session.elicit_form = decline
        token = _mcp_ctx.set(SimpleNamespace(request_context=SimpleNamespace(session=session), request_id="test"))
        try:
            if event != "expiry":
                await manager.present_admission(run_id, pending.proposal_id, pending.digest)
        finally:
            _mcp_ctx.reset(token)
        await asyncio.wait_for(manager.observer, 2.0)
    await manager.aclose()
    assert form_calls == ([True] if event == "decline" else [])
    assert fixture.closed and fixture.clean
    assert manager.recording_provider is None
    assert (tmp_path / "report.json").is_file()


async def test_listener_is_exact_bounded_and_closes_partial_client():
    fixture = counter.CounterFixture()
    await fixture.start()
    port = int(fixture.url.split(":")[2].split("/")[0])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /counter.html HTTP/1.1\r\nHost: localhost\r\n\r\n")
    await writer.drain()
    response = await reader.read()
    assert response.endswith(counter.COUNTER_HTML)
    writer.close()
    await writer.wait_closed()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /counter")
    await writer.drain()
    await fixture.close()
    try:
        assert await asyncio.wait_for(reader.read(), 1.0) == b""
    except ConnectionResetError:
        pass
    writer.close()
    try:
        await writer.wait_closed()
    except ConnectionResetError:
        pass
    assert fixture.clean
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)


@pytest.mark.parametrize("fail", [False, True])
async def test_close_reads_count_and_always_closes_base(monkeypatch, fail):
    closed = []
    driver = counter.RecordingDriver("isolated", "http://127.0.0.1:12345/counter.html")

    async def evaluate(*args, **kwargs):
        if fail:
            raise RuntimeError("page error must not escape")
        return {"url": driver._expected_url, "count": "3"}

    driver._page = SimpleNamespace(locator=lambda selector: SimpleNamespace(evaluate=evaluate))

    async def close(self):
        closed.append(True)

    monkeypatch.setattr(BrowserDriver, "close", close)
    await driver.close()
    assert closed == [True]
    assert driver.count_error == ("count_read_failed" if fail else None)
    assert driver.count == (None if fail else "3")
    assert driver.close_completed


@pytest.mark.parametrize("choice", ["screenshot", "reset", "fill", "select", "navigate", "wrong_target"])
async def test_provider_rejects_unwanted_decisions_before_dispatch(choice):
    req, _, arts, _, _ = evidence()
    node = json.loads(json.loads(arts[4].content)["semantic_fingerprint"])["semantic"]["node"]
    target = "reset" if choice == "reset" else "inc-token"
    op = "click" if choice in {"reset", "wrong_target"} else choice
    provider = SimpleNamespace()

    async def decide(*args, **kwargs):
        return Decision(op, "missing" if choice == "wrong_target" else target, 1.0)

    provider.decide = decide
    wrapper = counter.DecisionsOnlyProvider(provider, req.start_url)
    raw = json.dumps({"candidates": {"click": {"inc-token": {
        "operation": "click", "node": node, "url": req.start_url,
    }}}})
    with pytest.raises(RunnerError, match="counter_decision_rejected"):
        await wrapper.decide([], raw, budget=None, privacy=None)
    with pytest.raises(RunnerError, match="counter_text_helper_disabled"):
        await wrapper.generate_text("", budget=None, privacy=None)


@pytest.mark.parametrize("stop", ["DONE", "UNABLE"])
@pytest.mark.parametrize("text,current", [
    ("0 inc reset", "0"), ("1\ninc\nreset", "1"), ("2\ninc\nreset", "2"),
    ("untrusted prose inc reset", None),
])
async def test_counter_offers_click_and_stops_before_model_inference(stop, text, current):
    request, _, artifacts, _, _ = evidence()
    node = json.loads(json.loads(artifacts[4].content)["semantic_fingerprint"])["semantic"]["node"]
    calls = []

    async def decide(questions, input_text, **kwargs):
        operation, target = questions
        assert operation.choices == ("DONE", "UNABLE", "click")
        assert operation.descriptions == ("done", "unable", "click")
        assert target.choices == ("inc-token", "reset-token")
        assert kwargs["target_heads"] == {"click": "button"}
        candidates = json.loads(input_text)["candidates"]
        assert set(candidates) == {"click"}
        assert set(candidates["click"]) == {"inc-token", "reset-token"}
        for provider in ("openai", "systemone"):
            prepared, state, _ = prepare_context(
                questions, input_text, operation_head="action",
                target_heads=kwargs["target_heads"], compact=True, lean=True, provider=provider,
            )
            observed_text = json.loads(state)["text"]
            if current is None:
                assert "Counter progress" not in observed_text
            else:
                assert f'Counter progress for #count: current="{current}", expected="3".' in observed_text
                assert json.loads(state)["goal"] == request.goal
                assert "UNABLE" in prepared[0].choices
        calls.append(True)
        return Decision(stop, None, 1.0)

    questions = (
        ChoiceQuestion("action", "Choose", ("DONE", "UNABLE", "click", "wait"),
                       ("done", "unable", "click", "wait")),
        ChoiceQuestion("button", "Choose target", ("inc-token", "reset-token")),
        ChoiceQuestion("pause", "Choose wait", ("short",)),
    )
    candidates = {
        "click": {
            "inc-token": {"operation": "click", "node": node, "url": request.start_url},
            "reset-token": {"operation": "click", "node": {**node, "name": "reset"},
                            "url": request.start_url},
        },
        "wait": {"short": {"operation": "wait"}},
    }
    for binding in candidates["click"].values():
        binding.update(form={}, context={})
    async def read_count():
        assert current is not None
        return current

    driver = SimpleNamespace(read_count=read_count) if current is not None else None
    wrapper = counter.DecisionsOnlyProvider(SimpleNamespace(decide=decide), request.start_url, driver)
    result = await wrapper.decide(
        questions, json.dumps({
            "context_format": "decision-browser-context-v1", "candidates": candidates,
            "goal": request.goal, "origin": "http://127.0.0.1:12345",
            "url": request.start_url, "text": text,
            "recent_actions": [], "excluded_mechanisms": [],
        }), budget=None, privacy=None,
        operation_head="action", target_heads={"click": "button", "wait": "pause"},
    )
    assert result.operation == stop and calls == [True]
    assert questions[0].choices == ("DONE", "UNABLE", "click", "wait")
    assert "wait" in candidates


async def test_observer_waits_finality_exports_and_drains(tmp_path, monkeypatch):
    fixture = counter.CounterFixture()
    await fixture.start()
    manager = counter.CounterAcceptanceManager(fixture, tmp_path)
    _request, result, artifacts, driver, proposal = evidence(fixture.url)
    current = [result.model_copy(update={"terminal": "running", "reason": "finalizing"})]
    monkeypatch.setattr(manager, "get_task", lambda _: current[0])
    monkeypatch.setattr(manager, "read_artifact", lambda run, key: next(a for a in artifacts if a.id == key))
    manager.admission_proposal = proposal
    manager.recording_driver = SimpleNamespace(evidence=lambda: driver)
    manager.observer = asyncio.create_task(manager._observe("run"))
    await asyncio.sleep(0.02)
    assert not (tmp_path / "result.json").exists()
    current[0] = result
    await manager.aclose()
    assert manager.observer.done() and fixture.clean
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["structural_status"] == "PASS" and report["live_acceptance"] == "UNVERIFIED"
    assert json.loads((tmp_path / "result.json").read_text()) == result.model_dump()
    for a in artifacts:
        assert (tmp_path / "artifacts" / a.id).read_bytes() == a.content


async def test_report_failure_still_drains_and_closes(tmp_path, monkeypatch, capsys):
    fixture = counter.CounterFixture()
    await fixture.start()
    manager = counter.CounterAcceptanceManager(fixture, tmp_path)
    _, result, _, _, _ = evidence(fixture.url)
    monkeypatch.setattr(manager, "get_task", lambda _: result)
    monkeypatch.setattr(manager, "read_artifact", lambda *args: (_ for _ in ()).throw(RuntimeError("not retained")))

    def fail(*args):
        raise OSError("private error")

    monkeypatch.setattr(counter, "_write_json", fail)
    manager.observer = asyncio.create_task(manager._observe("run"))
    await manager.aclose()
    assert manager.observer.done() and fixture.clean
    assert manager.report_error == "evidence_collection_failed"
    assert capsys.readouterr().out == ""


async def test_no_start_disconnect_uses_actual_server_and_preserves_stdout(tmp_path, monkeypatch, capsys):
    seen = []

    async def disconnect(self):
        seen.append(self.name)

    from mcp.server.mcpserver import MCPServer
    monkeypatch.setattr(MCPServer, "run_stdio_async", disconnect)
    output = tmp_path / "evidence"
    assert await counter.serve(output) == 1
    assert seen == ["decision-browser"]
    report = json.loads((output / "report.json").read_text())
    assert report["fixture_cleanup"] is True
    assert report["failures"] == ["task_not_started"]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("field,value", [
    ("visual", True), ("mode", "existing_chrome"), ("checks", []),
    ("values", {"field": "text"}), ("start_url", "https://example.test/counter.html"),
    ("start_url", "http://127.0.0.1:12345/wrong"),
])
def test_validator_rejects_noncanonical_request(field, value):
    request, result, artifacts, driver, proposal = evidence()
    altered = request.model_copy(update={field: value})
    assert "noncanonical_request" in counter.validate_evidence(altered, result, artifacts, driver, True, proposal=proposal)


def test_validator_rejects_changed_privacy_or_limits():
    for kind in ("privacy", "limits"):
        request, result, artifacts, driver, proposal = evidence()
        if kind == "privacy":
            request.privacy.send_images = True
        else:
            request.limits.max_input_bytes = 262144
        assert "noncanonical_request" in counter.validate_evidence(request, result, artifacts, driver, True, proposal=proposal)


async def test_recording_read_cancellation_still_closes_base(monkeypatch):
    entered = asyncio.Event()
    closed = []
    driver = counter.RecordingDriver("isolated", "http://127.0.0.1:12345/counter.html")

    async def read(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    async def close(self):
        closed.append(True)

    driver._page = SimpleNamespace(locator=lambda _: SimpleNamespace(evaluate=read))
    monkeypatch.setattr(BrowserDriver, "close", close)
    job = asyncio.create_task(driver.close())
    await entered.wait()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert closed == [True] and driver.count_error == "count_read_cancelled"


async def test_base_close_failure_is_explicit(monkeypatch):
    driver = counter.RecordingDriver("isolated", "http://127.0.0.1:12345/counter.html")

    async def fail(self):
        raise RuntimeError("close failed")

    monkeypatch.setattr(BrowserDriver, "close", fail)
    with pytest.raises(RuntimeError):
        await driver.close()
    assert not driver.close_completed and driver.cleanup_errors == ["counter_close_failed"]


async def test_actual_snapshot_fingerprint_and_provider_descriptor(monkeypatch):
    """Run shipped snapshot JS and runner choice serialization without a browser."""
    import shutil
    import subprocess

    from test_snapshot_semantics import _SCRIPT, _SNAPSHOT

    from decision_browser.dom import Action, Candidate, GuardResult, Observation, _fp

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js required for browser-free shipped snapshot test")
    request, result, artifacts, driver, proposal = evidence()
    origin = request.start_url.rsplit("/", 1)[0]
    script = _SCRIPT.replace("https://example.test/settings", request.start_url).replace("https://example.test", origin)
    completed = await asyncio.to_thread(
        subprocess.run, [node, "-e", script, str(_SNAPSHOT), json.dumps({"tag": "BUTTON", "type": "button", "aria": "inc"})],
        capture_output=True, text=True, check=True,
    )
    semantic = json.loads(completed.stdout)["semantic"]
    fingerprint = _fp("inc-token", "counter", None, semantic)
    action = Action("click", "inc-token", None, None, "obs", fingerprint)
    host = RunManager(lambda: None)
    for index in (4, 8, 12):
        event = json.loads(artifacts[index].content)
        event.update(semantic_fingerprint=fingerprint, action_digest=host._digest(action))
        artifacts[index] = artifact(index + 1, event)
    # Produce the passing journal with the actual store/redaction/ID machinery.
    from decision_browser.artifacts import ArtifactStore, Journal

    store = ArtifactStore(1048576)
    journal = Journal(store)
    saved = []
    attempt = None
    for source in artifacts:
        event = json.loads(source.content)
        if event.get("event") == "before_dispatch":
            attempt = journal.before_dispatch("run", event["action_digest"],
                                              event["semantic_fingerprint"], event["admission_class"])
            saved.append(attempt)
        elif event.get("event") == "admission_binding":
            saved.append(journal.append("admission_binding", "run",
                                        proposal_id=event["proposal_id"], digest=event["digest"]))
        elif event.get("event") == "outcome":
            saved.append(journal.outcome("run", attempt, "acknowledged"))
        else:
            saved.append(store.put_json(event))
    artifacts = [store.read(key) for key in saved]
    result.artifact_ids = saved
    result.usage.stored_bytes = store.stored_bytes
    assert counter.validate_evidence(request, result, artifacts, driver, True, proposal=proposal) == []

    class Provider:
        async def decide(self, questions, input_text, **kwargs):
            self.payload = json.loads(input_text)
            return Decision("click", "inc-token", 1.0)

    class DOM:
        async def prepare(self, *args, **kwargs):
            return action

        async def guard(self, action):
            return GuardResult("pass", "")

    raw_provider = Provider()
    run = host._open(request)
    run.provider = counter.DecisionsOnlyProvider(raw_provider, request.start_url)
    run.dom = DOM()
    obs = Observation("obs", origin, "counter", "0 inc reset", {
        "inc-token": Candidate("inc-token", ("click",), {"click": semantic}, None),
    }, ())
    assert await host._choose(run, obs) is action
    assert raw_provider.payload["candidates"]["click"]["inc-token"]["node"] == semantic["node"]


@pytest.mark.parametrize("status,expected", [("PASS", 0), ("FAIL", 1), ("ERROR", 1), ("NOT_RUN", 1)])
async def test_cli_exit_code_is_structural_status(tmp_path, monkeypatch, status, expected):
    from mcp.server.mcpserver import MCPServer

    async def disconnect(self):
        pass

    async def close(self):
        await self.fixture.close()
        self.structural_status = status

    monkeypatch.setattr(MCPServer, "run_stdio_async", disconnect)
    monkeypatch.setattr(counter.CounterAcceptanceManager, "aclose", close)
    assert await counter.serve(tmp_path / "run") == expected


@pytest.mark.parametrize("case", ["missing", "malformed", "duplicate", "orphan", "changed", "wrong_run"])
def test_admission_bindings_are_required_and_paired(case):
    request, result, artifacts, driver, proposal = evidence()
    if case == "missing":
        artifacts.pop(3)
    elif case == "duplicate":
        artifacts.insert(4, artifact(100, json.loads(artifacts[3].content)))
    elif case == "orphan":
        artifacts.append(artifact(100, json.loads(artifacts[3].content)))
    else:
        index = 7 if case == "changed" else 3
        event = json.loads(artifacts[index].content)
        if case == "malformed":
            event["proposal_id"] = "not-a-proposal"
        elif case == "changed":
            event["digest"] = "c" * 64
        else:
            event["run_id"] = "other-run"
        artifacts[index] = artifact(index + 1, event)
    result.artifact_ids = [a.id for a in artifacts]
    assert counter.validate_evidence(request, result, artifacts, driver, True, proposal=proposal)


async def test_cancelled_aclose_cancels_and_drains_retained_observer(tmp_path, monkeypatch):
    fixture = counter.CounterFixture()
    await fixture.start()
    manager = counter.CounterAcceptanceManager(fixture, tmp_path)
    _, result, _, _, _ = evidence(fixture.url)
    running = result.model_copy(update={"terminal": "running", "reason": "finalizing"})
    monkeypatch.setattr(manager, "get_task", lambda _: running)
    manager.observer = asyncio.create_task(manager._observe("run"))
    closing = asyncio.create_task(manager.aclose())
    await asyncio.sleep(0.02)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert fixture.clean and manager.observer.done()
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["structural_status"] == "ERROR"
    assert report["failures"] == ["observer_cancelled"]


@pytest.mark.parametrize("payload", [
    {"screenshot": "not-image-proof"}, {"control": "reset"}, {},
    {"terminal": "verified", "functional": "verified", "evidence": []},
    {"terminal": "verified", "functional": "verified", "evidence": [
        {"kind": "text", "target": "#other", "passed": True, "reason": "ok"}]},
    {"terminal": "verified", "functional": "verified", "evidence": [
        {"kind": "text", "target": "#count", "passed": True, "reason": "ok",
         "screenshot": "side-record"}]},
    {"terminal": "verified", "functional": "verified", "evidence": [
        {"kind": "text", "target": "#count", "passed": True, "reason": "ok"}],
     "control": "reset"},
])
def test_eventless_side_records_and_noncanonical_checks_fail(payload):
    expected_code = "functional_sequence"
    request, result, artifacts, driver, proposal = evidence()
    artifacts.insert(3, artifact(100, payload))
    result.artifact_ids = [item.id for item in artifacts]
    assert expected_code in counter.validate_evidence(
        request, result, artifacts, driver, True, proposal=proposal)


@pytest.mark.parametrize("case", ["missing", "early_success", "late_failure", "wrong_target",
                                  "unexpected_expected", "duplicate", "during_dispatch"])
def test_functional_reports_are_closed_and_tied_to_dispatch_sequence(case):
    expected_code = {"missing": "dispatch_sequence", "early_success": "functional_report",
                     "late_failure": "functional_report", "wrong_target": "functional_check",
                     "unexpected_expected": "artifact_schema", "duplicate": "functional_sequence",
                     "during_dispatch": "functional_sequence"}[case]
    request, result, artifacts, driver, proposal = evidence()
    if case == "missing":
        artifacts.pop()
    elif case == "duplicate":
        artifacts.append(artifact(100, json.loads(artifacts[-1].content)))
    elif case == "during_dispatch":
        artifacts.insert(1, artifacts.pop(2))
    else:
        index = 2 if case == "early_success" else len(artifacts) - 1
        payload = json.loads(artifacts[index].content)
        if case == "early_success":
            payload = json.loads(artifacts[-1].content)
        elif case == "late_failure":
            payload = json.loads(artifacts[2].content)
        elif case == "wrong_target":
            payload["evidence"][0]["target"] = "#reset"
        else:
            payload["evidence"][0]["expected"] = "3"
        artifacts[index] = artifact(index + 1, payload)
    result.artifact_ids = [item.id for item in artifacts]
    assert expected_code in counter.validate_evidence(
        request, result, artifacts, driver, True, proposal=proposal)


def test_bootstrap_requires_actual_blank_source_url():
    expected_code = "bootstrap_navigation"
    request, result, artifacts, driver, proposal = evidence()
    event = json.loads(artifacts[0].content)
    fingerprint = json.loads(event["semantic_fingerprint"])
    fingerprint["semantic"]["url"] = "https://attacker.example/"
    event["semantic_fingerprint"] = json.dumps(fingerprint)
    artifacts[0] = artifact(1, event)
    assert expected_code in counter.validate_evidence(
        request, result, artifacts, driver, True, proposal=proposal)


async def test_stale_returned_choice_does_not_consume_increment_allowance():
    request, _, artifacts, _, _ = evidence()
    node = json.loads(json.loads(artifacts[4].content)["semantic_fingerprint"])["semantic"]["node"]

    async def decide(*args, budget, **kwargs):
        budget.reserve_provider(1)
        return Decision("click", "inc-token", 1.0)

    wrapper = counter.DecisionsOnlyProvider(SimpleNamespace(decide=decide), request.start_url)
    budget = counter.Budget(request.limits)
    budget.reserve_step()  # Bootstrap navigation.
    raw = json.dumps({"candidates": {"click": {"inc-token": {
        "operation": "click", "node": node, "url": request.start_url,
    }}}})
    for index in range(4):
        assert (await wrapper.decide([], raw, budget=budget, privacy=None)).operation == "click"
        if index != 2:  # Third returned choice was stale before dispatch.
            budget.reserve_step()
    assert budget.usage().steps == 4 and budget.usage().calls == 4



async def test_count_from_another_document_cannot_validate(monkeypatch):
    request, _, _, _, _ = evidence()
    driver = counter.RecordingDriver("isolated", "http://127.0.0.1:12345/counter.html")
    driver._expected_url = request.start_url

    async def inner_text(**kwargs):
        return "3"

    async def evaluate(*args, **kwargs):
        return {"url": "https://other.example/counter.html", "count": "3"}

    async def close(self):
        pass

    driver._page = SimpleNamespace(url="https://other.example/counter.html", locator=lambda _: SimpleNamespace(
        inner_text=inner_text, evaluate=evaluate))
    monkeypatch.setattr(BrowserDriver, "close", close)
    await driver.close()
    assert driver.count is None and driver.count_error == "count_read_failed"
    assert driver.close_completed


async def test_fixture_lifetime_covers_late_task_and_cleanup(tmp_path, monkeypatch):
    now = [0.0]
    real_sleep = asyncio.sleep
    expiry_wake = asyncio.Event()
    waits = []

    async def sleep(delay):
        if delay > 1.0:
            waits.append(delay)
            await expiry_wake.wait()
            expiry_wake.clear()
        else:
            await real_sleep(delay)

    monkeypatch.setattr(counter, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(counter.asyncio, "sleep", sleep)
    fixture = counter.CounterFixture()
    await fixture.start()
    await real_sleep(0)
    assert waits == [120.0]
    now[0] = 30.0
    manager = counter.CounterAcceptanceManager(fixture, tmp_path)
    manager.start_task(manager.request)
    try:
        now[0] = 120.0
        expiry_wake.set()
        for _ in range(5):
            await real_sleep(0)
        assert not fixture.closed
        assert waits == [120.0, 32.0]  # Original task deadline 150 + cleanup grace 2.
    finally:
        await manager.aclose()
    assert fixture.clean


def test_consistent_but_unrelated_admission_digest_is_rejected():
    expected_code = "unrelated_binding"
    request, result, artifacts, driver, proposal = evidence()
    for index, item in enumerate(artifacts):
        payload = json.loads(item.content)
        if payload.get("event") == "admission_binding":
            payload["digest"] = "c" * 64
            artifacts[index] = artifact(index + 1, payload)
    assert expected_code in counter.validate_evidence(
        request, result, artifacts, driver, True, proposal=proposal)


@pytest.mark.parametrize("stale_at", [None, "guard", "dispatch", "exhausted"])
async def test_real_manager_scripted_form_journal_checks_and_stale_budget(tmp_path, monkeypatch, stale_at):
    """Synthetic form/page/provider only: exercises shipped runner, never human proof."""
    from decision_browser import runner, verification
    from decision_browser.admission import canonical_origin
    from decision_browser.dom import (
        Action,
        Candidate,
        DispatchResult,
        GuardResult,
        Observation,
        _fp,
    )

    fixture = counter.CounterFixture()
    fixture.url = "http://127.0.0.1:12345/counter.html"
    effects = []

    class Page:
        url = "about:blank"
        count = 0

        def locator(self, selector):
            assert selector == "#count"
            return self

        async def wait_for(self, **kwargs):
            assert kwargs == {"state": "attached", "timeout": 500}

        async def evaluate(self, expression, **kwargs):
            assert "location.href" in expression and "node.innerText" in expression
            effects.append(("independent", self.count))
            return {"url": self.url, "count": str(self.count)}

    page = Page()

    class VerificationWorld:
        """The same synthetic counter read at the new isolated batch seam."""

        def __init__(self, current_page, *, world_name=None):
            assert current_page is page

        async def refresh(self):
            pass

        async def evaluate(self, expression, checks):
            assert len(checks) == 1
            check = checks[0]
            assert (check["kind"], check["target"]) == ("text", "#count")
            effects.append(("check", page.count))
            passed = str(page.count) == check["expected"]
            return [{"passed": passed, "reason": "ok" if passed else "mismatch"}]

        async def close(self):
            pass

    class Provider:
        calls = 0

        async def decide(self, questions, input_text, *, budget, **kwargs):
            budget.reserve_provider(len(input_text.encode()))
            budget.consume_output(1)
            self.calls += 1
            return Decision("click", "inc-token", 1.0)

        async def aclose(self):
            effects.append(("provider_closed", page.count))

    provider = Provider()

    class DOM:
        def __init__(self, current_page, *, navigation_remaining):
            assert current_page is page
            assert callable(navigation_remaining) and navigation_remaining() > 0
            self.observation = None
            self.serial = 0

        def semantic(self, operation):
            return {
                "operation": operation, "unsupported": None, "url": page.url, "form": None,
                "origin": "null" if page.url == "about:blank" else canonical_origin(page.url),
                "node": {"name": "inc", "role": "button", "state": {
                    "tag": "button", "type": "button", "disabled": False,
                }} if operation == "click" else None,
            }

        async def observe(self):
            self.serial += 1
            semantic = self.semantic("click")
            self.observation = Observation(
                str(self.serial), semantic["origin"],
                "blank" if page.url == "about:blank" else "counter", str(page.count),
                {"inc-token": Candidate("inc-token", ("click",), {"click": semantic}, None)}, (),
            )
            return self.observation

        async def prepare(self, operation, token=None, *, value=None, destination=None):
            return Action(operation, token, value, destination, self.observation.id,
                          _fp(token, self.observation.document_id, destination, self.semantic(operation)))

        async def guard(self, action):
            if action.operation == "click" and (
                (stale_at == "guard" and provider.calls == 3)
                or (stale_at == "exhausted" and provider.calls in {2, 3})
            ):
                return GuardResult("stale", "stale")
            return GuardResult("pass", "")

        async def dispatch(self, action, before):
            if action.operation == "click" and stale_at == "dispatch" and provider.calls == 3:
                return DispatchResult("stale", "stale")
            before(action)
            effects.append(("dispatch", action.operation))
            if action.operation == "navigate":
                page.url = action.destination
            else:
                assert action.operation == "click"
                page.count += 1
            return DispatchResult("pass", "")

        async def close(self):
            effects.append(("dom_closed", page.count))

    async def start(self):
        self._page = page

    async def close(self):
        effects.append(("browser_closed", page.count))
        self.residual_owned = False

    monkeypatch.setattr(counter, "production_factory", lambda: provider)
    monkeypatch.setattr(BrowserDriver, "start", start)
    monkeypatch.setattr(BrowserDriver, "close", close)
    monkeypatch.setattr(runner, "DOMSession", DOM)
    monkeypatch.setattr(verification, "_CDPWorld", VerificationWorld)
    manager = counter.CounterAcceptanceManager(fixture, tmp_path)
    run_id = manager.start_task(manager.request)
    async with asyncio.timeout(2):
        while manager.pending_proposal(run_id) is None:
            await asyncio.sleep(0)
    assert manager.admission_proposal is None and not effects
    pending = manager.pending_proposal(run_id)
    session = SimpleNamespace(
        check_client_capability=lambda _: True,
        client_capabilities=ClientCapabilities(
            elicitation=ElicitationCapability(form=FormElicitationCapability())
        ),
    )

    async def scripted_form(**kwargs):
        return SimpleNamespace(action="accept", content={"reviewed": True})

    session.elicit_form = scripted_form
    ctx = SimpleNamespace(request_context=SimpleNamespace(session=session), request_id="offline-script")
    token = _mcp_ctx.set(ctx)
    try:
        await manager.present_admission(run_id, pending.proposal_id, pending.digest)
    finally:
        _mcp_ctx.reset(token)
    await asyncio.wait_for(manager.observer, 2.0)
    result = manager.get_task(run_id)
    assert result.last_url is None  # Shipped runner leaves the optional field unset.
    await manager.aclose()
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["live_acceptance"] == report["human_admission"] == "UNVERIFIED"
    assert report["live_provider"] == "UNVERIFIED" and report["native_host"] == "NOT_RUN"
    assert manager.admission_proposal is pending
    assert not manager._admission.is_live(pending.proposal_id)  # Cleanup revoked it.
    assert report["admission_scope"]["digest"] == pending.digest
    assert page.count == (2 if stale_at == "exhausted" else 3)
    assert result.usage.steps == page.count + 1 <= 4
    assert result.usage.calls == (3 if stale_at is None else 4)
    assert [entry for entry in effects if entry[0] == "dispatch"] == [
        ("dispatch", "navigate"), *[("dispatch", "click")] * page.count,
    ]
    assert effects.index(("independent", page.count)) < effects.index(("browser_closed", page.count))
    if stale_at == "exhausted":
        assert result.terminal == "limit_exceeded"
        assert report["structural_status"] == "FAIL"
    else:
        assert result.terminal == "verified"
        assert report["structural_status"] == "PASS", report["failures"]
        assert [entry for entry in effects if entry[0] == "check"] == [
            ("check", 0), ("check", 1), ("check", 2), ("check", 3),
        ]
        artifacts = [json.loads((tmp_path / "artifacts" / key).read_text()) for key in result.artifact_ids]
        dispatches = [event for event in artifacts if event.get("event") == "before_dispatch"]
        assert len({event["action_digest"] for event in dispatches}) == 4
        assert all(event["action_digest"] != pending.digest for event in dispatches)


@pytest.mark.parametrize("event_kind", ["admission_binding", "before_dispatch", "outcome"])
def test_journal_event_cannot_hide_an_arbitrary_side_payload(event_kind):
    expected_code = {"admission_binding": "binding_schema", "before_dispatch": "dispatch_schema",
                     "outcome": "outcome_schema"}[event_kind]
    request, result, artifacts, driver, proposal = evidence()
    index = next(i for i, item in enumerate(artifacts) if json.loads(item.content).get("event") == event_kind)
    payload = json.loads(artifacts[index].content)
    payload["screenshot"] = "unexpected-side-payload"
    artifacts[index] = artifact(index + 1, payload)
    assert expected_code in counter.validate_evidence(
        request, result, artifacts, driver, True, proposal=proposal)


def test_action_digest_is_a_string_not_a_coercible_json_number():
    expected_code = "dispatch_admission"
    request, result, artifacts, driver, proposal = evidence()
    payload = json.loads(artifacts[0].content)
    payload["action_digest"] = int("1" * 64)
    artifacts[0] = artifact(1, payload)
    assert expected_code in counter.validate_evidence(
        request, result, artifacts, driver, True, proposal=proposal)


@pytest.mark.parametrize("value", ["3\n", " 3", "3 ", 3, None, {"count": "3"}])
async def test_independent_read_does_not_normalize_count(monkeypatch, value):
    driver = counter.RecordingDriver("isolated", "http://127.0.0.1:12345/counter.html")

    async def evaluate(*args, **kwargs):
        return {"url": driver._expected_url, "count": value}

    async def close(self):
        pass

    driver._page = SimpleNamespace(locator=lambda _: SimpleNamespace(evaluate=evaluate))
    monkeypatch.setattr(BrowserDriver, "close", close)
    await driver.close()
    assert driver.count is None and driver.count_url is None
    assert driver.count_error == "count_read_failed" and driver.close_completed


@pytest.mark.parametrize("case", ["missing", "digest", "run", "url", "origin", "operations", "budgets"])
def test_historical_alpha_scope_is_required(case):
    expected_code = "proposal_digest" if case == "digest" else "proposal_scope"
    request, result, artifacts, driver, proposal = evidence()
    updates = {
        "digest": {"digest": "c" * 64}, "run": {"run_id": "other-run"},
        "url": {"canonical_urls": ("http://127.0.0.1:12345/other.html",)},
        "origin": {"origins": frozenset({"http://127.0.0.1:54321"})},
        "operations": {"operations": frozenset({"navigate"})},
        "budgets": {"budgets": {**proposal.budgets, "max_steps": 5}},
    }
    changed = None if case == "missing" else replace(proposal, **updates[case])
    assert expected_code in counter.validate_evidence(
        request, result, artifacts, driver, True, proposal=changed)


@pytest.mark.parametrize("case", ["result_url", "count_url", "expected"])
def test_url_and_expected_count_remain_exact(case):
    request, result, artifacts, driver, proposal = evidence()
    if case == "result_url":
        result.last_url = "http://127.0.0.1:12345/other.html"
    elif case == "count_url":
        driver["count_url"] = "http://127.0.0.1:12345/other.html"
    else:
        request.checks[0].expected = "4"
    assert counter.validate_evidence(
        request, result, artifacts, driver, True, proposal=proposal
    )


async def test_final_report_failure_retains_known_started_run_id(tmp_path, monkeypatch):
    fixture = counter.CounterFixture()
    fixture.url = "http://127.0.0.1:12345/counter.html"
    manager = counter.CounterAcceptanceManager(fixture, tmp_path)
    monkeypatch.setattr(RunManager, "start_task", lambda self, request: "known-run")
    monkeypatch.setattr(
        manager,
        "_live",
        lambda _: SimpleNamespace(budget=SimpleNamespace(original_deadline=130.0)),
    )

    async def observed(_run):
        return None

    monkeypatch.setattr(manager, "_observe", observed)
    assert manager.start_task(manager.request) == "known-run"
    await manager.observer
    manager.report_error = "evidence_collection_failed"
    await manager.aclose()
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["run_id"] == "known-run" and report["failures"] == [
        "evidence_collection_failed"
    ]


@pytest.mark.parametrize(
    "change,code",
    [
        ("digest", "artifact_digest"),
        ("pair", "paired_outcome"),
        ("type", "artifact_type"),
        ("sequence", "dispatch_sequence"),
    ],
)
def test_distinct_sanitized_audit_diagnostics(change, code):
    request, result, artifacts, driver, proposal = evidence()
    if change == "digest":
        artifacts[0] = replace(artifacts[0], sha256="0" * 64)
    elif change == "type":
        artifacts[0] = replace(artifacts[0], media_type="image/png")
    elif change == "pair":
        body = json.loads(artifacts[1].content)
        body["attempt_id"] = "PRIVATE_CANARY"
        artifacts[1] = artifact(2, body)
    else:
        artifacts = artifacts[:-2]
        result.artifact_ids = [a.id for a in artifacts]
    failures = counter.validate_evidence(
        request, result, artifacts, driver, True, proposal=proposal
    )
    assert failures == [code]
    assert "PRIVATE_CANARY" not in json.dumps(failures)
