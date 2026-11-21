"""Human-approved inline contract for desk grants.

DevelopmentDesk carries an explicit development-only label.
Native production proof is NOT_RUN.
"""

import json
import math
import uuid
from dataclasses import replace

import pytest

from decision_browser.admission import AdmissionStore
from decision_browser.desk import (
    DeskUnavailable,
    DevelopmentDesk,
    ProductionDesk,
    describe,
)

pytestmark = pytest.mark.asyncio

_ALPHA_URLS = (
    "https://app.example/workspace",
    "https://other.example/board",
)
_BETA_DOCUMENT = "https://docs.example/spec"
_BETA_URL = "https://app.example/workspace/item"
_FINGERPRINT = "beta-fill-fingerprint"
_RISK = 'human-supplied-origins-are-not-approval; ordinary admission does not exclude autosave or hidden mutation'
_PROOF_FLAGS = (
    "approved",
    "attestation",
    "granted",
    "proof",
    "receipt",
    "signature",
    "trusted",
    "verified",
)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        if seconds < 0:
            raise RuntimeError("clock does not move backward")
        self.now += seconds

    def rewind_for_no_resurrection(self, seconds):
        # Intentional backward step for the no-resurrection case only.
        if seconds <= 0:
            raise RuntimeError("rewind distance is positive")
        self.now -= seconds


@pytest.fixture
def clock():
    return Clock(10_000.0)


@pytest.fixture
def store(clock):
    return AdmissionStore(clock=clock)


def _deadlines(clock):
    now = clock()
    return now + 80.0, now + 40.0


def _propose(store, kind, *, development_only, deadline, expires_at):
    if kind == "alpha":
        return store.propose_alpha(
            "run-alpha",
            [_ALPHA_URLS[0]],
            ["fill"],
            {"steps": 1},
            deadline=deadline,
            expires_at=expires_at,
            development_only=development_only,
        )
    if kind == "beta":
        return store.propose_beta(
            "run-beta",
            _FINGERPRINT,
            "fill",
            _BETA_DOCUMENT,
            _BETA_URL,
            destination_url=_BETA_URL,
            deadline=deadline,
            expires_at=expires_at,
            development_only=development_only,
        )
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["alpha", "beta"])
async def test_explicit_development_accept_grants_once(store, clock, kind):
    deadline, expires_at = _deadlines(clock)
    proposal = _propose(
        store,
        kind,
        development_only=True,
        deadline=deadline,
        expires_at=expires_at,
    )
    desk = DevelopmentDesk(store, ("accept",))
    assert desk.development_only is True
    assert await desk.present(proposal) is True
    assert store.is_live(proposal.proposal_id) is True
    assert store.get_proposal(proposal.proposal_id) is proposal
    assert await desk.present(proposal) is False
    assert store.is_live(proposal.proposal_id) is True
    assert store.get_proposal(proposal.proposal_id) is proposal


@pytest.mark.parametrize("kind", ["alpha", "beta"])
async def test_auto_accept_does_not_grant(store, clock, kind):
    deadline, expires_at = _deadlines(clock)
    proposal = _propose(
        store,
        kind,
        development_only=True,
        deadline=deadline,
        expires_at=expires_at,
    )
    assert await DevelopmentDesk(store, ("auto_accept",)).present(proposal) is False
    assert store.is_live(proposal.proposal_id) is False
    assert store.get_proposal(proposal.proposal_id) is None
    assert store._state[proposal.proposal_id] == "cancelled"
    assert await DevelopmentDesk(store, ("accept",)).present(proposal) is False


@pytest.mark.parametrize("kind", ["alpha", "beta"])
@pytest.mark.parametrize("responses", [("decline",), ()])
async def test_decline_and_exhausted_responses_do_not_grant(
    store, clock, kind, responses
):
    deadline, expires_at = _deadlines(clock)
    proposal = _propose(
        store,
        kind,
        development_only=True,
        deadline=deadline,
        expires_at=expires_at,
    )
    assert await DevelopmentDesk(store, responses).present(proposal) is False
    assert store.is_live(proposal.proposal_id) is False
    assert store.get_proposal(proposal.proposal_id) is None


