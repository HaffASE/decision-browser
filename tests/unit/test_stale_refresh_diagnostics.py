"""Stable public diagnostics; no page-derived values and no relaxed guard."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.dom import Candidate, Observation
from decision_browser.flights_pilot import export_journal, safe_failure
from decision_browser.runner import RunManager, RunnerError

CANARY = "TEST_ONLY_PRIVATE_VALUE_NOT_A_CREDENTIAL"
PRIOR_URL = "https://fixture.test/start?private=" + CANARY
CHANGED_URL = "https://fixture.test/changed?private=" + CANARY


def scoped_run():
    provider = Mock(side_effect=AssertionError("No provider during stale refresh"))
    host = RunManager(provider, admission_mode="task_scope")
    run = host._open(TaskRequest(
        goal="Observe an owned fixture", start_url="https://fixture.test/start",
        values={}, checks=[], limits=Limits(max_seconds=10), privacy=PrivacyPolicy(),
    ))
    host._run = run
    prior = Observation(
        "prior", "https://fixture.test", "doc-" + CANARY, CANARY, {}, (), url=PRIOR_URL
    )
    return host, run, prior, provider


@pytest.mark.parametrize("kind,expected", [
    ("document", "stale_refresh:document_changed"),
    ("observation_url", "stale_refresh:observation_url_changed"),
    ("live_page_url", "stale_refresh:live_page_url_changed"),
])
async def test_changed_boundary_has_fixed_reason_in_exported_report(kind, expected):
    host, run, prior, provider = scoped_run()
    fresh = replace(prior, id="fresh")
    actual_url = PRIOR_URL
    if kind == "document":
        fresh = replace(fresh, document_id="replacement-" + CANARY)
    elif kind == "observation_url":
        fresh = replace(fresh, url=CHANGED_URL)
    else:
        actual_url = CHANGED_URL
    run.dom = SimpleNamespace(observe=AsyncMock(return_value=fresh))
    run.driver = SimpleNamespace(page=SimpleNamespace(url=actual_url))
    with pytest.raises(RunnerError, match="^navigation$") as caught:
        await host._refresh_stale(run, prior)
    host._apply(run, caught.value)
    result = host.get_task(run.result.run_id)
    report = {"result": result.model_dump(mode="json"),
              "failure_code": safe_failure(caught.value), "journal": export_journal(run)}
    encoded = json.dumps(report)
    assert result.terminal == "failed" and result.reason == "failed"
    assert result.evidence == ["runner:navigation", expected]
    assert report["failure_code"] == "navigation"
    assert CANARY not in encoded and "https://" not in encoded
    assert "replacement-" not in encoded and "doc-" not in encoded
    assert report["journal"] == []
    assert result.usage.calls == result.usage.steps == 0
    run.dom.observe.assert_awaited_once()
    provider.assert_not_called()


@pytest.mark.parametrize("document_changed", [False, True])
async def test_multiple_changes_keep_original_first_guard_precedence(document_changed):
    host, run, prior, provider = scoped_run()
    fresh = replace(prior, id="fresh", url=CHANGED_URL,
                    document_id="new" if document_changed else prior.document_id)
    run.dom = SimpleNamespace(observe=AsyncMock(return_value=fresh))
    run.driver = SimpleNamespace(page=SimpleNamespace(url=CHANGED_URL))
    with pytest.raises(RunnerError, match="^navigation$"):
        await host._refresh_stale(run, prior)
    assert run.result.evidence == [
        "stale_refresh:document_changed" if document_changed
        else "stale_refresh:observation_url_changed"
    ]
    provider.assert_not_called()


async def test_unchanged_boundary_returns_fresh_observation_without_dispatch():
    host, run, prior, provider = scoped_run()
    fresh = replace(prior, id="fresh", text="Changed harmless control text")
    run.dom = SimpleNamespace(observe=AsyncMock(return_value=fresh))
    run.driver = SimpleNamespace(page=SimpleNamespace(url=PRIOR_URL))
    assert await host._refresh_stale(run, prior) is fresh
    assert run.result.evidence == []
    assert run.result.usage.calls == run.result.usage.steps == 0
    provider.assert_not_called()


async def test_legacy_observation_url_fallback_still_rejects_live_url_change():
    host, run, prior, _provider = scoped_run()
    candidate = Candidate("control", ("click",), {"click": {"url": PRIOR_URL}}, None)
    prior = replace(prior, url=None, candidates={"control": candidate})
    run.dom = SimpleNamespace(observe=AsyncMock(return_value=replace(prior, id="fresh")))
    run.driver = SimpleNamespace(page=SimpleNamespace(url=CHANGED_URL))
    with pytest.raises(RunnerError, match="^navigation$"):
        await host._refresh_stale(run, prior)
    assert run.result.evidence == ["stale_refresh:live_page_url_changed"]
    assert CANARY not in run.result.model_dump_json()


async def test_new_diagnostic_survives_bounded_existing_evidence_without_duplicates():
    host, run, prior, _provider = scoped_run()
    note = "stale_refresh:document_changed"
    host._set(run, evidence=[f"existing:{i}" for i in range(31)] + [note])
    run.dom = SimpleNamespace(observe=AsyncMock(return_value=replace(prior, document_id="new")))
    run.driver = SimpleNamespace(page=SimpleNamespace(url=PRIOR_URL))
    with pytest.raises(RunnerError, match="^navigation$"):
        await host._refresh_stale(run, prior)
    assert run.result.evidence[0] == note
    assert run.result.evidence.count(note) == 1
    assert len(run.result.evidence) == 32
