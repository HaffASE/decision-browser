"""Project-only installation from a distribution or checkout; Python 3.11+ and uv required."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError:
    raise SystemExit("Python 3.11+ is required; use python3.12 scripts/install.py.") from None
from pathlib import Path
from typing import Any

SERVER = "decision_browser"
STATE = ".decision-browser-install"
BEGIN = "# BEGIN decision-browser managed MCP\n"
END = "# END decision-browser managed MCP\n"
ENV_NAMES = [
    "OPENAI_API_KEY", "TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "OPENCODE_API_KEY",
    "DECISION_BROWSER_PROVIDER", "DECISION_BROWSER_API_KEY", "DECISION_BROWSER_ENDPOINT",
    "DECISION_BROWSER_MODEL", "DECISION_BROWSER_MAX_CHOICES", "DECISION_BROWSER_TEXT_ENDPOINT",
    "DECISION_BROWSER_TEXT_MODEL", "DECISION_BROWSER_TEXT_API_KEY",
    "DECISION_BROWSER_CONFIG_FILE",
]
BROWSER_PROBE = """
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(chromium_sandbox=True, channel="chromium")
    page = browser.new_page()
    page.set_content('<title>Decision Browser setup</title><button>Check</button>')
    assert page.title() == 'Decision Browser setup'
    page.get_by_role('button').click(timeout=5000)
    assert page.screenshot(timeout=5000).startswith(b'\\x89PNG')
    browser.close()
"""


class InstallError(Exception):
    """Only fixed, credential-free diagnostics reach the user."""


_LEASE = b'{"owner":"decision-browser-lease-v1"}\n'
_ACTIVE_INTENT = b'{"active":"stage-lifecycle-v1"}\n'
_POISON = b'{"blocked":"stage-cleanup-unknown"}\n'
_ACTIVE_LEASE: ContextVar[tuple[Path, int] | None] = ContextVar("installer_lease", default=None)
_STAGE_TIMEOUT = 900.0
_CLEANUP_TIMEOUT = 5.0
_MANIFEST_LIMIT = 1024 * 1024
_RESIDUAL = ".decision-browser-stage-cleanup-unknown.json"


@contextmanager
def held_lock(root: Path, relative: str) -> Iterator[Callable[[], None]]:
    """Keep a kernel lease on a persistent inode; crash recovery never unlinks it."""
    path = safe_path(root, relative)
    if Path(relative).name != relative:
        raise InstallError("Invalid installation lease location.")
    directory = _parent_descriptor(root) if os.name != "nt" else None
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path.name if directory is not None else path, flags, 0o600,
                     **({"dir_fd": directory} if directory is not None else {}))
    except OSError:
        if directory is not None:
            os.close(directory)
        raise InstallError("Unsafe installation lease; inspect the reserved lease path.") from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 128
                or os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077)):
            raise InstallError("Unsafe installation lease; inspect its owner and permissions.")
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise InstallError("Installation locked by another active setup; retry after it finishes.") from None
        def validate() -> None:
            current = safe_path(root, relative).lstat()
            opened = os.fstat(fd)
            if ((current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                    or opened.st_nlink != 1 or not stat.S_ISREG(opened.st_mode)
                    or os.name != "nt" and (opened.st_uid != os.getuid() or opened.st_mode & 0o077)):
                raise InstallError("Installation lease changed; no further state was committed.")
            if directory is not None:
                present, held = root.stat(follow_symlinks=False), os.fstat(directory)
                if (present.st_dev, present.st_ino) != (held.st_dev, held.st_ino):
                    raise InstallError("Installation lease directory changed; no state was committed.")

        validate()
        body = os.read(fd, 129)
        if body in {_ACTIVE_INTENT, _POISON}:
            raise InstallError("stage_cleanup_unknown: inspect the retained blocked lease before retrying.")
        if body not in (b"", _LEASE):
            raise InstallError("Unrecognized installation lease; no foreign lock was changed.")
        if not body:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, _LEASE)
            os.fsync(fd)
        token = _ACTIVE_LEASE.set((root, fd))
        try:
            yield validate
        finally:
            _ACTIVE_LEASE.reset(token)
    finally:
        os.close(fd)
        if directory is not None:
            os.close(directory)


def safe_path(project: Path, relative: str) -> Path:
    path = project / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise InstallError("Invalid managed path.")
    for part in (path, *path.parents):
        if part == project:
            break
        if part.is_symlink():
            raise InstallError("Symlink in installation target; choose a regular project directory.")
    return path


def read(path: Path) -> bytes | None:
    try:
        return bounded_read(path, 4 * 1024 * 1024)
    except FileNotFoundError:
        return None


def _parent_descriptor(parent: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    current = os.open(parent.anchor, flags)
    try:
        for part in parent.parts[1:]:
            following = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


def atomic_write(path: Path, data: bytes) -> None:
    path = path.absolute()
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise InstallError("Symlink in managed destination; refusing overwrite.")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise InstallError("Symlink in managed destination; refusing overwrite.")
    identity = lambda value: None if value is None else (value.st_dev, value.st_ino, value.st_mtime_ns, value.st_size)
    if os.name != "nt":
        directory = _parent_descriptor(path.parent)
        temporary = ".decision-browser-" + uuid.uuid4().hex
        try:
            parent = os.fstat(directory)
            try:
                before = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                before = None
            if before is not None and not stat.S_ISREG(before.st_mode):
                raise InstallError("Managed destination must be a regular file.")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            current = path.parent.stat(follow_symlinks=False)
            try:
                after = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                after = None
            if ((parent.st_dev, parent.st_ino) != (current.st_dev, current.st_ino)
                    or identity(before) != identity(after)):
                raise InstallError("Managed destination changed; preserving concurrent edits.")
            os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
            present = path.parent.stat(follow_symlinks=False)
            if (present.st_dev, present.st_ino) != (parent.st_dev, parent.st_ino):
                raise InstallError("Managed destination directory changed; preserve the retained original inode.")
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)
        return
    # Windows has no Python dir_fd replacement. Keep bounded pathname identity
    # checks and file fsync; native filesystem qualification remains separate.
    parent = path.parent.stat()
    before = path.lstat() if path.exists() else None
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise InstallError("Managed destination must be a regular file.")
    fd, name = tempfile.mkstemp(prefix=".decision-browser-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        current = path.parent.stat()
        after = path.lstat() if path.exists() else None
        if ((parent.st_dev, parent.st_ino) != (current.st_dev, current.st_ino)
                or path.is_symlink() or identity(before) != identity(after)):
            raise InstallError("Managed destination changed; preserving concurrent edits.")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean_environment() -> dict[str, str]:
    # Network settings are explicit; model credentials never reach setup children.
    env = {key: os.environ[key] for key in (
        "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "TMPDIR", "TMP", "TEMP",
        "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "HTTPS_PROXY", "HTTP_PROXY",
        "ALL_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "all_proxy", "no_proxy",
    ) if key in os.environ}
    index = os.environ.get("DECISION_BROWSER_INSTALL_INDEX_URL")
    if index is not None:
        parts = urlsplit(index)
        if (parts.scheme != "https" or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment
                or any(c.isspace() for c in index)):
            raise InstallError("invalid_index_url: use an HTTPS index without credentials/query/fragment.")
        env["UV_DEFAULT_INDEX"] = index
    return env


def _live_group(pgid: int) -> bool:
    """A killed zombie cannot execute or hold a stage's output pipe open."""
    rows = subprocess.run(["ps", "-A", "-o", "pgid=,stat="], capture_output=True,
                          text=True, timeout=1, check=False, env={})
    if rows.returncode:
        raise InstallError("stage_cleanup_unknown: could not inspect owned process group.")
    return any(len(parts := line.split()) == 2 and parts[0] == str(pgid)
               and not parts[1].startswith("Z") for line in rows.stdout.splitlines())


