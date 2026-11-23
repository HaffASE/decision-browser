"""Single-use counter MCP server and structural evidence collector.

This module never answers an admission form. Structural evidence is not proof
of a live provider exchange or a human/native-host interaction.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import socket
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from decision_browser.admission import (
    AdmissionStore,
    AlphaProposal,
    canonical_origin,
    canonical_url,
)
from decision_browser.artifacts import Artifact
from decision_browser.browser import BrowserDriver
from decision_browser.budget import Budget
from decision_browser.contracts import (
    Check,
    Limits,
    PrivacyPolicy,
    TaskRequest,
    TaskResult,
    Viewport,
)
from decision_browser.decision_codec import (
    TERMINAL_OPERATIONS,
    ChoiceQuestion,
    Decision,
)
from decision_browser.privacy import PrivacyGate
from decision_browser.runner import ProviderPort, RunManager, RunnerError
from decision_browser.server import StandardFormDesk, create_server, production_factory
from decision_browser.verification import FunctionalReport

_OPEN = {"queued", "awaiting_human_admission", "running"}
_ID = re.compile(r"[a-f0-9]{32}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
COUNTER_HTML = b'''<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>decision-browser-counter</title></head>
<body>
<p id="count">0</p>
<button id="inc" type="button">inc</button>
<button id="reset" type="button">reset</button>
<script>
const count = document.getElementById("count");
let n = 0;
document.getElementById("inc").addEventListener("click", () => {
  n += 1;
  count.textContent = String(n);
});
document.getElementById("reset").addEventListener("click", () => {
  n = 0;
  count.textContent = "0";
});
</script>
</body>
</html>
'''


class CounterFixture:
    """Owned IPv4 loopback listener; requires the public selector add_reader API."""

    def __init__(self) -> None:
        self.url = ""
        self.closed = False
        self.clean = False
        self._listener: socket.socket | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader_registered = False
        self._connections: set[socket.socket] = set()
        self._clients: dict[asyncio.Task[None], socket.socket] = {}
        self._expiry: asyncio.Task[None] | None = None
        self._deadline = 0.0
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._listener is not None or self.closed:
            raise RuntimeError("fixture_already_started")
        try:
            self._loop = asyncio.get_running_loop()
            self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._listener.setblocking(False)
            self._listener.bind(("127.0.0.1", 0))
            self._listener.listen(8)
            port = self._listener.getsockname()[1]
            # Accept and register ownership synchronously. asyncio.Server can
            # close before a scheduled transport attaches, losing that socket.
            self._loop.add_reader(self._listener.fileno(), self._accept_ready)
            self._reader_registered = True
            self.url = f"http://127.0.0.1:{port}/counter.html"
            self._deadline = time.monotonic() + 120.0
            self._expiry = asyncio.create_task(self._expire())
        except BaseException:
            await self.close()
            raise

    def _accept_ready(self) -> None:
        if self.closed or self._listener is None:
            return
        for _ in range(8):
            try:
                conn, _address = self._listener.accept()
            except (BlockingIOError, InterruptedError):
                return
            self._connections.add(conn)  # Before task creation or any await.
            if len(self._connections) > 8:
                conn.close()
                if conn.fileno() < 0:
                    self._connections.discard(conn)
                continue
            response = None
            try:
                conn.setblocking(False)
                response = self._respond(conn)
                task = asyncio.create_task(response)
            except BaseException:
                if response is not None:
                    response.close()
                conn.close()
                if conn.fileno() < 0:
                    self._connections.discard(conn)
                raise
            self._clients[task] = conn
            task.add_done_callback(lambda done: self._clients.pop(done, None))

    async def _respond(self, conn: socket.socket) -> None:
        try:
            async with asyncio.timeout(1.0):
                loop = asyncio.get_running_loop()
                received = bytearray()
                end = -1
                while end < 0:
                    chunk = await loop.sock_recv(conn, 8193 - len(received))
                    if not chunk:
                        return
                    received.extend(chunk)
                    end = received.find(b"\r\n\r\n")
                    if end < 0 and len(received) > 8192:
                        return
                first = bytes(received[:end]).split(b"\r\n", 1)[0]
                if end + 4 > 8192 or first not in {
                    b"GET /counter.html HTTP/1.1", b"GET /counter.html HTTP/1.0"
                }:
                    return
                await loop.sock_sendall(conn,
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                    b"Cache-Control: no-store\r\nConnection: close\r\nContent-Length: "
                    + str(len(COUNTER_HTML)).encode() + b"\r\n\r\n" + COUNTER_HTML
                )
        except (TimeoutError, OSError):
            pass
        finally:
            try:
                conn.close()
            finally:
                if conn.fileno() < 0:
                    self._connections.discard(conn)

    async def _expire(self) -> None:
        # No-start idle time is bounded, but cannot shorten a later task's
        # original budget. Recheck after waking if start_task extended it.
        while not self.closed:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(remaining)
        await self.close()

    def hold_until(self, deadline: float) -> None:
        self._deadline = deadline

    async def close(self) -> None:
        async with self._lock:
            if self.closed:
                return
            self.closed = True
            tasks = list(self._clients)
            if self._expiry is not None and self._expiry is not asyncio.current_task():
                tasks.append(self._expiry)
            cleanup_failed = False
            if self._listener is not None:
                try:
                    if self._reader_registered and self._loop is not None:
                        self._loop.remove_reader(self._listener.fileno())
                        self._reader_registered = False
                except (OSError, RuntimeError):
                    cleanup_failed = True
                try:
                    self._listener.close()
                except OSError:
                    cleanup_failed = True
            # A cancelled task may never execute its first instruction.
            # Keep cleaning other owned resources if one close fails.
            for conn in tuple(self._connections):
                try:
                    conn.close()
                except OSError:
                    cleanup_failed = True
                finally:
                    if conn.fileno() < 0:
                        self._connections.discard(conn)
            for task in tasks:
                task.cancel()
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=1.0)
                for task in done:
                    if not task.cancelled():
                        task.exception()
                cleanup_failed = cleanup_failed or bool(pending)
            self.clean = (not cleanup_failed and not self._connections
                          and not self._reader_registered
                          and (self._listener is None or self._listener.fileno() < 0))


def counter_request(url: str) -> TaskRequest:
    return TaskRequest(
        goal="Increase the count from 0 to exactly 3 by clicking inc three times. "
             "Never click reset. Use DOM controls only; do not take screenshots. "
             "Finish when the visible count is 3.",
        start_url=url, mode="isolated", values={},
        checks=[Check(kind="text", target="#count", expected="3")],
        viewports=[Viewport(width=1280, height=720)],
        limits=Limits(max_steps=4, max_seconds=120.0, max_provider_calls=4,
                      max_input_bytes=65536, max_output_bytes=65536,
                      max_stored_bytes=1048576),
        privacy=PrivacyPolicy(send_images=False), visual=False,
    )


class RecordingDriver(BrowserDriver):
    """Read #count independently during close, and always close the base driver."""

    def __init__(self, mode: str, expected_url: str) -> None:
        super().__init__(mode)
        self._expected_url = expected_url
        self.count: str | None = None
        self.count_url: str | None = None
        self.count_error: str | None = None
        self.close_completed = False

    async def close(self) -> None:
        if self.close_completed:
            return
        try:
            async with asyncio.timeout(0.5):
                # Read both in one document evaluation: a navigation between
                # separate page.url and locator reads cannot bind foreign text.
                reading = await self.page.locator("#count").evaluate(
                    "node => ({url: location.href, count: node.innerText})", timeout=400,
                )
                if _object(reading).get("url") != self._expected_url:
                    raise ValueError("unexpected_count_url")
                value = reading.get("count")
                if value not in {"0", "1", "2", "3", "4"}:
                    raise ValueError("unexpected_count")
                self.count = value
                self.count_url = self._expected_url
        except asyncio.CancelledError:
            self.count_error = "count_read_cancelled"
            raise
        except Exception:  # noqa: BLE001 - no page or exception text in diagnostics
            self.count_error = "count_read_failed"
        finally:
            try:
                await super().close()
                self.close_completed = True
            except BaseException:
                self.cleanup_errors.append("counter_close_failed")
                raise

    def evidence(self) -> dict[str, Any]:
        return {
            "count": self.count, "count_url": self.count_url, "count_error": self.count_error,
            "close_completed": self.close_completed,
            "cleanup_errors": list(self.cleanup_errors),
            "residual_owned": self.residual_owned,
            "foreign_mismatch": self.foreign_mismatch,
        }


