"""Trusted tool-runtime preparation, with no real package downloads or clients."""
from __future__ import annotations

import importlib.util
import json
import marshal
import os
import py_compile
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("tool_runtime", ROOT / "scripts/prepare_tool_runtime.py")
assert spec and spec.loader
room = importlib.util.module_from_spec(spec)
spec.loader.exec_module(room)
install_spec = importlib.util.spec_from_file_location("tool_installer", ROOT / "scripts/install.py")
assert install_spec and install_spec.loader
installer = importlib.util.module_from_spec(install_spec)
install_spec.loader.exec_module(installer)


def make_venv(runtime: Path) -> None:
    python = room._python(runtime)
    python.parent.mkdir(parents=True)
    python.write_text("fixture executable, never run")
    python.chmod(0o700)
    lock = runtime / "venv/.lock"
    lock.write_text("")
    lock.chmod(0o666)
    package = runtime / "venv/lib/python3.12/site-packages/decision_browser"
    package.mkdir(parents=True)
    (package / "cli.py").write_text("fixture CLI")
    doctor = package / "skill/scripts/doctor.py"
    doctor.parent.mkdir(parents=True)
    doctor.write_text("fixture doctor")


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    cache = tmp_path / "cache"
    calls = []

    def prepare(source, project, runtime, uv, **kwargs):
        calls.append((source, project, runtime, uv, kwargs))
        print("fixture progress")
        make_venv(runtime)

    module = SimpleNamespace(source_id=lambda _: "a" * 20, atomic_write=installer.atomic_write,
                             held_lock=installer.held_lock, InstallError=installer.InstallError,
                             ensure_no_residual=installer.ensure_no_residual,
                             stage_uncertain=installer.stage_uncertain,
                             check_uv=lambda _: None, prepare_package=prepare)
    monkeypatch.setattr(room, "_installer", lambda _: module)
    monkeypatch.setattr(room.shutil, "which", lambda _: "/fixture/uv")
    return source, cache, calls, module


def receipt(cache):
    return next((cache / "runtimes").glob("*/active.json"))


def test_prepare_then_reuse_checks_integrity_without_install_or_browser(fixture, capsys):
    source, cache, calls, _ = fixture
    python = room.prepare_runtime(source, cache)
    assert len(calls) == 1 and calls[0][1] == cache
    assert calls[0][4] == {"cache_dir": cache / "packages"}
    assert python.is_file() and "runtimes" in python.parts
    assert room.prepare_runtime(source, cache) == python
    assert len(calls) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "fixture progress" in captured.err
    data = json.loads(receipt(cache).read_text())
    assert "python" not in data and len(data["attempt"]) == 32
    if os.name != "nt":
        assert cache.stat().st_mode & 0o777 == 0o700
        assert receipt(cache).stat().st_mode & 0o777 == 0o600


