"""Recovery clears only verified stale barriers and preserves project contents."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ATTEMPT = "b" * 32


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def recovery(monkeypatch, tmp_path):
    module = load_module("recovery_test_subject", ROOT / "npm/bin/install-recovery.py")
    local_home = tmp_path / "trusted home"
    local_home.mkdir(mode=0o700)
    monkeypatch.setattr(module, "_anchor_home", lambda: local_home)
    monkeypatch.setattr(module.evidence, "boot_identity", lambda: {"host": "a" * 64, "boot": "b" * 64})
    monkeypatch.setattr(module.evidence, "check_unused", lambda project: None)
    monkeypatch.setattr(module.evidence, "_local_filesystem", lambda project: None)
    return module


def write_private(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)


@pytest.fixture
def project(tmp_path):
    project = tmp_path / "project with spaces"
    project.mkdir(mode=0o700)
    write_private(project / ".decision-browser-install.lease", b'{"blocked":"stage-cleanup-unknown"}\n')
    write_private(project / ".decision-browser-stage-cleanup-unknown.json", (json.dumps({
        "schema": 1, "owner": "decision-browser-installer-stage-v1", "attempt": ATTEMPT,
        "pid": 987654321, "new_session": True, "reason": "stage_failed_uncertainty",
    }) + "\n").encode())
    write_private(project / ".decision-browser-install/owner.json",
                  b'{"owner":"decision-browser-installer-v1"}\n')
    write_private(project / ".decision-browser-install/runtimes/" / ("a" * 20 + "-" + "c" * 32) / "retained",
                  b"retained runtime bytes")
    write_private(project / ".codex/config.toml", b'model="preserved-model"\n')
    write_private(project / ".mcp.json", b'{"mcpServers":{},"custom":"preserved-custom"}\n')
    return project


def files(project):
    return {str(path.relative_to(project)): path.read_bytes() for path in project.rglob("*")
            if path.is_file() and not path.is_symlink()}


def invoke(recovery, project, capsys, command="install-status", apply=False):
    args = [command, "--project", str(project), "--json"]
    if apply:
        args.append("--apply")
    code = recovery.main(args)
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert captured.err == ""
    assert set(report) >= {"schema", "status", "reason", "recovery", "changed"}
    assert report["schema"] == 1
    return code, report


@pytest.mark.parametrize("command", ["install-status", "recover-install"])
def test_diagnostics_are_read_only_for_unstarted_project(recovery, tmp_path, capsys, command):
    project = tmp_path / "empty"
    project.mkdir()
    before = files(project)
    code, report = invoke(recovery, project, capsys, command)
    assert code == 0 and report["status"] == "clear" and report["changed"] is False
    assert files(project) == before
    assert not list(project.iterdir())
    assert not list(recovery._anchor_home().iterdir())


@pytest.mark.parametrize("command", ["install-status", "recover-install"])
def test_blocked_status_and_dry_run_preserve_all_bytes(recovery, project, capsys, command):
    before = files(project)
    code, report = invoke(recovery, project, capsys, command)
    assert code == 1 and report["status"] == "blocked"
    assert report["recovery"] == "record_reboot_witness"
    assert report["changed"] is False
    assert files(project) == before
    assert not list(recovery._anchor_home().iterdir())


def test_explicit_apply_records_witness_and_same_boot_keeps_block(recovery, project, capsys):
    before = files(project)
    code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
    assert code == 1 and report["status"] == "blocked"
    assert report["recovery"] == "reboot_required" and report["changed"] is True
    after = files(project)
    assert all(after[name] == data for name, data in before.items())
    added = set(after) - set(before)
    assert added == {".decision-browser-recovery." + ATTEMPT + ".json"}
    assert (project / next(iter(added))).stat().st_mode & 0o077 == 0
    for command, apply in [("install-status", False), ("recover-install", True)]:
        code, report = invoke(recovery, project, capsys, command, apply)
        assert code == 1 and report["recovery"] == "reboot_required"
        assert report["changed"] is False
        assert files(project) == after


def test_verified_boot_change_recovers_with_existing_lease_inode(recovery, project, capsys, monkeypatch):
    before = files(project)
    lease = project / ".decision-browser-install.lease"
    inode = lease.stat().st_ino
    invoke(recovery, project, capsys, "recover-install", apply=True)
    monkeypatch.setattr(recovery.evidence, "boot_identity", lambda: {"host": "a" * 64, "boot": "c" * 64})
    code, report = invoke(recovery, project, capsys)
    assert code == 1 and report["recovery"] == "ready" and report["changed"] is False
    code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
    assert code == 0 and report["status"] == "recovered" and report["changed"] is True
    assert report["recovery"] == "complete"
    assert lease.stat().st_ino == inode
    assert lease.read_bytes() == b'{"owner":"decision-browser-lease-v1"}\n'
    assert not (project / ".decision-browser-stage-cleanup-unknown.json").exists()
    archived = project / (".decision-browser-recovered." + ATTEMPT + ".json")
    assert archived.read_bytes() == before[".decision-browser-stage-cleanup-unknown.json"]
    for name, data in before.items():
        if name not in {".decision-browser-install.lease", ".decision-browser-stage-cleanup-unknown.json"}:
            assert (project / name).read_bytes() == data
    installer = load_module("recovery_original_installer", ROOT / "scripts/install.py")
    with installer.held_lock(project, ".decision-browser-install.lease"):
        installer.ensure_no_residual(project)


@pytest.mark.skipif(os.name == "nt", reason="POSIX kernel lease")
def test_active_installer_lease_refuses_recovery(recovery, project, capsys):
    import fcntl

    lease = project / ".decision-browser-install.lease"
    before = files(project)
    with lease.open("rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
        assert code == 1 and report["status"] == "blocked" and report["changed"] is False
        assert files(project) == before


@pytest.mark.parametrize("relative,data", [
    (".decision-browser-install.lease", b'{"active":"stage-lifecycle-v1"}\n'),
    (".decision-browser-install.lease", b"unknown lease\n"),
    (".decision-browser-stage-cleanup-unknown.json", b"invalid private-canary"),
    (".decision-browser-stage-cleanup-unknown.json", b'{"schema":1,"pid":1}\n'),
    (".decision-browser-install/owner.json", b"unknown owner"),
    (".decision-browser-install/install.lock", b"legacy lock"),
])
def test_uncertain_or_foreign_state_stays_blocked(recovery, project, capsys, relative, data):
    write_private(project / relative, data)
    before = files(project)
    code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
    assert code == 1 and report["status"] == "blocked" and report["changed"] is False
    assert report["recovery"] == "unavailable"
    assert files(project) == before
    assert "private-canary" not in json.dumps(report)


@pytest.mark.parametrize("missing", [".decision-browser-install.lease", ".decision-browser-stage-cleanup-unknown.json"])
def test_missing_evidence_never_creates_lease_or_authorizes_recovery(recovery, project, capsys, missing):
    (project / missing).unlink()
    before = files(project)
    code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
    assert code == 1 and report["status"] == "blocked" and report["changed"] is False
    assert report["recovery"] == "unavailable"
    assert files(project) == before


@pytest.mark.parametrize("link", ["symlink", "hardlink"])
def test_alias_lease_is_rejected_without_changing_target(recovery, project, tmp_path, capsys, link):
    lease = project / ".decision-browser-install.lease"
    outside = tmp_path / "outside"
    outside.write_bytes(lease.read_bytes())
    outside.chmod(0o600)
    lease.unlink()
    if link == "symlink":
        lease.symlink_to(outside)
    else:
        os.link(outside, lease)
    before = outside.read_bytes()
    code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
    assert code == 1 and report["changed"] is False and report["recovery"] == "unavailable"
    assert outside.read_bytes() == before


def test_foreign_host_and_changed_retained_files_refuse_recovery(recovery, project, capsys, monkeypatch):
    invoke(recovery, project, capsys, "recover-install", apply=True)
    before = files(project)
    monkeypatch.setattr(recovery.evidence, "boot_identity", lambda: {"host": "d" * 64, "boot": "c" * 64})
    code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
    assert code == 1 and report["changed"] is False
    assert files(project) == before
    monkeypatch.setattr(recovery.evidence, "boot_identity", lambda: {"host": "a" * 64, "boot": "c" * 64})
    write_private(project / ".codex/config.toml", b'# changed by another writer\n')
    changed = files(project)
    code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
    assert code == 1 and report["changed"] is False and files(project) == changed


@pytest.mark.parametrize("function", ["boot_identity", "snapshot", "check_unused"])
def test_missing_proof_or_in_use_resources_leave_barrier(recovery, project, capsys, monkeypatch, function):
    def unavailable(*args):
        raise recovery.evidence.EvidenceError("inspection_unavailable")

    monkeypatch.setattr(recovery.evidence, function, unavailable)
    before = files(project)
    code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
    assert code == 1 and report["changed"] is False
    assert report["recovery"] == "unavailable" and files(project) == before


def test_post_reboot_use_check_prevents_unlock(recovery, project, capsys, monkeypatch):
    invoke(recovery, project, capsys, "recover-install", apply=True)
    monkeypatch.setattr(recovery.evidence, "boot_identity", lambda: {"host": "a" * 64, "boot": "c" * 64})

    def in_use(project):
        raise recovery.evidence.EvidenceError("resources_in_use")

    monkeypatch.setattr(recovery.evidence, "check_unused", in_use)
    before = files(project)
    code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
    assert code == 1 and report["changed"] is False and files(project) == before


def test_witness_write_failure_never_unblocks_or_discloses_exception(recovery, project, capsys, monkeypatch):
    lease = project / ".decision-browser-install.lease"
    old = lease.read_bytes()

    write = recovery.os.write

    def failed_write(fd, data):
        if len(data) == 32:
            return write(fd, data)
        raise OSError("https://private-user:private-key@private-host.invalid/?secret=private-query")

    monkeypatch.setattr(recovery.os, "write", failed_write)
    code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
    assert code == 1 and report["status"] == "blocked"
    assert lease.read_bytes() == old
    assert (project / ".decision-browser-stage-cleanup-unknown.json").is_file()
    assert "private-" not in json.dumps(report)
    code, report = invoke(recovery, project, capsys)
    assert code == 1 and report["recovery"] == "unavailable"


def test_changed_snapshot_during_apply_is_not_accepted(recovery, project, capsys, monkeypatch):
    invoke(recovery, project, capsys, "recover-install", apply=True)
    monkeypatch.setattr(recovery.evidence, "boot_identity", lambda: {"host": "a" * 64, "boot": "c" * 64})
    snapshot = recovery.evidence.snapshot
    calls = []

    def concurrent_change(project):
        result = snapshot(project)
        calls.append(True)
        if len(calls) > 1:
            result[".codex/config.toml"] = "concurrent-change"
        return result

    monkeypatch.setattr(recovery.evidence, "snapshot", concurrent_change)
    before = files(project)
    code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
    assert code == 1 and report["status"] == "blocked"
    assert files(project) == before


def test_symlink_project_is_not_followed(recovery, project, tmp_path, capsys):
    alias = tmp_path / "project alias"
    alias.symlink_to(project, target_is_directory=True)
    before = files(project)
    code, report = invoke(recovery, alias, capsys, "recover-install", apply=True)
    assert code == 1 and report["changed"] is False
    assert report["recovery"] == "unavailable" and files(project) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX special file")
def test_fifo_residual_is_rejected_without_blocking_read(recovery, project, capsys):
    residual = project / ".decision-browser-stage-cleanup-unknown.json"
    residual.unlink()
    os.mkfifo(residual, mode=0o600)
    before = (project / ".decision-browser-install.lease").read_bytes()
    code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
    assert code == 1 and report["changed"] is False and report["recovery"] == "unavailable"
    assert (project / ".decision-browser-install.lease").read_bytes() == before


@pytest.mark.parametrize("relative", [
    ".decision-browser-install/manifest.json",
    ".decision-browser-stage.lease",
])
def test_registration_or_extra_lifecycle_state_remains_blocked(recovery, project, capsys, relative):
    write_private(project / relative, b"{}\n")
    before = files(project)
    code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
    assert code == 1 and report["changed"] is False and report["recovery"] == "unavailable"
    assert files(project) == before


@pytest.mark.parametrize("relative,body", [
    (".codex/config.toml", b'[mcp_servers.decision_browser]\ncommand="retained"\n'),
    (".mcp.json", b'{"mcpServers":{"decision_browser":{"command":"retained"}}}\n'),
    (".codex/config.toml", b'[mcp_servers.other]\ncommand="wrapper"\n'),
    (".mcp.json", b'{"mcpServers":{"other":{"command":"wrapper"}}}\n'),
    (".codex/config.toml", b"invalid-toml-with-private-key"),
    (".mcp.json", b"invalid-json-with-private-key"),
])
def test_existing_or_unreadable_registration_refuses_recovery(recovery, project, capsys, relative, body):
    write_private(project / relative, body)
    before = files(project)
    code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
    assert code == 1 and report["changed"] is False and report["recovery"] == "unavailable"
    assert files(project) == before
    assert "private-key" not in json.dumps(report)


@pytest.mark.parametrize("failure", ["short_write", "fsync"])
def test_interrupted_final_write_restores_blocked_lease(recovery, project, capsys, monkeypatch, failure):
    invoke(recovery, project, capsys, "recover-install", apply=True)
    monkeypatch.setattr(recovery.evidence, "boot_identity", lambda: {"host": "a" * 64, "boot": "c" * 64})
    write, fsync = recovery.os.write, recovery.os.fsync
    lease_inode = (project / ".decision-browser-install.lease").stat().st_ino
    attempts = []

    def interrupted_write(fd, data):
        if data == recovery.LEASE and os.fstat(fd).st_ino == lease_inode:
            attempts.append(True)
            if failure == "short_write":
                return write(fd, data[:5])
        return write(fd, data)

    def interrupted_fsync(fd):
        if failure == "fsync" and attempts and os.fstat(fd).st_ino == lease_inode:
            raise OSError("private-sync-failure")
        return fsync(fd)

    monkeypatch.setattr(recovery.os, "write", interrupted_write)
    monkeypatch.setattr(recovery.os, "fsync", interrupted_fsync)
    code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
    assert code == 1 and report["status"] == "blocked"
    assert attempts
    assert (project / ".decision-browser-install.lease").read_bytes() == recovery.POISON
    assert (project / (".decision-browser-recovered." + ATTEMPT + ".json")).is_file()
    assert "private-sync" not in json.dumps(report)
    monkeypatch.setattr(recovery.os, "write", write)
    monkeypatch.setattr(recovery.os, "fsync", fsync)
    code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
    assert code == 1 and report["status"] == "blocked"
    assert (project / ".decision-browser-install.lease").read_bytes() == recovery.POISON


def test_altered_project_witness_cannot_fabricate_boot_change(recovery, project, capsys):
    invoke(recovery, project, capsys, "recover-install", apply=True)
    witness = project / (".decision-browser-recovery." + ATTEMPT + ".json")
    data = json.loads(witness.read_text())
    data["boot"] = "c" * 64
    witness.write_text(json.dumps(data))
    before = files(project)
    code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
    assert code == 1 and report["status"] == "blocked" and report["changed"] is False
    assert report["recovery"] == "unavailable" and files(project) == before


def test_missing_trusted_key_is_not_recreated_for_existing_witness(recovery, project, capsys):
    invoke(recovery, project, capsys, "recover-install", apply=True)
    key = recovery._anchor_home() / ".local/state/decision-browser/recovery/key"
    key.unlink()
    before = files(project)
    for command, apply in [("install-status", False), ("recover-install", True)]:
        code, report = invoke(recovery, project, capsys, command, apply)
        assert code == 1 and report["changed"] is False
        assert report["recovery"] == "unavailable" and files(project) == before
        assert not key.exists()


def test_swapped_lease_before_commit_keeps_replacement_blocked(recovery, project, capsys, monkeypatch):
    invoke(recovery, project, capsys, "recover-install", apply=True)
    monkeypatch.setattr(recovery.evidence, "boot_identity", lambda: {"host": "a" * 64, "boot": "c" * 64})
    snapshot = recovery.evidence.snapshot
    calls = []
    lease = project / ".decision-browser-install.lease"

    def replace_after_scan(project):
        result = snapshot(project)
        calls.append(True)
        if len(calls) == 2:
            replacement = project / "replacement-lease"
            write_private(replacement, recovery.POISON)
            replacement.replace(lease)
        return result

    monkeypatch.setattr(recovery.evidence, "snapshot", replace_after_scan)
    code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
    assert code == 1 and report["status"] == "blocked" and report["changed"] is False
    assert lease.read_bytes() == recovery.POISON
    assert (project / ".decision-browser-stage-cleanup-unknown.json").is_file()
    assert not (project / (".decision-browser-recovered." + ATTEMPT + ".json")).exists()


def test_archived_transaction_resumes_after_interrupted_residual_retirement(recovery, project, capsys, monkeypatch):
    invoke(recovery, project, capsys, "recover-install", apply=True)
    monkeypatch.setattr(recovery.evidence, "boot_identity", lambda: {"host": "a" * 64, "boot": "c" * 64})
    original_unlink = recovery.os.unlink

    def interrupted_unlink(path, **kwargs):
        original_unlink(path, **kwargs)
        if path == recovery.RESIDUAL:
            raise OSError("interrupted after retirement")

    monkeypatch.setattr(recovery.os, "unlink", interrupted_unlink)
    code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
    assert code == 1 and report["status"] == "blocked"
    assert (project / ".decision-browser-install.lease").read_bytes() == recovery.POISON
    assert not (project / recovery.RESIDUAL).exists()
    assert (project / (".decision-browser-recovered." + ATTEMPT + ".json")).exists()
    monkeypatch.setattr(recovery.os, "unlink", original_unlink)
    code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
    assert code == 0 and report["status"] == "recovered"


def test_idle_lease_with_legacy_lock_remains_blocked(recovery, project, capsys):
    write_private(project / ".decision-browser-install.lease", recovery.LEASE)
    (project / recovery.RESIDUAL).unlink()
    write_private(project / ".decision-browser-install/install.lock", b"legacy-lock")
    before = files(project)
    code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
    assert code == 1 and report["status"] == "blocked" and report["changed"] is False
    assert files(project) == before


@pytest.mark.parametrize("arguments", [
    ["install-status", "--apply"],
    ["recover-install", "--force"],
    ["https://user:private-key@private-host.invalid/?token=private-query"],
    ["recover-install", "--project"],
])
def test_invalid_arguments_return_redacted_json(recovery, project, capsys, arguments):
    before = files(project)
    assert recovery.main([*arguments, "--json"]) == 2
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert captured.err == "" and report["reason"] == "invalid_arguments"
    assert report["changed"] is False and files(project) == before
    assert "private-" not in captured.out


@pytest.mark.parametrize("kind", ["witness", "archive"])
@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_aliased_recovery_evidence_is_not_used(recovery, project, capsys, monkeypatch, tmp_path, kind, alias):
    invoke(recovery, project, capsys, "recover-install", apply=True)
    witness = project / (".decision-browser-recovery." + ATTEMPT + ".json")
    target = witness if kind == "witness" else project / (".decision-browser-recovered." + ATTEMPT + ".json")
    outside = tmp_path / "foreign-evidence"
    original = witness.read_bytes() if kind == "witness" else (project / recovery.RESIDUAL).read_bytes()
    write_private(outside, original)
    if target.exists():
        target.unlink()
    if alias == "symlink":
        target.symlink_to(outside)
    else:
        os.link(outside, target)
    monkeypatch.setattr(recovery.evidence, "boot_identity", lambda: {"host": "a" * 64, "boot": "c" * 64})
    code, report = invoke(recovery, project, capsys, "recover-install", apply=True)
    assert code == 1 and report["status"] == "blocked"
    assert outside.read_bytes() == original
    assert (project / ".decision-browser-install.lease").read_bytes() == recovery.POISON
    assert (project / recovery.RESIDUAL).exists()
