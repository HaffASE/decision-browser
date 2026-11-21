"""In-process browser admission. Memory only; nothing here proves a human.

Production has no validated manual controller. A public accept flag, signed
receipt, file, environment variable, or clientInfo value is not proof.
_accept_development is a private test capability bound to this process and
only for proposals stored with development_only metadata. It is not host
proof and must not be used on a production path. Real desks fail closed. A
trusted transport would have to live outside this process; this module does
not consult a caller-supplied bool.

accept_standard_form is the ordinary production record path. A trusted
standard MCP form adapter calls it after action accept and strict
reviewedTrue. That call is not human-origin proof and is not a caller grant
flag, file, or environment capability. The method is not an exposed tool.
The server standard form desk calls it after those guards. The call is not
native-human proof.

An explicitly opted-in backend may instead record a single-origin ordinary
task scope with admit_task_scope. This is code-owned policy, distinct from
reviewed standard forms and human proof. Per-action Policy checks still apply.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit

_OPS: frozenset[str] = frozenset(
    {
        "click",
        "fill",
        "select",
        "scroll",
        "navigate",
        "enter",
        "submit",
        "screenshot",
        "wait",
    }
)
_BETA_OPS: frozenset[str] = frozenset(
    {
        "click",
        "fill",
        "select",
        "scroll",
        "navigate",
        "enter",
        "submit",
    }
)
_RISK = "human-supplied-origins-are-not-approval; ordinary admission does not exclude autosave or hidden mutation"
_State = str


class TrustedTransport(Protocol):
    """Out-of-process controller. No implementation in this module may say yes."""

    def attest(self, proposal_id: str, digest: str) -> bool:
        """Confirm proposal_id and digest. A local True is not trust."""


class ProductionAuthority:
    """Fail closed. Implements the transport shape and never admits."""

    def attest(self, proposal_id: str, digest: str) -> bool:
        del proposal_id, digest
        return False


def _finite(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("number")
    if not math.isfinite(value):
        raise ValueError("number")
    return float(value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or value == "":
        raise ValueError(label)
    return value


def _parts(url: str) -> SplitResult:
    if not isinstance(url, str) or url.strip() == "":
        raise ValueError("url")
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("scheme")
    if parts.username is not None or parts.password is not None:
        raise ValueError("credentials")
    if parts.hostname is None or parts.hostname == "":
        raise ValueError("host")
    return parts


def _endpoint(parts: SplitResult) -> tuple[str, str, int]:
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    host = host.lower()
    try:
        parsed = parts.port
    except ValueError as exc:
        raise ValueError("port") from exc
    port = 443 if parsed is None and scheme == "https" else parsed
    port = 80 if port is None else port
    if port < 1 or port > 65535:
        raise ValueError("port")
    shown = f"[{host}]" if ":" in host else host
    return scheme, shown, port


def canonical_origin(url: str) -> str:
    """HTTP(S) origin: lowercase host and effective port. No whitelist."""
    scheme, host, port = _endpoint(_parts(url))
    return f"{scheme}://{host}:{port}"


def canonical_url(url: str) -> str:
    """Canonical URL. Fragment stripped. Credentials rejected."""
    parts = _parts(url)
    scheme, host, port = _endpoint(parts)
    default = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = host if default else f"{host}:{port}"
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def _digest(body: Mapping[str, object]) -> str:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _match(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


@dataclass(frozen=True, slots=True)
class AlphaProposal:
    run_id: str
    proposal_id: str
    digest: str
    origins: frozenset[str]
    operations: frozenset[str]
    budgets: Mapping[str, int | float]
    deadline: float
    expires_at: float
    residual_risk: str
    canonical_urls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BetaProposal:
    run_id: str
    proposal_id: str
    digest: str
    semantic_fingerprint: str
    operation: str
    document_origin: str
    destination_origin: str
    destination_url: str
    deadline: float
    expires_at: float


class DevelopmentDesk:
    """Test-only desk. Calling it does not prove a production human controller."""

    def __init__(self, store: AdmissionStore) -> None:
        self._store = store

    def accept(self, proposal_id: str, digest: str) -> bool:
        return self._store._accept_development(proposal_id, digest)


class AdmissionStore:
    """Process-local proposals.

    accept_standard_form is the ordinary production record path. A trusted
    standard MCP form adapter calls it after action accept and strict
    reviewedTrue. That is not human-origin proof and not a caller grant flag,
    file, or environment capability. The method is not an exposed tool.
    The server standard form desk calls it after those guards. The call is not
    native-human proof. Development acceptance stays on DevelopmentDesk and
    does not flip into production.
    """

    def __init__(
        self,
        clock: Callable[[], float] | None = None,
        *,
        max_proposals: int = 256,
        max_runs: int = 64,
        cleanup_grace: float = 5.0,
        task_scope_enabled: bool = False,
    ) -> None:
        if type(task_scope_enabled) is not bool:
            raise TypeError("task_scope_enabled")
        if type(max_proposals) is not int or max_proposals < 1:
            raise ValueError("max_proposals")
        if type(max_runs) is not int or max_runs < 1:
            raise ValueError("max_runs")
        if isinstance(cleanup_grace, bool) or not isinstance(cleanup_grace, (int, float)):
            raise TypeError("cleanup_grace")
        grace = _finite(float(cleanup_grace))
        if grace < 0:
            raise ValueError("cleanup_grace")
        self._clock: Callable[[], float] = time.monotonic if clock is None else clock
        self._proposals: dict[str, AlphaProposal | BetaProposal] = {}
        self._state: dict[str, _State] = {}
        self._dev: dict[str, bool] = {}
        self._records: dict[str, str] = {}
        self._task_scopes: set[str] = set()
        self._task_scope_enabled = task_scope_enabled
        self._revoked: set[str] = set()
        self._run_deadlines: dict[str, float] = {}
        self._last_now: float | None = None
        self._max_proposals = max_proposals
        self._max_runs = max_runs
        self._cleanup_grace = grace

    @property
    def task_scope_enabled(self) -> bool:
        """Whether this backend opts into ordinary task-scope records."""
        return self._task_scope_enabled

    def now(self) -> float:
        try:
            current = _finite(self._clock())
        except Exception:  # noqa: BLE001 - custom clock failure must not expose callback data
            raise ValueError("clock_error") from None
        previous = self._last_now
        if previous is not None and current < previous:
            return previous
        self._last_now = current
        self._cleanup(current)
        return current


    def cleanup(self) -> None:
        self.now()


    def _cleanup(self, current: float) -> None:
        expired = {
            rid
            for rid, deadline in self._run_deadlines.items()
            if current >= deadline + self._cleanup_grace
        }
        if not expired:
            return
        for rid in expired:
            self._run_deadlines.pop(rid, None)
            self._revoked.discard(rid)
        for pid in [
            pid
            for pid, proposal in self._proposals.items()
            if proposal.run_id in expired
        ]:
            self._proposals.pop(pid, None)
            self._state.pop(pid, None)
            self._dev.pop(pid, None)
            self._records.pop(pid, None)
            self._task_scopes.discard(pid)

    def development_desk(self) -> DevelopmentDesk:
        return DevelopmentDesk(self)

    def propose_alpha(
        self,
        run_id: str,
        urls: Sequence[str],
        operations: Sequence[str],
        budgets: Mapping[str, int | float],
        deadline: float,
        expires_at: float | None = None,
        development_only: bool = False,
    ) -> AlphaProposal:
        rid = self._open_run(run_id)
        if not isinstance(development_only, bool):
            raise TypeError("development_only")
        ops = frozenset(operations)
        if not ops or not ops <= _OPS:
            raise ValueError("operations")
        canon = tuple(sorted({canonical_url(url) for url in urls}))
        if not canon:
            raise ValueError("urls")
        origins = frozenset(canonical_origin(url) for url in canon)
        plain = self._budgets(budgets)
        exp = self._window(deadline, expires_at)
        pid = str(uuid.uuid4())
        digest = _digest(
            {
                "budgets": plain,
                "canonical_urls": list(canon),
                "deadline": _finite(deadline),
                "expires_at": exp,
                "kind": "alpha",
                "operations": sorted(ops),
                "origins": sorted(origins),
                "proposal_id": pid,
                "residual_risk": _RISK,
                "run_id": rid,
            }
        )
        proposal = AlphaProposal(
            run_id=rid,
            proposal_id=pid,
            digest=digest,
            origins=origins,
            operations=ops,
            budgets=MappingProxyType(plain),
            deadline=_finite(deadline),
            expires_at=exp,
            residual_risk=_RISK,
            canonical_urls=canon,
        )
        self._keep(proposal, development_only)
        return proposal

    def propose_beta(
        self,
        run_id: str,
        semantic_fingerprint: str,
        operation: str,
        document_origin: str,
        destination_origin: str,
        destination_url: str,
        deadline: float,
        expires_at: float | None = None,
        development_only: bool = False,
    ) -> BetaProposal:
        """Uncertain-classifier route only. Binds the exact destination URL."""
        rid = self._open_run(run_id)
        if not isinstance(development_only, bool):
            raise TypeError("development_only")
        if operation not in _BETA_OPS:
            raise ValueError("operation")
        finger = _text(semantic_fingerprint, "semantic_fingerprint")
        doc = canonical_origin(document_origin)
        dest = canonical_origin(destination_origin)
        url = canonical_url(destination_url)
        if canonical_origin(url) != dest:
            raise ValueError("destination")
        exp = self._window(deadline, expires_at)
        pid = str(uuid.uuid4())
        digest = _digest(
            {
                "deadline": _finite(deadline),
                "destination_origin": dest,
                "destination_url": url,
                "document_origin": doc,
                "expires_at": exp,
                "kind": "beta",
                "operation": operation,
                "proposal_id": pid,
                "run_id": rid,
                "semantic_fingerprint": finger,
            }
        )
        proposal = BetaProposal(
            run_id=rid,
            proposal_id=pid,
            digest=digest,
            semantic_fingerprint=finger,
            operation=operation,
            document_origin=doc,
            destination_origin=dest,
            destination_url=url,
            deadline=_finite(deadline),
            expires_at=exp,
        )
        self._keep(proposal, development_only)
        return proposal

    def standard_form_pending(self, proposal: AlphaProposal | BetaProposal) -> bool:
        """Read the existing standard-form pending contract without exporting store state."""
        stored = self._proposals.get(proposal.proposal_id)
        if stored is not proposal or type(stored) is not type(proposal):
            return False
        if self._state.get(proposal.proposal_id) != "pending":
            return False
        if self._dev.get(proposal.proposal_id) is not False:
            return False
        if proposal.proposal_id in self._records:
            return False
        digest = proposal.digest
        if type(digest) is not str or digest == "":
            return False
        if proposal.run_id in self._revoked:
            return False
        return self._fresh(proposal)


    def _accept_development(self, proposal_id: str, digest: str) -> bool:
        """Test-only. development_only metadata required. Not a production proof."""
        proposal = self._proposals.get(proposal_id)
        if proposal is None or self._state.get(proposal_id) != "pending":
            return False
        if not self._dev.get(proposal_id, False):
            return False
        if not isinstance(digest, str) or not _match(proposal.digest, digest):
            return False
        if not self._fresh(proposal):
            return False
        if proposal.run_id in self._revoked:
            return False
        self._records[proposal_id] = str(uuid.uuid4())
        self._state[proposal_id] = "live"
        return True

    def accept_standard_form(self, proposal_id: str, digest: str) -> bool:
        """Ordinary production record path for a trusted standard MCP form adapter.

        Called after action accept and strict reviewedTrue. Not human-origin
        proof. Not a caller grant flag, file, or environment capability. Not
        an exposed tool. The server standard form desk calls this only after
        its guards. That call is not native-human proof. Does not change
        development metadata.
        """
        if not isinstance(proposal_id, str) or proposal_id == "":
            return False
        proposal = self._proposals.get(proposal_id)
        if proposal is None or self._state.get(proposal_id) != "pending":
            return False
        if self._dev.get(proposal_id) is not False:
            return False
        if proposal_id in self._records:
            return False
        if not isinstance(digest, str) or not _match(proposal.digest, digest):
            return False
        if not self._fresh(proposal):
            return False
        if proposal.run_id in self._revoked:
            return False
        self._records[proposal_id] = str(uuid.uuid4())
        self._state[proposal_id] = "live"
        return True

    def admit_task_scope(self, proposal_id: str, digest: str) -> bool:
        """Record a backend-owned ordinary task scope, without form/human proof.

        Only an opted-in backend may admit a pending production Alpha bounded
        to one canonical URL and origin. This does not admit consequential
        actions or uncertain Beta actions; Policy still checks each action.
        """
        if not self._task_scope_enabled or not isinstance(proposal_id, str):
            return False
        proposal = self._proposals.get(proposal_id)
        if not isinstance(proposal, AlphaProposal):
            return False
        if self._state.get(proposal_id) != "pending":
            return False
        if self._dev.get(proposal_id) is not False or proposal_id in self._records:
            return False
        if len(proposal.canonical_urls) != 1 or len(proposal.origins) != 1:
            return False
        if not isinstance(digest, str) or not _match(proposal.digest, digest):
            return False
        if not self._fresh(proposal) or proposal.run_id in self._revoked:
            return False
        self._records[proposal_id] = str(uuid.uuid4())
        self._task_scopes.add(proposal_id)
        self._state[proposal_id] = "live"
        return True

    def task_scope_allows(
        self,
        run_id: str,
        operation: str,
        document_origin: str,
        destination_origin: str,
    ) -> bool:
        """Check the exact live task record via its Alpha digest binding."""
        if not self._task_scope_enabled:
            return False
        self.now()
        for pid in tuple(self._task_scopes):
            proposal = self._proposals.get(pid)
            if (
                isinstance(proposal, AlphaProposal)
                and proposal.run_id == run_id
                and pid in self._records
                and proposal.run_id not in self._revoked
                and self.is_live(pid)
                and self.alpha_allows(
                    run_id, operation, document_origin, destination_origin,
                    digest=proposal.digest,
                )
            ):
                return True
        return False

    def is_live(self, proposal_id: str) -> bool:
        proposal = self._proposals.get(proposal_id)
        if proposal is None or self._state.get(proposal_id) != "live":
            return False
        return self._fresh(proposal)

    def get_proposal(self, proposal_id: str) -> AlphaProposal | BetaProposal | None:
        proposal = self._proposals.get(proposal_id)
        if proposal is None or not self._readable(proposal_id):
            return None
        return proposal

    def alpha_allows(
        self,
        run_id: str,
        operation: str,
        document_origin: str,
        destination_origin: str,
        digest: str | None = None,
    ) -> bool:
        try:
            doc = canonical_origin(document_origin)
            dest = canonical_origin(destination_origin)
        except ValueError:
            return False
        if operation not in _OPS:
            return False
        self.now()
        for pid, proposal in list(self._proposals.items()):
            if not isinstance(proposal, AlphaProposal) or proposal.run_id != run_id:
                continue
            if digest is not None and (
                not isinstance(digest, str) or not _match(proposal.digest, digest)
            ):
                continue
            if not self.is_live(pid):
                continue
            if operation not in proposal.operations:
                continue
            if doc in proposal.origins and dest in proposal.origins:
                return True
        return False

    def beta_basis_allows(
        self, run_id: str, operation: str, document_origin: str
    ) -> bool:
        """Beta requires a manual/development Alpha, never a task-only record."""
        self.now()
        for pid, proposal in list(self._proposals.items()):
            if (
                isinstance(proposal, AlphaProposal)
                and proposal.run_id == run_id
                and pid not in self._task_scopes
                and self.alpha_allows(
                    run_id, operation, document_origin, document_origin,
                    digest=proposal.digest,
                )
            ):
                return True
        return False

    def beta_allows(
        self,
        run_id: str,
        proposal_id: str,
        digest: str,
        semantic_fingerprint: str,
        operation: str,
        document_origin: str,
        destination_origin: str,
        destination_url: str,
    ) -> bool:
        proposal = self._proposals.get(proposal_id)
        if not isinstance(proposal, BetaProposal) or not self.is_live(proposal_id):
            return False
        if proposal.run_id != run_id or not isinstance(digest, str):
            return False
        if not _match(proposal.digest, digest):
            return False
        try:
            doc = canonical_origin(document_origin)
            dest = canonical_origin(destination_origin)
            url = canonical_url(destination_url)
        except ValueError:
            return False
        if not self.beta_basis_allows(run_id, operation, document_origin):
            return False
        return (
            proposal.semantic_fingerprint == semantic_fingerprint
            and proposal.operation == operation
            and proposal.document_origin == doc
            and proposal.destination_origin == dest
            and proposal.destination_url == url
        )

    def consume_beta(
        self,
        run_id: str,
        proposal_id: str,
        digest: str,
        semantic_fingerprint: str,
        operation: str,
        document_origin: str,
        destination_origin: str,
        destination_url: str,
    ) -> bool:
        if not self.beta_allows(
            run_id,
            proposal_id,
            digest,
            semantic_fingerprint,
            operation,
            document_origin,
            destination_origin,
            destination_url,
        ):
            return False
        self._state[proposal_id] = "consumed"
        self._records.pop(proposal_id, None)
        self._task_scopes.discard(proposal_id)
        return True

    def revoke(self, run_id: str) -> None:
        rid = _text(run_id, "run_id")
        if rid not in self._run_deadlines:
            return
        self._revoked.add(rid)
        for pid, proposal in self._proposals.items():
            if proposal.run_id == rid and self._state.get(pid) in {"pending", "live"}:
                self._state[pid] = "revoked"
                self._records.pop(pid, None)
                self._task_scopes.discard(pid)

    def cancel(self, proposal_id: str) -> None:
        if proposal_id in self._proposals and self._state.get(proposal_id) in {
            "pending",
            "live",
        }:
            self._state[proposal_id] = "cancelled"
            self._records.pop(proposal_id, None)
            self._task_scopes.discard(proposal_id)

    def restart(self) -> None:
        """Drop process memory. Prior ids, consumes, and revocations are gone."""
        self._proposals.clear()
        self._state.clear()
        self._dev.clear()
        self._records.clear()
        self._task_scopes.clear()
        self._revoked.clear()
        self._run_deadlines.clear()

    def _open_run(self, run_id: str) -> str:
        rid = _text(run_id, "run_id")
        self.now()
        if rid in self._revoked:
            raise ValueError("revoked")
        return rid

    def _window(self, deadline: float, expires_at: float | None) -> float:
        end = _finite(deadline)
        exp = end if expires_at is None else _finite(expires_at)
        now = self.now()
        if exp > end or exp <= now or end <= now:
            raise ValueError("expiry")
        return exp

    def _budgets(self, budgets: Mapping[str, int | float]) -> dict[str, int | float]:
        if not isinstance(budgets, Mapping):
            raise TypeError("budgets")
        plain: dict[str, int | float] = {}
        for key in sorted(budgets):
            if not isinstance(key, str) or key == "":
                raise ValueError("budgets")
            plain[key] = _finite(budgets[key])
        return plain

    def _keep(
        self, proposal: AlphaProposal | BetaProposal, development_only: bool
    ) -> None:
        first = self._run_deadlines.get(proposal.run_id)
        if first is not None and proposal.deadline > first:
            raise ValueError("deadline")
        if (
            proposal.proposal_id not in self._proposals
            and len(self._proposals) >= self._max_proposals
        ):
            raise ValueError("max_proposals")
        if first is None and len(self._run_deadlines) >= self._max_runs:
            raise ValueError("max_runs")
        _finite(proposal.deadline + self._cleanup_grace)
        self._proposals[proposal.proposal_id] = proposal
        self._state[proposal.proposal_id] = "pending"
        self._dev[proposal.proposal_id] = development_only
        if first is None:
            self._run_deadlines[proposal.run_id] = proposal.deadline

    def _fresh(self, proposal: AlphaProposal | BetaProposal) -> bool:
        return self.now() < min(proposal.deadline, proposal.expires_at)

    def _readable(self, proposal_id: str) -> bool:
        proposal = self._proposals[proposal_id]
        if self._state.get(proposal_id) not in {"pending", "live"}:
            return False
        return self._fresh(proposal)


__all__ = [
    "AdmissionStore",
    "AlphaProposal",
    "BetaProposal",
    "DevelopmentDesk",
    "ProductionAuthority",
    "TrustedTransport",
    "canonical_origin",
    "canonical_url",
]
