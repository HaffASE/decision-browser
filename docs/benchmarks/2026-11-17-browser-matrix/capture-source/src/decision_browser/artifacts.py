"""Bounded in-process artifacts and an append-only evidence journal."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Literal
from urllib.parse import urlsplit, urlunsplit

from .budget import Budget, StepReservation, StoredReservation

_MAX_BYTES: Final = 104_857_600
_MAX_ARTS: Final = 128
ARTIFACT_READ_MAX_BYTES: Final = 65_536
_MEDIA: Final = frozenset({"image/png", "image/jpeg", "application/octet-stream"})
_ID_RE: Final = re.compile(r"^[0-9a-f]{32}$")
_URL_RE: Final = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_PHRASES: Final = (
    "cookie",
    "authorization",
    "password",
    "token",
    "apikey",
    "secret",
    "accesstoken",
    "refreshtoken",
    "clientsecret",
    "privatekey",
    "passwd",
)
_STATUSES: Final = frozenset({"acknowledged", "outcome_unknown", "rejected"})
_REDACTED: Final = "[REDACTED]"


class ArtifactError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _bound(value: int, limit: int, code: str) -> int:
    if type(value) is not int or not 0 < value <= limit:
        raise ArtifactError(code)
    return value


@dataclass(frozen=True, slots=True)
class Artifact:
    id: str
    media_type: str
    content: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class ArtifactRange:
    id: str
    media_type: str
    content: bytes
    sha256: str
    total_bytes: int
    offset: int

    @property
    def next_offset(self) -> int | None:
        end = self.offset + len(self.content)
        return end if end < self.total_bytes else None


class _ArtifactReservation:
    """Capacity for fixed, already sanitized records, never arbitrary writes."""

    def __init__(self, store: ArtifactStore, groups: dict[str, dict[str, Artifact]],
                 budget: Budget) -> None:
        self._store = store
        self._budget = budget
        self._groups = {name: dict(options) for name, options in groups.items()}
        self._sizes = {name: max(len(item.content) for item in options.values())
                       for name, options in groups.items()}
        total = sum(self._sizes.values())
        if len(store._items) + store._held_count + len(groups) > store._max_artifacts:
            raise ArtifactError("over_count")
        if store._used + store._held_bytes + total > store._max_bytes:
            raise ArtifactError("over_bytes")
        self._stored: StoredReservation = budget.hold_stored(total)
        store._held_count += len(groups)
        store._held_bytes += total
        store._reservations.add(self)

    def has(self, name: str) -> bool:
        return name in self._groups

    def commit(self, name: str, choice: str) -> str:
        options = self._groups.get(name)
        if options is None or choice not in options:
            raise ArtifactError("bad_reservation")
        if name in {"binding", "before"}:
            self._budget.remaining()
        artifact = options[choice]
        maximum = self._sizes[name]
        self._stored.commit(len(artifact.content), maximum)
        self._store._held_count -= 1
        self._store._held_bytes -= maximum
        self._store._items[artifact.id] = artifact
        self._store._used += len(artifact.content)
        del self._groups[name], self._sizes[name]
        return artifact.id

    def release(self) -> None:
        self._store._held_count -= len(self._groups)
        self._store._held_bytes -= sum(self._sizes.values())
        self._groups.clear()
        self._sizes.clear()
        self._stored.release()
        self._store._reservations.discard(self)


class JournalAttempt:
    """One prepared attempt's step and mandatory audit envelope."""

    def __init__(self, journal: Journal, step: StepReservation,
                 records: dict[str, dict[str, tuple[dict[str, Any], Artifact]]],
                 budget: Budget) -> None:
        self._journal = journal
        self._step = step
        self._records = records
        self.unknown_recorded = False
        try:
            self._held = _ArtifactReservation(journal._store, {
                group: {key: record[1] for key, record in options.items()}
                for group, options in records.items()
            }, budget)
        except BaseException:
            step.release()
            raise

    def append(self, event: str, body: dict[str, Any]) -> str | None:
        if event == "admission_binding":
            group, key = "binding", "binding"
        elif event == "before_dispatch":
            group, key = "before", "before"
        elif event == "outcome":
            group = "outcome" if self._held.has("outcome") else "final"
            key = str(body.get("status"))
        elif event == "post_observe_failure":
            group, key = "final", "post_observe_failure"
        else:
            return None
        expected = self._records.get(group, {}).get(key)
        if expected is None or body != expected[0]:
            raise ArtifactError("bad_reservation")
        artifact_id = self._held.commit(group, key)
        if event == "post_observe_failure" or event == "outcome" and key == "outcome_unknown":
            self.unknown_recorded = True
        return artifact_id

    def commit_step(self) -> None:
        self._step.commit()

    def release(self) -> None:
        self._step.release()
        self._held.release()
        if self._journal._reservation is self:
            self._journal._reservation = None