class DecisionsOnlyProvider:
    """Preserve production Decisions behavior; never invoke the Responses helper."""

    def __init__(self, provider: ProviderPort, url: str) -> None:
        self._provider = provider
        self._url = url
        self.calls = 0
        self.returned = 0

    async def decide(
        self, questions: Sequence[ChoiceQuestion], input_text: str, *,
        budget: Budget, privacy: PrivacyGate, operation_head: str = "operation",
        target_heads: Mapping[str, str] | None = None,
    ) -> Decision:
        # The counter accepts only increment clicks and explicit stops. Offer
        # that operation domain before inference, retaining every click target
        # so the provider still has to distinguish inc from reset.
        if questions:
            heads = {op: head for op, head in (target_heads or {}).items() if op == "click"}
            scoped = []
            for question in questions:
                if question.name == operation_head:
                    indices = [
                        index for index, choice in enumerate(question.choices)
                        if choice in TERMINAL_OPERATIONS or choice in heads
                    ]
                    question = replace(
                        question,
                        choices=tuple(question.choices[index] for index in indices),
                        descriptions=tuple(question.descriptions[index] for index in indices)
                        if question.descriptions else (),
                    )
                    scoped.append(question)
                elif question.name in heads.values():
                    scoped.append(question)
            payload = _object(json.loads(input_text))
            candidates = _object(payload["candidates"])
            payload["candidates"] = {op: candidates[op] for op in heads}
            questions = tuple(scoped)
            target_heads = heads
            input_text = json.dumps(payload)
        self.calls += 1
        value = await self._provider.decide(
            questions, input_text, budget=budget, privacy=privacy,
            operation_head=operation_head, target_heads=target_heads,
        )
        self.returned += 1
        if value.operation in TERMINAL_OPERATIONS and value.target is None:
            return value
        # Reject unwanted actions before the runner can prepare or dispatch them.
        # The actual provider-facing descriptor is the binding, not a caller flag.
        try:
            candidates = _object(_object(json.loads(input_text)).get("candidates"))
            group = _object(candidates.get("click"))
            if not isinstance(value.target, str):
                raise TypeError("counter_target")
            binding = _object(group.get(value.target))
            node = _object(binding.get("node"))
            state = _object(node.get("state"))
            allowed = (
                value.operation == "click"
                and binding.get("operation") == "click" and node.get("name") == "inc"
                and node.get("role") == "button" and state.get("tag") == "button"
                and state.get("type") == "button" and state.get("disabled") is False
                and binding.get("url") == self._url
            )
        except (ValueError, TypeError, KeyError):
            allowed = False
        if not allowed:
            raise RunnerError("counter_decision_rejected")
        # The runner reserves its four steps at before_dispatch (bootstrap plus
        # three clicks). A stale returned choice spends a call, but not a step.
        return value

    async def generate_text(
        self, prompt: str, *, budget: Budget, privacy: PrivacyGate,
        max_text_chars: int = 4096,
    ) -> str:
        raise RunnerError("counter_text_helper_disabled")

    async def aclose(self) -> None:
        await self._provider.aclose()


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("invalid_evidence_object")
    return value