def _stop_stage(child: subprocess.Popen[bytes], *, timed_out: bool = False,
                platform: str = sys.platform) -> None:
    if platform == "win32":
        if not timed_out:
            child.wait(timeout=_CLEANUP_TIMEOUT)
            return
        # Native Windows qualification is separate. taskkill targets this exact
        # process tree; no broad image-name matching or shell command is used.
        result = subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=_CLEANUP_TIMEOUT, check=False)
        if result.returncode and child.poll() is None:
            raise InstallError("stage_cleanup_unknown: owned Windows process tree did not stop.")
        child.wait(timeout=_CLEANUP_TIMEOUT)
        # A root wait or taskkill exit status does not prove native descendant
        # quiescence. The caller retains a poison receipt and blocks retries.
        raise InstallError("stage_cleanup_unknown: Windows timeout requires native tree inspection.")
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            pass
        if sig == signal.SIGTERM:
            try:
                child.wait(timeout=min(0.2, _CLEANUP_TIMEOUT))
            except subprocess.TimeoutExpired:
                pass
    child.wait(timeout=_CLEANUP_TIMEOUT)
    deadline = time.monotonic() + _CLEANUP_TIMEOUT
    while _live_group(child.pid):
        if time.monotonic() >= deadline:
            raise InstallError("stage_cleanup_unknown: owned process group did not quiesce.")
        time.sleep(0.02)


def _stage_hint(tail: bytes) -> str:
    lowered = tail.lower()
    for needles, code in [
        ((b"certificate", b"ssl", b"tls"), "tls_certificate"),
        ((b"no space left", b"enospc"), "disk_space"),
        ((b"permission denied", b"eacces"), "permissions"),
        ((b"name resolution", b"resolve host", b"getaddrinfo"), "dns_resolution"),
        ((b"host dependencies", b"missing libraries"), "host_libraries"),
    ]:
        if any(needle in lowered for needle in needles):
            return code
    return "check_prerequisites"


def _lease_state(root: Path) -> bytes | None:
    active = _ACTIVE_LEASE.get()
    if active is None or active[0] != root:
        return None
    os.lseek(active[1], 0, os.SEEK_SET)
    return os.read(active[1], 129)


def _set_lease_state(root: Path, state: bytes) -> None:
    active = _ACTIVE_LEASE.get()
    if active is None or active[0] != root:
        raise InstallError("stage_lifecycle_unavailable: no owned lifecycle lease; no stage was started.")
    try:
        fd = active[1]
        os.lseek(fd, 0, os.SEEK_SET)
        if os.write(fd, state) != len(state):
            raise OSError("short lease write")
        os.ftruncate(fd, len(state))
        os.fsync(fd)
        if _lease_state(root) != state:
            raise OSError("lease readback mismatch")
    except OSError:
        raise InstallError("stage_lifecycle_unavailable: lifecycle state was not persisted; no new stage may start.") from None


def stage_uncertain(root: Path) -> bool:
    """The held lease is authoritative even when a residual JSON write failed."""
    try:
        state = _lease_state(root)
        return state is not None and state != _LEASE or read(safe_path(root, _RESIDUAL)) is not None
    except (OSError, ValueError, InstallError):
        return True


def ensure_no_residual(root: Path) -> None:
    if stage_uncertain(root):
        raise InstallError("stage_cleanup_unknown: inspect the retained owned-stage residual before retrying.")


def _retain_stage_residual(root: Path, child: subprocess.Popen[bytes] | None, reason: str) -> None:
    active = _ACTIVE_LEASE.get()
    if active is not None and active[0] == root:
        try:
            _set_lease_state(root, _POISON)
        except InstallError:
            # The complete ACTIVE intent predates process launch. A failed
            # post-hoc poison write cannot downgrade that durable uncertainty.
            pass
    try:
        atomic_write(safe_path(root, _RESIDUAL), (json.dumps({
            "schema": 1, "owner": "decision-browser-installer-stage-v1",
            "attempt": uuid.uuid4().hex, "pid": child.pid if child is not None else None,
            "new_session": os.name != "nt", "reason": reason,
        }) + "\n").encode())
    except (OSError, InstallError):
        raise InstallError("stage_cleanup_unknown: residual write failed; preserve the retained blocked lease.") from None


