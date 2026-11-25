"""Read-only evidence for conservative recovery of an installer lease."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

STATE = ".decision-browser-install"
_LIMIT = 100_000
_BYTE_LIMIT = 2 * 1024 * 1024 * 1024
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")


class EvidenceError(Exception):
    """Only a fixed code may cross the diagnostic boundary."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _capture(arguments: list[str]) -> tuple[bytes, int]:
    try:
        with subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env={}) as child:
            try:
                output, error = child.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
                raise EvidenceError("inspection_unavailable") from None
    except (OSError, subprocess.TimeoutExpired):
        raise EvidenceError("inspection_unavailable") from None
    if child.returncode or error or len(output) > 64 * 1024 * 1024:
        raise EvidenceError("inspection_incomplete")
    return output, child.pid


def _run(arguments: list[str]) -> bytes:
    return _capture(arguments)[0]


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def boot_identity() -> dict[str, str]:
    """Bind a witness to the same machine and a distinct later kernel boot."""
    try:
        if sys.platform == "darwin":
            boot = _run(["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"]).decode().strip()
            platform = _run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"])
            match = re.search(rb'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]+)"', platform)
            host = match.group(1).decode() if match else ""
            if not _UUID.fullmatch(host):
                raise EvidenceError("boot_identity_unavailable")
        elif sys.platform == "linux":
            # PID_NS_INIT_INO and USER_NS_INIT_INO in Linux UAPI linux/nsfs.h.
            # A container's /proc/1 can be the init of a nested namespace.
            if (str(Path("/proc/self/ns/pid").readlink()) != "pid:[4026531836]"
                    or str(Path("/proc/self/ns/user").readlink()) != "user:[4026531837]"
                    or Path("/proc/self/uid_map").read_text().split() != ["0", "0", "4294967295"]):
                raise EvidenceError("process_scope_unavailable")
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            machine = Path("/etc/machine-id")
            info = machine.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise EvidenceError("boot_identity_unavailable")
            host = machine.read_text().strip()
            if not re.fullmatch(r"[0-9a-f]{32}", host):
                raise EvidenceError("boot_identity_unavailable")
        else:
            raise EvidenceError("platform_inspection_unavailable")
        if not _UUID.fullmatch(boot):
            raise EvidenceError("boot_identity_unavailable")
        return {"host": _digest(host.lower().encode()), "boot": _digest(boot.lower().encode())}
    except (OSError, UnicodeError):
        raise EvidenceError("boot_identity_unavailable") from None


def _local_filesystem(project: Path) -> None:
    """Shared or unrecognized filesystems cannot support a host boot proof."""
    if sys.platform == "darwin":
        candidates: list[tuple[int, str, list[str]]] = []
        for line in _run(["/sbin/mount"]).decode().splitlines():
            if " on " not in line or " (" not in line or not line.endswith(")"):
                raise EvidenceError("filesystem_unverified")
            source, rest = line.split(" on ", 1)
            mounted, flags = rest.rsplit(" (", 1)
            target = Path(mounted)
            if target.is_relative_to(project / STATE):
                raise EvidenceError("filesystem_unverified")
            if project.is_relative_to(target):
                candidates.append((len(target.parts), source, flags[:-1].split(", ")))
        if not candidates:
            raise EvidenceError("filesystem_unverified")
        _, source, mount_flags = max(candidates)
        if not source.startswith("/dev/disk") or "local" not in mount_flags or mount_flags[0] not in {"apfs", "hfs"}:
            raise EvidenceError("filesystem_unverified")
    elif sys.platform == "linux":
        linux_mounts: list[tuple[int, str, str]] = []
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            before, after = line.split(" - ", 1)
            fields = before.split()
            mounted = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])
            target = Path(mounted)
            if target.is_relative_to(project / STATE):
                raise EvidenceError("filesystem_unverified")
            proc_options = before.split()[5] + "," + after.split()[2]
            if target == Path("/proc") and any(option.startswith("hidepid=") and option != "hidepid=0"
                                                for option in proc_options.split(",")):
                raise EvidenceError("process_scope_unavailable")
            if project.is_relative_to(target):
                linux_mounts.append((len(target.parts), after.split()[0], fields[3]))
        if not linux_mounts:
            raise EvidenceError("filesystem_unverified")
        _, filesystem, root = max(linux_mounts)
        if filesystem not in {"ext2", "ext3", "ext4", "xfs", "btrfs", "tmpfs"} or root != "/":
            raise EvidenceError("filesystem_unverified")
    else:
        raise EvidenceError("platform_inspection_unavailable")


