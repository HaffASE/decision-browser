"""Prepare the npm-owned command runtime without browser or project changes."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import uuid
from collections.abc import Callable
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import ModuleType
from typing import Any

_OWNER = b'{"owner":"decision-browser-tool-runtimes-v1"}\n'
_RECEIPT_LIMIT = 8 * 1024 * 1024
_MAX_FILES = 20_000


class RuntimeErrorCode(Exception):
    """Fixed diagnostic codes only; never include settings or child output."""


def _directory(path: Path, *, private: bool = True, create: bool = False) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimeErrorCode("invalid_runtime_cache_path")
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise RuntimeErrorCode("unsafe_runtime_cache_path")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeErrorCode("unsafe_runtime_cache_path")
    if os.name != "nt" and (
        info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & (0o077 if private else 0o022)
    ):
        raise RuntimeErrorCode("unsafe_runtime_cache_permissions")


def _read(path: Path, limit: int) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RuntimeErrorCode("unsafe_runtime_cache_path")
    if os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise RuntimeErrorCode("unsafe_runtime_cache_permissions")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise RuntimeErrorCode("runtime_cache_changed")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise RuntimeErrorCode("invalid_runtime_receipt")
    return data


def _hash(path: Path) -> str:
    if path.stat().st_size > 256 * 1024 * 1024:
        raise RuntimeErrorCode("invalid_runtime_cache_file")
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _python(runtime: Path) -> Path:
    return runtime / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _interpreter_identity() -> str:
    executable = Path(sys.executable).resolve()
    value = str(executable).encode() + b"\0" + _hash(executable).encode()
    return hashlib.sha256(value).hexdigest()[:16]


def _manifest(runtime: Path) -> dict[str, str]:
    """Check installed sources, startup hooks and executable bytecode."""
    venv = runtime / "venv"
    _directory(runtime)
    _directory(venv, private=False)
    python = _python(runtime)
    if not python.is_file() or not os.access(python, os.X_OK):
        raise RuntimeErrorCode("incomplete_runtime_cache")
    files: dict[str, str] = {}
    for directory, directories, names in os.walk(venv, followlinks=False):
        parent = Path(directory)
        _directory(parent, private=False)
        for name in list(directories):
            child = parent / name
            if child.is_symlink():
                # CPython/uv may create this redundant alias on 64-bit Linux.
                if child != venv / "lib64" or os.readlink(child) != "lib":
                    raise RuntimeErrorCode("unsafe_runtime_cache_path")
                files[child.relative_to(venv).as_posix()] = "link:lib"
                directories.remove(name)
        for name in names:
            child = parent / name
            relative = child.relative_to(venv).as_posix()
            info = child.lstat()
            # uv's advisory lock is intentionally 0666; private parents protect it.
            if relative == ".lock":
                if not stat.S_ISREG(info.st_mode) or os.name != "nt" and info.st_uid != os.getuid():
                    raise RuntimeErrorCode("unsafe_runtime_cache_path")
                continue
            if child.is_symlink():
                if (parent != venv / "bin" or not re.fullmatch(r"python(?:3(?:\.\d+)?)?", name)
                        or child.resolve() != Path(sys.executable).resolve()):
                    raise RuntimeErrorCode("unsafe_runtime_cache_path")
                files[relative] = "python:" + _hash(child)
            else:
                if not stat.S_ISREG(info.st_mode):
                    raise RuntimeErrorCode("unsafe_runtime_cache_path")
                if os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022):
                    raise RuntimeErrorCode("unsafe_runtime_cache_permissions")
                files[relative] = "sha256:" + _hash(child)
            if len(files) > _MAX_FILES:
                raise RuntimeErrorCode("invalid_runtime_receipt")
    if not any(name.endswith("/site-packages/decision_browser/cli.py") for name in files):
        raise RuntimeErrorCode("incomplete_runtime_cache")
    if not any(name.endswith("/site-packages/decision_browser/skill/scripts/doctor.py") for name in files):
        raise RuntimeErrorCode("incomplete_runtime_cache")
    return files


def _installer(source: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("decision_browser_tool_installer", source / "scripts/install.py")
    if spec is None or spec.loader is None:
        raise RuntimeErrorCode("invalid_runtime_package")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise RuntimeErrorCode("invalid_runtime_receipt")
        result[name] = value
    return result


def prepare_runtime(source: Path, cache: Path) -> Path:
    installer = _installer(source)
    _directory(cache, create=True)
    with ExitStack() as stack:
        try:
            validate = stack.enter_context(installer.held_lock(cache, ".runtimes.lease"))
        except installer.InstallError as exc:
            code = ("runtime_cleanup_unknown" if str(exc).startswith("stage_cleanup_unknown")
                    else "runtime_preparation_locked" if "locked" in str(exc)
                    else "unsafe_runtime_cache_path")
            raise RuntimeErrorCode(code) from None
        installer.ensure_no_residual(cache)
        return _prepare_locked(source, cache, installer, validate)


def _prepare_locked(source: Path, cache: Path, installer: ModuleType,
                    validate: Callable[[], None]) -> Path:
    validate()
    runtimes = cache / "runtimes"
    existed = runtimes.exists()
    _directory(runtimes, create=True)
    marker = runtimes / "owner.json"
    if existed:
        if _read(marker, 256) != _OWNER:
            raise RuntimeErrorCode("unrecognized_runtime_cache")
    else:
        installer.atomic_write(marker, _OWNER)
    fingerprint = installer.source_id(source)
    tag = sys.implementation.cache_tag
    if tag is None or not re.fullmatch(r"[a-zA-Z0-9_-]+", tag):
        raise RuntimeErrorCode("unsupported_python_runtime")
    namespace = runtimes / f"{fingerprint}-{tag}-{_interpreter_identity()}"
    _directory(namespace, create=True)
    receipt = namespace / "active.json"
    if receipt.exists() or receipt.is_symlink():
        data = json.loads(_read(receipt, _RECEIPT_LIMIT), object_pairs_hook=_unique)
        if (type(data) is not dict or set(data) != {"schema", "cache", "source_id", "tag", "attempt", "files"}
                or type(data["schema"]) is not int or data["schema"] != 1
                or data["cache"] != str(cache) or data["source_id"] != fingerprint or data["tag"] != tag
                or type(data["attempt"]) is not str or not re.fullmatch(r"[0-9a-f]{32}", data["attempt"])
                or type(data["files"]) is not dict):
            raise RuntimeErrorCode("invalid_runtime_receipt")
        runtime = namespace / data["attempt"]
        if _manifest(runtime) != data["files"]:
            raise RuntimeErrorCode("modified_runtime_cache")
        return _python(runtime)
    legacy_lock = namespace / "prepare.lock"
    if legacy_lock.exists() or legacy_lock.is_symlink():
        raise RuntimeErrorCode("runtime_preparation_locked")
    attempt = uuid.uuid4().hex
    runtime = namespace / attempt
    created = False
    try:
        runtime.mkdir(mode=0o700)
        created = True
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeErrorCode("uv_unavailable")
        packages = cache / "packages"
        _directory(packages, create=True)
        with redirect_stdout(sys.stderr):
            installer.check_uv(uv)
            installer.prepare_package(source, cache, runtime, uv, cache_dir=packages)
        if installer.source_id(source) != fingerprint:
            raise RuntimeErrorCode("runtime_package_changed")
        files = _manifest(runtime)
        data = {"schema": 1, "cache": str(cache), "source_id": fingerprint,
                "tag": tag, "attempt": attempt, "files": files}
        payload = (json.dumps(data, sort_keys=True) + "\n").encode()
        if len(payload) > _RECEIPT_LIMIT:
            raise RuntimeErrorCode("invalid_runtime_receipt")
        validate()
        installer.atomic_write(receipt, payload)
        return _python(runtime)
    except BaseException:
        if created and not installer.stage_uncertain(cache):
            shutil.rmtree(runtime)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-runtime", action="store_true", required=True)
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        python = prepare_runtime(Path(__file__).resolve().parents[1], args.cache)
        print(json.dumps({"python": str(python)}))
        return 0
    except RuntimeErrorCode as exc:
        print(f"Tool runtime preparation stopped: {exc}", file=sys.stderr)
        if str(exc) == "runtime_cleanup_unknown":
            print("Owned stage state and runtime were retained. Inspect them before another preparation.", file=sys.stderr)
        if str(exc) == "runtime_preparation_locked":
            print("Wait for the other setup. For an interrupted run, inspect its prepare.lock before removing it.",
                  file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Tool runtime preparation cancelled.", file=sys.stderr)
        return 130
    except Exception:  # noqa: BLE001 - bootstrap errors must not reveal child data
        print("Tool runtime preparation failed. Check cache permissions and network settings, then retry.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
