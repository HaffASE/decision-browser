"""Portable upstream-inspired workflows; real Chromium, scripted model, no API."""

import asyncio

import pytest
from browser_scenarios import (
    CASES,
    ScenarioSite,
    ScriptedScenarioProvider,
    Step,
    run_scenario,
)
from test_runner import record_final_verification


@pytest.fixture
def final_verification_calls(monkeypatch):
    return record_final_verification(monkeypatch)


@pytest.mark.parametrize("case_id", CASES)
async def test_owned_workflow_has_verified_runner_and_independent_oracle(case_id):
    result = await run_scenario(case_id)
    assert result["success"], result
    assert (result["terminal"], result["functional"]) == ("verified", "verified")
    assert result["oracle"]["observed"] == list(CASES[case_id].expected_events)
    assert result["development_only"] is False
    assert result["admission_class"] == "ordinary_task_scoped"
    assert result["provider_mode"] == "scripted"
    assert result["steps"] >= len(CASES[case_id].plan) + 1
    if case_id == "autocomplete":
        assert result["snapshot"]["options"] == 0
    if case_id == "below_fold":
        assert result["snapshot"]["scrollY"] > 0
    if case_id == "consent":
        assert not result["snapshot"]["overlayVisible"]
    if case_id == "styled_filter":
        assert result["snapshot"]["values"]["brand"] is True
    if case_id in {"loading_overlay", "delayed_menu"}:
        assert result["scripted_evidence"]["wait_calls"] >= 1
    if case_id == "loading_overlay":
        assert result["scripted_evidence"]["observed_loading"]
        assert result["scripted_evidence"]["waited_for"] == ["Service query"]


@pytest.mark.parametrize("case_id", ["loading_overlay", "delayed_menu"])
async def test_scripted_reveal_still_requires_wait_after_slow_decision(case_id, monkeypatch):
    original = ScriptedScenarioProvider.decide

    async def slow_decide(self, *args, **kwargs):
        if self.calls == 1:
            # Longer than the configured-mode timer: a delayed host must still
            # observe the held control and execute the production WAIT action.
            await asyncio.sleep(.7)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(ScriptedScenarioProvider, "decide", slow_decide)
    result = await run_scenario(case_id)
    assert result["success"], result
    assert result["scripted_evidence"]["wait_calls"] >= 1
    assert result["steps"] >= len(CASES[case_id].plan) + 2
    if case_id == "loading_overlay":
        assert result["scripted_evidence"]["observed_loading"]


@pytest.mark.parametrize("case_id", ["loading_overlay", "delayed_menu"])
async def test_pending_ui_can_still_wait_until_its_fourth_poll(case_id, monkeypatch):
    original = ScriptedScenarioProvider.__init__

    def hold_reveal(self, plan, on_wait=None):
        async def release_when_ready():
            if self.wait_calls >= 4:
                await on_wait()

        original(self, plan, release_when_ready)

    monkeypatch.setattr(ScriptedScenarioProvider, "__init__", hold_reveal)
    result = await run_scenario(case_id)
    assert result["success"], result
    assert result["scripted_evidence"]["wait_calls"] >= 4


@pytest.mark.parametrize("case_id", CASES)
@pytest.mark.parametrize("terminal", ["DONE", "UNABLE"])
async def test_early_done_never_passes_workflow_or_server_oracle(case_id, terminal, final_verification_calls):
    provider = ScriptedScenarioProvider((), terminal=terminal)
    result = await run_scenario(case_id, provider_factory=lambda: provider)
    assert "DONE" not in provider.stop_choices and "UNABLE" in provider.stop_choices
    assert (terminal in provider.stop_choices) is (terminal == "UNABLE")
    if terminal == "DONE":
        assert result["terminal"] == "failed", result
        assert "runner:helper_schema" in result["evidence"]
        assert final_verification_calls == []
    else:
        assert result["terminal"] == "verification_failed", result
        assert "runner:helper_schema" not in result["evidence"]
        assert "model:unable" in result["evidence"]
        assert final_verification_calls == [True]
    assert result["functional"] == "failed"
    assert result["calls"] == 1
    assert not result["success"] and not result["oracle"]["success"]
    assert result["oracle"]["observed"] == []


