"""Task-scope manager policy: no form, no human proof, no scope expansion."""

import asyncio
import json

import pytest

from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.dom import Action, Observation
from decision_browser.runner import RunManager, RunnerError
from decision_browser.server import create_server


class NoFormDesk:
    development_only = False

    async def present(self, _proposal):
        raise AssertionError("Task-scope policy must not present a form")


class GateDriver:
    cleanup_errors = ()
    residual_owned = False
    foreign_mismatch = False

    def __init__(self, _mode):
        self.started = asyncio.Event()

    async def start(self):
        self.started.set()
        await asyncio.Future()

    async def close(self):
        pass


class NoProviderCalls:
    async def decide(self, *_args, **_kwargs):
        raise AssertionError("Provider must not be called while driver is starting")

    async def aclose(self):
        pass


def request(mode="isolated"):
    return TaskRequest(
        goal="Read the test page", start_url="https://example.test/", mode=mode,
        values={}, checks=[], limits=Limits(max_seconds=10), privacy=PrivacyPolicy(),
    )


def host():
    provider = NoProviderCalls()
    driver = GateDriver("isolated")
    manager = RunManager(
        lambda: provider, lambda _mode: driver, desk=NoFormDesk(),
        admission_mode="task_scope",
    )
    return manager, driver, provider


async def test_task_scope_starts_without_form_and_revokes_on_cancel():
    manager, driver, _provider = host()
    run_id = manager.start_task(request())
    try:
        assert manager.get_task(run_id).terminal == "queued"
        await asyncio.wait_for(driver.started.wait(), 2)
        result = manager.get_task(run_id)
        assert result.terminal == "running"
        assert result.admission_class == "ordinary_task_scoped"
        assert result.development_only is False
        assert result.usage.calls == result.usage.steps == 0
        assert manager.pending_proposal(run_id) is None
        assert manager._admission.task_scope_allows(
            run_id, "click", "https://example.test", "https://example.test"
        )
    finally:
        await manager.aclose()
    assert manager.get_task(run_id).terminal == "cancelled"
    assert not manager._admission.task_scope_allows(
        run_id, "click", "https://example.test", "https://example.test"
    )


def test_task_scope_rejects_existing_chrome_before_driver_factory():
    manager, _driver, _provider = host()
    manager._driver_factory = lambda _mode: pytest.fail("driver constructed")
    with pytest.raises(RunnerError, match="task_scope_isolated_only"):
        manager._open(request("existing_chrome"))


async def test_failed_task_scope_grant_never_starts_or_reports_admitted(monkeypatch):
    manager, driver, _provider = host()
    monkeypatch.setattr(manager._admission, "admit_task_scope", lambda *_args: False)
    run_id = manager.start_task(request())
    await manager._run.task
    result = manager.get_task(run_id)
    assert result.terminal == "blocked"
    assert result.admission_class == "unresolved"
    assert not driver.started.is_set()
    assert result.usage.steps == result.usage.calls == 0
    await manager.aclose()


def open_scoped_run():
    manager, _driver, _provider = host()
    run = manager._open(request())
    proposal = manager._admission.propose_alpha(
        run.result.run_id, [run.request.start_url], ["navigate", "click"],
        run.request.limits.model_dump(), run.budget.original_deadline,
    )
    assert manager._admission.admit_task_scope(proposal.proposal_id, proposal.digest)
    run.alpha = proposal
    return manager, run


@pytest.mark.parametrize("origin", ["https://other.test", "https://example.test:8443", "null"])
async def test_foreign_observation_cannot_reach_verification_or_provider(origin):
    manager, run = open_scoped_run()
    manager._verify = lambda *_args, **_kw: pytest.fail("verification exposed foreign page")
    manager._choose = lambda *_args: pytest.fail("provider exposed foreign page")
    obs = Observation("obs", origin, "doc", "sensitive foreign page", {}, ())
    await manager._loop(run, obs)
    assert run.result.reason == "task_scope_origin_mismatch"
    assert run.result.usage.calls == run.result.usage.steps == 0


async def test_task_scope_does_not_propose_beta_or_show_form():
    manager, run = open_scoped_run()
    semantic = {"url": "https://example.test/", "node": {
        "name": "Details", "href": "https://other.test/"}, "form": None}
    action = Action("click", "link", None, None, "obs", json.dumps({"semantic": semantic}))
    obs = Observation("obs", "https://example.test", "doc", "", {}, ())
    with pytest.raises(RunnerError, match="blocked"):
        await manager._admit_scope(run, manager._scope(run, obs, action))
    assert run.result.reason == "task_scope_expansion_required"
    assert run.pending is None
    assert len(manager._admission._proposals) == 1


