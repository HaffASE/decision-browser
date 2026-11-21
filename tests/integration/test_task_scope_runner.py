"""Owned native page, production task-scope policy, scripted provider; no human proof."""

import pytest
from test_bootstrap_origin import OwnedRouteDriver
from test_runner import ScriptedProvider, finish, request

from decision_browser.contracts import Check
from decision_browser.runner import RunManager


@pytest.mark.parametrize("check", [True, False])
async def test_native_task_scope_runs_without_presenting_admission(check):
    url = "https://example.test/"
    provider = ScriptedProvider(["DONE"])
    manager = RunManager(
        lambda: provider, lambda mode: OwnedRouteDriver(mode, url),
        admission_mode="task_scope",
    )
    checks = [Check(kind="text", target="#ready", expected="Owned bootstrap ready")] if check else []
    run_id = manager.start_task(request(url, checks))
    result = await finish(manager, run_id)
    assert result.terminal == ("verified" if check else "completed_unverified")
    assert result.admission_class == "ordinary_task_scoped"
    assert result.development_only is False
    assert result.usage.steps == 1
    assert result.usage.calls == provider.calls == (0 if check else 1)
    assert manager.pending_proposal(run_id) is None
    assert not manager._admission.task_scope_allows(run_id, "navigate", url, url)
