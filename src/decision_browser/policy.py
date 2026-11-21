from __future__ import annotations

import re
from dataclasses import dataclass
from ipaddress import IPv6Address
from typing import Literal
from urllib.parse import unquote, urlsplit

from decision_browser.admission import AdmissionStore, canonical_origin, canonical_url
from decision_browser.contracts import AdmissionClass

ClassifierState = Literal["match", "uncertain", "clear_non_match"]

_UNSUPPORTED = "unsupported_operation"
_CONSEQUENTIAL = "consequential_action_blocked"
_NO_BASIS = "no_independent_admission_basis"
_INVALID_DESTINATION = "invalid_destination"
_OBSERVE = frozenset({"screenshot", "wait"})
_ROUTED = frozenset(
    {"click", "fill", "select", "scroll", "navigate", "submit", "enter"}
)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# DOM identifiers and route segments use word separators that ``\w`` treats as
# part of a single word. Preserve acronym groups while splitting camelCase.
_IDENTIFIER_BOUNDARY = re.compile(
    r"_+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])"
)
_LEXICON = re.compile(
    r"(?<!\w)(?:checkouts?|purchases?|buy|pay|payments?|send|publish|delete|remove|"
    r"transfers?|bookings?|reserve|orders?|оформить|оформление|купить|покупк\w*|"
    r"оплат\w*|платеж\w*|платёж\w*|отправ\w*|опубликов\w*|удал\w*|убра\w*|"
    r"перевод\w*|перевести|бронирован\w*|заброниров\w*|резерв\w*|"
    r"зарезервир\w*|заказ\w*|заказать)(?!\w)",
    re.IGNORECASE,
)
# These complete DOM labels use "order" for arrangement, rather than a purchase.
# Do not apply this wording exception to URLs or to another form/submitter signal.
_ORDERING_LABEL = re.compile(r"(?:sort|apply)\s+order", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ActionScope:
    run_id: str
    operation: str
    document_origin: str
    destination_origin: str
    semantic_fingerprint: str
    signals: tuple[str, ...] = ()
    signal_conflict: bool = False
    proposal_id: str | None = None
    action_digest: str | None = None
    target_key: str | None = None
    destination_url: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    admission_class: AdmissionClass
    classification: ClassifierState
    reason: str | None = None
    record_id: str | None = None


def _effective_url(scope: ActionScope) -> str:
    return scope.destination_url or scope.destination_origin


def _route(effective: str) -> str:
    """Path and query of the effective URL, percent-decoded. Host is never included."""
    parsed = urlsplit(effective)
    path = unquote(parsed.path)
    query = unquote(parsed.query)
    return f"{path}?{query}" if query else path


def _is_http_loopback(origin: str) -> bool:
    parsed = urlsplit(origin)
    try:
        host, port = parsed.hostname, parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and host in _LOOPBACK_HOSTS
        and parsed.username is None
        and parsed.password is None
        and port != 0
    )


def _reject_unsafe_url(value: object) -> None:
    if not isinstance(value, str) or not value:
        raise TypeError
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError
    host, port = parsed.hostname, parsed.port
    if not parsed.scheme or not host or not parsed.netloc or port == 0:
        raise ValueError
    bracketed = parsed.netloc.startswith("[")
    if bracketed or ":" in host or "]" in parsed.netloc:
        if not bracketed or "]" not in parsed.netloc:
            raise ValueError
        IPv6Address(host)


def classify(scope: ActionScope) -> ClassifierState:
    """Match selected-action signals and destination route, never task text or host."""
    try:
        route = _route(_effective_url(scope))
    except (ValueError, TypeError):
        route = ""
    if _LEXICON.search(_IDENTIFIER_BOUNDARY.sub(" ", route)) or any(
        _LEXICON.search(part) and not _ORDERING_LABEL.fullmatch(part.strip())
        for part in (_IDENTIFIER_BOUNDARY.sub(" ", signal) for signal in scope.signals)
    ):
        return "match"
    if scope.signal_conflict:
        return "uncertain"
    return "clear_non_match"


