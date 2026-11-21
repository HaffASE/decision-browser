"""Public bounded diagnostics preserve primary and cleanup causes without raw data."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from decision_browser import runner
from decision_browser.artifacts import ArtifactError
from decision_browser.browser import BrowserBlocked
from decision_browser.budget import BudgetExceeded
from decision_browser.contracts import Check, Limits, PrivacyPolicy, TaskRequest
from decision_browser.desk import DeskUnavailable
from decision_browser.policy import ActionScope
from decision_browser.provider_http import ProviderError
from decision_browser.runner import RunManager, RunnerError
from decision_browser.verification import CheckEvidence, FunctionalReport

CANARY = 'SYNTHETIC_PRIVATE_ERROR_VALUE_42'


class Driver:
    cleanup_errors = ()
    residual_owned = ()
    foreign_mismatch = False
    page = SimpleNamespace(url="https://example.test/")

    async def close(self):
        pass


def opened(checks=(), *, desk=None, driver=None):
    driver = driver or Driver()
    manager = RunManager(lambda: None, lambda mode: driver, desk=desk)
    run = manager._open(TaskRequest(
        goal="Review", start_url="https://example.test/", values={}, checks=list(checks),
        privacy=PrivacyPolicy(), limits=Limits(max_seconds=10),
    ))
    manager._run = run
    manager._set(run, terminal="running", evidence=["stale_refresh:document_changed", *(
        f"old:{i}" for i in range(31)
    )])
    return manager, run


async def test_check_evidence_does_not_erase_lifecycle_notes_at_cap(monkeypatch):
    manager, run = opened([Check(kind="route", expected="https://example.test/")])
    report = FunctionalReport(terminal="verified", functional="verified", evidence=[
        CheckEvidence(kind="route", target="", passed=True, reason="ok"),
    ])
    monkeypatch.setattr(runner, "verify_functional", AsyncMock(return_value=report))
    assert await manager._verify(run, finish=True)
    result = manager.get_task(run.result.run_id)
    assert "stale_refresh:document_changed" in result.evidence
    assert "route:ok" in result.evidence
    assert len(result.evidence) <= 32


@pytest.mark.parametrize("error,note", [
    (ProviderError("invalid_mapping"), "provider:invalid_mapping"),
    (ProviderError("invalid_model"), "provider:invalid_model"),
    (ProviderError("malformed"), "provider:malformed"),
    (ProviderError("invalid_api_key"), "provider:invalid_api_key"),
    (RunnerError("invalid_provider_config"), "runner:invalid_provider_config"),
    (RunnerError("navigation"), "runner:navigation"),
    (ArtifactError("unknown_id"), "artifact:unknown_id"),
    (BudgetExceeded("output_bytes"), "budget:output_bytes"),
    (DeskUnavailable("no_supported_human_grant_channel"), "desk:no_supported_human_grant_channel"),
])
def test_known_error_codes_survive_full_buffer_in_public_result(error, note):
    manager, run = opened()
    manager._apply(run, error)
    result = manager.get_task(run.result.run_id)
    assert note in result.evidence
    assert len(result.evidence) <= 32
    assert CANARY not in result.model_dump_json()


@pytest.mark.parametrize("error", [ProviderError(CANARY), RunnerError(CANARY), ArtifactError(CANARY)])
def test_unknown_error_text_is_never_published(error):
    manager, run = opened()
    manager._apply(run, error)
    result = manager.get_task(run.result.run_id)
    assert CANARY not in result.model_dump_json()
    assert len(result.evidence) <= 32


def test_queued_unknown_keeps_sanitized_primary_and_secondary_causes():
    manager, run = opened()
    run.queued = True
    error = ProviderError("malformed_response")
    error.diagnostics = ("provider:response_close_failed", "http_status:server_error", CANARY)
    manager._apply(run, error)
    result = manager.get_task(run.result.run_id)
    assert result.terminal == "outcome_unknown"
    assert {"provider:malformed_response", "provider:response_close_failed", "http_status:server_error"} <= set(result.evidence)
    assert CANARY not in result.model_dump_json()
    assert len(result.evidence) <= 32


async def test_primary_and_cleanup_errors_both_survive_full_buffer():
    class BrokenDriver(Driver):
        async def close(self):
            raise RuntimeError(CANARY)

    manager, run = opened(driver=BrokenDriver())
    manager._apply(run, ProviderError("invalid_mapping"))
    await manager._cleanup(run)
    result = manager.get_task(run.result.run_id)
    assert result.terminal == "outcome_unknown"
    assert result.reason == "cleanup_unknown"
    assert "provider:invalid_mapping" in result.evidence
    assert "cleanup:unknown" in result.evidence
    assert CANARY not in result.model_dump_json()
    assert len(result.evidence) <= 32


@pytest.mark.parametrize("error,note", [
    (BrowserBlocked("cleanup_uncertain", CANARY), "browser:cleanup_uncertain"),
    (ProviderError("transport_error"), "provider:transport_error"),
])
async def test_coded_cleanup_cause_keeps_primary_and_suppresses_raw_message(error, note):
    class CodedBrokenDriver(Driver):
        async def close(self):
            raise error

    manager, run = opened(driver=CodedBrokenDriver())
    manager._apply(run, RunnerError("navigation"))
    await manager._cleanup(run)
    result = manager.get_task(run.result.run_id)
    assert result.terminal == "outcome_unknown" and result.reason == "cleanup_unknown"
    assert {"runner:navigation", "cleanup:unknown", note} <= set(result.evidence)
    assert CANARY not in result.model_dump_json()


def test_latest_journal_failure_survives_full_evidence_buffer(monkeypatch):
    manager, run = opened()
    run.attempt_id = "a" * 32
    def failing_outcome(*args):
        raise ArtifactError("bad_attempt")

    monkeypatch.setattr(run.journal, "outcome", failing_outcome)
    manager._audit_unknown(run)
    result = manager.get_task(run.result.run_id)
    assert "journal_unavailable" in result.evidence
    assert len(result.evidence) <= 32


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_resumed_beta_preserves_public_refusal_reason(cleanup_fails):
    class UnavailableDesk:
        development_only = True

        async def present(self, proposal):
            raise DeskUnavailable("no_supported_human_grant_channel")

    class RefusalDriver(Driver):
        async def close(self):
            if cleanup_fails:
                raise RuntimeError(CANARY)

    manager, run = opened(desk=UnavailableDesk(), driver=RefusalDriver())
    store = manager._admission
    alpha = store.propose_alpha(
        run.result.run_id, ["https://example.test/"], ["click"], {},
        run.budget.original_deadline, development_only=True,
    )
    assert store.development_desk().accept(alpha.proposal_id, alpha.digest)
    scope = ActionScope(
        run.result.run_id, "click", "https://example.test", "https://other.test",
        "semantic-fingerprint", signal_conflict=True, destination_url="https://other.test/read",
    )
    waiting = asyncio.create_task(manager._admit_scope(run, scope))
    try:
        async with asyncio.timeout(1):
            while run.pending is None:
                await asyncio.sleep(0)
        proposal = run.pending
        presented = await manager.present_admission(run.result.run_id, proposal.proposal_id, proposal.digest)
        assert presented.reason == "no_supported_human_grant_channel"
        with pytest.raises(RunnerError, match="blocked"):
            await waiting
        result = manager.get_task(run.result.run_id)
        assert result.terminal == "blocked"
        assert result.reason == "no_supported_human_grant_channel"
        if cleanup_fails:
            await manager._cleanup(run)
            result = manager.get_task(run.result.run_id)
            assert result.terminal == "outcome_unknown"
            assert {"desk:no_supported_human_grant_channel", "cleanup:unknown"} <= set(result.evidence)
            assert CANARY not in result.model_dump_json()
    finally:
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_owned_bind_keeps_primary_when_cleanup_forces_unknown(monkeypatch):
    from test_owned_initial_document import _bind

    manager, run, driver, _page, _session, dom = await _bind()
    monkeypatch.setattr(manager, "_owned_navigate", AsyncMock(side_effect=RunnerError("blocked")))
    monkeypatch.setattr(manager, "_owned_close_for_deadline", AsyncMock(return_value=SimpleNamespace(unknown=True)))
    try:
        with pytest.raises(RunnerError, match="outcome_unknown") as caught:
            await manager.bind_owned_initial_document(run)
        manager._apply(run, caught.value)
        result = manager.get_task(run.result.run_id)
        assert result.terminal == "outcome_unknown"
        assert "runner:blocked" in result.evidence
        assert "cleanup:unknown" in result.evidence
        assert CANARY not in result.model_dump_json()
    finally:
        if run.owned_resource is not None:
            await run.owned_resource.close()
        await dom.close()
        await driver.close()


async def test_dispatch_keeps_coded_journal_cause_when_effect_is_unknown(monkeypatch):
    from test_owned_observation_alignment import raced_dispatch

    manager, run, obs, action, dom, _corrected, provider = raced_dispatch()
    original = run.journal.outcome

    def fail_ack(run_id, attempt_id, status):
        if status == "acknowledged":
            raise ArtifactError("bad_status")
        return original(run_id, attempt_id, status)

    monkeypatch.setattr(run.journal, "outcome", fail_ack)
    with pytest.raises(RunnerError, match="outcome_unknown") as caught:
        await manager._dispatch_one(run, obs, action)
    manager._apply(run, caught.value)
    result = manager.get_task(run.result.run_id)
    assert result.terminal == "outcome_unknown"
    assert "artifact:bad_status" in result.evidence
    assert dom.dispatches == result.usage.steps == 1
    assert any(json.loads(manager.read_artifact(result.run_id, artifact).content).get("status") == "outcome_unknown"
               for artifact in result.artifact_ids)
    provider.assert_not_called()