def test_build_metadata_changes_installer_and_runtime_source_identity(fixture):
    source, cache, calls, module = fixture
    for name in ("pyproject.toml", "uv.lock", "scripts/build-constraints.txt",
                 "scripts/install.py", "README.md", "THIRD_PARTY_NOTICES.md"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture input", encoding="utf-8")
    metadata = source / "scripts/build_metadata.py"
    metadata.write_text("metadata version one", encoding="utf-8")
    module.source_id = installer.source_id
    original_id = installer.source_id(source)
    previous = room.prepare_runtime(source, cache)
    assert room.prepare_runtime(source, cache) == previous and len(calls) == 1
    metadata.write_text("metadata version two", encoding="utf-8")
    assert installer.source_id(source) != original_id
    current = room.prepare_runtime(source, cache)
    assert current != previous and previous.is_file() and current.is_file()
    assert len(calls) == 2
    assert room.prepare_runtime(source, cache) == current and len(calls) == 2


def test_main_has_one_json_result_and_no_progress_on_stdout(fixture, capsys):
    _, cache, _, _ = fixture
    assert room.main(["--prepare-runtime", "--cache", str(cache)]) == 0
    captured = capsys.readouterr()
    assert set(json.loads(captured.out)) == {"python"}
    assert "fixture progress" in captured.err


def test_new_managed_python_uses_another_namespace_without_invalidating_old_cache(fixture, monkeypatch):
    source, cache, calls, _ = fixture
    monkeypatch.setattr(room, "_interpreter_identity", lambda: "1" * 16)
    previous = room.prepare_runtime(source, cache)
    monkeypatch.setattr(room, "_interpreter_identity", lambda: "2" * 16)
    current = room.prepare_runtime(source, cache)
    assert current != previous and current.is_file() and previous.is_file()
    assert len(calls) == 2
    assert room.prepare_runtime(source, cache) == current
    monkeypatch.setattr(room, "_interpreter_identity", lambda: "1" * 16)
    assert room.prepare_runtime(source, cache) == previous
    assert len(calls) == 2


@pytest.mark.parametrize("change", ["source", "startup_hook", "interpreter"])
def test_modified_runtime_is_never_executed_or_silently_reused(fixture, change):
    source, cache, calls, _ = fixture
    python = room.prepare_runtime(source, cache)
    venv = python.parent.parent
    target = (venv / "lib/python3.12/site-packages/decision_browser/cli.py" if change == "source"
              else venv / "lib/python3.12/site-packages/unexpected.pth" if change == "startup_hook"
              else python)
    target.write_text("changed")
    with pytest.raises(room.RuntimeErrorCode, match="modified_runtime_cache"):
        room.prepare_runtime(source, cache)
    assert len(calls) == 1


@pytest.mark.parametrize("layout", ["pycache", "legacy"])
def test_modified_compiled_runtime_is_rejected_even_with_unchanged_source(fixture, layout):
    source, cache, calls, module = fixture
    original = module.prepare_package
    artifacts = []

    def prepare(*args, **kwargs):
        original(*args, **kwargs)
        package = room._python(args[2]).parent.parent / "lib/python3.12/site-packages/decision_browser"
        script = package / "cli.py"
        script.write_text("VALUE = 'original'\n")
        compiled = package / ("__pycache__/cli.cpython-312.pyc" if layout == "pycache" else "cli.pyc")
        compiled.parent.mkdir(exist_ok=True)
        py_compile.compile(str(script), cfile=str(compiled), doraise=True)
        artifacts.append((script, compiled))

    module.prepare_package = prepare
    python = room.prepare_runtime(source, cache)
    assert room.prepare_runtime(source, cache) == python
    script, compiled = artifacts[0]
    before = script.read_bytes()
    header = compiled.read_bytes()[:16]
    compiled.write_bytes(header + marshal.dumps(compile("VALUE = 'modified'\n", str(script), "exec")))
    assert script.read_bytes() == before
    with pytest.raises(room.RuntimeErrorCode, match="modified_runtime_cache"):
        room.prepare_runtime(source, cache)
    assert len(calls) == 1


def test_added_sourceless_startup_bytecode_is_rejected(fixture):
    source, cache, calls, _ = fixture
    python = room.prepare_runtime(source, cache)
    startup = python.parent.parent / "lib/python3.12/site-packages/sitecustomize.pyc"
    startup.write_bytes(b"unverified startup bytecode")
    with pytest.raises(room.RuntimeErrorCode, match="modified_runtime_cache"):
        room.prepare_runtime(source, cache)
    assert len(calls) == 1


def test_venv_python_symlink_to_current_managed_interpreter_is_allowed(fixture):
    if os.name == "nt":
        return
    source, cache, _, module = fixture
    original = module.prepare_package

    def prepare(*args, **kwargs):
        original(*args, **kwargs)
        python = room._python(args[2])
        python.unlink()
        python.symlink_to(Path(sys.executable).resolve())

    module.prepare_package = prepare
    python = room.prepare_runtime(source, cache)
    assert python.is_symlink()
    assert room.prepare_runtime(source, cache) == python


@pytest.mark.parametrize("change", ["command", "traversal", "schema", "moved"])
def test_invalid_receipt_cannot_choose_executable(fixture, change):
    source, cache, calls, _ = fixture
    room.prepare_runtime(source, cache)
    record = receipt(cache)
    data = json.loads(record.read_text())
    if change == "command":
        data["python"] = "/tmp/hostile-python"
    elif change == "traversal":
        data["attempt"] = "../../escape"
    elif change == "schema":
        data["schema"] = True
    else:
        data["cache"] = "/a/different/cache"
    record.write_text(json.dumps(data))
    with pytest.raises(room.RuntimeErrorCode, match="invalid_runtime_receipt"):
        room.prepare_runtime(source, cache)
    assert len(calls) == 1


def test_failed_attempt_is_cleaned_and_does_not_poison_retry(fixture):
    source, cache, _, module = fixture
    original = module.prepare_package

    def fail(*args, **kwargs):
        raise RuntimeError("placeholder failure")

    module.prepare_package = fail
    with pytest.raises(RuntimeError, match="placeholder"):
        room.prepare_runtime(source, cache)
    assert not list((cache / "runtimes").glob("*/prepare.lock"))
    assert not list((cache / "runtimes").glob("*/active.json"))
    assert not list((cache / "runtimes").glob("*/*/venv"))
    module.prepare_package = original
    assert room.prepare_runtime(source, cache).is_file()


def test_different_package_failure_preserves_previous_runtime(fixture):
    source, cache, _, module = fixture
    previous = room.prepare_runtime(source, cache)
    before = receipt(cache).read_bytes()
    module.source_id = lambda _: "b" * 20

    def fail(*args, **kwargs):
        raise RuntimeError("failure")

    module.prepare_package = fail
    with pytest.raises(RuntimeError):
        room.prepare_runtime(source, cache)
    assert previous.is_file()
    assert next((cache / "runtimes").glob("a*/active.json")).read_bytes() == before


def test_interrupted_lock_is_reported_without_deleting_it(fixture, capsys):
    source, cache, _, _ = fixture
    room.prepare_runtime(source, cache)
    namespace = receipt(cache).parent
    receipt(cache).unlink()
    lock = namespace / "prepare.lock"
    lock.write_text("another owner")
    lock.chmod(0o600)
    assert room.main(["--prepare-runtime", "--cache", str(cache)]) == 1
    assert lock.read_text() == "another owner"
    assert "inspect its prepare.lock" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["root", "namespace", "receipt", "package"])
