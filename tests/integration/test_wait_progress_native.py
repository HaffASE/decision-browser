"""Selected waits settle before the next model request and keep uncertain waits executable."""
import pytest

from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.runner import RunManager


@pytest.mark.parametrize("transition", ["ready", "satisfied", "navigation", "late"])
async def test_selected_wait_settles_before_next_model_decision(transition):
    import asyncio
    import json

    from decision_browser.browser import BrowserDriver
    from decision_browser.contracts import Check
    from decision_browser.decision_codec import Decision

    class Driver(BrowserDriver):
        async def start(self):
            await super().start()
            body = '<p id="count">0</p><button onclick="count.textContent=3">Continue</button>'
            if transition == "late":
                body = '<p id="count">0</p><button>Cancel report</button><p>Generating report</p>\n<script>setTimeout(() => {const b=document.createElement(\'button\'); b.textContent=\'Open report\';\nb.onclick=()=>count.textContent=3; document.body.append(b)},3000)</script>'
            await self.page.context.route("**/*", lambda route: route.fulfill(
                status=200, content_type="text/html", body=body,
            ))

    driver = Driver("isolated")

    class Provider:
        calls = 0

        async def decide(self, questions, input_text, *, budget, target_heads, **kwargs):
            budget.reserve_provider(len(input_text.encode()))
            budget.consume_output(1)
            self.calls += 1
            if self.calls == 2 and transition in {"satisfied", "navigation"}:
                expression = ("count.textContent=3" if transition == "satisfied"
                              else "history.replaceState({},'', '/foreign')")
                await driver.page.evaluate("expression => setTimeout(() => eval(expression), 400)", expression)
            data = json.loads(input_text)
            late_target = next((key for key, value in data['candidates'].get('click', {}).items()
                                if value['name'] == 'Open report'), None)
            if self.calls <= 2 or transition == "late" and late_target is None:
                assert "wait" in target_heads
                return Decision("wait", "short", 1.)
            assert transition in {"ready", "late"}
            assert "wait" in target_heads
            target = late_target if transition == "late" else next(iter(data['candidates']['click']))
            return Decision("click", target, 1.)

        async def aclose(self):
            pass

    provider = Provider()
    host = RunManager(lambda: provider, lambda mode: driver, admission_mode="task_scope")
    try:
        rid = host.start_task(TaskRequest(
            goal="Continue until count is 3", start_url="http://example.test/",
            values={}, checks=[Check(kind="text", target="#count", expected="3")],
            limits=Limits(max_steps=8, max_provider_calls=6, max_seconds=20), privacy=PrivacyPolicy(),
        ))
        await asyncio.wait_for(host._run.task, 25)
        result = host.get_task(rid)
        if transition == "navigation":
            assert result.terminal == "outcome_unknown", result
            assert provider.calls == 2
        else:
            assert result.terminal == result.functional == "verified", result
            if transition == "late":
                assert provider.calls <= 6
            else:
                assert provider.calls == (2 if transition == "satisfied" else 3)
    finally:
        await host.aclose()
