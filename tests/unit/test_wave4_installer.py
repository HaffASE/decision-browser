"""Installer transaction regressions use disposable projects and owned children."""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("wave4_installer", ROOT / "scripts/install.py")
assert spec and spec.loader
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture
def project_fixture(tmp_path, monkeypatch):
    source, project = tmp_path / "source", tmp_path / "project"
    source.mkdir()
    project.mkdir()
    for name in ("pyproject.toml", "uv.lock", "scripts/build-constraints.txt", "scripts/install.py",
                 "README.md", "THIRD_PARTY_NOTICES.md", "src/decision_browser/cli.py",
                 "skills/decision-browser/SKILL.md"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(installer, "check_uv", lambda _: None)
    monkeypatch.setattr(installer, "prepare", lambda *args: None)
    return source, project


def test_state_planning_holds_bootstrap_lease(project_fixture, monkeypatch):
    source, project = project_fixture
    original = installer.plan

    def checked(*args):
        with pytest.raises(installer.InstallError, match="locked"), installer.held_lock(project, ".decision-browser-install.lease"):
            pytest.fail("parallel planning acquired the lease")
        return original(*args)

    monkeypatch.setattr(installer, "plan", checked)
    assert installer.install(source, project, ["codex"]) == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX kernel-lock crash proof")
def test_kernel_lease_releases_on_crash_without_unlink(tmp_path):
    marker = tmp_path / "ready"
    code = (
        "import importlib.util,time;from pathlib import Path;"
        f"s=importlib.util.spec_from_file_location('i',{str(ROOT / 'scripts/install.py')!r});"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        f"p=Path({str(tmp_path)!r});"
        "exec(\"with m.held_lock(p, 'lease'):\\n (p/'ready').write_text('ready')\\n time.sleep(10)\")"
    )
    child = subprocess.Popen([sys.executable, "-I", "-c", code], stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 2
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), child.communicate(timeout=1)[1].decode()
        lease = tmp_path / "lease"
        before = lease.stat()
        child.kill()
        child.wait(timeout=2)
        with installer.held_lock(tmp_path, "lease") as validate:
            validate()
        after = lease.stat()
        assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)


def test_replaced_lease_is_detected_and_foreign_file_retained(tmp_path):
    with installer.held_lock(tmp_path, "lease") as validate:
        (tmp_path / "lease").rename(tmp_path / "old")
        (tmp_path / "lease").write_text("foreign")
        with pytest.raises(installer.InstallError, match="changed"):
            validate()
    assert (tmp_path / "lease").read_text() == "foreign"
    assert (tmp_path / "old").exists()


@pytest.mark.parametrize("kind", ["symlink", "fifo", "hardlink", "public"])
@pytest.mark.skipif(os.name == "nt", reason="POSIX lease permissions and special files")
def test_unsafe_lease_is_not_opened(tmp_path, kind):
    path = tmp_path / "lease"
    if kind == "symlink":
        (tmp_path / "outside").write_text("foreign")
        path.symlink_to(tmp_path / "outside")
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "hardlink":
        (tmp_path / "outside").write_text("foreign")
        os.link(tmp_path / "outside", path)
    else:
        path.write_text("foreign")
        path.chmod(0o666)
    with pytest.raises(installer.InstallError), installer.held_lock(tmp_path, "lease"):
        pytest.fail("unsafe lease acquired")


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_missing_registration_and_skill_files_allow_repair_and_remove(project_fixture, client):
    source, project = project_fixture
    installer.install(source, project, [client])
    config = project / (".codex/config.toml" if client == "codex" else ".mcp.json")
    skill = project / (".agents" if client == "codex" else ".claude") / "skills/decision-browser/SKILL.md"
    config.unlink()
    skill.unlink()
    assert installer.install(source, project, [client]) == 0
    assert config.is_file() and skill.is_file()
    config.unlink()
    skill.unlink()
    assert installer.install(source, project, [client], uninstall=True) == 0
    assert not skill.exists()


@pytest.mark.parametrize("body", [b"[]", b'{"clients":[]}', b'{"schema":true,"clients":{}}',
                                       b'{"clients":{"unknown":{}}}', b'{"clients":{},"clients":{}}',
                                       b"\xff", b" " * (1024 * 1024 + 1)])
def test_invalid_manifest_has_bounded_fixed_diagnostic(project_fixture, body):
    source, project = project_fixture
    root = project / installer.STATE
    root.mkdir()
    (root / "owner.json").write_bytes(b'{"owner":"decision-browser-installer-v1"}\n')
    (root / "manifest.json").write_bytes(body)
    with pytest.raises(installer.InstallError, match="Invalid installation metadata"):
        installer.install(source, project, ["codex"], uninstall=True)
    assert (root / "manifest.json").read_bytes() == body