def _read_regular(path: Path, limit: int) -> tuple[bytes, os.stat_result]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise EvidenceError("unsafe_retained_state")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            data = stream.read(limit + 1)
        after = os.fstat(descriptor)
        if len(data) > limit or _metadata(info) != _metadata(after) or _metadata(path.lstat()) != _metadata(after):
            raise EvidenceError("retained_state_changed")
        return data, info
    finally:
        os.close(descriptor)


def _metadata(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _file_digest(path: Path, limit: int) -> tuple[str, os.stat_result]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise EvidenceError("unsafe_retained_state")
        digest = hashlib.sha256()
        total = 0
        while block := stream.read(1024 * 1024):
            total += len(block)
            if total > limit:
                raise EvidenceError("retained_state_too_large")
            digest.update(block)
        if (_metadata(info) != _metadata(os.fstat(stream.fileno()))
                or _metadata(info) != _metadata(path.lstat())):
            raise EvidenceError("retained_state_changed")
        return digest.hexdigest(), info


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _remote_server(server: object) -> bool:
    """An explicit HTTP MCP transport has no local command reference graph."""
    if not isinstance(server, dict) or not set(server).issubset({
        "url", "type", "http_headers", "env_http_headers", "bearer_token_env_var",
        "enabled", "required", "startup_timeout_sec", "tool_timeout_sec", "enabled_tools", "disabled_tools",
    }):
        return False
    if not isinstance(server.get("url"), str) or server.get("type", "http") not in {"http", "sse"}:
        return False
    try:
        target = urlsplit(server["url"])
        return target.scheme in {"http", "https"} and bool(target.hostname)
    except ValueError:
        return False


def _references(project: Path) -> list[list[object]]:
    import tomllib

    result: list[list[object]] = []
    for relative in (".codex/config.toml", ".mcp.json", STATE + "/manifest.json"):
        path = project / relative
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != project):
            raise EvidenceError("unsafe_registration")
        if not path.exists():
            result.append([relative, None])
            continue
        data, info = _read_regular(path, 1024 * 1024)
        if info.st_uid != os.getuid() or info.st_mode & 0o022 or info.st_nlink != 1:
            raise EvidenceError("unsafe_registration")
        if relative.endswith("manifest.json"):
            raise EvidenceError("runtime_referenced")
        try:
            parsed = (tomllib.loads(data.decode()) if relative.endswith(".toml")
                      else json.loads(data, object_pairs_hook=_unique))
            if not isinstance(parsed, dict):
                raise TypeError
            servers = parsed.get("mcp_servers" if relative.endswith(".toml") else "mcpServers", {})
            if not isinstance(servers, dict):
                raise TypeError
            # An existing arbitrary MCP command can invoke a wrapper which
            # refers to retained resources indirectly. Initial recovery cannot
            # prove that command's reference graph without executing it.
            if (any(not _remote_server(server) for server in servers.values())
                    or STATE in unquote(json.dumps(parsed))):
                raise EvidenceError("runtime_referenced")
        except (ValueError, TypeError, UnicodeError):
            raise EvidenceError("registration_unreadable") from None
        result.append([relative, _metadata(info), _digest(data)])
    return result