class Policy:
    """Routes every targeted browser action. Screenshot and wait only observe."""

    def __init__(
        self,
        store: AdmissionStore,
        fixture_origin: str | None = None,
        fixture_actions: frozenset[tuple[str, str]] = frozenset(),
        development_only: bool = False,
    ) -> None:
        if not isinstance(development_only, bool):
            raise TypeError
        self._store = store
        self._development_only = development_only
        actions = frozenset(fixture_actions)
        if (fixture_origin is not None or actions) and development_only is not True:
            raise ValueError("fixture checks require development_only is True")
        if any(op not in _ROUTED for op, _target in actions):
            raise ValueError("fixture actions must be routed operation/target tuples")
        origin = None
        if fixture_origin is not None:
            _reject_unsafe_url(fixture_origin)
            origin = canonical_origin(fixture_origin)
            if not _is_http_loopback(origin):
                raise ValueError("public or non-loopback fixture origin is rejected")
        self._fixture_origin = origin
        self._fixture_actions = actions

    def evaluate(self, scope: ActionScope, consume: bool = False) -> PolicyDecision:
        classification = classify(scope)
        targets = self._targets(scope)
        if targets is None:
            return self._denied(classification, _INVALID_DESTINATION)
        if scope.operation in _OBSERVE:
            return PolicyDecision(True, "observe_driver", classification)
        if scope.operation not in _ROUTED:
            return self._denied(classification, _UNSUPPORTED)
        if classification == "match":
            return self._denied(classification, _CONSEQUENTIAL, "consequential")
        document_origin, destination_origin, destination_url = targets
        if self._fixture_allows(scope, document_origin, destination_origin):
            return PolicyDecision(True, "fixture_mapped", classification)
        if classification == "clear_non_match":
            return self._from_alpha(
                scope, document_origin, destination_origin, classification
            )
        return self._from_beta(
            scope,
            document_origin,
            destination_origin,
            destination_url,
            classification,
            consume,
        )

    def _targets(self, scope: ActionScope) -> tuple[str, str, str] | None:
        """Canonical document origin, destination origin, and destination URL.

        Malformed input and an effective URL whose origin disagrees with
        destination_origin deny. The reason is fixed; exception text is not returned.
        """
        try:
            _reject_unsafe_url(scope.document_origin)
            _reject_unsafe_url(scope.destination_origin)
            effective = _effective_url(scope)
            _reject_unsafe_url(effective)
            document_origin = canonical_origin(scope.document_origin)
            destination_origin = canonical_origin(scope.destination_origin)
            if canonical_origin(effective) != destination_origin:
                return None
            return document_origin, destination_origin, canonical_url(effective)
        except (ValueError, TypeError):
            return None

    def _fixture_allows(
        self,
        scope: ActionScope,
        document_origin: str,
        destination_origin: str,
    ) -> bool:
        key = scope.target_key
        owned = self._fixture_origin
        if (
            not self._development_only
            or owned is None
            or key is None
            or scope.operation not in _ROUTED
            or not _is_http_loopback(owned)
        ):
            return False
        paired = (scope.operation, key) in self._fixture_actions
        return paired and document_origin == owned and destination_origin == owned

    def _denied(
        self,
        classification: ClassifierState,
        reason: str,
        admission_class: AdmissionClass = "unresolved",
    ) -> PolicyDecision:
        return PolicyDecision(False, admission_class, classification, reason)

    def _from_alpha(
        self,
        scope: ActionScope,
        document_origin: str,
        destination_origin: str,
        classification: ClassifierState,
    ) -> PolicyDecision:
        allowed = self._store.alpha_allows(
            run_id=scope.run_id,
            operation=scope.operation,
            document_origin=document_origin,
            destination_origin=destination_origin,
        )
        if not allowed:
            return self._denied(classification, _NO_BASIS)
        if self._store.task_scope_allows(
            scope.run_id, scope.operation, document_origin, destination_origin
        ):
            return PolicyDecision(True, "ordinary_task_scoped", classification)
        return PolicyDecision(True, "ordinary_admitted", classification, None, None)

    def _from_beta(
        self,
        scope: ActionScope,
        document_origin: str,
        destination_origin: str,
        destination_url: str,
        classification: ClassifierState,
        consume: bool,
    ) -> PolicyDecision:
        proposal_id = scope.proposal_id
        digest = scope.action_digest
        if not proposal_id or not digest:
            return self._denied(classification, _NO_BASIS)
        if not self._store.beta_basis_allows(
            run_id=scope.run_id,
            operation=scope.operation,
            document_origin=document_origin,
        ):
            return self._denied(classification, _NO_BASIS)
        admit = self._store.consume_beta if consume else self._store.beta_allows
        allowed = admit(
            run_id=scope.run_id,
            proposal_id=proposal_id,
            digest=digest,
            semantic_fingerprint=scope.semantic_fingerprint,
            operation=scope.operation,
            document_origin=document_origin,
            destination_origin=destination_origin,
            destination_url=destination_url,
        )
        if not allowed:
            return self._denied(classification, _NO_BASIS)
        return PolicyDecision(
            True, "ordinary_beta_admitted", classification, None, proposal_id
        )
