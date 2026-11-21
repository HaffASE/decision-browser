"""Independent-review regressions: stage uncertainty survives root death/I/O failure."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("review_installer", ROOT / "scripts/install.py")
assert spec and spec.loader
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
runtime_spec = importlib.util.spec_from_file_location("review_runtime", ROOT / "scripts/prepare_tool_runtime.py")
assert runtime_spec and runtime_spec.loader
runtime = importlib.util.module_from_spec(runtime_spec)
runtime_spec.loader.exec_module(runtime)


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer SIGKILL/process-group ownership proof")
def test_root_crash_during_owned_stage_refuses_new_installer(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    marker = tmp_path / "stage.json"
    stage = (
        "import os,time,json;from pathlib import Path;"
        f"Path({str(marker)!r}).write_text(json.dumps({{'pid':os.getpid(),'pgid':os.getpgrp()}}));"
        "time.sleep(20)"
    )
    outer = f'''
import importlib.util,sys
from pathlib import Path
s=importlib.util.spec_from_file_location('i',{str(ROOT / 'scripts/install.py')!r})
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
m.shutil.which=lambda name:'/bin/'+name
m.check_uv=lambda _:None
def prepare(source,project,runtime,uv):
 runtime.mkdir(parents=True)
 m.run({[sys.executable, '-I', '-c', stage]!r}, {{}}, 'crash-fixture', project)
m.prepare=prepare
m.install(Path({str(ROOT)!r}),Path({str(project)!r}),['codex'])
'''
    foreign = subprocess.Popen([sys.executable, "-I", "-c", "import time;time.sleep(20)"], start_new_session=True)
    child = subprocess.Popen([sys.executable, "-I", "-c", outer], env={},
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    stage_pid = None
    try:
        deadline = time.monotonic() + 3
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert marker.exists(), child.communicate(timeout=1)[1].decode()
        identity = json.loads(marker.read_text())
        stage_pid = identity["pid"]
        assert identity["pgid"] == stage_pid and os.getpgid(stage_pid) == stage_pid
        child.kill()
        child.wait(timeout=2)
        os.kill(stage_pid, 0)
        assert foreign.poll() is None
        monkeypatch.setattr(installer.shutil, "which", lambda name: "/bin/" + name)
        monkeypatch.setattr(installer, "check_uv", lambda _: None)
        monkeypatch.setattr(installer, "prepare", lambda *args: pytest.fail("second preparation started"))
        with pytest.raises(installer.InstallError, match="stage_cleanup_unknown"):
            installer.install(ROOT, project, ["codex"])
        assert not (project / ".codex/config.toml").exists()
        assert foreign.poll() is None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)
        if stage_pid is not None:
            try:
                if os.getpgid(stage_pid) == stage_pid:
                    os.killpg(stage_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        foreign.terminate()
        foreign.wait(timeout=2)
        child.communicate(timeout=2)


@pytest.mark.parametrize("mode", ["write_error", "short_write", "fsync", "readback"])
def test_active_intent_persistence_failure_aborts_before_process_start(tmp_path, monkeypatch, mode):
    with installer.held_lock(tmp_path, "lease"):
        if mode == "write_error":
            monkeypatch.setattr(installer.os, "write", lambda *args: (_ for _ in ()).throw(OSError("fixture-EIO")))
        elif mode == "short_write":
            write = installer.os.write
            monkeypatch.setattr(installer.os, "write", lambda fd, data: write(fd, data[:-1]))
        elif mode == "fsync":
            monkeypatch.setattr(installer.os, "fsync", lambda *args: (_ for _ in ()).throw(OSError("fixture-EIO")))
        else:
            reads = []

            def mismatch(*args):
                reads.append(True)
                return installer._LEASE if len(reads) == 1 else b"mismatch"

            monkeypatch.setattr(installer.os, "read", mismatch)
        monkeypatch.setattr(installer.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("launched before durable intent"))
        with pytest.raises(installer.InstallError, match="stage_lifecycle_unavailable"):
            installer.run(["not-started"], {}, "fixture", tmp_path)


def test_successful_stage_intent_is_visible_then_cleared_after_quiescence(tmp_path, monkeypatch):
    command = [sys.executable, "-I", "-c", "pass"]
    popen = installer.subprocess.Popen
    states = []

    def launch(argv, **kwargs):
        if argv == command:
            # Observe the actual held descriptor. Windows byte locks are
            # mandatory, so another process cannot read its locked first byte.
            states.append(installer._lease_state(tmp_path))
        return popen(argv, **kwargs)

    monkeypatch.setattr(installer.subprocess, "Popen", launch)
    with installer.held_lock(tmp_path, "lease"):
        installer.run(command, {}, "fixture", tmp_path)
        assert states == [installer._ACTIVE_INTENT]
        assert (tmp_path / "lease").read_bytes() == installer._LEASE
    with installer.held_lock(tmp_path, "lease") as validate:
        validate()


def test_timeout_retains_barrier_even_when_original_group_is_gone(tmp_path, monkeypatch, capsys):
    class Child:
        pid = 12345
        stdout = io.BytesIO()

        def wait(self, **kwargs):
            raise subprocess.TimeoutExpired("fixture", .01)

    monkeypatch.setattr(installer.subprocess, "Popen", lambda *args, **kwargs: Child())
    monkeypatch.setattr(installer, "_stop_stage", lambda *args, **kwargs: None)
    with installer.held_lock(tmp_path, "lease"), pytest.raises(installer.InstallError, match="stage_timeout"):
        installer.run(["fixture"], {}, "fixture", tmp_path)
    assert json.loads((tmp_path / installer._RESIDUAL).read_text())["reason"] == "stage_timeout_uncertainty"
    with pytest.raises(installer.InstallError, match="stage_cleanup_unknown"), installer.held_lock(tmp_path, "lease"):
        pytest.fail("a vanished original group cleared detached-topology uncertainty")
    assert "retry the same command" not in capsys.readouterr().err.lower()


def test_poison_write_failure_keeps_active_intent_and_fixed_unknown_error(tmp_path, monkeypatch):
    write = installer.os.write

    def fail_poison(fd, data):
        if data == installer._POISON:
            raise OSError("fixture-EIO")
        return write(fd, data)

    with installer.held_lock(tmp_path, "lease"):
        monkeypatch.setattr(installer.os, "write", fail_poison)
        monkeypatch.setattr(installer, "_stop_stage", lambda *args, **kwargs: (_ for _ in ()).throw(installer.InstallError("unknown")))
        with pytest.raises(installer.InstallError, match="stage_cleanup_unknown"):
            installer.run([sys.executable, "-I", "-c", "pass"], {}, "fixture", tmp_path)
    assert b"active" in (tmp_path / "lease").read_bytes()
    with pytest.raises(installer.InstallError, match="stage_cleanup_unknown"), installer.held_lock(tmp_path, "lease"):
        pytest.fail("failed post-hoc poison write erased active uncertainty")


def test_tool_runtime_survives_residual_json_failure_using_lease_authority(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    source = tmp_path / "source"
    source.mkdir()
    write = installer.atomic_write
    attempts = []

    def fail_json(path, data):
        if path.name == installer._RESIDUAL:
            raise OSError("fixture-full")
        return write(path, data)

    def prepare(source, project, attempt, uv, **kwargs):
        attempts.append(attempt)
        (attempt / "owned-evidence").write_bytes(b"retain-until-quiescent")
        installer.run([sys.executable, "-I", "-c", "pass"], {}, "fixture", cache)

    module = SimpleNamespace(source_id=lambda _: "a" * 20, atomic_write=fail_json,
                             held_lock=installer.held_lock, InstallError=installer.InstallError,
                             ensure_no_residual=installer.ensure_no_residual,
                             stage_uncertain=lambda root: installer.stage_uncertain(root),
                             check_uv=lambda _: None, prepare_package=prepare)
    monkeypatch.setattr(runtime, "_installer", lambda _: module)
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/fixture/uv")
    monkeypatch.setattr(installer, "atomic_write", fail_json)
    monkeypatch.setattr(installer, "_stop_stage", lambda *args, **kwargs: (_ for _ in ()).throw(installer.InstallError("unknown")))
    with pytest.raises(installer.InstallError, match="stage_cleanup_unknown"):
        runtime.prepare_runtime(source, cache)
    assert len(attempts) == 1
    assert (attempts[0] / "owned-evidence").read_bytes() == b"retain-until-quiescent"
    assert not (cache / installer._RESIDUAL).exists()
    assert not list((cache / "runtimes").glob("*/active.json"))
    with pytest.raises(installer.InstallError, match="stage_cleanup_unknown"), installer.held_lock(cache, ".runtimes.lease"):
        pytest.fail("uncertain runtime state was deleted and reused")


def test_tool_runtime_active_refusal_has_truthful_fixed_code(tmp_path, monkeypatch, capsys):
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    with installer.held_lock(cache, ".runtimes.lease"):
        installer._set_lease_state(cache, installer._ACTIVE_INTENT)
    monkeypatch.setattr(runtime, "_installer", lambda _: installer)
    with pytest.raises(runtime.RuntimeErrorCode, match="runtime_cleanup_unknown"):
        runtime.prepare_runtime(tmp_path, cache)
    assert runtime.main(["--prepare-runtime", "--cache", str(cache)]) == 1
    output = capsys.readouterr().err
    assert "runtime_cleanup_unknown" in output and "Inspect them before another preparation" in output


@pytest.mark.parametrize("failure", [KeyboardInterrupt, OSError])
def test_failed_spawn_attempt_does_not_infer_no_child_from_missing_handle(tmp_path, monkeypatch, failure):
    def interrupted(*args, **kwargs):
        raise failure()

    monkeypatch.setattr(installer.subprocess, "Popen", interrupted)
    with installer.held_lock(tmp_path, "lease"), pytest.raises(installer.InstallError, match="stage_cleanup_unknown"):
        installer.run(["fixture"], {}, "fixture", tmp_path)
    assert json.loads((tmp_path / installer._RESIDUAL).read_text())["pid"] is None
    with pytest.raises(installer.InstallError, match="stage_cleanup_unknown"), installer.held_lock(tmp_path, "lease"):
        pytest.fail("missing process handle was treated as proof that no stage started")
