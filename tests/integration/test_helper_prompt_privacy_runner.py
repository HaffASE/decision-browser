"""Public free-fill journey: native owned page and mock text HTTP, no live provider."""
from __future__ import annotations

import json

import httpx
import pytest
from test_runner import finish

from decision_browser.browser import BrowserDriver
from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.decision_codec import Decision
from decision_browser.provider import OpenAIProvider
from decision_browser.runner import RunManager

_URL = "https://example.test/"
_CANARIES = ('canary"quoted', 'canary\\slash', 'canary\nline', 'canary\tcontrol', 'canary\x01control')


class _OwnedFillDriver(BrowserDriver):
    def __init__(self, mode):
        super().__init__(mode)
        self.final_value = None
        self.input_events = None

    async def start(self):
        await super().start()

        async def owned(route):
            if route.request.url != _URL:
                await route.abort("blockedbyclient")
                return
            await route.fulfill(status=200, content_type="text/html", body='''
                <!doctype html><label>Name <input aria-label="Name" id="name"></label>
                <script>window.inputEvents=0;
                document.querySelector('input').addEventListener('input',()=>window.inputEvents++);
                </script>''')

        await self.page.context.route("**/*", owned)

    async def close(self):
        try:
            self.final_value = await self.page.locator("#name").input_value()
            self.input_events = await self.page.evaluate("window.inputEvents")
        finally:
            await super().close()


class _FillThenDone:
    def __init__(self, helper):
        self.helper = helper
        self.decisions = 0
        self.helper_calls = 0

    async def decide(self, questions, input_text, *, target_heads, **kwargs):
        self.decisions += 1
        if self.decisions > 1:
            return Decision("DONE", None, 1.0)
        head = target_heads["fill"]
        return Decision("fill", next(q for q in questions if q.name == head).choices[0], 1.0)

    async def generate_text(self, prompt, **kwargs):
        self.helper_calls += 1
        return await self.helper.generate_text(prompt, **kwargs)

    async def aclose(self):
        await self.helper.aclose()


@pytest.mark.parametrize("secret", [*_CANARIES, None])
async def test_public_helper_privacy_blocks_before_native_fill(secret):
    outgoing = []

    def handler(request):
        outgoing.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "completed", "output": [{
            "type": "message", "content": [{"type": "output_text", "text": '{"text":"Ada"}'}],
        }]})

    helper = OpenAIProvider("synthetic-key", transport=httpx.MockTransport(handler))
    provider = _FillThenDone(helper)
    drivers = []
    manager = RunManager(
        lambda: provider, lambda mode: drivers.append(_OwnedFillDriver(mode)) or drivers[-1],
        admission_mode="task_scope",
    )
    request = TaskRequest(
        goal="Enter the name " + (secret or 'Ada "ordinary" \\ text\nline'),
        start_url=_URL, values={}, checks=[], limits=Limits(max_seconds=15),
        privacy=PrivacyPolicy(blocked_literals=[secret or "absent-secret"]),
    )
    result = await finish(manager, manager.start_task(request))
    if secret is not None:
        assert (result.terminal, result.reason) == ("blocked", "privacy_blocked")
        assert not outgoing and provider.helper_calls == result.usage.calls == 0
        assert result.usage.steps == 1
        assert (drivers[0].final_value, drivers[0].input_events) == ("", 0)
    else:
        assert result.terminal == "completed_unverified"
        assert provider.helper_calls == result.usage.calls == len(outgoing) == 1
        assert result.usage.steps == 2
        assert (drivers[0].final_value, drivers[0].input_events) == ("Ada", 1)
        prompt = json.loads(outgoing[0]["input"][0]["content"][0]["text"])
        assert prompt["goal"] == request.goal
    assert result.admission_class == "ordinary_task_scoped"
    assert result.development_only is False
