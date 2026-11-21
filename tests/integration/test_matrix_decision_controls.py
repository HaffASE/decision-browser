"""Offline native controls for failed matrix paths, without a model or API call."""

from __future__ import annotations

import json

import pytest
from browser_scenarios import ScriptedScenarioProvider, Step, _ScenarioDriver
from test_long_benchmark_scenarios import _suite

from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.runner import RunManager


@pytest.mark.parametrize("case_id,heading,target,clue", [
    ("library_research", "Companion editions", "Peer-reviewed paper", "reference GL-42"),
    ("exhibit_planner", "Second exhibition", "Fresco Study", "Copper wing"),
    ("equipment_pickup", "Depot inventory", "East depot", "Configure the camera kit."),
    ("support_handoff", "Routing", "On-call rotation", "EDGE-72"),
])
async def test_matrix_failed_paths_keep_controls_and_prior_visible_evidence(
    case_id, heading, target, clue,
):
    case = _suite.CASES[case_id]
    contexts = []

    class VisibleProvider(ScriptedScenarioProvider):
        async def decide(self, questions, input_text, **kwargs):
            contexts.append(json.loads(input_text))
            return await super().decide(questions, input_text, **kwargs)

    plan = []
    for section in case.sections:
        for field in section.fields:
            operation = "click" if field.checkbox else "select" if field.options else "fill"
            plan.append(Step(operation, field.label, field.expected if field.options else None))
        plan.append(Step("click", section.action))
    provider = VisibleProvider(plan)
    driver = _ScenarioDriver("isolated")
    with _suite.ScenarioSite() as site:
        url = site.start(case_id)
        manager = RunManager(lambda: provider, lambda _: driver, admission_mode="task_scope")
        try:
            run_id = manager.start_task(TaskRequest(
                goal=case.goal, start_url=url, values=case.values, checks=case.checks(url),
                mode="isolated", visual=False, privacy=PrivacyPolicy(),
                limits=Limits(max_seconds=90, max_steps=25, max_provider_calls=25),
            ))
            await manager._run.task
            result = manager.get_task(run_id)
            assert result.terminal == "verified", result
            assert site.verify(case_id)["success"]
            assert provider.wait_calls == 0
            assert driver.final_url == url + "summary"
        finally:
            await manager.aclose()

    state = next(state for state in contexts if heading in state["text"])
    assert any(row["name"] == target for rows in state["candidates"].values()
               for row in rows.values())
    assert any(clue in excerpt for excerpt in state["previous_pages"])

    initial = contexts[0]["candidates"]
    for field in case.sections[0].fields:
        operation = "select" if field.options else "fill"
        rows = [row for row in initial[operation].values() if row["name"] == field.label]
        assert rows and all(row["node"]["valueMissing"] is True for row in rows)
        if operation == "fill":
            assert all(row["fill_state"] == "differs" for row in rows)