@pytest.mark.parametrize("case_id,plan", [
    ("autocomplete", (Step("fill", "Origin"), Step("click", "Zurich Airport hotel"))),
    ("calendar", (Step("click", "Departure"), Step("click", "August 20, 2026"), Step("click", "Done"))),
    ("styled_filter", (Step("click", "다른 브랜드"),)),
    ("hotel", (Step("fill", "Destination"), Step("click", "Find stays"),
               Step("click", "Free cancellation"), Step("click", "Casa Flora"))),
    ("article", (Step("fill", "Search query"), Step("click", "Search articles"), Step("click", "Kurt Gödel biography"))),
    ("delayed_menu", (Step("click", "Sections"), Step("click", "Business"))),
])
@pytest.mark.parametrize("terminal", ["DONE", "UNABLE"])
async def test_similar_or_incomplete_results_do_not_satisfy_workflow(
    case_id, plan, terminal, final_verification_calls,
):
    provider = ScriptedScenarioProvider(plan, terminal=terminal)
    result = await run_scenario(case_id, provider_factory=lambda: provider)
    assert "DONE" not in provider.stop_choices and "UNABLE" in provider.stop_choices
    assert (terminal in provider.stop_choices) is (terminal == "UNABLE")
    if terminal == "DONE":
        assert result["terminal"] == "failed", result
        assert "runner:helper_schema" in result["evidence"]
        assert final_verification_calls == []
    else:
        assert result["terminal"] == "verification_failed", result
        assert "runner:helper_schema" not in result["evidence"]
        assert "model:unable" in result["evidence"]
        assert final_verification_calls == [True]
    assert result["functional"] == "failed"
    assert not result["success"] and not result["oracle"]["success"]


async def test_hotel_accepts_filters_before_search_in_another_valid_order():
    plan = (Step("click", "Free cancellation"), Step("select", "Stay category", "design"),
            Step("fill", "Destination"), Step("click", "Find stays"), Step("click", "Casa Flora"))
    result = await run_scenario("hotel", provider_factory=lambda: ScriptedScenarioProvider(plan))
    assert result["success"], result
    assert result["oracle"]["observed"] != list(CASES["hotel"].expected_events)
    assert result["oracle"]["observed"][-1] == CASES["hotel"].expected_events[-1]


def test_oracle_rejects_missing_fields_and_duplicate_effects():
    with ScenarioSite() as site:
        site.start("profile_form")
        event = CASES["profile_form"].expected_events[0]
        site.record("profile_form", {"name": "profile", "data": {"name": "Ada Lovelace"}})
        assert not site.verify("profile_form")["success"]
    with ScenarioSite() as site:
        site.start("profile_form")
        site.record("profile_form", event)
        assert site.verify("profile_form")["success"]
        site.record("profile_form", event)
        assert not site.verify("profile_form")["success"]


@pytest.mark.parametrize("events", [
    CASES["hotel"].expected_events[:-1],
    (*CASES["hotel"].expected_events, CASES["hotel"].expected_events[-1]),
    (CASES["hotel"].expected_events[0], CASES["hotel"].expected_events[0],
     CASES["hotel"].expected_events[2], CASES["hotel"].expected_events[3]),
    (CASES["hotel"].expected_events[-1], *CASES["hotel"].expected_events[:-1]),
])
def test_hotel_oracle_rejects_missing_duplicate_and_premature_events(events):
    with ScenarioSite() as site:
        site.start("hotel")
        for event in events:
            site.record("hotel", event)
        assert not site.verify("hotel")["success"]