def run(command: list[str], env: dict[str, str], stage: str, cwd: Path) -> None:
    with ExitStack() as stack:
        active = _ACTIVE_LEASE.get()
        if active is None or active[0] != cwd:
            stack.enter_context(held_lock(cwd, ".decision-browser-stage.lease"))
        _run_stage(command, env, stage, cwd)


def _run_stage(command: list[str], env: dict[str, str], stage: str, cwd: Path) -> None:
    ensure_no_residual(cwd)
    # A process may outlive the installer root. Publish and read back intent
    # before Popen; only positively completed stages can return the lease idle.
    _set_lease_state(cwd, _ACTIVE_INTENT)
    print(f"Preparing: {stage}", flush=True)
    started = time.monotonic()
    code, exit_code, hint = "ok", None, "none"
    tail = bytearray()
    child: subprocess.Popen[bytes] | None = None
    reader: threading.Thread | None = None
    cleanup_unknown = False
    drain_failed = False
    try:
        options: dict[str, Any] = {"start_new_session": True, "umask": 0o077}
        if sys.platform == "win32":
            options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        child = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, shell=False, **options)
        assert child.stdout is not None

        def drain() -> None:
            nonlocal drain_failed
            assert child is not None and child.stdout is not None
            try:
                with child.stdout:
                    while data := child.stdout.read(4096):
                        tail.extend(data)
                        del tail[:-16384]
            except (OSError, ValueError):
                drain_failed = True

        reader = threading.Thread(target=drain, name="installer-stage-output", daemon=True)
        reader.start()
        try:
            exit_code = child.wait(timeout=_STAGE_TIMEOUT)
            if exit_code:
                code = "stage_failed"
        except subprocess.TimeoutExpired:
            code = "stage_timeout"
        except (KeyboardInterrupt, SystemExit):
            code = "stage_interrupted"
        finally:
            try:
                _stop_stage(child, timed_out=code == "stage_timeout")
            except (InstallError, OSError, subprocess.TimeoutExpired):
                cleanup_unknown = True
        reader.join(timeout=_CLEANUP_TIMEOUT)
        cleanup_unknown |= reader.is_alive() or drain_failed
    except OSError:
        if child is None:
            code = "stage_start_failed"
        else:
            code = "stage_cleanup_unknown"
            cleanup_unknown = True
    except (KeyboardInterrupt, SystemExit, RuntimeError, AssertionError):
        # Once launched, every uncompleted lifecycle remains ACTIVE; cleanup
        # errors must not be reported as a failure to start a new process.
        code = "stage_interrupted"
        cleanup_unknown = child is not None
        if child is not None:
            try:
                _stop_stage(child)
            except (InstallError, OSError, subprocess.TimeoutExpired):
                pass
    if code == "ok" and cleanup_unknown:
        code = "stage_cleanup_unknown"
    hint = _stage_hint(bytes(tail)) if code != "ok" else "none"
    retained = code != "ok" or cleanup_unknown
    retention_failed = False
    if retained:
        # Even a vanished original group does not prove closure of detached
        # browser topology after timeout, failure, interruption or parent death.
        try:
            _retain_stage_residual(cwd, child, code + "_uncertainty" if code != "ok"
                                   else "quiescence_unknown")
        except InstallError:
            retention_failed = True
    else:
        # Successful completion plus original-group/output quiescence was
        # observed. No failed or incomplete lifecycle may clear intent.
        _set_lease_state(cwd, _LEASE)
    print(json.dumps({"stage": stage, "code": code, "exit_code": exit_code,
                      "hint": hint, "cleanup": "unknown_retained" if retained else "complete",
                      "residual_write": "failed" if retention_failed else "retained" if retained else "not_needed",
                      "duration_seconds": round(time.monotonic() - started, 3)}), file=sys.stderr)
    if retained:
        primary = f"{code}: {stage} failed; exit={exit_code}; hint={hint}. "
        raise InstallError(primary + "stage_cleanup_unknown: retained blocked lease and owned runtime; "
                           + ("residual write failed; " if retention_failed else "")
                           + "inspect the owned stage before another preparation. Raw child output is suppressed for privacy.")


def check_uv(uv: str) -> None:
    result = subprocess.run([uv, "--version"], env=clean_environment(),
                            capture_output=True, text=True, timeout=15, check=False)
    match = re.fullmatch(r"uv (\d+)\.(\d+)\.(\d+)(?:[^\r\n]*)\n?", result.stdout)
    version = ".".join(match.groups()) if match else "unknown"
    print(f"Using uv {version}: {uv}", file=sys.stderr)
    if result.returncode or not match or tuple(map(int, match.groups())) < (0, 13, 7):
        raise InstallError(f"uv 0.13.7+ is required; selected {uv} ({version}). "
                           "Update uv and check PATH order in the client shell.")