@pytest.mark.parametrize("kind", ["alpha", "beta"])
async def test_cancel_revokes_the_run(store, clock, kind):
    deadline, expires_at = _deadlines(clock)
    proposal = _propose(
        store,
        kind,
        development_only=True,
        deadline=deadline,
        expires_at=expires_at,
    )
    sibling = _propose(
        store,
        kind,
        development_only=True,
        deadline=deadline,
        expires_at=expires_at,
    )
    assert sibling.run_id == proposal.run_id
    assert sibling.proposal_id != proposal.proposal_id
    assert await DevelopmentDesk(store, ("cancel",)).present(proposal) is False
    assert store.is_live(proposal.proposal_id) is False
    assert store.is_live(sibling.proposal_id) is False
    assert store.get_proposal(proposal.proposal_id) is None
    assert store.get_proposal(sibling.proposal_id) is None
    assert await DevelopmentDesk(store, ("accept",)).present(sibling) is False
    with pytest.raises(ValueError, match="revoked"):
        _propose(
            store,
            kind,
            development_only=True,
            deadline=deadline,
            expires_at=expires_at,
        )


async def test_production_present_requires_proposal():
    with pytest.raises(TypeError):
        await ProductionDesk().present()


async def test_development_present_requires_proposal(store):
    with pytest.raises(TypeError):
        await DevelopmentDesk(store, ("accept",)).present()


async def test_describe_requires_proposal():
    with pytest.raises(TypeError):
        describe()


@pytest.mark.parametrize("kind", ["alpha", "beta"])
@pytest.mark.parametrize("development_only", [True, False])
async def test_production_desk_fails_closed(store, clock, kind, development_only):
    """Native production proof is NOT_RUN."""
    deadline, expires_at = _deadlines(clock)
    proposal = _propose(
        store,
        kind,
        development_only=development_only,
        deadline=deadline,
        expires_at=expires_at,
    )
    desk = ProductionDesk()
    assert desk.development_only is False
    with pytest.raises(DeskUnavailable) as caught:
        await desk.present(proposal)
    assert caught.value.reason == "no_supported_human_grant_channel"
    assert store.is_live(proposal.proposal_id) is False
    assert store.get_proposal(proposal.proposal_id) is proposal


@pytest.mark.parametrize("kind", ["alpha", "beta"])
async def test_accept_without_development_only_does_not_grant(store, clock, kind):
    deadline, expires_at = _deadlines(clock)
    proposal = _propose(
        store,
        kind,
        development_only=False,
        deadline=deadline,
        expires_at=expires_at,
    )
    assert await DevelopmentDesk(store, ("accept",)).present(proposal) is False
    assert store.is_live(proposal.proposal_id) is False
    assert store.get_proposal(proposal.proposal_id) is proposal


@pytest.mark.parametrize("kind", ["alpha", "beta"])
@pytest.mark.parametrize("limit", ["expires_at", "deadline"])
async def test_stale_or_expired_proposal_never_grants(store, clock, kind, limit):
    deadline, expires_at = _deadlines(clock)
    proposal = _propose(
        store,
        kind,
        development_only=True,
        deadline=deadline,
        expires_at=expires_at,
    )
    assert store.get_proposal(proposal.proposal_id) is proposal
    clock.advance((expires_at if limit == "expires_at" else deadline) - clock())
    assert store.get_proposal(proposal.proposal_id) is None
    assert await DevelopmentDesk(store, ("accept",)).present(proposal) is False
    assert store.is_live(proposal.proposal_id) is False


