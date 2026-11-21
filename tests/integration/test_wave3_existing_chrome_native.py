"""F11 managed sandboxed Chromium/CDP proof; no personal Chrome profile."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from decision_browser.browser import BrowserBlocked, BrowserDriver


@pytest.mark.parametrize("failure", ["identity", "inventory", "popup"])
async def test_wave3_native_created_and_opener_targets_cleaned(tmp_path, monkeypatch, failure):
    async with async_playwright() as owner:
        profile = tmp_path / "managed-chromium-profile"
        context = await owner.chromium.launch_persistent_context(
            str(profile), channel="chromium", headless=True, chromium_sandbox=True,
            args=["--remote-debugging-port=0"],
        )
        driver = BrowserDriver("existing_chrome", devtools_active_port=profile / "DevToolsActivePort")
        try:
            foreign = context.pages[0]
            await foreign.set_content('<p id="sentinel">Foreign disposable fixture</p>')
            if failure == "identity":
                monkeypatch.setattr(driver, "_page_target_id", AsyncMock(side_effect=BrowserBlocked("ownership_unverified")))
            elif failure == "inventory":
                inventory = driver._cdp_inventory
                calls = 0

                async def fail_confirmation():
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise BrowserBlocked("inventory_failed")
                    return await inventory()

                monkeypatch.setattr(driver, "_cdp_inventory", fail_confirmation)
            if failure in {"identity", "inventory"}:
                with pytest.raises(BrowserBlocked):
                    await driver.start()
            else:
                await driver.start()
                attached_browser, attached_context = driver._browser, driver._context
                monkeypatch.setattr(attached_browser, "close", AsyncMock(side_effect=AssertionError("foreign browser close")))
                monkeypatch.setattr(attached_context, "close", AsyncMock(side_effect=AssertionError("foreign context close")))
                async with driver.page.expect_popup():
                    await driver.page.evaluate("window.open('about:blank')")
                # The independent owner can receive the popup after the CDP attachment.
                async with asyncio.timeout(5):
                    while len(context.pages) < 3:
                        await asyncio.sleep(0.01)
                assert len(context.pages) == 3
                await driver.close()
            async with asyncio.timeout(5):
                while len(context.pages) != 1:
                    await asyncio.sleep(0.01)
                assert context.pages == [foreign]
                # CDP may acknowledge the close request before the owned target
                # disappears. After the owner saw it gone, reconcile pending cleanup.
                if driver.cleanup_pending:
                    await driver.close()
            assert not foreign.is_closed()
            assert await foreign.locator("#sentinel").inner_text() == "Foreign disposable fixture"
            assert driver.residual_owned is False and not driver.cleanup_pending
            assert driver.foreign_mismatch is False
        finally:
            await driver.close()
            await context.close()


async def test_wave3_native_public_popup_remains_unknown_and_cleans_owned_children(
    tmp_path, counter_page_url,
):
    import json

    from test_bootstrap_origin import admit
    from test_runner import ScriptedProvider, finish, request

    from decision_browser.admission import AdmissionStore
    from decision_browser.desk import DevelopmentDesk
    from decision_browser.runner import RunManager

    async with async_playwright() as owner:
        profile = tmp_path / "managed-runner-profile"
        context = await owner.chromium.launch_persistent_context(
            str(profile), channel="chromium", headless=True, chromium_sandbox=True,
            args=["--remote-debugging-port=0"],
        )
        foreign = context.pages[0]
        await foreign.set_content('<p id="sentinel">Foreign runner fixture</p>')

        class OwnedDriver(BrowserDriver):
            async def start(self):
                await super().start()
                await self.page.route("**/*", lambda route: route.fulfill(
                    status=200, content_type="text/html",
                    body='<button id="inc" type="button">inc</button>'
                         '<script>document.querySelector("#inc").onclick = () => '
                         'window.open("about:blank")</script>',
                ))

        driver = OwnedDriver("existing_chrome", devtools_active_port=profile / "DevToolsActivePort")
        store = AdmissionStore()
        provider = ScriptedProvider(["click", "DONE"])
        host = RunManager(
            lambda: provider, lambda mode: driver, admission_store=store,
            desk=DevelopmentDesk(store, ["accept"]), cleanup_grace=5,
        )
        try:
            task = request(counter_page_url)
            task.mode = "existing_chrome"
            run_id = host.start_task(task)
            await admit(host, run_id)
            result = await finish(host, run_id)
            assert result.terminal == "outcome_unknown", (result.reason, result.evidence)
            assert result.usage.steps == 2 and result.usage.calls == provider.calls == 1
            events = [json.loads(host.read_artifact(run_id, key).content)
                      for key in result.artifact_ids
                      if host.read_artifact(run_id, key).media_type == "application/json"]
            assert any(event.get("event") == "before_dispatch" for event in events)
            assert any(event.get("status") == "outcome_unknown" for event in events)
            assert result.development_only is True  # scripted consent, not native human grant
            assert driver.residual_owned is False and not driver.cleanup_pending
            assert driver.foreign_mismatch is False
            assert context.pages == [foreign]
            assert await foreign.locator("#sentinel").inner_text() == "Foreign runner fixture"
        finally:
            await host.aclose()
            await driver.close()
            await context.close()