def source_id(source: Path) -> str:
    files = [source / name for name in ("pyproject.toml", "uv.lock", "scripts/build-constraints.txt",
                                           "scripts/install.py", "README.md", "THIRD_PARTY_NOTICES.md")]
    prebuilt = source / "prebuilt"
    if prebuilt.is_symlink():
        raise InstallError("Source symlinks are not supported.")
    if prebuilt.is_dir():
        files.extend(prebuilt.glob("*.whl"))
    if (source / "hatch_build.py").is_file():
        files.append(source / "hatch_build.py")
    if (source / "scripts/prepare_tool_runtime.py").is_file():
        files.append(source / "scripts/prepare_tool_runtime.py")
    metadata = source / "scripts/build_metadata.py"
    if metadata.exists() or metadata.is_symlink():
        files.append(metadata)
    if (source / "LICENSE").is_file():
        files.append(source / "LICENSE")
    for folder in ("src/decision_browser", "skills/decision-browser"):
        root = source / folder
        if any((source / part).is_symlink() for part in (Path(folder), *Path(folder).parents)):
            raise InstallError("Source symlinks are not supported.")
        for directory, dirs, names in os.walk(root, followlinks=False):
            if any((Path(directory) / name).is_symlink() for name in (*dirs, *names)):
                raise InstallError("Source symlinks are not supported.")
        files.extend(p for p in root.rglob("*") if p.is_file()
                     and "__pycache__" not in p.parts and p.suffix not in (".pyc", ".pyo"))
    h = hashlib.sha256()
    for path in sorted(files):
        if path.is_symlink() or any((source / p).is_symlink()
                                    for p in path.relative_to(source).parents):
            raise InstallError("Source symlinks are not supported.")
        h.update(path.relative_to(source).as_posix().encode())
        if path.parent == prebuilt:
            from zipfile import ZipFile

            with ZipFile(path) as archive:
                names = archive.namelist()
                if len(set(names)) != len(names):
                    raise InstallError("Duplicate prebuilt wheel entries.")
                for name in sorted(names):
                    if name.endswith(("/", ".dist-info/RECORD")):
                        continue
                    h.update(name.encode())
                    h.update(hashlib.sha256(archive.read(name)).digest())
        else:
            h.update(path.read_bytes())
    return h.hexdigest()[:20]


def prepare_package(source: Path, project: Path, runtime: Path, uv: str, *,
                    cache_dir: Path | None = None) -> None:
    """Install the locked package without browser or client-registration work."""
    env = clean_environment()
    env.update(UV_PROJECT_ENVIRONMENT=str(runtime / "venv"),
               UV_CACHE_DIR=str(cache_dir if cache_dir is not None else project / STATE / "cache"),
               PLAYWRIGHT_BROWSERS_PATH=str(runtime / "browsers"))
    python = runtime / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if "UV_DEFAULT_INDEX" in env:
        runtime.mkdir(parents=True, exist_ok=True)
        requirements = runtime / "requirements.txt"
        run([uv, "--no-config", "export", "--project", str(source), "--frozen", "--no-dev",
             "--no-emit-project", "--format", "requirements.txt", "--output-file", str(requirements)],
            env, "locked dependency export", project)
        run([uv, "--no-config", "venv", "--python", sys.executable, str(runtime / "venv")],
            env, "Python environment", project)
        run([uv, "--no-config", "pip", "sync", "--python", str(python), "--require-hashes",
             "--default-index", env["UV_DEFAULT_INDEX"], str(requirements)],
            env, "locked Python package", project)
    else:
        run([uv, "--no-config", "--no-python-downloads", "sync", "--project", str(source),
             "--frozen", "--no-dev", "--no-install-project", "--python", sys.executable],
            env, "locked Python package", project)
    wheels = list((source / "prebuilt").glob("*.whl"))
    if not wheels:
        run([uv, "--no-config", "--no-python-downloads", "build", "--wheel", str(source),
             "--python", str(python), "--build-constraints", str(source / "scripts/build-constraints.txt"),
             "--out-dir", str(runtime / "dist")], env, "package build", project)
        wheels = list((runtime / "dist").glob("*.whl"))
    if len(wheels) != 1:
        raise InstallError("Expected exactly one built wheel; client settings were not installed.")
    run([uv, "--no-config", "pip", "install", "--python", str(python),
         "--no-deps", "--no-index", "--reinstall", str(wheels[0])], env, "wheel installation", project)


def prepare(source: Path, project: Path, runtime: Path, uv: str) -> None:
    prepare_package(source, project, runtime, uv)
    env = clean_environment()
    env.update(UV_PROJECT_ENVIRONMENT=str(runtime / "venv"),
               UV_CACHE_DIR=str(project / STATE / "cache"),
               PLAYWRIGHT_BROWSERS_PATH=str(runtime / "browsers"))
    python = runtime / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    run([str(python), "-I", "-m", "playwright", "install", "--no-shell", "chromium"],
        env, "Chromium download", project)
    run([str(python), "-I", "-c", BROWSER_PROBE], env, "sandboxed Chromium smoke test", project)
    run([str(python), "-I", "-c", "from decision_browser.server import create_server; create_server()"],
        env, "MCP server import", project)


def validate_profile_path(value: str) -> Path:
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise InstallError("Invalid provider profile path; use an absolute path without parent traversal.")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise InstallError("Invalid provider profile path; use an absolute path without parent traversal.")
    return path


def configuration(client: str, project: Path, runtime: Path) -> dict[str, Any]:
    python = runtime / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    common: dict[str, Any] = {
        "command": str(python),
        "args": ["-I", "-m", "decision_browser.cli", "serve", "--admission", "standard_form"],
        "env": {"PLAYWRIGHT_BROWSERS_PATH": str(runtime / "browsers")},
    }
    profile = os.environ.get("DECISION_BROWSER_CONFIG_FILE")
    if profile is not None:
        common["env"]["DECISION_BROWSER_CONFIG_FILE"] = str(validate_profile_path(profile))
    if client == "codex":
        common.update(cwd=str(project), env_vars=ENV_NAMES, startup_timeout_sec=30,
                      tool_timeout_sec=180)
    else:
        common["type"] = "stdio"
    return common


def codex_block(config: dict[str, Any], *, legacy: bool = False) -> str:
    def value_text(value: Any) -> str:
        text = json.dumps(value, ensure_ascii=legacy)
        # JSON surrogate pairs are not TOML Unicode escapes. Retain the old
        # BMP spelling for matching while using TOML's eight-digit scalar form.
        return re.sub(r"\\u(d[89ab][0-9a-f]{2})\\u(d[cdef][0-9a-f]{2})",
                      lambda match: "\\U" + f"{0x10000 + ((int(match[1], 16) - 0xD800) << 10) + int(match[2], 16) - 0xDC00:08x}",
                      text)

    lines = [BEGIN.rstrip(), f"[mcp_servers.{SERVER}]"]
    for key, value in config.items():
        if key != "env":
            lines.append(f"{key} = {value_text(value)}")
    lines.append(f"[mcp_servers.{SERVER}.env]")
    lines.extend(f"{key} = {value_text(value)}" for key, value in config["env"].items())
    lines.append(END.rstrip())
    return ("\n".join(lines) + "\n").replace("\x7f", "\\u007f")