def test_legacy_non_bmp_values_are_valid_toml():
    config = {"command": "/tmp/\U0001f680/python", "args": ["\U0001f600"], "env": {"PATH": "\U0001f30d"}}
    block = installer.codex_block(config, legacy=True)
    assert tomllib.loads(block)["mcp_servers"][installer.SERVER] == config
    assert "\\ud83" not in block


@pytest.mark.skipif(os.name == "nt", reason="POSIX owned process group proof")
def test_stage_timeout_stops_owned_descendants_and_reports_fixed_hint(tmp_path, monkeypatch):
    marker = tmp_path / "descendant-effect"
    foreign = subprocess.Popen([sys.executable, "-I", "-c", "import time;time.sleep(5)"])
    script = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-I','-c',"
        + repr(f"import time;from pathlib import Path;time.sleep(.7);Path({str(marker)!r}).write_text('escaped')")
        + "]);time.sleep(2)"
    )
    monkeypatch.setattr(installer, "_STAGE_TIMEOUT", 0.15, raising=False)
    try:
        with pytest.raises(installer.InstallError, match="stage_timeout"):
            installer.run([sys.executable, "-I", "-c", script], {}, "fixture", tmp_path)
        time.sleep(0.8)
        assert not marker.exists()
        assert foreign.poll() is None
    finally:
        foreign.terminate()
        foreign.wait(timeout=2)


def test_stage_diagnostics_are_fixed_not_raw(tmp_path, capsys):
    script = "import sys;sys.stderr.write('private-marker https://user:secret@host Certificate verify failed');sys.exit(1)"
    with pytest.raises(installer.InstallError, match="tls_certificate"):
        installer.run([sys.executable, "-I", "-c", script], {}, "fixture", tmp_path)
    output = capsys.readouterr()
    assert "tls_certificate" in output.err
    assert all(value not in output.out + output.err for value in ["private-marker", "secret", "https://"])