def test_cache_symlinks_are_rejected(fixture, tmp_path, kind):
    source, cache, _, _ = fixture
    room.prepare_runtime(source, cache)
    if kind == "root":
        target = cache
    elif kind == "namespace":
        target = receipt(cache).parent
    elif kind == "receipt":
        target = receipt(cache)
    else:
        python = room.prepare_runtime(source, cache)
        target = python.parent.parent / "lib/python3.12/site-packages/decision_browser"
    alternate = tmp_path / "moved"
    target.rename(alternate)
    target.symlink_to(alternate, target_is_directory=alternate.is_dir())
    with pytest.raises(room.RuntimeErrorCode, match="unsafe_runtime_cache_path"):
        room.prepare_runtime(source, cache)


def test_permissive_cache_and_relative_path_are_rejected(fixture):
    source, cache, _, _ = fixture
    with pytest.raises(room.RuntimeErrorCode, match="invalid_runtime_cache_path"):
        room.prepare_runtime(source, Path("relative"))
    cache.mkdir(mode=0o755)
    if os.name != "nt":
        with pytest.raises(room.RuntimeErrorCode, match="unsafe_runtime_cache_permissions"):
            room.prepare_runtime(source, cache)


def test_preexisting_unknown_cache_is_not_claimed(fixture):
    source, cache, _, _ = fixture
    (cache / "runtimes").mkdir(parents=True, mode=0o700)
    cache.chmod(0o700)
    with pytest.raises(FileNotFoundError):
        room.prepare_runtime(source, cache)


def test_errors_are_sanitized_and_do_not_pollute_stdout(fixture, capsys):
    _, cache, _, module = fixture

    def fail(*args, **kwargs):
        raise OSError("CANARY-private-network-error")

    module.prepare_package = fail
    assert room.main(["--prepare-runtime", "--cache", str(cache)]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "CANARY" not in captured.err


def test_shared_package_preparation_keeps_frozen_lock_and_excludes_browser(tmp_path, monkeypatch):
    source, project, runtime = tmp_path / "source", tmp_path / "cache", tmp_path / "runtime"
    (source / "prebuilt").mkdir(parents=True)
    (source / "prebuilt/package.whl").write_text("not executed")
    monkeypatch.setenv("OPENAI_API_KEY", "CANARY")
    calls = []
    monkeypatch.setattr(installer, "run", lambda *args: calls.append(args))
    installer.prepare_package(source, project, runtime, "/uv", cache_dir=tmp_path / "packages")
    assert len(calls) == 2
    assert "--frozen" in calls[0][0]
    assert "--no-index" in calls[1][0] and "--no-deps" in calls[1][0]
    assert all("CANARY" not in repr(call) for call in calls)
    assert all("playwright" not in " ".join(call[0]) for call in calls)


def test_shared_package_preparation_preserves_hash_locked_mirror(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "prebuilt").mkdir(parents=True)
    (source / "prebuilt/package.whl").write_text("not executed")
    monkeypatch.setenv("DECISION_BROWSER_INSTALL_INDEX_URL", "https://mirror.example/simple")
    calls = []
    monkeypatch.setattr(installer, "run", lambda *args: calls.append(args))
    installer.prepare_package(source, tmp_path / "cache", tmp_path / "runtime", "/uv")
    assert len(calls) == 4
    assert "--frozen" in calls[0][0]
    assert "--require-hashes" in calls[2][0]
    assert all("playwright" not in " ".join(call[0]) for call in calls)