PROVIDER_SETTINGS = frozenset(name for name in ENV_NAMES if not name.endswith("API_KEY"))


def owned_config(config: dict[str, Any]) -> dict[str, Any]:
    """Compare installer-owned fields while allowing named nonsecret preferences."""
    env = config.get("env", {})
    if not isinstance(env, dict):
        raise InstallError("Invalid provider environment.")
    for key, value in env.items():
        if key in PROVIDER_SETTINGS and (not isinstance(value, str) or len(value) > 4096):
            raise InstallError("Invalid provider setting.")
        if key == "DECISION_BROWSER_CONFIG_FILE":
            validate_profile_path(value)
        if key in PROVIDER_SETTINGS and key.endswith("ENDPOINT"):
            from urllib.parse import urlsplit
            endpoint = urlsplit(value)
            if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
                raise InstallError("Provider endpoint must not contain credentials, query or fragment.")
    result = {**config, "env": {k: v for k, v in env.items() if k not in PROVIDER_SETTINGS}}
    if "env_vars" in result:
        names = result["env_vars"]
        if not isinstance(names, list) or any(not isinstance(n, str) or n not in ENV_NAMES for n in names):
            raise InstallError("Only named provider environment variables may be forwarded.")
        result.pop("env_vars")
    return result


def config_change(client: str, data: bytes | None, expected: dict[str, Any] | None,
                  desired: dict[str, Any] | None) -> bytes | None:
    text = (data or b"").decode()
    parsed = tomllib.loads(text) if client == "codex" else json.loads(text or "{}")
    key = "mcp_servers" if client == "codex" else "mcpServers"
    servers = parsed.get(key, {})
    if not isinstance(servers, dict):
        raise InstallError("Invalid MCP server table.")
    current = servers.get(SERVER)
    if expected is None:
        matches = SERVER not in servers
    else:
        matches = SERVER not in servers or (
            isinstance(current, dict) and owned_config(current) == owned_config(expected)
        )
    if not matches:
        raise InstallError("Conflicting or edited decision_browser server; no overwrite performed.")
    if SERVER not in servers and desired is None:
        return data
    if desired is not None and isinstance(current, dict):
        desired = {**desired, "env": {**desired["env"], **{
            k: v for k, v in current.get("env", {}).items() if k in PROVIDER_SETTINGS}}}
        if "env_vars" in current:
            desired["env_vars"] = current["env_vars"]
        else:
            desired.pop("env_vars", None)
    if client == "codex":
        if isinstance(current, dict):
            block = codex_block(current)
            if text.count(block) != 1:
                block = codex_block(current, legacy=True)
            if text.count(block) != 1:
                if text.count(BEGIN) != 1 or text.count(END) != 1:
                    raise InstallError("Managed Codex block was edited; refusing overwrite.")
                start, end = text.index(BEGIN), text.index(END) + len(END)
                block = text[start:end]
                if tomllib.loads(block) != {"mcp_servers": {SERVER: current}}:
                    raise InstallError("Managed Codex block contains unrelated settings.")
            text = text.replace(block, "", 1)
        elif BEGIN in text or END in text:
            raise InstallError("Conflicting Codex installation markers.")
        if desired is not None:
            text += ("\n" if text and not text.endswith("\n") else "") + codex_block(desired)
        # Verify appended tables do not change other TOML settings.
        check = tomllib.loads(text)
        before = {**parsed, key: {k: v for k, v in servers.items() if k != SERVER}}
        after = {**check, key: {k: v for k, v in check.get(key, {}).items() if k != SERVER}}
        if before != after:
            raise InstallError("Cannot preserve existing Codex configuration.")
        return text.encode()
    if desired is None:
        servers.pop(SERVER, None)
    else:
        servers[SERVER] = desired
    parsed[key] = servers
    return (json.dumps(parsed, indent=2, ensure_ascii=True) + "\n").encode()


def plan(source: Path, project: Path, clients: list[str], runtime: Path,
         state: dict[str, Any], uninstall: bool) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    changes: dict[str, bytes | None] = {}
    records = dict(state.get("clients", {}))
    for client in clients:
        previous = records.get(client, {})
        config = None if uninstall else configuration(client, project, runtime)
        config_path = ".codex/config.toml" if client == "codex" else ".mcp.json"
        if uninstall and not previous:
            continue
        changes[config_path] = config_change(client, read(safe_path(project, config_path)),
                                              previous.get("config"), config)
        skill_root = (".agents" if client == "codex" else ".claude") + "/skills/decision-browser"
        root = safe_path(project, skill_root)
        recorded_files = previous.get("files", {})
        # Migrate older Windows manifests that mixed slash conventions.
        old_files = {name.replace("\\", "/"): value for name, value in recorded_files.items()}
        if len(old_files) != len(recorded_files):
            raise InstallError("Ambiguous managed skill paths.")
        actual = {p.relative_to(project).as_posix(): digest(p.read_bytes()) for p in root.rglob("*")
                  if p.is_file() and not p.is_symlink()}
        if root.exists() and not previous:
            raise InstallError("Conflicting decision-browser skill directory; no overwrite performed.")
        if (any(p.is_symlink() for p in root.rglob("*"))
                or any(old_files.get(name) != value for name, value in actual.items())):
            raise InstallError("Managed skill was edited; refusing overwrite.")
        new_files = {}
        if not uninstall:
            for path in sorted((source / "skills/decision-browser").rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    relative = skill_root + "/" + path.relative_to(source / "skills/decision-browser").as_posix()
                    safe_path(project, relative)
                    data = path.read_bytes()
                    changes[relative] = data
                    new_files[relative] = digest(data)
        for relative in old_files.keys() - new_files.keys():
            safe_path(project, relative)
            if not relative.startswith(skill_root + "/"):
                raise InstallError("Invalid managed skill record.")
            changes[relative] = None
        if uninstall:
            records.pop(client, None)
        else:
            records[client] = {"config": config, "files": new_files}
    return changes, {"schema": 1, "project": str(project), "clients": records}


def commit(project: Path, changes: dict[str, bytes | None], originals: dict[str, bytes | None]) -> None:
    for relative in changes:
        if read(safe_path(project, relative)) != originals[relative]:
            raise InstallError("Target changed during setup; retry after reviewing concurrent edits.")
    applied: list[str] = []
    new_directories = {parent for name in changes for parent in (project / name).parents
                       if parent != project and project in parent.parents and not parent.exists()}
    try:
        for relative, data in changes.items():
            path = safe_path(project, relative)
            if read(path) != originals[relative]:
                raise InstallError("Target changed during commit; preserving the concurrent edit.")
            if data == originals[relative]:
                continue
            applied.append(relative)
            if data is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, data)
    except BaseException:
        for relative in reversed(applied):
            path = safe_path(project, relative)
            # Do not undo another process's edit during rollback.
            if read(path) != changes[relative]:
                continue
            original = originals[relative]
            if original is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, original)
        for directory in sorted(new_directories, key=lambda p: len(p.parts), reverse=True):
            if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
                directory.rmdir()
        raise


