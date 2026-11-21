"""Real observations supply progress feedback without changing actions or goals."""

from __future__ import annotations

import json

import pytest
from browser_scenarios import CASES, ScriptedScenarioProvider, run_scenario


@pytest.mark.parametrize("case_id", ["autocomplete", "hotel", "profile_form", "below_fold"])
async def test_native_workflow_reports_pending_checks_and_current_fill_or_scroll_state(case_id):
    class ObservingProvider(ScriptedScenarioProvider):
        contexts = None

        async def decide(self, questions, input_text, **kwargs):
            self.contexts.append(json.loads(input_text))
            return await super().decide(questions, input_text, **kwargs)

    provider = ObservingProvider(CASES[case_id].plan)
    provider.contexts = []
    result = await run_scenario(case_id, provider_factory=lambda: provider)
    assert result["success"], result
    assert provider.contexts
    for context in provider.contexts:
        assert context["completion"]["pending"] > 0
        assert context["completion"]["total"] == len(CASES[case_id].checks("http://example.test/"))
        assert set(context["completion"]) == {"total", "pending"}
        assert "checks" not in context and "expected_events" not in context
    if case_id == "below_fold":
        assert [context["scroll"] for context in provider.contexts] == [
            {"up": False, "down": True}, {"up": True, "down": True}, {"up": True, "down": False},
        ]
    else:
        statuses = [row["fill_state"] for context in provider.contexts
                    for row in context["candidates"].get("fill", {}).values()]
        assert "differs" in statuses and "matches" in statuses