def _inventory(project: Path, *, content: bool = True) -> tuple[list[list[object]], set[tuple[int, int]]]:
    root = project / STATE
    root_info = root.lstat()
    if (not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.getuid()
            or root_info.st_mode & 0o077 or root_info.st_dev != project.stat().st_dev):
        raise EvidenceError("unsafe_retained_state")
    if _read_regular(root / "owner.json", 256)[0] != b'{"owner":"decision-browser-installer-v1"}\n':
        raise EvidenceError("unrecognized_retained_state")
    rows: list[list[object]] = []
    identities: set[tuple[int, int]] = set()
    pending = [root]
    total = 0
    while pending:
        path = pending.pop()
        info = path.lstat()
        if info.st_uid != os.getuid() or info.st_dev != root_info.st_dev:
            raise EvidenceError("unsafe_retained_state")
        relative = path.relative_to(project).as_posix()
        if path.name in {"install.lock", "prepare.lock"}:
            raise EvidenceError("legacy_lock_present")
        if stat.S_ISLNK(info.st_mode):
            # uv's interpreter links are the sole intentional links in a venv.
            if (not re.fullmatch(r"python(?:3(?:\.\d+)?)?", path.name)
                    or path.parent.name != "bin" or path.parent.parent.name != "venv"):
                raise EvidenceError("unsafe_retained_state")
            target = path.resolve(strict=True)
            target_info = target.stat()
            if (target.is_relative_to(root) or not stat.S_ISREG(target_info.st_mode)
                    or target_info.st_uid not in {0, os.getuid()} or target_info.st_mode & 0o022):
                raise EvidenceError("unsafe_retained_state")
            rows.append([relative, _metadata(info), "symlink", _digest(os.readlink(path).encode())])
        elif stat.S_ISDIR(info.st_mode):
            if info.st_mode & 0o022:
                raise EvidenceError("unsafe_retained_state")
            identities.add((info.st_dev, info.st_ino))
            rows.append([relative, _metadata(info), "directory"])
            pending.extend(sorted(path.iterdir(), reverse=True))
        elif stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1 or (info.st_mode & 0o022 and path.name != ".lock"):
                raise EvidenceError("aliased_retained_state")
            digest = None
            if content:
                digest, info = _file_digest(path, min(_BYTE_LIMIT - total, 512 * 1024 * 1024))
            total += info.st_size
            identities.add((info.st_dev, info.st_ino))
            rows.append([relative, _metadata(info), digest])
        else:
            raise EvidenceError("unsafe_retained_state")
        if len(rows) > _LIMIT or total > _BYTE_LIMIT:
            raise EvidenceError("retained_state_too_large")
    return rows, identities


def snapshot(project: Path) -> dict[str, object]:
    """Return only digests and identities. Never publish configuration bytes."""
    try:
        if project != project.resolve(strict=True):
            raise EvidenceError("unsafe_project")
        project_info = project.stat()
        if project_info.st_uid != os.getuid() or project_info.st_mode & 0o022:
            raise EvidenceError("unsafe_project")
        _local_filesystem(project)
        stage_lease = project / ".decision-browser-stage.lease"
        if stage_lease.exists() or stage_lease.is_symlink():
            raise EvidenceError("stage_lease_present")
        references = _references(project)
        rows, _ = _inventory(project)
        identity = project.stat()
        return {"project": [_digest(os.fsencode(project)), identity.st_dev, identity.st_ino, identity.st_uid],
                "references": _digest(json.dumps(references, sort_keys=True).encode()),
                "inventory": _digest(json.dumps(rows, sort_keys=True).encode())}
    except (OSError, ValueError, UnicodeError):
        raise EvidenceError("retained_state_unreadable") from None


def _mentioned(command: bytes, project: Path, cwd: Path) -> bool:
    # Relative runtime arguments matter even when the executable is a shared
    # Python interpreter and has already closed its package source files.
    retained = project / STATE
    if os.fsencode(retained) in command:
        return True
    if STATE.encode() not in command:
        return False
    if cwd.is_relative_to(project):
        return True
    try:
        arguments = (os.fsdecode(command).split("\0") if b"\0" in command
                     else shlex.split(os.fsdecode(command)))
        for argument in arguments:
            if STATE not in argument:
                continue
            value = argument.split("=", 1)[-1]
            path = Path(os.path.normpath(cwd / value))
            if path == retained or path.is_relative_to(retained):
                return True
    except ValueError:
        raise EvidenceError("inspection_incomplete") from None
    return False


def _processes() -> set[int]:
    output, inspector = _capture(["/bin/ps", "-axo", "uid=,pid="])
    processes = set()
    for row in output.splitlines():
        fields = row.split()
        if len(fields) != 2 or not re.fullmatch(rb"-?\d+", fields[0]) or not fields[1].isdigit():
            raise EvidenceError("inspection_incomplete")
        if int(fields[0]) == os.getuid():
            processes.add(int(fields[1]))
    return processes - {os.getpid(), inspector}