def test_child_umask_is_restrictive_without_parent_change(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX umask")
    destination = tmp_path / "child-directory"
    previous = os.umask(0o002)
    try:
        installer.run([sys.executable, "-I", "-c", f"from pathlib import Path;Path({str(destination)!r}).mkdir()"], {}, "fixture", tmp_path)
        current = os.umask(0o002)
        assert current == 0o002
    finally:
        os.umask(previous)
    assert destination.stat().st_mode & 0o077 == 0


def test_atomic_write_rejects_symlink_and_preserves_target(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX symlink fixture")
    other = tmp_path / "outside"
    other.write_bytes(b"foreign")
    path = tmp_path / "file"
    path.symlink_to(other)
    with pytest.raises(installer.InstallError):
        installer.atomic_write(path, b"replacement")
    assert path.is_symlink() and other.read_bytes() == b"foreign"


def test_uncertain_stage_cleanup_retains_residual_and_blocks_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "_stop_stage", lambda *a, **k: (_ for _ in ()).throw(installer.InstallError("unknown")))
    with pytest.raises(installer.InstallError, match="stage_cleanup_unknown"):
        installer.run([sys.executable, "-I", "-c", "pass"], {}, "fixture", tmp_path)
    marker = tmp_path / ".decision-browser-stage-cleanup-unknown.json"
    before = marker.read_bytes()
    effect = tmp_path / "second-effect"
    with pytest.raises(installer.InstallError, match="stage_cleanup_unknown"):
        installer.run([sys.executable, "-I", "-c", f"from pathlib import Path;Path({str(effect)!r}).touch()"], {}, "fixture", tmp_path)
    assert marker.read_bytes() == before and not effect.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir_fd parent-swap proof")
def test_atomic_write_parent_swap_stays_on_held_directory(tmp_path, monkeypatch):
    parent, foreign = tmp_path / "parent", tmp_path / "foreign"
    parent.mkdir()
    foreign.mkdir()
    (foreign / "file").write_bytes(b"foreign")
    original = installer.os.replace

    def swap(source, destination, **kwargs):
        parent.rename(tmp_path / "retained")
        parent.symlink_to(foreign, target_is_directory=True)
        original(source, destination, **kwargs)

    monkeypatch.setattr(installer.os, "replace", swap)
    with pytest.raises(installer.InstallError, match="directory changed"):
        installer.atomic_write(parent / "file", b"owned")
    assert (foreign / "file").read_bytes() == b"foreign"
    assert (tmp_path / "retained/file").read_bytes() == b"owned"


@pytest.mark.skipif(os.name == "nt", reason="POSIX uv umask/native manifest proof")
def test_actual_uv_runtime_created_under_permissive_parent_umask_is_valid(tmp_path):
    import importlib.util

    runtime_spec = importlib.util.spec_from_file_location("wave4_runtime", ROOT / "scripts/prepare_tool_runtime.py")
    assert runtime_spec and runtime_spec.loader
    runtime = importlib.util.module_from_spec(runtime_spec)
    runtime_spec.loader.exec_module(runtime)
    uv = shutil.which("uv")
    assert uv is not None, "uv is a required verifier prerequisite"
    attempt = tmp_path / "attempt"
    attempt.mkdir(mode=0o700)
    previous = os.umask(0o002)
    try:
        installer.run([uv, "--no-config", "--no-python-downloads", "venv", "--python", sys.executable,
                       str(attempt / "venv")], {"UV_CACHE_DIR": str(tmp_path / "uv-cache")}, "fixture", tmp_path)
        site = next((attempt / "venv/lib").glob("python*/site-packages"))
        package = site / "decision_browser"
        (package / "skill/scripts").mkdir(mode=0o700, parents=True)
        for directory in [package, package / "skill", package / "skill/scripts"]:
            directory.chmod(0o700)
        for name in ["cli.py", "skill/scripts/doctor.py"]:
            p = package / name
            p.write_bytes(b"# fixture")
            p.chmod(0o600)
    finally:
        os.umask(previous)
    manifest = runtime._manifest(attempt)
    assert any(x.endswith("/site-packages/decision_browser/cli.py") for x in manifest)


def test_windows_timeout_taskkill_is_exact_but_never_clean_proof(monkeypatch):
    from types import SimpleNamespace

    calls = []
    child = SimpleNamespace(pid=12345, poll=lambda: 0, wait=lambda **kwargs: 0)
    monkeypatch.setattr(installer.subprocess, "run", lambda command, **kwargs: (
        calls.append(command) or subprocess.CompletedProcess(command, 0)))
    with pytest.raises(installer.InstallError, match="Windows timeout requires native tree inspection"):
        installer._stop_stage(child, timed_out=True, platform="win32")
    assert calls == [["taskkill", "/PID", "12345", "/T", "/F"]]


def test_uncertain_cleanup_keeps_lease_blocked_when_residual_file_write_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "_stop_stage", lambda *a, **k: (_ for _ in ()).throw(installer.InstallError("unknown")))
    monkeypatch.setattr(installer, "atomic_write", lambda *a: (_ for _ in ()).throw(OSError("fixture-full")))
    with installer.held_lock(tmp_path, "lease"), pytest.raises(installer.InstallError, match="residual write failed"):
        installer.run([sys.executable, "-I", "-c", "pass"], {}, "fixture", tmp_path)
    with pytest.raises(installer.InstallError, match="retained blocked lease"), installer.held_lock(tmp_path, "lease"):
        pytest.fail("unknown cleanup allowed another preparation")


@pytest.mark.skipif(os.name == "nt", reason="POSIX concurrent installer native fixture")
def test_concurrent_installer_refuses_before_mutable_plan(project_fixture, tmp_path, monkeypatch):
    source, project = project_fixture
    ready = tmp_path / "ready"
    code = f'''
import importlib.util, time
from pathlib import Path
s = importlib.util.spec_from_file_location('i', {str(ROOT / 'scripts/install.py')!r})
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)
m.shutil.which = lambda name: '/bin/' + name
m.check_uv = lambda _: None
def prepare(*args):
    Path({str(ready)!r}).write_text('ready')
    time.sleep(.4)
m.prepare = prepare
m.install(Path({str(source)!r}), Path({str(project)!r}), ['codex'])
'''
    child = subprocess.Popen([sys.executable, "-I", "-c", code], env={},
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 2
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), child.communicate(timeout=1)[1].decode()
        with monkeypatch.context() as scoped:
            scoped.setattr(installer, "plan", lambda *args: pytest.fail("stale plan executed outside the lease"))
            with pytest.raises(installer.InstallError, match="locked"):
                installer.install(source, project, ["claude"])
        assert child.wait(timeout=2) == 0
        assert installer.install(source, project, ["claude"]) == 0
        state = installer.installation_state(project / installer.STATE / "manifest.json", project)
        assert set(state["clients"]) == {"codex", "claude"}
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)
