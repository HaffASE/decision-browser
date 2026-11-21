"""F05 public RunManager regressions on owned routes and real policy/journal."""

import asyncio
import json
from contextlib import suppress

import pytest
from playwright.async_api import Error
from test_bootstrap_origin import admit
from test_runner import ScriptedProvider, finish, request

from decision_browser import runner
from decision_browser.admission import AdmissionStore, BetaProposal
from decision_browser.artifacts import _canonical
from decision_browser.browser import BrowserBlocked, BrowserDriver
from decision_browser.desk import DevelopmentDesk
from decision_browser.runner import DevelopmentConfig, RunManager


class SaturatedManager(RunManager):
    def __init__(self, *args, saturation, **kwargs):
        super().__init__(*args, **kwargs)
        self.saturation = saturation

    async def _dispatch_one(self, run, obs, action):
        if self.saturation == "count":
            while len(run.store._items) < 127:
                self._stamp(run, run.store.put_text("retained evidence"))
        elif self.saturation == "bytes":
            needed = len(_canonical({
                "event": "before_dispatch", "run_id": run.result.run_id,
                "action_digest": self._digest(action), "semantic_fingerprint": action.fingerprint,
                "admission_class": "fixture_mapped",
            }, ()))
            remaining = run.request.limits.max_stored_bytes - run.store.stored_bytes
            self._stamp(run, run.store.put_bytes(b"x" * (remaining - needed - 10), "application/octet-stream"))
        return await super()._dispatch_one(run, obs, action)


@pytest.mark.parametrize("saturation", ["count", "bytes", "safe"])
async def test_public_dispatch_reserves_final_audit_before_effect(counter_page_url, saturation):
    provider = ScriptedProvider(["click", "DONE"])
    observed_counts = []

    class CounterManager(SaturatedManager):
        async def _dispatch_one(self, run, obs, action):
            try:
                return await super()._dispatch_one(run, obs, action)
            finally:
                # Observe the fixture before the runner starts its bounded cleanup.
                with suppress(BrowserBlocked, Error):
                    observed_counts.append(
                        await run.driver.page.locator("#count").inner_text(timeout=1000)
                    )

    host = CounterManager(
        lambda: provider,
        development_config=DevelopmentConfig(counter_page_url.rstrip("/"),
            frozenset({("navigate", "__start__"), ("click", "inc")})),
        saturation=saturation,
    )
    result = await finish(host, host.start_task(request(counter_page_url)))
    safe = saturation == "safe"
    assert result.terminal == ("completed_unverified" if safe else "limit_exceeded"), {
        "reason": result.reason,
        "evidence": result.evidence,
        "steps": result.usage.steps,
        "timings": result.timings,
        "cleanup_errors": host._run.driver.cleanup_errors,
        "observed_counts": observed_counts,
    }
    assert observed_counts == (["1"] if safe else ["0"])
    if not safe:
        assert result.usage.steps == 1
    events = [json.loads(host.read_artifact(result.run_id, key).content)
              for key in result.artifact_ids if host.read_artifact(result.run_id, key).media_type == "application/json"]
    assert sum(item.get("event") == "before_dispatch" for item in events) == (2 if safe else 1)
    assert all(item in result.artifact_ids for item in host._run.journal._attempts)


@pytest.mark.parametrize("failure", ["bootstrap_raise", "bootstrap_deadline", "post_observe"])
async def test_public_failure_outcome_is_reserved_and_discoverable(counter_page_url, monkeypatch, failure):
    clock = [0.0]
    manager_box = []

    class FailingSession(runner.DOMSession):
        async def _native(self, action):
            result = await super()._native(action)
            if action.operation == "navigate":
                if failure == "bootstrap_raise":
                    raise RuntimeError("synthetic lost bootstrap acknowledgement")
                if failure == "bootstrap_deadline":
                    clock[0] = manager_box[0]._run.budget.original_deadline
            return result

        async def observe(self):
            if failure == "post_observe" and manager_box[0]._run.budget.usage().steps == 2:
                raise RuntimeError("synthetic post observation failure")
            return await super().observe()

    monkeypatch.setattr(runner, "DOMSession", FailingSession)
    provider = ScriptedProvider(["click", "DONE"])
    host = RunManager(
        lambda: provider, clock=lambda: clock[0],
        development_config=DevelopmentConfig(counter_page_url.rstrip("/"),
            frozenset({("navigate", "__start__"), ("click", "inc")})),
    )
    manager_box.append(host)
    result = await finish(host, host.start_task(request(counter_page_url)))
    assert result.terminal == "outcome_unknown"
    events = [json.loads(host.read_artifact(result.run_id, key).content) for key in result.artifact_ids]
    assert any(item.get("status") == "outcome_unknown" for item in events)
    assert all(key in result.artifact_ids for key in host._run.journal._attempts)
    assert result.usage.steps == (2 if failure == "post_observe" else 1)


@pytest.mark.parametrize("failure", ["count", "bytes", "steps", "journal"])
async def test_public_beta_grant_is_not_consumed_before_failed_reservation(counter_page_url, monkeypatch, failure):
    class TrackingStore(AdmissionStore):
        def __init__(self):
            super().__init__()
            self.accepted_beta = 0
            self.beta_consumptions = 0

        def _accept_development(self, proposal_id, digest):
            proposal = self.get_proposal(proposal_id)
            accepted = super()._accept_development(proposal_id, digest)
            if accepted and isinstance(proposal, BetaProposal):
                self.accepted_beta += 1
            return accepted

        def consume_beta(self, *args, **kwargs):
            self.beta_consumptions += 1
            return super().consume_beta(*args, **kwargs)

    class Driver(BrowserDriver):
        async def start(self):
            await super().start()
            await self.page.context.route("**/*", lambda route: route.fulfill(
                content_type="text/html", body='<p id="count">0</p><a href="https://other.test/finish">Details</a>',
            ))

    class Host(SaturatedManager):
        async def _dispatch_one(self, run, obs, action):
            if failure == "journal":
                def fail_before(*args, **kwargs):
                    from decision_browser.artifacts import ArtifactError
                    raise ArtifactError("over_bytes")
                monkeypatch.setattr(run.journal, "before_dispatch", fail_before)
            return await super()._dispatch_one(run, obs, action)

    store = TrackingStore()
    provider = ScriptedProvider(["click", "DONE"])
    host = Host(lambda: provider, lambda mode: Driver(mode), admission_store=store,
                desk=DevelopmentDesk(store, ["accept", "accept"]),
                saturation=failure if failure in {"count", "bytes"} else "safe")
    task = request(counter_page_url)
    if failure == "steps":
        # Keep one quota slot until the action reaches its dispatch boundary;
        # avoid the loop's earlier honest exhaustion guard.
        original = host._admit_scope

        async def after_grant(run, scope):
            admitted = await original(run, scope)
            while run.budget.usage().steps < run.request.limits.max_steps:
                run.budget.reserve_step()
            return admitted

        monkeypatch.setattr(host, "_admit_scope", after_grant)
    run_id = host.start_task(task)
    await admit(host, run_id)
    async with asyncio.timeout(5):
        while not isinstance(host.pending_proposal(run_id), BetaProposal):
            await asyncio.sleep(0)
    proposal = host.pending_proposal(run_id)
    await host.present_admission(run_id, proposal.proposal_id, proposal.digest)
    result = await finish(host, run_id)
    assert store.accepted_beta == 1
    assert store.beta_consumptions == 0
    assert result.terminal == "limit_exceeded"
    assert provider.calls == 1
