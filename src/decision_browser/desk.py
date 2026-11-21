"""Human grant desks.

Request-scoped native presentation needs a manually validated host. Live proof
of that host is NOT_RUN, so ProductionDesk fails closed. The standard MCP form
adapter is a separate channel: a host form response is not proof of a native
human click. Nothing here is a host controller, and no bool, verified flag,
receipt, file, or auto-grant is proof.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from types import MappingProxyType
from typing import Literal, Protocol

from decision_browser.admission import AdmissionStore, AlphaProposal, BetaProposal

Proposal = AlphaProposal | BetaProposal
Response = Literal["accept", "decline", "cancel", "auto_accept"]
Before = Callable[[], Awaitable[None]]


class DeskUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class DeskPort(Protocol):
    @property
    def development_only(self) -> bool: ...

    async def present(self, proposal: Proposal) -> bool: ...


def _token(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _public(value: object) -> object:
    """JSON scalars and collections. MappingProxyType is a mapping, not a dict."""
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    if isinstance(value, (MappingProxyType, Mapping)):
        pairs = ((str(key), _public(item)) for key, item in value.items())
        return {key: item for key, item in sorted(pairs)}
    if isinstance(value, (set, frozenset)):
        return sorted((_public(item) for item in value), key=_token)
    if isinstance(value, (list, tuple)):
        return [_public(item) for item in value]
    raise TypeError(type(value).__name__)


def describe(proposal: Proposal, development_only: bool = False) -> dict[str, object]:
    """Scope, residual risk, and server digest. Proposals carry no raw goal."""
    if isinstance(proposal, AlphaProposal):
        shown: dict[str, object] = {
            "budgets": _public(proposal.budgets),
            "canonical_urls": _public(proposal.canonical_urls),
            "deadline": proposal.deadline,
            "development_only": development_only,
            "digest": proposal.digest,
            "expires_at": proposal.expires_at,
            "operations": _public(proposal.operations),
            "origins": _public(proposal.origins),
            "proposal_id": proposal.proposal_id,
            "residual_risk": proposal.residual_risk,
            "run_id": proposal.run_id,
        }
    else:
        shown = {
            "deadline": proposal.deadline,
            "destination_origin": proposal.destination_origin,
            "development_only": development_only,
            "digest": proposal.digest,
            "document_origin": proposal.document_origin,
            "expires_at": proposal.expires_at,
            "semantic_fingerprint": proposal.semantic_fingerprint,
            "operation": proposal.operation,
            "proposal_id": proposal.proposal_id,
            "run_id": proposal.run_id,
            "destination_url": proposal.destination_url,
        }
    return dict(sorted(shown.items()))


class ProductionDesk:
    """Fail closed. No supported human grant until live host proof, which is NOT_RUN.

    Request-scoped native presentation stays deferred. No environment, receipt,
    verified flag, file, or transport is consulted, and none can grant.
    """

    @property
    def development_only(self) -> bool:
        return False

    async def present(self, proposal: Proposal) -> bool:
        del proposal
        raise DeskUnavailable(reason="no_supported_human_grant_channel")


class DevelopmentDesk:
    """Explicit development proposals only. This is not a host controller.

    Accept records a store grant. Decline, auto-accept, and an exhausted
    response script do not. Cancel revokes the run. Decline and unsupported
    responses terminalize the proposal without freeing its retained-object
    capacity before the store's existing run-expiry cleanup.
    """

    def __init__(
        self,
        store: AdmissionStore,
        responses: Iterable[Response],
        before_response: Before | None = None,
    ) -> None:
        self._store = store
        self._responses: Iterator[Response] = iter(tuple(responses))
        self._before_response = before_response

    @property
    def development_only(self) -> bool:
        return True

    def _response(self) -> Response:
        try:
            return next(self._responses)
        except StopIteration:
            return "decline"

    def _open(self, proposal: Proposal) -> bool:
        stored = self._store._proposals.get(proposal.proposal_id)
        if stored is not proposal:
            return False
        if self._store._dev.get(proposal.proposal_id) is not True:
            return False
        if self._store._state.get(proposal.proposal_id) != "pending":
            return False
        if proposal.run_id in self._store._revoked:
            return False
        return self._store._fresh(proposal)

    async def present(self, proposal: Proposal) -> bool:
        if not self._open(proposal):
            return False
        if self._before_response is not None:
            await self._before_response()
        choice = self._response()
        if not self._open(proposal):
            return False
        if choice == "cancel":
            self._store.revoke(proposal.run_id)
            return False
        if choice == "decline":
            self._store.cancel(proposal.proposal_id)
            return False
        if choice == "accept":
            return self._store._accept_development(
                proposal.proposal_id, proposal.digest
            )
        self._store.cancel(proposal.proposal_id)
        return False