def _proposal_body(proposal: AlphaProposal) -> dict[str, Any]:
    """Historical digest input from the manager's actual proposal, never a grant."""
    return {
        "kind": "alpha", "run_id": proposal.run_id, "proposal_id": proposal.proposal_id,
        "budgets": dict(proposal.budgets), "canonical_urls": list(proposal.canonical_urls),
        "deadline": proposal.deadline, "expires_at": proposal.expires_at,
        "operations": sorted(proposal.operations), "origins": sorted(proposal.origins),
        "residual_risk": proposal.residual_risk,
    }


class _EvidenceError(ValueError):
    """A fixed collector-defined code, never page content or exception text."""


def validate_evidence(
    request: TaskRequest, result: TaskResult, artifacts: Sequence[Artifact],
    driver: Mapping[str, Any], fixture_clean: bool,
    *, proposal: AlphaProposal | None = None,
) -> list[str]:
    """Technical checks only. No supplied flag can turn these into live acceptance."""
    failures = []
    try:
        url = urlsplit(request.start_url)
        if (url.port is None or request.start_url != f"http://127.0.0.1:{url.port}/counter.html"
                or request.model_dump_json() != counter_request(request.start_url).model_dump_json()):
            failures.append("noncanonical_request")
    except (ValueError, TypeError):
        failures.append("noncanonical_request")
    if (result.terminal != "verified" or result.functional != "verified"
            or result.development_only or result.visual_diagnostic != "not_requested"
            or result.reason != "verified"
            or result.last_url not in (None, request.start_url)):
        failures.append("final_result")
    usage = result.usage
    if (usage.steps != 4 or not 3 <= usage.calls <= 4 or usage.input_bytes > 65536
            or usage.output_bytes > 65536 or usage.stored_bytes > 1048576):
        failures.append("usage_bounds")
    if (driver.get("count") != "3" or driver.get("count_url") != request.start_url
            or driver.get("count_error") is not None):
        failures.append("independent_count")
    if (driver.get("close_completed") is not True or driver.get("cleanup_errors") != []
            or driver.get("residual_owned") is not False
            or driver.get("foreign_mismatch") not in (None, False) or not fixture_clean):
        failures.append("cleanup")
    try:
        if (not isinstance(proposal, AlphaProposal) or proposal.run_id != result.run_id
                or proposal.canonical_urls != (canonical_url(request.start_url),)
                or proposal.origins != frozenset({canonical_origin(request.start_url)})
                or not {"navigate", "click"} <= proposal.operations
                or dict(proposal.budgets) != request.limits.model_dump()):
            raise _EvidenceError("proposal_scope")
        raw = json.dumps(_proposal_body(proposal), sort_keys=True, separators=(",", ":"), allow_nan=False)
        if hashlib.sha256(raw.encode()).hexdigest() != proposal.digest:
            raise _EvidenceError("proposal_digest")
        # Alpha admits a run scope. Its digest is intentionally different from
        # an action's observation/token/fingerprint digest, and survives revoke.
        admitted_binding = (proposal.proposal_id, proposal.digest)
        ids = [artifact.id for artifact in artifacts]
        if (ids != result.artifact_ids or len(ids) != len(set(ids))
                or len(ids) > 128 or sum(len(a.content) for a in artifacts) > 1048576):
            raise _EvidenceError("artifact_manifest")
        attempts: list[str] = []
        checked_attempts = 0
        pending = None
        click_document = None
        binding: tuple[str, str] | None = None
        origin = f"{urlsplit(request.start_url).scheme}://{urlsplit(request.start_url).netloc}"
        for artifact in artifacts:
            if _ID.fullmatch(artifact.id) is None:
                raise _EvidenceError("artifact_id")
            if artifact.media_type != "application/json":
                raise _EvidenceError("artifact_type")
            if hashlib.sha256(artifact.content).hexdigest() != artifact.sha256:
                raise _EvidenceError("artifact_digest")
            event = _object(json.loads(artifact.content))
            if "event" not in event:
                if (binding is not None or pending is not None or not attempts
                        or checked_attempts != len(attempts) - 1):
                    raise _EvidenceError("functional_sequence")
                report = FunctionalReport.model_validate(event)
                final = len(attempts) == 4
                # Expected value is fixed by the canonical request. The real
                # report intentionally stores neither expected nor page text.
                if (report.terminal != ("verified" if final else "verification_failed")
                        or report.functional != ("verified" if final else "failed")
                        or len(report.evidence) != 1):
                    raise _EvidenceError("functional_report")
                check = report.evidence[0]
                if (check.kind != "text" or check.target != "#count"
                        or check.passed is not final
                        or check.reason != ("ok" if final else "mismatch")):
                    raise _EvidenceError("functional_check")
                checked_attempts = len(attempts)
                continue
            if event.get("run_id") != result.run_id:
                raise _EvidenceError("journal_run")
            kind = event["event"]
            if kind == "admission_binding":
                if set(event) != {"event", "run_id", "proposal_id", "digest"}:
                    raise _EvidenceError("binding_schema")
                if (pending is not None or binding is not None or not attempts
                        or checked_attempts != len(attempts)):
                    raise _EvidenceError("orphan_binding")
                proposal_id, digest = event.get("proposal_id"), event.get("digest")
                if (not isinstance(proposal_id, str) or str(uuid.UUID(proposal_id)) != proposal_id
                        or not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None):
                    raise _EvidenceError("invalid_binding")
                binding = (proposal_id, digest)
                if binding != admitted_binding:
                    raise _EvidenceError("unrelated_binding")
                continue
            if kind == "before_dispatch":
                if set(event) != {"event", "run_id", "action_digest", "admission_class", "semantic_fingerprint"}:
                    raise _EvidenceError("dispatch_schema")
                if (pending is not None or len(attempts) >= 4
                        or checked_attempts != len(attempts)):
                    raise _EvidenceError("dispatch_sequence")
                if (event.get("admission_class") != "ordinary_admitted"
                        or not isinstance(event.get("action_digest"), str)
                        or _DIGEST.fullmatch(event["action_digest"]) is None):
                    raise _EvidenceError("dispatch_admission")
                fingerprint = _object(json.loads(event["semantic_fingerprint"]))
                semantic = _object(fingerprint.get("semantic"))
                document = fingerprint.get("document_id")
                if not isinstance(document, str) or not document or semantic.get("unsupported") is not None:
                    raise _EvidenceError("dispatch_semantic")
                if not attempts:
                    if binding is not None:
                        raise _EvidenceError("bootstrap_binding")
                    if (semantic.get("operation") != "navigate" or semantic.get("origin") != "null"
                            or semantic.get("url") != "about:blank"
                            or fingerprint.get("destination") != request.start_url
                            or fingerprint.get("token") is not None or semantic.get("node") is not None):
                        raise _EvidenceError("bootstrap_navigation")
                else:
                    if binding is None:
                        raise _EvidenceError("missing_binding")
                    binding = None
                    node = _object(semantic.get("node"))
                    state = _object(node.get("state"))
                    if (semantic.get("operation") != "click" or node.get("name") != "inc"
                            or node.get("role") != "button" or state.get("tag") != "button"
                            or state.get("type") != "button" or state.get("disabled") is not False
                            or semantic.get("url") != request.start_url or semantic.get("origin") != origin
                            or semantic.get("form") is not None or fingerprint.get("destination") is not None
                            or not isinstance(fingerprint.get("token"), str)):
                        raise _EvidenceError("increment_click")
                    if click_document is not None and document != click_document:
                        raise _EvidenceError("click_document")
                    click_document = document
                attempts.append(artifact.id)
                pending = artifact.id
            elif kind == "outcome":
                if set(event) != {"event", "run_id", "attempt_id", "status"}:
                    raise _EvidenceError("outcome_schema")
                if (pending is None or event.get("attempt_id") != pending
                        or event.get("status") != "acknowledged"):
                    raise _EvidenceError("paired_outcome")
                pending = None
            else:
                raise _EvidenceError("unexpected_journal_event")
        if len(attempts) != 4 or checked_attempts != 4 or pending is not None or binding is not None:
            raise _EvidenceError("dispatch_sequence")
    except _EvidenceError as exc:
        failures.append(exc.args[0])
    except (ValueError, TypeError, KeyError):
        failures.append("artifact_schema")
    return failures


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


