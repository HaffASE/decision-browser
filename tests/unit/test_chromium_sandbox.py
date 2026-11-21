"""Owned launches use full Chromium headless mode and retain the sandbox."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from decision_browser.browser import BrowserBlocked, BrowserDriver

ROOT = Path(__file__).resolve().parents[2]


def _launches(path):
    tree = ast.parse(path.read_text())
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "launch"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "chromium"]


def test_all_owned_chromium_launches_explicitly_request_sandbox():
    sources = [ROOT / "src/decision_browser/browser.py", *sorted((ROOT / "tests/integration").glob("*.py"))]
    calls = [(path, call) for path in sources for call in _launches(path)]
    assert calls, "No Chromium launch sites inspected"
    for path, call in calls:
        kwargs = {keyword.arg: keyword.value for keyword in call.keywords}
        sandbox = kwargs.get("chromium_sandbox")
        assert isinstance(sandbox, ast.Constant) and sandbox.value is True, (
            str(path.relative_to(ROOT)), call.lineno, "sandbox must be explicitly enabled"
        )
        channel = kwargs.get("channel")
        assert isinstance(channel, ast.Constant) and channel.value == "chromium"
        arguments = ast.dump(call)
        assert "--no-sandbox" not in arguments
        assert "--disable-setuid-sandbox" not in arguments


async def test_isolated_driver_requests_sandbox_and_preserves_launch_failure():
    calls = []

    async def launch(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("launch failed")

    driver = BrowserDriver("isolated")
    driver._playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    with pytest.raises(BrowserBlocked, match="launch_failed"):
        await driver._start_isolated()
    assert calls == [{"chromium_sandbox": True, "channel": "chromium"}]
    assert driver._browser is None
    assert driver._context is None
    assert driver._page is None


async def test_isolated_driver_sandbox_launch_preserves_owned_page_setup(monkeypatch):
    from unittest.mock import AsyncMock

    page = object()
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context))
    launch = AsyncMock(return_value=browser)
    driver = BrowserDriver("isolated")
    driver._playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    monkeypatch.setattr(driver, "_page_target_id", AsyncMock(return_value="owned-page"))
    await driver._start_isolated()
    launch.assert_awaited_once_with(chromium_sandbox=True, channel="chromium")
    assert driver._browser is browser
    assert driver._context is context
    assert driver._page is page
    assert driver.owned_target_id == driver._owned_page_target_id == "owned-page"
    assert driver.residual_owned is False