async def test_capabilities_distinguish_task_policy_from_human_channel():
    server = create_server(admission_mode="task_scope")
    tool = server._tool_manager._tools["capabilities"].fn
    reply = await tool()
    assert reply.is_error is False
    body = reply.structured_content
    assert isinstance(body, dict)
    assert body["admission_mode"] == "task_scope"
    assert body["human_grant_channel"] is None
    assert body["native_human"] == "NOT_RUN"
    assert body["form_candidate_unverified"] is False
    assert body["modes"] == ["isolated"]


@pytest.mark.parametrize("mode", ["task_scope", "standard_form"])
def test_keychain_entry_forwards_task_policy_without_exposing_key(monkeypatch, mode):
    import os
    import subprocess
    import sys

    from decision_browser import keychain_entry

    monkeypatch.setattr(sys, "platform", "darwin")
    key = "placeholder-task-mode-key"

    def lookup(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, stdout=key.encode(), stderr=b"")

    def replace(executable, argv, env):
        assert executable == sys.executable
        assert argv == [sys.executable, "-I", "-m", "decision_browser.cli", "serve",
                        "--admission", mode]
        assert key not in " ".join(argv)
        assert env["OPENAI_API_KEY"] == key
        raise SystemExit(0)

    monkeypatch.setattr(subprocess, "run", lookup)
    monkeypatch.setattr(os, "execve", replace)
    with pytest.raises(SystemExit) as done:
        keychain_entry.main(["--admission", mode])
    assert done.value.code == 0


def test_cli_serve_passes_backend_admission_policy(monkeypatch, tmp_path):
    from decision_browser import cli, server

    monkeypatch.chdir(tmp_path)

    class StubServer:
        def run(self, transport):
            assert transport == "stdio"

    modes = []

    def create(**kwargs):
        modes.append(kwargs["admission_mode"])
        return StubServer()

    monkeypatch.setattr(server, "create_server", create)
    assert cli.main(["serve", "--admission", "task_scope"]) == 0
    assert cli.main(["serve"]) == 0
    assert modes == ["task_scope", "standard_form"]


@pytest.mark.parametrize("command_matches,cwd_matches,expected", [
    (True, True, "task_scope"),
    (False, True, "standard_form"),
    (True, False, "standard_form"),
])
@pytest.mark.parametrize("prefix", [[], ["-I"]])
def test_cached_launcher_resolves_only_matching_project_policy(
    monkeypatch, tmp_path, command_matches, cwd_matches, expected, prefix
):
    import sys

    from decision_browser import cli

    monkeypatch.chdir(tmp_path)
    folder = tmp_path / ".codex"
    folder.mkdir()
    command = sys.executable if command_matches else str(tmp_path / "other-python")
    cwd = str(tmp_path if cwd_matches else tmp_path / "other-project")
    config = (
        '[mcp_servers.decision_browser]\n'
        f'command = {json.dumps(command)}\n'
        f'cwd = {json.dumps(cwd)}\n'
        f'args = {json.dumps([*prefix, "-m", "decision_browser.keychain_entry", "--admission", "task_scope"])}\n'
    )
    (folder / "config.toml").write_text(config)
    assert cli._project_admission_mode() == expected


@pytest.mark.parametrize("tail", [
    ["--admission", "unknown"],
    ["--admission", "task_scope", "--unknown"],
    ["--admission", "task_scope", "--admission", "task_scope"],
    ["--admission"],
    ["--unknown"],
    ["--admission", ["task_scope"]],
])
@pytest.mark.parametrize("prefix", [[], ["-I"]])
def test_invalid_saved_policy_fails_before_server_start(monkeypatch, tmp_path, tail, prefix):
    import sys

    from decision_browser import cli, server

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex/config.toml").write_text(
        '[mcp_servers.decision_browser]\n'
        f'command = {json.dumps(sys.executable)}\n'
        f'cwd = {json.dumps(str(tmp_path))}\n'
        f'args = {json.dumps([*prefix, "-m", "decision_browser.keychain_entry", *tail])}\n'
    )
    monkeypatch.setattr(server, "create_server", lambda **_kwargs: pytest.fail("server started"))
    assert cli.main(["serve"]) == 1


def test_explicit_standard_form_overrides_saved_task_scope(monkeypatch, tmp_path):
    from decision_browser import cli

    monkeypatch.setattr(cli, "_project_admission_mode", lambda: pytest.fail("saved policy read"))
    modes = []
    monkeypatch.setattr(cli, "_serve", lambda manager, mode: modes.append(mode) or 0)
    assert cli.main(["serve", "--admission", "standard_form"]) == 0
    assert modes == ["standard_form"]
