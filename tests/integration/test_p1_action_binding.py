"""P1 regressions on owned Chromium pages; scripted decisions and test consent only."""

import json

import pytest
from test_bootstrap_origin import admit
from test_dom_actions import page as page  # noqa: PLC0414 - pytest fixture re-export
from test_dom_actions import pick
from test_runner import RecordingDriver, finish, request

from decision_browser.admission import AdmissionStore
from decision_browser.decision_codec import Decision
from decision_browser.desk import DevelopmentDesk
from decision_browser.dom import DOMSession
from decision_browser.runner import RunManager


@pytest.mark.parametrize("mutation", ["redirect", "reroute", "safe"])
async def test_enter_rechecks_recipient_after_focus(page, mutation):
    handler = {
        "redirect": "document.getElementById('danger').focus()",
        "reroute": "document.getElementById('safe').action='/delete'",
        "safe": "window.focusEffects++",
    }[mutation]
    await page.set_content(f'''<form id="safe" action="https://example.test/read">
      <input aria-label="Query" onfocus="{handler}"><button>Search</button></form>
      <form id="bad" action="https://example.test/delete" method="post">
      <input id="danger"><button>Delete account</button></form>
      <script>window.effects=[];window.focusEffects=0;
      document.addEventListener('submit',e=>{{e.preventDefault();effects.push(e.target.id)}},true)</script>''')
    dom = DOMSession(page)
    attempts = []
    try:
        obs = await dom.observe()
        action = await dom.prepare("enter", pick(obs, "enter", "Query"))
        if mutation == "safe":
            result = await dom.dispatch(action, attempts.append)
            assert result.status == "pass"
            assert await page.evaluate("effects") == ["safe"]
            assert await page.evaluate("focusEffects") == 1
        else:
            with pytest.raises(RuntimeError, match="recipient"):
                await dom.dispatch(action, attempts.append)
            assert await page.evaluate("effects") == []
        assert attempts == [action]
    finally:
        await dom.close()


@pytest.mark.parametrize("mutation", ["replace", "rename", "safe"])
async def test_click_rechecks_recipient_after_hover(page, mutation):
    handler = {
        "replace": "this.remove()",
        "rename": "this.textContent='Delete account'",
        "safe": "window.hoverEffects++",
    }[mutation]
    await page.set_content(f'''<button id="danger" style="position:absolute;left:10px;top:10px;width:180px;height:40px">Delete account</button>
      <button id="safe" style="position:absolute;left:10px;top:10px;width:180px;height:40px" onmouseover="{handler}">Review</button>
      <script>window.effects=[];window.hoverEffects=0;
      safe.onclick=()=>effects.push('safe');danger.onclick=()=>effects.push('danger')</script>''')
    dom = DOMSession(page)
    attempts = []
    try:
        obs = await dom.observe()
        action = await dom.prepare("click", pick(obs, "click", "Review"))
        if mutation == "safe":
            result = await dom.dispatch(action, attempts.append)
            assert result.status == "pass"
            assert await page.evaluate("effects") == ["safe"]
            assert await page.evaluate("hoverEffects") == 1
        else:
            with pytest.raises(RuntimeError, match="recipient"):
                await dom.dispatch(action, attempts.append)
            assert await page.evaluate("effects") == []
        assert attempts == [action]
    finally:
        await dom.close()


@pytest.mark.parametrize("attribute", ["href", "xlink:href", "alternate:href"])
async def test_nested_svg_link_cannot_receive_wrapper_click(page, attribute):
    await page.set_content(f'''<div role="button" aria-label="Review"
      style="position:absolute;left:20px;top:20px;width:180px;height:60px">
      <svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
      xmlns:alternate="http://www.w3.org/1999/xlink" width="180" height="60">
      <a {attribute}="https://example.test/purchase"><rect width="180" height="60" fill="green"/></a></svg></div>
      <script>window.effects=[];document.querySelector('a').onclick=e=>{{e.preventDefault();effects.push('purchase')}}</script>''')
    if attribute == "alternate:href":
        # HTML parsing only assigns the standard xlink prefix a namespace.
        await page.locator("a").evaluate("el => el.setAttributeNS('http://www.w3.org/1999/xlink','alternate:href','https://example.test/purchase')")
    dom = DOMSession(page)
    attempts = []
    try:
        obs = await dom.observe()
        action = await dom.prepare("click", pick(obs, "click", "Review"))
        result = await dom.dispatch(action, attempts.append)
        assert result.status == "blocked"
        assert result.reason == "covered"
        assert attempts == []
        assert await page.evaluate("effects") == []
    finally:
        await dom.close()


async def test_link_navigation_binds_source_and_checks_freshness(page):
    await page.route("**/*", lambda route: route.fulfill(
        body='<a href="/finish">Details</a><a href="/finish">Purchase</a>'
    ))
    await page.goto("https://example.test/")
    dom = DOMSession(page)
    try:
        obs = await dom.observe()
        safe = pick(obs, "click", "Details")
        danger = pick(obs, "click", "Purchase")
        action = await dom.prepare("navigate", safe, destination="https://example.test/finish")
        other = await dom.prepare("navigate", danger, destination=action.destination)
        assert action.token != other.token
        assert action.fingerprint != other.fingerprint
        assert json.loads(action.fingerprint)["semantic"]["node"]["name"] == "Details"
        assert (await dom.guard(action)).status == "pass"
        await page.locator("a").first.evaluate("el => el.textContent='Purchase'")
        assert (await dom.guard(action)).status == "stale"
        await page.locator("a").first.evaluate("el => el.remove()")
        assert (await dom.guard(action)).status == "stale"
    finally:
        await dom.close()


