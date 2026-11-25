"""Inspect an installation barrier and explicitly recover a verified stopped attempt."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

LEASE = b'{"owner":"decision-browser-lease-v1"}\n'
ACTIVE = b'{"active":"stage-lifecycle-v1"}\n'
POISON = b'{"blocked":"stage-cleanup-unknown"}\n'
LEASE_NAME = ".decision-browser-install.lease"
RESIDUAL = ".decision-browser-stage-cleanup-unknown.json"
STATE = ".decision-browser-install"
MAX_FILE = 2 * 1024 * 1024
ATTEMPT = re.compile(r"[0-9a-f]{32}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MUTATION_STARTED = False


class RecoveryError(Exception):
    """A fixed diagnostic code without filesystem or process details."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


spec = importlib.util.spec_from_file_location(
    "decision_browser_recovery_evidence", Path(__file__).with_name("install_recovery_evidence.py")
)
assert spec is not None and spec.loader is not None
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


def _identity(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_size, info.st_mtime_ns]


def _unique(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryError("invalid_recovery_evidence")
        result[key] = value
    return result


def _json(data: bytes) -> dict:
    try:
        value = json.loads(data, object_pairs_hook=_unique)
    except (ValueError, UnicodeError):
        raise RecoveryError("invalid_recovery_evidence") from None
    if type(value) is not dict:
        raise RecoveryError("invalid_recovery_evidence")
    return value


def _read(directory: int, name: str, limit: int = MAX_FILE) -> tuple[bytes, dict] | None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.getuid()
                or before.st_mode & 0o077 or before.st_size > limit):
            raise RecoveryError("unsafe_recovery_evidence")
        parts = bytearray()
        while len(parts) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(parts)))
            if not chunk:
                break
            parts.extend(chunk)
        after = os.fstat(fd)
        present = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if len(parts) > limit or _identity(before) != _identity(after) or _identity(after) != _identity(present):
            raise RecoveryError("recovery_evidence_changed")
        data = bytes(parts)
        return data, {"identity": _identity(after), "sha256": hashlib.sha256(data).hexdigest()}
    finally:
        os.close(fd)


def _open_project(project: Path) -> int:
    if not project.is_absolute() or ".." in project.parts:
        raise RecoveryError("invalid_project")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(project.anchor, flags)
    try:
        for part in project.parts[1:]:
            following = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = following
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise RecoveryError("unsafe_project")
        return directory
    except BaseException:
        os.close(directory)
        raise


def _validate_project(project: Path, directory: int) -> None:
    checked = _open_project(project)
    try:
        original, current = os.fstat(directory), os.fstat(checked)
        if (original.st_dev, original.st_ino) != (current.st_dev, current.st_ino):
            raise RecoveryError("recovery_evidence_changed")
    finally:
        os.close(checked)


@contextmanager
def _locked_lease(directory: int):
    try:
        fd = os.open(LEASE_NAME, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        yield None
        return
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 128):
            raise RecoveryError("unsafe_lease")
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RecoveryError("active_setup") from None
        yield fd
    finally:
        os.close(fd)


def _lease(directory: int, fd: int) -> tuple[bytes, dict]:
    original = os.fstat(fd)
    record = _read(directory, LEASE_NAME, 128)
    if record is None or record[1]["identity"] != _identity(original):
        raise RecoveryError("recovery_evidence_changed")
    return record


def _residual(data: bytes) -> dict:
    value = _json(data)
    if (set(value) != {"schema", "owner", "attempt", "pid", "new_session", "reason"}
            or type(value["schema"]) is not int or value["schema"] != 1
            or value["owner"] != "decision-browser-installer-stage-v1"
            or type(value["attempt"]) is not str or not ATTEMPT.fullmatch(value["attempt"])
            or value["pid"] is not None and (type(value["pid"]) is not int or value["pid"] <= 0)
            or value["new_session"] is not True
            or value["reason"] not in {"stage_failed_uncertainty", "stage_timeout_uncertainty",
                                      "stage_interrupted_uncertainty", "stage_cleanup_unknown_uncertainty",
                                      "stage_start_failed_uncertainty", "quiescence_unknown"}):
        raise RecoveryError("invalid_residual")
    return value