@pytest.mark.parametrize("kind", ["alpha", "beta"])
async def test_replaced_digest_never_grants(store, clock, kind):
    deadline, expires_at = _deadlines(clock)
    proposal = _propose(
        store,
        kind,
        development_only=True,
        deadline=deadline,
        expires_at=expires_at,
    )
    forged = replace(proposal, digest="0" * 64)
    assert forged.digest != proposal.digest
    assert await DevelopmentDesk(store, ("accept",)).present(forged) is False
    assert store.is_live(proposal.proposal_id) is False
    assert store.is_live(forged.proposal_id) is False
    assert store.get_proposal(proposal.proposal_id) is proposal


@pytest.mark.parametrize("kind", ["alpha", "beta"])
async def test_foreign_proposal_never_grants(store, clock, kind):
    deadline, expires_at = _deadlines(clock)
    other = AdmissionStore(clock=Clock(clock()))
    foreign = _propose(
        other,
        kind,
        development_only=True,
        deadline=deadline,
        expires_at=expires_at,
    )
    assert await DevelopmentDesk(store, ("accept",)).present(foreign) is False
    assert store.is_live(foreign.proposal_id) is False
    assert store.get_proposal(foreign.proposal_id) is None
    assert other.is_live(foreign.proposal_id) is False


@pytest.mark.parametrize("kind", ["alpha", "beta"])
async def test_restart_before_response_never_grants(store, clock, kind):
    deadline, expires_at = _deadlines(clock)
    proposal = _propose(
        store,
        kind,
        development_only=True,
        deadline=deadline,
        expires_at=expires_at,
    )

    async def drop():
        store.restart()

    desk = DevelopmentDesk(store, ("accept",), before_response=drop)
    assert await desk.present(proposal) is False
    assert store.is_live(proposal.proposal_id) is False
    assert store.get_proposal(proposal.proposal_id) is None


@pytest.mark.parametrize("kind", ["alpha", "beta"])
async def test_revoke_before_response_never_grants(store, clock, kind):
    deadline, expires_at = _deadlines(clock)
    proposal = _propose(
        store,
        kind,
        development_only=True,
        deadline=deadline,
        expires_at=expires_at,
    )

    async def cut():
        store.revoke(proposal.run_id)

    desk = DevelopmentDesk(store, ("accept",), before_response=cut)
    assert await desk.present(proposal) is False
    assert store.is_live(proposal.proposal_id) is False
    assert store.get_proposal(proposal.proposal_id) is None


@pytest.mark.parametrize("kind", ["alpha", "beta"])
@pytest.mark.parametrize("response", ["cancel", "decline"])
async def test_rewind_does_not_resurrect_a_closed_proposal(
    store, clock, kind, response
):
    deadline, expires_at = _deadlines(clock)
    proposal = _propose(
        store,
        kind,
        development_only=True,
        deadline=deadline,
        expires_at=expires_at,
    )
    assert await DevelopmentDesk(store, (response,)).present(proposal) is False
    clock.rewind_for_no_resurrection(500.0)
    assert store.is_live(proposal.proposal_id) is False
    assert await DevelopmentDesk(store, ("accept",)).present(proposal) is False
    assert store.is_live(proposal.proposal_id) is False