@pytest.mark.parametrize("attribute", ["href", "xlink:href"])
async def test_direct_svg_link_preserves_destination_and_safe_click(page, attribute):
    await page.set_content(f'''<svg xmlns="http://www.w3.org/2000/svg"
      xmlns:xlink="http://www.w3.org/1999/xlink" width="180" height="60">
      <a aria-label="Details" {attribute}="https://example.test/details">
      <rect width="180" height="60" fill="green"/></a></svg>
      <script>window.effects=[];document.querySelector('a').onclick=e=>{{e.preventDefault();effects.push('details')}}</script>''')
    dom = DOMSession(page)
    attempts = []
    try:
        obs = await dom.observe()
        token = pick(obs, "click", "Details")
        semantic = obs.candidates[token].semantic_by_operation["click"]
        assert semantic["node"]["href"] == "https://example.test/details"
        assert semantic["node"]["role"] == "link"
        action = await dom.prepare("click", token)
        assert (await dom.dispatch(action, attempts.append)).status == "pass"
        assert attempts == [action]
        assert await page.evaluate("effects") == ["details"]
    finally:
        await dom.close()


class LinkProvider:
    def __init__(self, operation, name):
        self.operation, self.name = operation, name
        self.calls = 0

    async def decide(self, questions, input_text, *, budget, privacy, target_heads, **kwargs):
        budget.reserve_provider(len(input_text.encode()))
        budget.consume_output(1)
        self.calls += 1
        if self.calls > 1:
            return Decision("DONE", None, 1.0)
        choices = json.loads(input_text)["candidates"][self.operation]
        target = next(key for key, row in choices.items() if row["name"] == self.name)
        return Decision(self.operation, target, 1.0)

    async def aclose(self):
        pass


class LinkDriver(RecordingDriver):
    def __init__(self, mode, first_name):
        super().__init__(mode)
        self.hits = []
        self.names = [first_name, *(name for name in ("Details", "Purchase", "Delete account") if name != first_name)]

    async def start(self):
        await super().start()

        async def route_owned(route):
            self.hits.append(route.request.url)
            await route.fulfill(content_type="text/html", body='<p id="count">0</p>' + ''.join(
                f'<a href="/finish">{name}</a>' for name in self.names
            ))

        await self.page.context.route("**/*", route_owned)


@pytest.mark.parametrize("operation", ["click", "navigate"])
@pytest.mark.parametrize("name", ["Purchase", "Delete account", "Details"])
async def test_public_runner_preserves_link_consequences(operation, name):
    provider = LinkProvider(operation, name)
    drivers = []
    store = AdmissionStore()
    manager = RunManager(
        lambda: provider,
        lambda mode: drivers.append(LinkDriver(mode, name)) or drivers[-1],
        admission_store=store, desk=DevelopmentDesk(store, ["accept"]),
    )
    run_id = manager.start_task(request("https://example.test/"))
    await admit(manager, run_id)
    result = await finish(manager, run_id)
    if name == "Details":
        assert result.terminal == "completed_unverified"
        assert drivers[0].hits == ["https://example.test/", "https://example.test/finish"]
        assert result.usage.steps == provider.calls == 2
    else:
        assert result.terminal == "blocked"
        assert result.reason == "consequential_action_blocked"
        assert drivers[0].hits == ["https://example.test/"]
        assert result.usage.steps == provider.calls == 1


class RecipientDriver(RecordingDriver):
    def __init__(self, mode, operation):
        super().__init__(mode)
        self.operation = operation
        self.effects = None

    async def start(self):
        await super().start()
        html = '''<form action="https://example.test/read"><input aria-label="Query"
          onfocus="window.preEffects++;document.getElementById('danger').focus()">
          <button>Search</button></form><form action="https://example.test/delete" method="post">
          <input id="danger"><button>Delete account</button></form>''' if self.operation == "enter" else '''
          <button id="danger" style="position:absolute;left:10px;top:10px;width:180px;height:40px">Delete account</button>
          <button id="safe" style="position:absolute;left:10px;top:10px;width:180px;height:40px"
          onmouseover="window.preEffects++;this.remove()">Review</button>'''
        html += '''<script>window.preEffects=0;window.effects=[];
          document.addEventListener('submit',e=>{e.preventDefault();effects.push('submit')},true);
          danger.onclick=()=>effects.push('danger')</script>'''
        await self.page.context.route("**/*", lambda route: route.fulfill(
            content_type="text/html", body=html,
        ))

    async def close(self):
        self.effects = await self.page.evaluate("[window.preEffects, window.effects]")
        await super().close()


@pytest.mark.parametrize("operation,name", [("enter", "Query"), ("click", "Review")])
async def test_public_runner_stops_partial_recipient_attempt_without_retry(operation, name):
    provider = LinkProvider(operation, name)
    drivers = []
    store = AdmissionStore()
    manager = RunManager(
        lambda: provider,
        lambda mode: drivers.append(RecipientDriver(mode, operation)) or drivers[-1],
        admission_store=store, desk=DevelopmentDesk(store, ["accept"]),
    )
    run_id = manager.start_task(request("https://example.test/"))
    await admit(manager, run_id)
    result = await finish(manager, run_id)
    assert result.terminal == "outcome_unknown"
    assert result.usage.steps == 2
    assert provider.calls == 1
    assert drivers[0].effects == [1, []]
    assert manager.get_task(run_id).terminal == "outcome_unknown"
