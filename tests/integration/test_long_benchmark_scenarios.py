"""Offline positive paths plus controls against guessing a receipt or saving the wrong data."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest import TestCase

from playwright.sync_api import sync_playwright

_path = Path(__file__).resolve().parents[2] / "scripts/benchmarks/long_scenarios.py"
_spec = importlib.util.spec_from_file_location("long_benchmark_scenarios_under_test", _path)
assert _spec is not None and _spec.loader is not None
_suite = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _suite
_spec.loader.exec_module(_suite)


class LongWorkflowTest(TestCase):
    def test_paths_and_oracle(self):
        counts = {"rail_itinerary": 10, "library_research": 12, "equipment_pickup": 14,
                  "support_handoff": 16, "exhibit_planner": 18}
        self.assertEqual(set(_suite.CASES), set(counts))
        with _suite.ScenarioSite() as site, sync_playwright() as playwright:
            browser = playwright.chromium.launch(chromium_sandbox=True, channel="chromium")
            try:
                for case_id, case in _suite.CASES.items():
                    with self.subTest(case_id=case_id):
                        page = browser.new_page(viewport={"width": 1280, "height": 720})
                        start = site.start(case_id)
                        page.goto(start + "summary")
                        self.assertEqual(page.url, start)
                        self.assertFalse(site.verify(case_id)["success"])

                        for wrong_values in (False, True):
                            site.reset(case_id)
                            page.goto(start)
                            actions = 0
                            for section in case.sections:
                                for field in section.fields:
                                    control = page.get_by_label(field.label, exact=True)
                                    if field.checkbox:
                                        if not (wrong_values and case_id == "support_handoff"):
                                            control.check()
                                    elif field.options:
                                        control.select_option(label=field.expected)
                                    else:
                                        value = "Wrong value" if wrong_values and case_id != "support_handoff" else field.expected
                                        control.fill(value)
                                    actions += 1
                                role = "link" if section.alternatives else "button"
                                control = page.get_by_role(role, name=section.action, exact=True)
                                declared_url = control.evaluate("el => el.href || el.form.action")
                                control.click()
                                self.assertEqual(page.url, declared_url)
                                actions += 1

                            self.assertEqual(actions, counts[case_id])
                            self.assertEqual(actions, case.minimum_actions)
                            self.assertEqual(page.url, start + "summary")
                            self.assertEqual(page.locator("#status").inner_text(), f"{case.title} saved")
                            self.assertEqual(site.verify(case_id)["success"], not wrong_values)
                            check = case.checks(start)[1]
                            receipt = page.locator(check.target)
                            self.assertEqual(receipt.text_content() == check.expected, not wrong_values)
                            self.assertEqual(receipt.inner_text() == check.expected, not wrong_values)
                        page.close()
            finally:
                browser.close()


async def test_runner_keeps_visible_timetable_after_opening_the_selected_train():
    from browser_scenarios import ScriptedScenarioProvider, Step, _ScenarioDriver

    from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
    from decision_browser.runner import RunManager

    case_id = "rail_itinerary"
    case = _suite.CASES[case_id]
    clue = "Express 09:20 is direct, platform 4."
    contexts = []

    class ObservingProvider(ScriptedScenarioProvider):
        async def decide(self, questions, input_text, **kwargs):
            contexts.append(json.loads(input_text))
            return await super().decide(questions, input_text, **kwargs)

    plan = []
    for section in case.sections:
        for field in section.fields:
            op = "click" if field.checkbox else "select" if field.options else "fill"
            plan.append(Step(op, field.label, field.expected if op == "select" else None))
        plan.append(Step("click", section.action))
    provider = ObservingProvider(plan)
    with _suite.ScenarioSite() as site:
        url = site.start(case_id)
        manager = RunManager(lambda: provider, lambda _: _ScenarioDriver("isolated"), admission_mode="task_scope")
        try:
            run_id = manager.start_task(TaskRequest(
                goal=case.goal, start_url=url, values=case.values, checks=case.checks(url),
                mode="isolated", visual=False, privacy=PrivacyPolicy(),
                limits=Limits(max_seconds=60, max_steps=25, max_provider_calls=25),
            ))
            await manager._run.task
            assert manager.get_task(run_id).terminal == "verified"
            assert site.verify(case_id)["success"]
        finally:
            await manager.aclose()
    assert "previous_pages" not in contexts[0]
    assert any(clue in state["text"] for state in contexts)
    platform = next(state for state in contexts if "Station route" in state["text"])
    assert clue not in platform["text"]
    assert any(clue in text for text in platform["previous_pages"])