class CounterAcceptanceManager(RunManager):
    """Observe results and the admitted alpha; inherit all execution/admission."""

    def __init__(self, fixture: CounterFixture, output: Path) -> None:
        self.fixture = fixture
        self.output = output
        self._request_json = counter_request(fixture.url).model_dump_json()
        self.observer: asyncio.Task[None] | None = None
        self.recording_driver: RecordingDriver | None = None
        self.recording_provider: DecisionsOnlyProvider | None = None
        self.admission_proposal: AlphaProposal | None = None
        self.report_error: str | None = None
        self.structural_status = "NOT_RUN"
        self._started_once = False
        self._started_run_id: str | None = None
        store = AdmissionStore(clock=time.monotonic, cleanup_grace=2.0)
        super().__init__(
            provider_factory=self._make_provider, driver_factory=self._make_driver,
            admission_store=store, desk=StandardFormDesk(store),
            clock=time.monotonic, cleanup_grace=2.0,
        )

    @property
    def request(self) -> TaskRequest:
        return TaskRequest.model_validate_json(self._request_json)

    def _make_driver(self, mode: str) -> RecordingDriver:
        self.recording_driver = RecordingDriver(mode, self.request.start_url)
        return self.recording_driver

    def _make_provider(self) -> DecisionsOnlyProvider:
        # The inherited body calls this only after alpha acceptance. Retain the
        # immutable scope while it is live; cleanup later revokes the record.
        # This observes existing admission and cannot create or accept one.
        run = self._run
        if (run is not None and isinstance(run.alpha, AlphaProposal)
                and run.admission_accepted and self._admission.is_live(run.alpha.proposal_id)):
            self.admission_proposal = run.alpha
        self.recording_provider = DecisionsOnlyProvider(production_factory(), self.request.start_url)
        return self.recording_provider

    def start_task(self, request: TaskRequest) -> str:
        if not isinstance(request, TaskRequest) or request.model_dump_json() != self._request_json:
            raise RunnerError("counter_request_mismatch")
        if self._started_once or self.fixture.closed:
            raise RunnerError("counter_unavailable")
        run_id = super().start_task(self.request)
        self._started_run_id = run_id
        run = self._live(run_id)
        self.fixture.hold_until(run.budget.original_deadline + self._grace)
        self._started_once = True
        self.observer = asyncio.create_task(self._observe(run_id), name="counter-evidence")
        return run_id

    def _report(self, status: str, **fields: Any) -> None:
        provider = self.recording_provider
        self.structural_status = status
        _write_json(self.output / "report.json", {
            **fields,
            "schema_version": 1, "structural_status": status,
            "live_acceptance": "UNVERIFIED", "human_admission": "UNVERIFIED",
            "native_host": "NOT_RUN", "live_provider": "UNVERIFIED",
            "provider_evidence": {
                "constructed": provider is not None,
                "decide_invocations": provider.calls if provider else 0,
                "decide_returns": provider.returned if provider else 0,
                "note": "Method counters do not prove outbound HTTP or a live response.",
            },
            "fixture_cleanup": self.fixture.clean,
            "admission_scope": (
                {**_proposal_body(self.admission_proposal), "digest": self.admission_proposal.digest}
                if self.admission_proposal is not None else None
            ),
        })

    async def _observe(self, run_id: str) -> None:
        try:
            async with asyncio.timeout(125.0):
                while True:
                    result = self.get_task(run_id)
                    if result.terminal not in _OPEN:
                        break
                    await asyncio.sleep(0.01)
                artifacts = [self.read_artifact(run_id, key) for key in result.artifact_ids]
                # Export synchronously while the final public result is retained.
                _write_json(self.output / "result.json", result.model_dump())
                directory = self.output / "artifacts"
                directory.mkdir()
                manifest = []
                for artifact in artifacts:
                    if _ID.fullmatch(artifact.id) is None:
                        raise ValueError("unsafe_artifact_id")
                    (directory / artifact.id).write_bytes(artifact.content)
                    manifest.append({"id": artifact.id, "media_type": artifact.media_type,
                                     "sha256": artifact.sha256, "bytes": len(artifact.content)})
                _write_json(self.output / "artifacts.json", manifest)
                await self.fixture.close()
                evidence = self.recording_driver.evidence() if self.recording_driver else {}
                failures = validate_evidence(self.request, result, artifacts, evidence, self.fixture.clean,
                                             proposal=self.admission_proposal)
                self._report("PASS" if not failures else "FAIL", run_id=run_id,
                             failures=failures, driver=evidence)
        except asyncio.CancelledError:
            self.report_error = "observer_cancelled"
            await self.fixture.close()
            self._failure_report(run_id)
            raise
        except Exception:  # noqa: BLE001 - sanitize file/transport/page errors
            self.report_error = "evidence_collection_failed"
            await self.fixture.close()
            self._failure_report(run_id)
        finally:
            await self.fixture.close()

    def _failure_report(self, run_id: str | None) -> None:
        try:
            self._report("ERROR", run_id=run_id, failures=[self.report_error])
        except Exception:  # noqa: BLE001 - still close resources on unwritable output
            print("counter acceptance: report_write_failed", file=sys.stderr)

    async def aclose(self) -> None:
        try:
            await super().aclose()
            if self.observer is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(self.observer), 5.0)
                except TimeoutError:
                    self.observer.cancel()
                    await asyncio.gather(self.observer, return_exceptions=True)
        except asyncio.CancelledError:
            # The caller's cancellation must not detach either owned job.
            await super().aclose()
            raise
        finally:
            if self.observer is not None and not self.observer.done():
                self.observer.cancel()
                await asyncio.gather(self.observer, return_exceptions=True)
                if self.report_error is None:
                    self.report_error = "observer_cancelled"
            await self.fixture.close()
            if self.observer is None:
                self.report_error = "task_not_started"
                self._failure_report(None)
            elif self.report_error is not None and self.structural_status != "ERROR":
                self._failure_report(self._started_run_id)


async def serve(output: Path) -> int:
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    fixture = CounterFixture()
    manager = None
    try:
        await fixture.start()
        manager = CounterAcceptanceManager(fixture, output)
        _write_json(output / "request.json", manager.request.model_dump())
        manager._report("NOT_RUN", failures=[])
        server = create_server(manager)
        print(f"Counter request and evidence directory: {output}", file=sys.stderr)
        await server.run_stdio_async()
    finally:
        if manager is not None:
            await manager.aclose()
        else:
            await fixture.close()
    return 0 if manager.structural_status == "PASS" and manager.report_error is None else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path,
                        help="New private directory for the request and exported evidence")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(serve(args.output_dir.resolve()))
    except Exception:  # noqa: BLE001 - never print credential/provider error text
        print("counter acceptance: server_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