def _darwin_unused(project: Path, identities: set[tuple[int, int]]) -> None:
    uid = os.getuid()
    expected = _processes()
    output = _run(["/usr/sbin/lsof", "-nP", "-a", "-u", str(uid), "-F0pufDtin"])
    observed, pid, device, inode, path, descriptor, kind = set(), None, None, None, b"", b"", b""
    working: dict[int, Path] = {}

    def check_file() -> None:
        if pid == os.getpid():
            return
        if device is not None and inode is not None and (device, inode) in identities:
            raise EvidenceError("retained_resources_in_use")
        if path.startswith(os.fsencode(project / STATE) + b"/") or path == os.fsencode(project / STATE):
            raise EvidenceError("retained_resources_in_use")
        if (descriptor in {b"cwd", b"txt"} or kind in {b"REG", b"DIR"}) and (device is None or inode is None):
            raise EvidenceError("inspection_incomplete")
        if descriptor == b"cwd" and pid is not None:
            working[pid] = Path(os.fsdecode(path))

    for field in output.split(b"\0"):
        field = field.removeprefix(b"\n")
        if not field:
            continue
        tag, value = field[:1], field[1:]
        if tag in {b"p", b"f"}:
            check_file()
            device, inode, path, descriptor, kind = None, None, b"", b"", b""
        if tag == b"p":
            pid = int(value)
            observed.add(pid)
        elif tag == b"f":
            descriptor = value
        elif tag == b"t":
            kind = value
        elif tag == b"D":
            device = int(value, 16)
        elif tag == b"i":
            inode = int(value)
        elif tag == b"n":
            path = value
    check_file()
    alive = _processes()
    if alive - observed or alive - working.keys():
        raise EvidenceError("inspection_incomplete")
    inspected = sorted(expected | alive)
    for start in range(0, len(inspected), 100):
        batch = inspected[start:start + 100]
        if not batch:
            continue
        # ps may lose a process between queries. Its nonzero exit is uncertainty.
        commands = _run(["/bin/ps", "-ww", "-p", ",".join(map(str, batch)), "-o", "pid=,command="])
        for line in commands.splitlines():
            match = re.fullmatch(rb"\s*(\d+)\s+(.*)", line)
            if match is None:
                raise EvidenceError("inspection_incomplete")
            process = int(match[1])
            if process in working and _mentioned(match[2], project, working[process]):
                raise EvidenceError("retained_resources_in_use")


def _linux_processes() -> dict[Path, bytes]:
    processes = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal() or int(entry.name) == os.getpid():
            continue
        try:
            status = (entry / "status").read_bytes()
            user = re.search(rb"^Uid:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$", status, re.MULTILINE)
            if user is None:
                raise EvidenceError("inspection_incomplete")
            if os.getuid() not in {int(value) for value in user.groups()}:
                continue
            # Field 22 is process start time in ticks since boot. The command
            # field in parentheses can itself contain spaces or parentheses.
            fields = (entry / "stat").read_bytes().rsplit(b")", 1)[-1].split()
            if len(fields) < 20 or not fields[19].isdigit():
                raise EvidenceError("inspection_incomplete")
            processes[entry] = fields[19]
        except FileNotFoundError:
            if entry.exists():
                raise EvidenceError("inspection_incomplete") from None
    return processes


def _linux_unused(project: Path, identities: set[tuple[int, int]]) -> None:
    processes = _linux_processes()
    for entry in processes:
        try:
            cwd = (entry / "cwd").resolve(strict=True)
            if _mentioned((entry / "cmdline").read_bytes(), project, cwd):
                raise EvidenceError("retained_resources_in_use")
            resources = [entry / "cwd", entry / "exe", *(entry / "fd").iterdir()]
            for resource in resources:
                try:
                    info = resource.stat()
                except FileNotFoundError:
                    continue
                if (info.st_dev, info.st_ino) in identities:
                    raise EvidenceError("retained_resources_in_use")
            for row in (entry / "maps").read_bytes().splitlines():
                fields = row.split(None, 5)
                if len(fields) < 5 or not re.fullmatch(rb"[0-9a-f]+:[0-9a-f]+", fields[3]) or not fields[4].isdigit():
                    raise EvidenceError("inspection_incomplete")
                major, minor = (int(part, 16) for part in fields[3].split(b":"))
                if (os.makedev(major, minor), int(fields[4])) in identities:
                    raise EvidenceError("retained_resources_in_use")
        except FileNotFoundError:
            if entry.exists():
                raise EvidenceError("inspection_incomplete") from None
        except PermissionError:
            raise EvidenceError("inspection_incomplete") from None
    if any(processes.get(pid) != start for pid, start in _linux_processes().items()):
        raise EvidenceError("inspection_incomplete")


def check_unused(project: Path) -> None:
    """Check cooperative same-user processes protected by the private tree."""
    try:
        _, identities = _inventory(project, content=False)
        if sys.platform == "darwin":
            _darwin_unused(project, identities)
        elif sys.platform == "linux":
            _linux_unused(project, identities)
        else:
            raise EvidenceError("platform_inspection_unavailable")
    except (OSError, ValueError, UnicodeError):
        raise EvidenceError("inspection_incomplete") from None