def _secret_key(key: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(phrase in compact for phrase in _PHRASES)


def _scrub_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"} or parts.hostname is None:
            return _REDACTED if parts.hostname is None else url
        port = parts.port
    except ValueError:
        return _REDACTED
    host = parts.hostname
    shown = f"[{host}]" if ":" in host else host
    netloc = f"{shown}:{port}" if port is not None else shown
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _scrub_text(text: str, blocked: tuple[str, ...]) -> str:
    for lit in sorted((item for item in blocked if item), key=len, reverse=True):
        text = text.replace(lit, _REDACTED)
    return _URL_RE.sub(lambda match: _scrub_url(match.group(0)), text)


def _ensure_clean(raw: bytes, blocked: tuple[str, ...]) -> None:
    if any(lit and lit.encode() in raw for lit in blocked):
        raise ArtifactError("redaction_failed")


def _unique(name: str, seen: dict[str, Any]) -> str:
    if name not in seen:
        return name
    suffix = len(seen)
    while f"{name}#{suffix}" in seen:
        suffix += 1
    return f"{name}#{suffix}"


def _redact(value: Any, blocked: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return _scrub_text(value, blocked)
    if isinstance(value, dict):
        seen: dict[str, Any] = {}
        for key, item in value.items():
            original = str(key)
            if _secret_key(original):
                name, item_out = _REDACTED, _REDACTED
            else:
                name, item_out = _scrub_text(original, blocked), _redact(item, blocked)
            seen[_unique(name, seen)] = item_out
        return seen
    if isinstance(value, (list, tuple)):
        return [_redact(item, blocked) for item in value]
    if value is None or isinstance(value, bool) or type(value) is int:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ArtifactError("non_finite")
        return value
    raise ArtifactError("unsupported_json")


def _canonical(payload: Any, blocked: tuple[str, ...]) -> bytes:
    try:
        text = json.dumps(
            _redact(payload, blocked),
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except ArtifactError:
        raise
    except (TypeError, ValueError):
        raise ArtifactError("json_encode") from None
    raw = text.encode()
    _ensure_clean(raw, blocked)
    return raw


class ArtifactStore:
    def __init__(
        self,
        max_bytes: int,
        blocked_literals: tuple[str, ...] = (),
        max_artifacts: int = 128,
        *,
        reserve_bytes: Callable[[int], None] | None = None,
    ) -> None:
        self._max_bytes = _bound(max_bytes, _MAX_BYTES, "bad_max_bytes")
        self._max_artifacts = _bound(max_artifacts, _MAX_ARTS, "bad_max_artifacts")
        if type(blocked_literals) is not tuple or any(
            type(item) is not str for item in blocked_literals
        ):
            raise ArtifactError("bad_blocked")
        if reserve_bytes is not None and not callable(reserve_bytes):
            raise ArtifactError("bad_reserve")
        self._blocked = blocked_literals
        self._reserve_bytes = reserve_bytes
        self._items: dict[str, Artifact] = {}
        self._used = 0
        self._held_count = self._held_bytes = 0
        self._reservations: set[_ArtifactReservation] = set()

    @property
    def stored_bytes(self) -> int:
        return self._used

    def clear(self) -> None:
        for reservation in tuple(self._reservations):
            reservation.release()
        self._items.clear()
        self._used = 0

    def read(self, artifact_id: str) -> Artifact:
        if type(artifact_id) is not str or _ID_RE.fullmatch(artifact_id) is None:
            raise ArtifactError("bad_id")
        try:
            return self._items[artifact_id]
        except KeyError:
            raise ArtifactError("unknown_id") from None

    def read_range(
        self, artifact_id: str, *, offset: int = 0, limit: int = ARTIFACT_READ_MAX_BYTES
    ) -> ArtifactRange:
        if type(offset) is not int or offset < 0:
            raise ArtifactError("bad_range")
        _bound(limit, ARTIFACT_READ_MAX_BYTES, "bad_range")
        artifact = self.read(artifact_id)
        if offset > len(artifact.content):
            raise ArtifactError("bad_range")
        return ArtifactRange(
            artifact.id, artifact.media_type, artifact.content[offset:offset + limit],
            artifact.sha256, len(artifact.content), offset,
        )

    def _commit(self, content: bytes, media_type: str) -> str:
        size = len(content)
        if size > self._max_bytes or self._used + self._held_bytes + size > self._max_bytes:
            raise ArtifactError("over_bytes")
        if len(self._items) + self._held_count >= self._max_artifacts:
            raise ArtifactError("over_count")
        if self._reserve_bytes is not None:
            self._reserve_bytes(size)
        artifact_id = uuid.uuid4().hex
        digest = hashlib.sha256(content).hexdigest()
        self._items[artifact_id] = Artifact(artifact_id, media_type, content, digest)
        self._used += size
        return artifact_id

    def put_bytes(self, content: bytes, media_type: str) -> str:
        if type(content) is not bytes:
            raise ArtifactError("bad_bytes")
        if type(media_type) is not str or media_type not in _MEDIA:
            raise ArtifactError("bad_media")
        if any(lit and lit.encode() in content for lit in self._blocked):
            raise ArtifactError("blocked_bytes")
        return self._commit(content, media_type)

    def put_text(self, text: str) -> str:
        if type(text) is not str:
            raise ArtifactError("bad_text")
        raw = _scrub_text(text, self._blocked).encode()
        _ensure_clean(raw, self._blocked)
        return self._commit(raw, "text/plain; charset=utf-8")

    def put_json(self, payload: object) -> str:
        return self._commit(_canonical(payload, self._blocked), "application/json")


class Journal:
    def __init__(self, store: ArtifactStore) -> None:
        if not isinstance(store, ArtifactStore):
            raise ArtifactError("bad_store")
        self._store = store
        self._attempts: list[str] = []
        self._dispatches: dict[str, str] = {}
        self._reservation: JournalAttempt | None = None

    def reserve_attempt(
        self, run_id: str, action_digest: str, semantic_fingerprint: str,
        admission_class: str, *, budget: Budget,
        admission_binding: dict[str, str] | None = None,
    ) -> JournalAttempt:
        if self._reservation is not None:
            raise ArtifactError("attempt_active")
        if any(type(item) is not str or not item for item in (
            run_id, action_digest, semantic_fingerprint, admission_class
        )):
            raise ArtifactError("bad_event")
        step = budget.hold_step()
        try:
            attempt_id = uuid.uuid4().hex

            def prepared(event: str, *, artifact_id: str | None = None,
                         **fields: Any) -> tuple[dict[str, Any], Artifact]:
                body = {**fields, "event": event, "run_id": run_id}
                raw = _canonical(body, self._store._blocked)
                record = Artifact(artifact_id or uuid.uuid4().hex, "application/json", raw,
                                  hashlib.sha256(raw).hexdigest())
                return body, record

            records = {
                "before": {"before": prepared("before_dispatch", artifact_id=attempt_id,
                    action_digest=action_digest, semantic_fingerprint=semantic_fingerprint,
                    admission_class=admission_class)},
                "outcome": {status: prepared("outcome", attempt_id=attempt_id, status=status)
                            for status in _STATUSES},
                "final": {
                    "post_observe_failure": prepared("post_observe_failure", attempt_id=attempt_id,
                                                     status="outcome_unknown"),
                    "outcome_unknown": prepared("outcome", attempt_id=attempt_id, status="outcome_unknown"),
                },
            }
            if admission_binding is not None:
                records["binding"] = {"binding": prepared("admission_binding", **admission_binding)}
            reservation = JournalAttempt(self, step, records, budget)
        except BaseException:
            step.release()
            raise
        self._reservation = reservation
        return reservation

    def append(self, event: str, run_id: str, **fields: Any) -> str:
        if type(event) is not str or not event or type(run_id) is not str or not run_id:
            raise ArtifactError("bad_event")
        body: dict[str, Any] = dict(fields)
        body["event"] = event
        body["run_id"] = run_id
        reservation = self._reservation
        artifact_id = None if reservation is None else reservation.append(event, body)
        if artifact_id is None:
            artifact_id = self._store.put_json(body)
        self._attempts.append(artifact_id)
        if event == "before_dispatch":
            self._dispatches[artifact_id] = run_id
        return artifact_id

    def before_dispatch(
        self,
        run_id: str,
        action_digest: str,
        semantic_fingerprint: str,
        admission_class: str,
    ) -> str:
        values = (action_digest, semantic_fingerprint, admission_class)
        if any(type(value) is not str or not value for value in values):
            raise ArtifactError("bad_event")
        return self.append(
            "before_dispatch",
            run_id,
            action_digest=action_digest,
            semantic_fingerprint=semantic_fingerprint,
            admission_class=admission_class,
        )

    def _held_dispatch(self, run_id: str, attempt_id: str) -> None:
        if type(attempt_id) is not str or _ID_RE.fullmatch(attempt_id) is None:
            raise ArtifactError("bad_attempt")
        if self._dispatches.get(attempt_id) != run_id:
            raise ArtifactError("bad_attempt")

    def outcome(
        self,
        run_id: str,
        attempt_id: str,
        status: Literal["acknowledged", "outcome_unknown", "rejected"],
    ) -> str:
        if type(status) is not str or status not in _STATUSES:
            raise ArtifactError("bad_status")
        self._held_dispatch(run_id, attempt_id)
        return self.append("outcome", run_id, attempt_id=attempt_id, status=status)

    def post_observe_failure(self, run_id: str, attempt_id: str) -> str:
        self._held_dispatch(run_id, attempt_id)
        return self.append(
            "post_observe_failure",
            run_id,
            attempt_id=attempt_id,
            status="outcome_unknown",
        )
