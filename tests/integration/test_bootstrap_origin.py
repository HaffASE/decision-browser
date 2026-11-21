"""Native Chromium bootstrap regressions; owned routes and development admission.

No live network or human-admission proof: every request is intercepted, and
DevelopmentDesk supplies an explicitly scripted local development grant.
"""

import asyncio
from dataclasses import replace

import pytest
from test_runner import RecordingDriver, ScriptedProvider, finish, request

from decision_browser import runner
from decision_browser.admission import AdmissionStore
from decision_browser.contracts import Check
from decision_browser.desk import DevelopmentDesk


class OwnedRouteDriver(RecordingDriver):
    def __init__(self, mode, start_url, final_url=None):
        super().__init__(mode)
        self.start_url = start_url
        self.final_url = final_url or start_url
        self.hits = []

    async def start(self):
        await super().start()

        async def route_owned(route):
            url = route.request.url
            self.hits.append(url)
            if url in {self.start_url, self.final_url}:
                await route.fulfill(
                    status=200, content_type="text/html",
                    body='<p id="count">0</p><p id="ready">Owned bootstrap ready</p>',
                )
            else:
                await route.abort("blockedbyclient")

        await self.page.context.route("**/*", route_owned)


def host_for(url, provider, final_url=None):
    drivers = []
    store = AdmissionStore()
    host = runner.RunManager(
        lambda: provider,
        lambda mode: drivers.append(OwnedRouteDriver(mode, url, final_url)) or drivers[-1],
        admission_store=store,
        desk=DevelopmentDesk(store, ["accept"]),
    )
    return host, drivers


async def admit(host, run_id):
    async with asyncio.timeout(5):
        while (proposal := host.pending_proposal(run_id)) is None:
            await asyncio.sleep(0)
    await host.present_admission(run_id, proposal.proposal_id, proposal.digest)


@pytest.mark.parametrize("url", ["https://example.test/", "http://example.test/"])
@pytest.mark.parametrize("check", ["route", "text", "DONE"])
async def test_default_port_bootstrap_reaches_checks_or_provider(url, check):
    provider = ScriptedProvider(["DONE"])
    host, drivers = host_for(url, provider)
    checks = {
        "route": [Check(kind="route", target="", expected=url)],
        "text": [Check(kind="text", target="#ready", expected="Owned bootstrap ready")],
        "DONE": [],
    }[check]
    run_id = host.start_task(request(url, checks))
    await admit(host, run_id)
    result = await finish(host, run_id)
    assert result.terminal == ("completed_unverified" if check == "DONE" else "verified"), {
        "reason": result.reason,
        "functional": result.functional,
        "evidence": result.evidence,
        "cleanup_pending": drivers[0].cleanup_pending,
        "cleanup_errors": drivers[0].cleanup_errors,
    }
    assert result.development_only is True
    assert result.usage.steps == 1
    assert result.usage.calls == provider.calls == (1 if check == "DONE" else 0)
    assert drivers[0].hits == [url]


@pytest.mark.parametrize("final_url", [
    "https://example.test/elsewhere",
    "https://example.test:8443/",
    "https://other.test/",
])
async def test_bootstrap_final_url_drift_still_fails_closed_without_replay(
    monkeypatch, final_url
):
    class DestinationDriftSession(runner.DOMSession):
        async def _native(self, action):
            result = await super()._native(action)
            if action.operation == "navigate":
                # Controlled browser navigation before acknowledgement, not an
                # HTTP redirect (redirect chains bypass Playwright routing).
                await self._page.goto(final_url)
            return result

    monkeypatch.setattr(runner, "DOMSession", DestinationDriftSession)
    url = "https://example.test/"
    provider = ScriptedProvider(["DONE"])
    host, drivers = host_for(url, provider, final_url)
    run_id = host.start_task(request(url))
    await admit(host, run_id)
    result = await finish(host, run_id)
    host.cancel_task(run_id)
    again = await finish(host, run_id)
    assert result.terminal == again.terminal == "outcome_unknown"
    assert result.usage.steps == again.usage.steps == 1
    assert result.usage.calls == provider.calls == 0
    assert drivers[0].hits == [url, final_url]
    assert "bootstrap:url_mismatch" in result.evidence


@pytest.mark.parametrize("mutation", [
    "wrong_origin", "cross_port", "popup", "invalid_origin", "post_observe_failed",
])
async def test_bootstrap_observation_mismatch_still_fails_closed(monkeypatch, mutation):
    class AlteredObservationSession(runner.DOMSession):
        async def observe(self):
            obs = await super().observe()
            if obs.origin != "null":
                if mutation == "post_observe_failed":
                    raise RuntimeError("owned post-navigation observation failure")
                if mutation == "invalid_origin":
                    return replace(obs, origin="not-a-valid-origin")
                if mutation == "popup":
                    return replace(obs, unsupported=(*obs.unsupported, "popup:owned"))
                origin = (
                    "https://other.test" if mutation == "wrong_origin"
                    else "https://example.test:8443"
                )
                return replace(obs, origin=origin)
            return obs

    monkeypatch.setattr(runner, "DOMSession", AlteredObservationSession)
    url = "https://example.test/"
    provider = ScriptedProvider(["DONE"])
    host, drivers = host_for(url, provider)
    run_id = host.start_task(request(url))
    await admit(host, run_id)
    result = await finish(host, run_id)
    host.cancel_task(run_id)
    again = await finish(host, run_id)
    assert result.terminal == again.terminal == "outcome_unknown"
    assert result.usage.steps == again.usage.steps == 1
    assert result.usage.calls == provider.calls == 0
    assert drivers[0].hits == [url]
    token = {
        "popup": "popup",
        "invalid_origin": "invalid_destination",
        "post_observe_failed": "post_observe_failed",
    }.get(mutation, "origin_mismatch")
    assert f"bootstrap:{token}" in result.evidence