def require_terminal() -> None:
    if not sys.stdin.isatty():
        raise InstallError("Interactive setup requires a terminal. Use install with an explicit client for unattended installation.")


def select_client() -> str:
    require_terminal()
    print("Choose your coding client:\n  1. Codex\n  2. Claude Code\n  3. Both")
    selected = input("Client [1/2/3]: ").strip().lower()
    client = {"1": "codex", "2": "claude", "3": "both"}.get(selected, selected)
    if client not in {"codex", "claude", "both"}:
        raise InstallError("Choose 1 (Codex), 2 (Claude Code), or 3 (both); rerun setup.")
    return client


def bounded_read(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("not_regular")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("too_large")
    return data


def profile_provider(path: Path) -> str | None:
    try:
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            return None
        data = json.loads(bounded_read(path, 16_384))
        name = data.get("provider") if isinstance(data, dict) else None
        return name if name in {"openai", "typesafe", "openrouter", "opencode", "gemini", "local"} else None
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def warn_project_overrides(project: Path, clients: list[str]) -> None:
    """Name effective project overrides; never emit settings or credential values."""
    profile_name = "DECISION_BROWSER_CONFIG_FILE"
    wizard_profile = (validate_profile_path(os.environ[profile_name]) if profile_name in os.environ
                      else Path.home() / ".config/decision-browser/provider.json")
    keys = {"openai": "OPENAI_API_KEY", "typesafe": "TYPESAFE_API_KEY",
            "openrouter": "OPENROUTER_API_KEY", "opencode": "OPENCODE_API_KEY",
            "gemini": "OPENCODE_API_KEY"}
    names = {"DECISION_BROWSER_PROVIDER", "DECISION_BROWSER_ENDPOINT", "DECISION_BROWSER_MODEL",
             "DECISION_BROWSER_MAX_CHOICES", "DECISION_BROWSER_API_KEY"}
    for client in clients:
        filename, table = ((".codex/config.toml", "mcp_servers") if client == "codex"
                           else (".mcp.json", "mcpServers"))
        try:
            raw = bounded_read(safe_path(project, filename), 1_048_576)
            parsed = tomllib.loads(raw.decode()) if client == "codex" else json.loads(raw)
            env = parsed.get(table, {}).get(SERVER, {}).get("env", {})
            if not isinstance(env, dict):
                raise TypeError("invalid_environment")
            selected_profile = (validate_profile_path(env[profile_name]) if profile_name in env
                                else wizard_profile)
            provider = env.get("DECISION_BROWSER_PROVIDER", os.environ.get("DECISION_BROWSER_PROVIDER"))
            if provider is None:
                provider = ("openai" if "DECISION_BROWSER_ENDPOINT" in env
                            or "DECISION_BROWSER_ENDPOINT" in os.environ
                            else profile_provider(selected_profile) or "openai")
            if not isinstance(provider, str):
                raise TypeError("invalid_provider")
            provider = {"jev": "typesafe", "nimble": "local"}.get(provider, provider)
            overridden = names.intersection(env)
            key = keys.get(provider)
            if key is not None and key in env:
                overridden.add(key)
            if selected_profile != wizard_profile:
                overridden.add(profile_name)
            if overridden:
                print(f"Warning: {client} project settings ({filename}) override the saved profile: "
                      + ", ".join(sorted(overridden)) + ". The saved profile will not replace these "
                      "project overrides. Remove those entries from this client's decision_browser env "
                      "settings to use the saved selection, then restart the client.", file=sys.stderr)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, TypeError, AttributeError, RecursionError, InstallError):
            print(f"Warning: could not inspect {client} project provider overrides; "
                  f"review its decision_browser env settings in {filename} before use.", file=sys.stderr)


def configure_runtime(runtime: Path, project: Path, clients: list[str] | None = None) -> int:
    python = runtime / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    command = [str(python), "-I", "-m", "decision_browser.cli", "configure"]
    print("Choose a provider to finish setup.", flush=True)
    try:
        code = subprocess.run(command, cwd=project, check=False, shell=False).returncode
        if code < 0:
            code = 130 if code == -2 else 1
    except KeyboardInterrupt:
        code = 130
    except OSError:
        code = 1
    warn_project_overrides(project, clients if clients is not None else ["codex", "claude"])
    if code:
        resume = subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
        print("Installation retained; provider setup is unfinished. Resume with:\n  " + resume,
              file=sys.stderr)
    return code


