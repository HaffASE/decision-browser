"""Evidence must refuse ambiguity and keep diagnostic reads side-effect free."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("recovery_evidence", ROOT / "npm/bin/install_recovery_evidence.py")
assert spec and spec.loader
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir(mode=0o700)
    managed = root / evidence.STATE
    managed.mkdir(mode=0o700)
    (managed / "owner.json").write_bytes(b'{"owner":"decision-browser-installer-v1"}\n')
    (managed / "runtimes").mkdir()
    (managed / "runtimes/wheel.whl").write_bytes(b"retained wheel")
    monkeypatch.setattr(evidence, "_local_filesystem", lambda _: None)
    return root


def test_snapshot_is_stable_and_redacts_registration_contents(project):
    folder = project / ".codex"
    folder.mkdir()
    (folder / "config.toml").write_text('model = "private-canary"\n')
    before = {str(p): p.read_bytes() for p in project.rglob("*") if p.is_file()}
    first = evidence.snapshot(project)
    assert first == evidence.snapshot(project)
    assert "private-canary" not in json.dumps(first)
    assert str(project) not in json.dumps(first)
    assert before == {str(p): p.read_bytes() for p in project.rglob("*") if p.is_file()}
    (project / evidence.STATE / "runtimes/wheel.whl").write_bytes(b"changed wheel")
    assert evidence.snapshot(project) != first


@pytest.mark.parametrize("relative,contents", [
    (".mcp.json", '{"mcpServers":{"decision\\u005f browser":{}}}'),
    (".mcp.json", '{"mcpServers":{"other":{"command":"wrapper"}}}'),
    (".codex/config.toml", '[mcp_servers.other]\ncommand="wrapper"\n'),
    (".mcp.json", '{"value":"\\u002edecision-browser-install/runtime"}'),
    (evidence.STATE + "/manifest.json", '{}'),
])
def test_registrations_and_indirect_wrappers_preserve_block(project, relative, contents):
    path = project / relative
    path.parent.mkdir(exist_ok=True)
    path.write_text(contents)
    with pytest.raises(evidence.EvidenceError, match="runtime_referenced"):
        evidence.snapshot(project)


def test_duplicate_json_is_ambiguous(project):
    (project / ".mcp.json").write_text('{"mcpServers":{"old":{}},"mcpServers":{}}')
    with pytest.raises(evidence.EvidenceError, match="registration_unreadable"):
        evidence.snapshot(project)


def test_remote_http_registration_can_be_fingerprinted_without_execution(project):
    path = project / ".mcp.json"
    path.write_text('{"mcpServers":{"remote":{"type":"http","url":"https://remote.invalid/mcp",'
                    '"http_headers":{"Authorization":"canary-secret"}}}}')
    first = evidence.snapshot(project)
    assert "canary-secret" not in json.dumps(first)
    assert "remote.invalid" not in json.dumps(first)
    path.write_text(path.read_text().replace("canary-secret", "other-secret"))
    assert evidence.snapshot(project) != first


def test_encoded_runtime_reference_in_remote_registration_is_ambiguous(project):
    (project / ".mcp.json").write_text('{"mcpServers":{"remote":{"type":"http",'
                                    '"url":"https://remote.invalid/%2edecision-browser-install"}}}')
    with pytest.raises(evidence.EvidenceError, match="runtime_referenced"):
        evidence.snapshot(project)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_unsafe_or_aliased_retained_files_refuse(project, kind):
    target = project / evidence.STATE / "runtimes/unsafe"
    if kind == "symlink":
        target.symlink_to(project / evidence.STATE / "owner.json")
    elif kind == "hardlink":
        os.link(project / evidence.STATE / "owner.json", target)
    else:
        os.mkfifo(target)
    with pytest.raises(evidence.EvidenceError):
        evidence.snapshot(project)


def test_owner_fifo_is_refused_without_blocking(project):
    marker = project / evidence.STATE / "owner.json"
    marker.unlink()
    os.mkfifo(marker)
    with pytest.raises(evidence.EvidenceError, match="unsafe_retained_state"):
        evidence.snapshot(project)


def test_private_parent_allows_uv_advisory_lock_and_python_link(project):
    runtime = project / evidence.STATE / "runtimes/attempt/venv/bin"
    runtime.mkdir(parents=True)
    interpreter = project / "trusted-python"
    interpreter.write_bytes(b"fixture executable")
    interpreter.chmod(0o700)
    (runtime / "python").symlink_to(interpreter)
    advisory = runtime.parent / ".lock"
    advisory.touch()
    advisory.chmod(0o666)
    evidence.snapshot(project)


def test_unknown_platform_cannot_authorize_recovery(monkeypatch):
    monkeypatch.setattr(evidence.sys, "platform", "unsupported")
    with pytest.raises(evidence.EvidenceError, match="platform_inspection_unavailable"):
        evidence.boot_identity()


def test_mac_boot_identity_requires_valid_distinct_sources(monkeypatch):
    monkeypatch.setattr(evidence.sys, "platform", "darwin")
    outputs = iter([b"01234567-89ab-cdef-0123-456789abcdef\n",
                    b'"IOPlatformUUID" = "abcdef01-2345-6789-abcd-ef0123456789"'])
    monkeypatch.setattr(evidence, "_run", lambda _: next(outputs))
    record = evidence.boot_identity()
    assert set(record) == {"host", "boot"}
    assert all(len(value) == 64 for value in record.values())
    monkeypatch.setattr(evidence, "_run", lambda _: b"private-canary")
    with pytest.raises(evidence.EvidenceError, match="boot_identity_unavailable"):
        evidence.boot_identity()


def test_remote_mac_mount_is_not_host_bound(project, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(evidence.sys, "platform", "darwin")
    monkeypatch.setattr(evidence, "_run", lambda _: b"server:/share on / (nfs, nodev)\n")
    with pytest.raises(evidence.EvidenceError, match="filesystem_unverified"):
        evidence._local_filesystem(project)


def test_nested_mount_with_same_device_is_ambiguous(project, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(evidence.sys, "platform", "darwin")
    mounts = f"/dev/disk1 on / (apfs, local)\n/dev/disk1 on {project / evidence.STATE} (apfs, local)\n"
    monkeypatch.setattr(evidence, "_run", lambda _: mounts.encode())
    with pytest.raises(evidence.EvidenceError, match="filesystem_unverified"):
        evidence._local_filesystem(project)


def test_command_references_are_scoped_to_project(project):
    assert evidence._mentioned(b"python .decision-browser-install/runtimes/venv/bin/server", project, project)
    assert evidence._mentioned(os.fsencode(project / evidence.STATE / "file"), project, Path("/"))
    assert not evidence._mentioned(b"python .decision-browser-install/other/server", project, project.parent / "other")
    relative = "../project/.decision-browser-install/runtimes/server"
    assert evidence._mentioned(relative.encode(), project, project.parent / "other")


def test_darwin_open_inode_alias_refuses_even_with_different_name(project, monkeypatch):
    process = os.getpid() + 100
    info = (project / evidence.STATE / "runtimes/wheel.whl").stat()
    listing = (f"p{process}\0u{os.getuid()}\0\nfcwd\0D0x1\0i2\0n/\0\n"
               f"f3\0D0x{info.st_dev:x}\0i{info.st_ino}\0n/aliased-name\0\n").encode()
    monkeypatch.setattr(evidence, "_run", lambda args:
                        listing if "lsof" in args[0] else f"{os.getuid()} {process}\n".encode())
    monkeypatch.setattr(evidence, "_processes", lambda: {process})
    with pytest.raises(evidence.EvidenceError, match="retained_resources_in_use"):
        evidence._darwin_unused(project, {(info.st_dev, info.st_ino)})


def test_darwin_invisible_own_process_refuses(project, monkeypatch):
    process = os.getpid() + 100
    monkeypatch.setattr(evidence, "_processes", lambda: {process})
    monkeypatch.setattr(evidence, "_run", lambda args:
                        b"" if "lsof" in args[0] else f"{os.getuid()} {process}\n".encode())
    with pytest.raises(evidence.EvidenceError, match="inspection_incomplete"):
        evidence._darwin_unused(project, set())


def test_new_unobserved_process_during_scan_refuses(project, monkeypatch):
    process = os.getpid() + 100
    extra = process + 1
    samples = iter([{process}, {process, extra}])
    monkeypatch.setattr(evidence, "_processes", lambda: next(samples))
    listing = f"p{process}\0u{os.getuid()}\0\nfcwd\0D0x1\0i2\0n/\0\n".encode()
    monkeypatch.setattr(evidence, "_run", lambda _: listing)
    with pytest.raises(evidence.EvidenceError, match="inspection_incomplete"):
        evidence._darwin_unused(project, set())


def test_process_listing_accepts_negative_nobody_uid(monkeypatch):
    process = os.getpid() + 100
    inspector = process + 1
    monkeypatch.setattr(evidence, "_capture", lambda _:
                        (f"-2 89\n{os.getuid()} {process}\n{os.getuid()} {inspector}\n".encode(), inspector))
    assert evidence._processes() == {process}


def test_resource_scan_does_not_hash_runtime_files_again(project, monkeypatch):
    monkeypatch.setattr(evidence.sys, "platform", "darwin")
    monkeypatch.setattr(evidence, "_file_digest", lambda *_: pytest.fail("resource scan rehashed runtime"))
    seen = []
    monkeypatch.setattr(evidence, "_darwin_unused", lambda root, identities: seen.append((root, identities)))
    evidence.check_unused(project)
    assert len(seen) == 1 and seen[0][0] == project
    assert seen[0][1]