def _names(attempt: str) -> tuple[str, str]:
    return f".decision-browser-recovery.{attempt}.json", f".decision-browser-recovered.{attempt}.json"


def _pending_residual(directory: int, lease: dict) -> tuple[bytes, dict, bool]:
    current = _read(directory, RESIDUAL)
    if current is not None:
        _residual(current[0])
        return *current, False
    # A recovery interrupted after archiving its receipt still has a poison lease.
    # Resume only one exact archive/witness pair; ambiguity preserves the block.
    names = [name for name in os.listdir(directory) if re.fullmatch(
        r"\.decision-browser-recovered\.[0-9a-f]{32}\.json", name
    )]
    if len(names) > 128:
        raise RecoveryError("ambiguous_recovery_evidence")
    candidates = []
    for name in names:
        archived = _read(directory, name)
        assert archived is not None
        attempt = _residual(archived[0])["attempt"]
        witness = _read(directory, _names(attempt)[0])
        if witness is not None:
            value = _json(witness[0])
            if (name == _names(attempt)[1] and value.get("lease") == lease
                    and value.get("residual", {}).get("sha256") == archived[1]["sha256"]):
                candidates.append(archived)
    if len(candidates) != 1:
        raise RecoveryError("missing_residual")
    return *candidates[0], True


def _boot() -> dict:
    value = evidence.boot_identity()
    if (type(value) is not dict or set(value) != {"host", "boot"}
            or any(type(item) is not str or not DIGEST.fullmatch(item) for item in value.values())):
        raise RecoveryError("boot_identity_unavailable")
    return value


def _anchor_home() -> Path:
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _recovery_key(project: Path, create: bool = False) -> bytes:
    """Authenticate boot evidence outside a potentially untrusted project."""
    global _MUTATION_STARTED
    home = _anchor_home()
    anchor = home / ".local/state/decision-browser/recovery"
    if anchor.is_relative_to(project) or project.is_relative_to(anchor):
        raise RecoveryError("trusted_witness_overlaps_project")
    directory = _open_project(home)
    try:
        if os.fstat(directory).st_mode & 0o022:
            raise RecoveryError("unsafe_trusted_witness")
        for part in (".local", "state", "decision-browser", "recovery"):
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=directory)
                    _MUTATION_STARTED = True
                    os.fsync(directory)
                except FileExistsError:
                    pass
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            info = os.fstat(following)
            if (info.st_uid != os.getuid() or info.st_mode & 0o022
                    or part == "recovery" and info.st_mode & 0o077):
                os.close(following)
                raise RecoveryError("unsafe_trusted_witness")
            os.close(directory)
            directory = following
        key = _read(directory, "key", 32)
        if key is None and create:
            try:
                _write_exclusive(directory, "key", os.urandom(32))
            except FileExistsError:
                pass
            key = _read(directory, "key", 32)
        if key is None or len(key[0]) != 32:
            raise RecoveryError("trusted_witness_unavailable")
        return key[0]
    except FileNotFoundError:
        raise RecoveryError("trusted_witness_unavailable") from None
    finally:
        os.close(directory)