def installation_state(path: Path, project: Path) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("duplicate")
            result[name] = value
        return result

    try:
        raw = bounded_read(path, _MANIFEST_LIMIT)
    except FileNotFoundError:
        return {"clients": {}}
    except (OSError, ValueError, UnicodeError):
        raise InstallError("Invalid installation metadata; preserve it and inspect the managed manifest.") from None
    try:
        data = json.loads(raw, object_pairs_hook=unique)
        if (type(data) is not dict or set(data) != {"schema", "project", "clients", "source_id"}
                or type(data["schema"]) is not int or data["schema"] != 1
                or type(data["project"]) is not str or len(data["project"]) > 4096
                or type(data["source_id"]) is not str or not re.fullmatch(r"[0-9a-f]{20}", data["source_id"])
                or type(data["clients"]) is not dict or set(data["clients"]) - {"codex", "claude"}):
            raise ValueError("schema")
        for client, record in data["clients"].items():
            if (type(record) is not dict or set(record) != {"config", "files"}
                    or type(record["config"]) is not dict or type(record["files"]) is not dict
                    or len(record["files"]) > 4096):
                raise ValueError("record")
            config = record["config"]
            if (set(config) - {"command", "args", "env", "cwd", "env_vars", "type",
                              "startup_timeout_sec", "tool_timeout_sec"}
                    or type(config.get("command")) is not str or not config["command"]
                    or len(config["command"]) > 4096 or type(config.get("args")) is not list
                    or len(config["args"]) > 64 or any(type(x) is not str or len(x) > 4096 for x in config["args"])
                    or type(config.get("env")) is not dict or len(config["env"]) > 64
                    or any(type(k) is not str or type(v) is not str or len(k) > 256 or len(v) > 4096
                           for k, v in config["env"].items())):
                raise ValueError("config")
            if ("type" in config and config["type"] != "stdio"
                    or "cwd" in config and (type(config["cwd"]) is not str or len(config["cwd"]) > 4096)
                    or any(type(config[k]) is not int or not 0 < config[k] <= 3600
                           for k in ["startup_timeout_sec", "tool_timeout_sec"] if k in config)):
                raise ValueError("config")
            owned_config(config)
            prefix = (".agents" if client == "codex" else ".claude") + "/skills/decision-browser/"
            for name, checksum in record["files"].items():
                if (type(name) is not str or len(name) > 4096 or not name.replace("\\", "/").startswith(prefix)
                        or type(checksum) is not str or not re.fullmatch(r"[0-9a-f]{64}", checksum)):
                    raise ValueError("file")
                safe_path(project, name.replace("\\", "/"))
        return data
    except (ValueError, TypeError, UnicodeError, RecursionError, InstallError):
        raise InstallError("Invalid installation metadata; preserve it and inspect the managed manifest.") from None


def install(source: Path, project: Path, clients: list[str], upgrade: bool = False,
            uninstall: bool = False, *, setup: bool = False) -> int:
    if setup:
        require_terminal()
        if uninstall:
            raise InstallError("Interactive setup cannot uninstall a project.")
    if sys.version_info < (3, 11):  # noqa: UP036 - standalone bootstrap guard
        raise InstallError("Python 3.11+ is required; no Python is installed automatically.")
    if not project.is_dir() or project.is_symlink():
        raise InstallError("Target must be an existing regular project directory.")
    project = project.resolve()
    source = source.resolve()
    if "DECISION_BROWSER_CONFIG_FILE" in os.environ:
        validate_profile_path(os.environ["DECISION_BROWSER_CONFIG_FILE"])
    uv = shutil.which("uv")
    if not uninstall:
        if not uv:
            raise InstallError("uv is required; install it from https://docs.astral.sh/uv/ first.")
        check_uv(uv)
        for client in clients:
            if not shutil.which("claude" if client == "claude" else "codex"):
                raise InstallError(f"Missing {client} CLI on PATH; install the official client first.")
    with held_lock(project, ".decision-browser-install.lease") as validate_lease:
        ensure_no_residual(project)
        managed = safe_path(project, STATE)
        marker = safe_path(project, STATE + "/owner.json")
        manifest = safe_path(project, STATE + "/manifest.json")
        if managed.exists() and read(marker) != b'{"owner":"decision-browser-installer-v1"}\n':
            raise InstallError("Unrecognized runtime directory; refusing to use it.")
        state = installation_state(manifest, project)
        if state.get("project", str(project)) != str(project):
            raise InstallError("Installation was moved; restore its original path before uninstalling.")
        if uninstall and not managed.exists():
            print("No managed installation found.")
            return 0
        legacy_lock = safe_path(project, STATE + "/install.lock")
        if legacy_lock.exists() or legacy_lock.is_symlink():
            raise InstallError("Unknown legacy install.lock; inspect it before retrying. It was not removed.")
        fingerprint = source_id(source) if not uninstall else state.get("source_id", "unused")
        if state.get("source_id", fingerprint) != fingerprint and state.get("clients") and not upgrade and not uninstall:
            raise InstallError("Checkout changed; review it and rerun with --upgrade (all installed clients).")
        if upgrade and not set(state.get("clients", {})).issubset(clients):
            raise InstallError("Upgrade must include all previously installed clients; use --client both.")
        # Never run uv sync against a referenced environment, even on repeat.
        # Prepare at its permanent unique path (venv scripts are not relocatable).
        # Only switch client registrations after all probes succeed. Interrupted
        # attempts may leave an orphan, but cannot invalidate the active runtime.
        runtime = safe_path(project, STATE + "/runtimes/" + fingerprint + "-" + uuid.uuid4().hex)
        validate_lease()
        changes, next_state = plan(source, project, clients, runtime, state, uninstall)
        next_state["source_id"] = fingerprint
        payload = (json.dumps(next_state, indent=2) + "\n").encode()
        if len(payload) > _MANIFEST_LIMIT:
            raise InstallError("Invalid installation metadata; managed manifest exceeds its bound.")
        changes[STATE + "/manifest.json"] = payload
        originals = {name: read(safe_path(project, name)) for name in changes}
        if not managed.exists():
            managed.mkdir(mode=0o700)
            atomic_write(marker, b'{"owner":"decision-browser-installer-v1"}\n')
        if not uninstall:
            assert uv is not None
            prepare(source, project, runtime, uv)
            if source_id(source) != fingerprint:
                raise InstallError("Source changed during installation; retry with a stable checkout.")
        validate_lease()
        commit(project, changes, originals)
        validate_lease()
        if uninstall:
            for client in clients:
                folder = project / (".agents" if client == "codex" else ".claude") / "skills/decision-browser"
                if folder.exists():
                    for directory in sorted((p for p in folder.rglob("*") if p.is_dir()), reverse=True):
                        if not any(directory.iterdir()):
                            directory.rmdir()
                    if not any(folder.iterdir()):
                        folder.rmdir()
            print("Managed registrations removed. Runtime/cache retained under .decision-browser-install; "
                  "remove that directory manually only after all clients are uninstalled.")
        else:
            print("Local package, sandboxed Chromium and project registrations: READY.\n"
                  "Restart the selected clients in this project and review their trust/MCP prompts.\n"
                  "API credentials, provider entitlement, native admission and client end-to-end: NOT_RUN.\n"
                  "Use decision-browser configure to choose a provider, or inherit provider credentials.\n"
                  "Keep .decision-browser-install out of version control; configs contain local paths.")
    return configure_runtime(runtime, project, clients) if setup else 0