async def test_describe_alpha_exposes_exact_scope(store, clock):
    deadline, expires_at = _deadlines(clock)
    proposal = store.propose_alpha(
        "run-alpha",
        list(_ALPHA_URLS),
        ["fill", "click"],
        {"steps": 2, "bytes": 32},
        deadline=deadline,
        expires_at=expires_at,
        development_only=True,
    )
    assert proposal.canonical_urls == _ALPHA_URLS
    assert proposal.origins == frozenset(
        {"https://app.example:443", "https://other.example:443"}
    )
    assert proposal.operations == frozenset({"click", "fill"})
    assert dict(proposal.budgets) == {"bytes": 32.0, "steps": 2.0}
    assert all(math.isfinite(value) for value in proposal.budgets.values())
    assert proposal.deadline == deadline
    assert proposal.expires_at == expires_at
    assert math.isfinite(proposal.deadline)
    assert math.isfinite(proposal.expires_at)
    assert proposal.residual_risk == _RISK
    assert uuid.UUID(proposal.proposal_id).version == 4
    shown = describe(proposal, development_only=True)
    assert shown == {
        "budgets": {"bytes": 32.0, "steps": 2.0},
        "canonical_urls": list(_ALPHA_URLS),
        "deadline": deadline,
        "development_only": True,
        "digest": proposal.digest,
        "expires_at": expires_at,
        "operations": ["click", "fill"],
        "origins": [
            "https://app.example:443",
            "https://other.example:443",
        ],
        "proposal_id": proposal.proposal_id,
        "residual_risk": _RISK,
        "run_id": proposal.run_id,
    }
    assert json.loads(json.dumps(shown)) == shown
    assert set(_PROOF_FLAGS).isdisjoint(shown)
    assert store.is_live(proposal.proposal_id) is False


async def test_describe_beta_exposes_exact_scope(store, clock):
    deadline, expires_at = _deadlines(clock)
    proposal = _propose(
        store,
        "beta",
        development_only=True,
        deadline=deadline,
        expires_at=expires_at,
    )
    assert proposal.operation == "fill"
    assert proposal.destination_url == _BETA_URL
    assert proposal.destination_origin == "https://app.example:443"
    assert proposal.document_origin == "https://docs.example:443"
    assert proposal.semantic_fingerprint == _FINGERPRINT
    assert proposal.deadline == deadline
    assert proposal.expires_at == expires_at
    assert math.isfinite(proposal.deadline)
    assert math.isfinite(proposal.expires_at)
    assert uuid.UUID(proposal.proposal_id).version == 4
    shown = describe(proposal, development_only=True)
    assert shown == {
        "deadline": deadline,
        "destination_origin": "https://app.example:443",
        "development_only": True,
        "digest": proposal.digest,
        "document_origin": "https://docs.example:443",
        "expires_at": expires_at,
        "semantic_fingerprint": _FINGERPRINT,
        "operation": "fill",
        "proposal_id": proposal.proposal_id,
        "run_id": proposal.run_id,
        "destination_url": _BETA_URL,
    }
    assert json.loads(json.dumps(shown)) == shown
    assert set(_PROOF_FLAGS).isdisjoint(shown)
    assert store.is_live(proposal.proposal_id) is False


async def test_describe_production_label_is_not_a_grant(store, clock):
    """Native production proof is NOT_RUN."""
    deadline, expires_at = _deadlines(clock)
    proposal = _propose(
        store,
        "alpha",
        development_only=False,
        deadline=deadline,
        expires_at=expires_at,
    )
    shown = describe(proposal, development_only=False)
    assert shown["development_only"] is False
    assert shown["digest"] == proposal.digest
    assert shown["proposal_id"] == proposal.proposal_id
    assert shown["run_id"] == proposal.run_id
    assert set(_PROOF_FLAGS).isdisjoint(shown)
    assert store.is_live(proposal.proposal_id) is False


@pytest.mark.parametrize("kind", ["alpha", "beta"])
async def test_unsupported_response_only_terminalizes_its_own_proposal(store, clock, kind):
    deadline, expires_at = _deadlines(clock)
    first = _propose(store, kind, development_only=True, deadline=deadline, expires_at=expires_at)
    sibling = _propose(store, kind, development_only=True, deadline=deadline, expires_at=expires_at)
    assert await DevelopmentDesk(store, ("auto_accept",)).present(first) is False
    assert store.get_proposal(first.proposal_id) is None
    assert store._proposals[first.proposal_id] is first
    assert store.get_proposal(sibling.proposal_id) is sibling
    assert await DevelopmentDesk(store, ("accept",)).present(sibling) is True
    assert store.is_live(sibling.proposal_id) is True