def _signature(value: dict, key: bytes) -> str:
    body = {name: item for name, item in value.items() if name != "mac"}
    return hmac.new(key, json.dumps(body, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()


def _write_exclusive(directory: int, name: str, data: bytes) -> None:
    global _MUTATION_STARTED
    if len(data) > MAX_FILE:
        raise RecoveryError("recovery_evidence_too_large")
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    _MUTATION_STARTED = True
    try:
        if os.write(fd, data) != len(data):
            raise RecoveryError("recovery_write_failed")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(directory)


def _report(status: str, reason: str, recovery: str = "unavailable", changed: bool = False) -> dict:
    actions = {
        "clear": "The installation barrier is clear.",
        "recovered": "The verified barrier was cleared. Retained runtimes are available for inspection.",
        "reboot_witness_required": "Run recover-install --apply to record this barrier for comparison after a reboot.",
        "reboot_required": "Reboot this machine, then inspect install-status and run recover-install --apply again.",
        "recovery_ready": "Stop clients using this project, then run recover-install --apply to recheck and recover.",
        "active_setup": "Wait for the active installer to finish, then inspect install-status again.",
        "retained_resources_in_use": "Close clients and processes using the retained runtime, then inspect install-status again.",
        "runtime_referenced": "Keep this installation in place. Use a fresh project while existing runtime references are unresolved.",
        "inspection_incomplete": "Run install-status in a local login session with visibility of the account's processes and files.",
        "recovery_evidence_changed": "The recorded project, host, or witness no longer matches. Preserve the block and use a fresh project.",
    }
    return {"schema": 1, "status": status, "reason": reason, "recovery": recovery,
            "changed": changed, "next_action": actions.get(
                reason, "Preserve the installation barrier and retained evidence until the reported uncertainty is resolved."
            )}


def _check_witness(witness: dict, attempt: str, lease: dict, residual: dict, snapshot: dict,
                   boot: dict, archived: bool, project: Path) -> None:
    if (set(witness) != {"schema", "owner", "attempt", "host", "boot", "lease", "residual", "snapshot", "mac"}
            or witness["schema"] != 1 or witness["owner"] != "decision-browser-recovery-v1"
            or witness["attempt"] != attempt or witness["lease"] != lease or witness["snapshot"] != snapshot
            or witness["host"] != boot["host"] or type(witness["boot"]) is not str
            or not DIGEST.fullmatch(witness["boot"])
            or type(witness["residual"]) is not dict
            or witness["residual"].get("sha256") != residual["sha256"]
            or not archived and witness["residual"] != residual
            or type(witness["mac"]) is not str
            or not hmac.compare_digest(witness["mac"], _signature(witness, _recovery_key(project)))):
        raise RecoveryError("recovery_evidence_changed")


def _known_barriers(directory: int) -> None:
    if ".decision-browser-stage.lease" in os.listdir(directory):
        raise RecoveryError("additional_stage_lease")
    try:
        managed = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
    except FileNotFoundError:
        return
    try:
        if {"install.lock", "prepare.lock"} & set(os.listdir(managed)):
            raise RecoveryError("legacy_install_lock")
    finally:
        os.close(managed)


def inspect(project: Path, apply: bool = False) -> dict:
    if sys.platform not in {"darwin", "linux"}:
        raise RecoveryError("recovery_platform_unavailable")
    directory = _open_project(project)
    try:
        with _locked_lease(directory) as fd:
            _known_barriers(directory)
            residual = _read(directory, RESIDUAL)
            if fd is None:
                if residual is not None or STATE in os.listdir(directory):
                    raise RecoveryError("missing_lease")
                return _report("clear", "clear")
            body, lease = _lease(directory, fd)
            if body == LEASE and residual is None:
                return _report("clear", "clear")
            if body == ACTIVE:
                raise RecoveryError("stage_lifecycle_uncertain")
            if body != POISON:
                raise RecoveryError("unrecognized_lease")
            raw, residual_identity, archived = _pending_residual(directory, lease)
            attempt = _residual(raw)["attempt"]
            witness_name, archive_name = _names(attempt)
            snapshot = evidence.snapshot(project)
            boot = _boot()
            witness_record = _read(directory, witness_name)
            if witness_record is None:
                if archived:
                    raise RecoveryError("missing_recovery_witness")
                evidence.check_unused(project)
                if not apply:
                    return _report("blocked", "reboot_witness_required", "record_reboot_witness")
                evidence.check_unused(project)
                if evidence.snapshot(project) != snapshot or _boot() != boot:
                    raise RecoveryError("recovery_evidence_changed")
                _validate_project(project, directory)
                if (_lease(directory, fd)[1] != lease or _read(directory, RESIDUAL) != (raw, residual_identity)
                        or _read(directory, witness_name) is not None):
                    raise RecoveryError("recovery_evidence_changed")
                witness = {"schema": 1, "owner": "decision-browser-recovery-v1", "attempt": attempt,
                           **boot, "lease": lease, "residual": residual_identity, "snapshot": snapshot}
                witness["mac"] = _signature(witness, _recovery_key(project, create=True))
                _validate_project(project, directory)
                if (_lease(directory, fd)[1] != lease or _read(directory, RESIDUAL) != (raw, residual_identity)):
                    raise RecoveryError("recovery_evidence_changed")
                _write_exclusive(directory, witness_name, (json.dumps(witness, sort_keys=True) + "\n").encode())
                return _report("blocked", "reboot_required", "reboot_required", True)
            witness = _json(witness_record[0])
            _check_witness(witness, attempt, lease, residual_identity, snapshot, boot, archived, project)
            if witness["boot"] == boot["boot"]:
                return _report("blocked", "reboot_required", "reboot_required")
            evidence.check_unused(project)
            if not apply:
                return _report("blocked", "recovery_ready", "ready")
            evidence.check_unused(project)
            if evidence.snapshot(project) != snapshot or _boot() != boot:
                raise RecoveryError("recovery_evidence_changed")
            _validate_project(project, directory)
            _check_witness(witness, attempt, lease, residual_identity, snapshot, boot, archived, project)
            if (_lease(directory, fd)[1] != lease or _read(directory, witness_name) != witness_record
                    or not archived and _read(directory, RESIDUAL) != (raw, residual_identity)):
                raise RecoveryError("recovery_evidence_changed")
            expected = _read(directory, archive_name)
            if expected is None:
                if archived:
                    raise RecoveryError("missing_residual")
                _write_exclusive(directory, archive_name, raw)
            elif expected[0] != raw:
                raise RecoveryError("recovery_evidence_changed")
            _validate_project(project, directory)
            if _lease(directory, fd)[1] != lease:
                raise RecoveryError("recovery_evidence_changed")
            if not archived:
                if _read(directory, RESIDUAL) != (raw, residual_identity):
                    raise RecoveryError("recovery_evidence_changed")
                os.unlink(RESIDUAL, dir_fd=directory)
                os.fsync(directory)
            # Every safety check precedes publishing idle on the existing inode.
            # An interrupted archive can resume while its poison lease is unchanged.
            # A partial lease write can alter its identity evidence and stays blocked.
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.write(fd, LEASE) != len(LEASE):
                    raise RecoveryError("recovery_write_failed")
                os.ftruncate(fd, len(LEASE))
                os.fsync(fd)
                if _lease(directory, fd)[0] != LEASE:
                    raise RecoveryError("recovery_write_failed")
            except BaseException:
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, POISON)
                os.ftruncate(fd, len(POISON))
                os.fsync(fd)
                raise
            return _report("recovered", "recovered", "complete", True)
    finally:
        os.close(directory)


def main(argv: list[str] | None = None) -> int:
    global _MUTATION_STARTED
    _MUTATION_STARTED = False

    class Parser(argparse.ArgumentParser):
        def error(self, message: str) -> NoReturn:
            raise RecoveryError("invalid_arguments")

    arguments = sys.argv[1:] if argv is None else argv
    parser = Parser(description=__doc__)
    parser.add_argument("command", choices=("install-status", "recover-install"))
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--apply", action="store_true")
    try:
        args = parser.parse_args(arguments)
        if args.apply and args.command != "recover-install":
            raise RecoveryError("invalid_arguments")
    except RecoveryError:
        report = _report("blocked", "invalid_arguments")
        print(json.dumps(report, sort_keys=True) if "--json" in arguments
              else "Installation blocked (invalid_arguments). See --help.")
        return 2
    try:
        report = inspect(args.project.absolute(), args.apply)
    except (RecoveryError, evidence.EvidenceError) as exc:
        report = _report("blocked", exc.code)
        report["changed"] = None if _MUTATION_STARTED else False
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 - fixed public diagnostic boundary
        report = _report("blocked", "recovery_inspection_failed")
        report["changed"] = None if _MUTATION_STARTED else False
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(f"Installation {report['status']} ({report['reason']}).\n{report['next_action']}")
    return 0 if report["status"] in {"clear", "recovered"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