def packaged_wheel(destination: Path) -> None:
    """Repack the installed, hash-checked distribution without a build backend.

    Installer-generated scripts and metadata are excluded. RECORD is regenerated
    for the portable wheel rather than retaining installation-relative paths.
    """
    import base64
    import csv
    import io
    from importlib.metadata import distribution
    from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

    dist = distribution("decision-browser")
    files = dist.files
    if files is None:
        raise InstallError("Installed package has no RECORD; use the reviewed archive.")
    payload = {}
    metadata_dir = None
    for entry in files:
        name = str(entry)
        parts = Path(name).parts
        if not parts or parts[0] not in ("decision_browser", f"decision_browser-{dist.version}.dist-info"):
            continue
        if ".." in parts or Path(name).is_absolute() or "\\" in name:
            raise InstallError("Invalid installed package path.")
        if name.endswith((".pyc", ".pyo")) or "__pycache__" in parts:
            continue
        if parts[0].endswith(".dist-info"):
            metadata_dir = parts[0]
            if parts[-1] in {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json", "uv_cache.json"}:
                continue
        installed_path = dist.locate_file(entry)
        if not isinstance(installed_path, os.PathLike):
            raise InstallError("Installed package files must be on disk; use the reviewed archive.")
        data = Path(installed_path).read_bytes()
        if entry.hash is None or entry.hash.mode != "sha256":
            raise InstallError("Installed package lacks a SHA-256 record; use the reviewed archive.")
        actual = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        if actual != entry.hash.value:
            raise InstallError("Installed package was modified; use the reviewed archive.")
        payload[name] = data
    if metadata_dir is None or "decision_browser/install.py" not in payload:
        raise InstallError("Incomplete installed package; use the reviewed archive.")
    wheel_metadata = payload.get(metadata_dir + "/WHEEL", b"").decode()
    if "Tag: py3-none-any" not in wheel_metadata:
        raise InstallError("Unsupported installed wheel platform; use the reviewed archive.")
    record = io.StringIO(newline="")
    writer = csv.writer(record)
    for name, data in sorted(payload.items()):
        digest_value = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow([name, "sha256=" + digest_value, len(data)])
    writer.writerow([metadata_dir + "/RECORD", "", ""])
    payload[metadata_dir + "/RECORD"] = record.getvalue().encode()
    destination.mkdir()
    with ZipFile(destination / f"decision_browser-{dist.version}-py3-none-any.whl", "x", ZIP_DEFLATED) as archive:
        for name, data in sorted(payload.items()):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client", nargs="?", choices=("codex", "claude", "both"),
                        help="coding client to configure")
    parser.add_argument("--client", dest="legacy_client", choices=("codex", "claude", "both"),
                        help="alias for the positional client")
    parser.add_argument("--project", type=Path, default=Path("."),
                        help="existing project (default: current directory)")
    parser.add_argument("--source", type=Path, default=None,
                        help="Optional reviewed checkout override; packaged inputs are the default")
    parser.add_argument("--setup", action="store_true",
                        help="interactively choose a client and configure a provider after installation")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--upgrade", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    args = parser.parse_args(argv)
    if args.client is not None and args.legacy_client is not None:
        parser.error("Choose a positional client or --client, not both.")
    if args.setup and args.uninstall:
        parser.error("Interactive setup cannot be combined with --uninstall.")
    client = args.client or args.legacy_client
    if client is None and not args.setup:
        parser.error("Choose codex, claude, or both.")
    try:
        if args.setup:
            require_terminal()
            if client is None:
                client = select_client()
        assert client is not None
        with ExitStack() as stack:
            source = args.source
            if source is None:
                bundled = Path(__file__).resolve().parent / "bootstrap"
                if bundled.is_dir():
                    # uv sync may replace this very installation. Copy build inputs
                    # before touching any runtime; never depend on a tool cache.
                    source = Path(stack.enter_context(tempfile.TemporaryDirectory(
                        prefix="decision-browser-package-")))
                    shutil.copytree(bundled, source, dirs_exist_ok=True)
                    if not args.uninstall:
                        packaged_wheel(source / "prebuilt")
                else:
                    source = Path(__file__).resolve().parents[1]
            options = {"setup": True} if args.setup else {}
            return install(source, args.project,
                           ["codex", "claude"] if client == "both" else [client],
                           args.upgrade, args.uninstall, **options) or 0
    except (KeyboardInterrupt, EOFError):
        print("Setup cancelled.", file=sys.stderr)
        return 130
    except InstallError as exc:
        print(f"Setup stopped: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - sanitized CLI boundary
        print(f"Setup stopped: {type(exc).__name__}; no exception details printed for privacy.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
