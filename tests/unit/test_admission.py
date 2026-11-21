"""Development-proposal admission cases. Not a production human proof.

Memory and a mock clock only. No network. A development_only flag is metadata,
not acceptance and not a human controller.
"""

from types import MappingProxyType

import pytest

from decision_browser.admission import (
    AdmissionStore,
    DevelopmentDesk,
    ProductionAuthority,
    canonical_origin,
    canonical_url,
)

RAW = "https://Example.COM:443/form"
ORIGIN = canonical_origin(RAW)
URL = canonical_url(RAW)


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


def _store(clock=None, **kwargs):
    return AdmissionStore(clock=clock or Clock(), **kwargs)


def _alpha(store, **kwargs):
    fields = {
        "run_id": "run-1",
        "urls": [RAW],
        "operations": ["fill"],
        "budgets": {"steps": 1},
        "deadline": 100.0,
        "expires_at": 100.0,
        "development_only": True,
    }
    fields.update(kwargs)
    return store.propose_alpha(**fields)


def _beta(store, **kwargs):
    fields = {
        "run_id": "run-1",
        "semantic_fingerprint": "fp",
        "operation": "fill",
        "document_origin": ORIGIN,
        "destination_origin": ORIGIN,
        "destination_url": URL,
        "deadline": 100.0,
        "expires_at": 100.0,
        "development_only": True,
    }
    fields.update(kwargs)
    return store.propose_beta(**fields)


def _accept(store, proposal):
    desk = store.development_desk()
    assert isinstance(desk, DevelopmentDesk)
    assert desk.accept(proposal.proposal_id, proposal.digest) is True
    assert store.is_live(proposal.proposal_id) is True


def _alpha_ok(store, proposal, **extra):
    fields = {
        "run_id": proposal.run_id,
        "operation": "fill",
        "document_origin": ORIGIN,
        "destination_origin": ORIGIN,
        "digest": proposal.digest,
    }
    fields.update(extra)
    return store.alpha_allows(**fields)


def _beta_fields(proposal, **extra):
    fields = {
        "run_id": proposal.run_id,
        "proposal_id": proposal.proposal_id,
        "digest": proposal.digest,
        "semantic_fingerprint": proposal.semantic_fingerprint,
        "operation": proposal.operation,
        "document_origin": proposal.document_origin,
        "destination_origin": proposal.destination_origin,
        "destination_url": proposal.destination_url,
    }
    fields.update(extra)
    return fields


def test_origin_includes_effective_443_and_url_omits_default_port():
    assert ORIGIN == "https://example.com:443"
    assert ":443" in ORIGIN
    assert URL == "https://example.com/form"
    assert ":443" not in URL
    proposal = _alpha(_store())
    assert proposal.origins == frozenset({ORIGIN})
    assert set(proposal.canonical_urls) == {URL}
    assert all(":443" not in item for item in proposal.canonical_urls)
    assert isinstance(proposal.residual_risk, str)
    assert "human-supplied-origins-are-not-approval" in proposal.residual_risk
    assert "autosave" in proposal.residual_risk
    assert "hidden mutation" in proposal.residual_risk


def test_alpha_copies_origins_operations_and_budgets():
    urls = [RAW]
    operations = ["fill", "click"]
    budgets = {"steps": 2}
    proposal = _alpha(_store(), urls=urls, operations=operations, budgets=budgets)
    urls.append("https://evil.example/x")
    operations.append("submit")
    budgets["steps"] = 9
    assert proposal.origins == frozenset({ORIGIN})
    assert proposal.operations == frozenset({"fill", "click"})
    assert isinstance(proposal.budgets, MappingProxyType)
    assert proposal.budgets["steps"] == 2
    with pytest.raises(TypeError):
        proposal.budgets["steps"] = 1


def test_expiry_attribute_is_expires_at():
    proposal = _alpha(_store())
    assert proposal.expires_at == 100.0
    assert "expiry" not in type(proposal).__dataclass_fields__


def test_pending_is_readable_and_not_live():
    store = _store()
    proposal = _alpha(store)
    assert store.get_proposal(proposal.proposal_id) == proposal
    assert store.is_live(proposal.proposal_id) is False
    assert _alpha_ok(store, proposal) is False


def test_production_metadata_cannot_accept():
    store = _store()
    proposal = _alpha(store, development_only=False)
    assert store.development_desk().accept(proposal.proposal_id, proposal.digest) is False
    assert store._accept_development(proposal.proposal_id, proposal.digest) is False
    assert store.is_live(proposal.proposal_id) is False
    assert _alpha_ok(store, proposal) is False
    assert ProductionAuthority().attest(proposal.proposal_id, proposal.digest) is False


def test_wrong_digest_foreign_run_doc_dest_and_annotate_deny():
    store = _store()
    proposal = _alpha(store)
    assert store.development_desk().accept(proposal.proposal_id, "wrong-digest") is False
    assert store.is_live(proposal.proposal_id) is False
    _accept(store, proposal)
    assert _alpha_ok(store, proposal, digest="wrong-digest") is False
    assert _alpha_ok(store, proposal, run_id="foreign-run") is False
    assert _alpha_ok(store, proposal, document_origin="https://other.example") is False
    assert _alpha_ok(store, proposal, destination_origin="https://other.example") is False
    assert _alpha_ok(store, proposal, operation="annotate") is False
    assert _alpha_ok(store, proposal, operation="scroll") is False
    assert _alpha_ok(store, proposal) is True


def test_propose_unsupported_annotate_rejected():
    with pytest.raises(ValueError):
        _alpha(_store(), operations=["annotate"])


def test_cancel_revoke_restart_and_expiry_invalidate():
    for name in ("cancel", "revoke", "restart"):
        clock = Clock()
        store = _store(clock)
        proposal = _alpha(store, deadline=10.0, expires_at=10.0)
        _accept(store, proposal)
        if name == "cancel":
            store.cancel(proposal.proposal_id)
        elif name == "revoke":
            store.revoke(proposal.run_id)
        else:
            store.restart()
        assert store.is_live(proposal.proposal_id) is False
        assert _alpha_ok(store, proposal) is False
    clock = Clock()
    store = _store(clock)
    proposal = _alpha(store, deadline=10.0, expires_at=10.0)
    _accept(store, proposal)
    assert store.get_proposal(proposal.proposal_id) == proposal
    clock.now = proposal.expires_at
    assert store.is_live(proposal.proposal_id) is False
    assert store.get_proposal(proposal.proposal_id) is None
    assert _alpha_ok(store, proposal) is False


def test_beta_fill_requires_live_alpha_and_consume_once():
    store = _store()
    alpha = _alpha(store)
    _accept(store, alpha)
    beta = _beta(
        store,
        destination_url=URL,
        deadline=alpha.deadline,
        expires_at=alpha.expires_at,
    )
    assert beta.deadline <= alpha.deadline
    assert beta.destination_url == URL
    assert ":443" not in beta.destination_url
    assert _beta_fields(beta)["destination_url"] == URL
    assert store.is_live(beta.proposal_id) is False
    assert store.beta_allows(**_beta_fields(beta)) is False
    _accept(store, beta)
    assert store.beta_allows(**_beta_fields(beta)) is True
    assert store.beta_allows(**_beta_fields(beta)) is True
    assert store.consume_beta(**_beta_fields(beta)) is True
    assert store.consume_beta(**_beta_fields(beta)) is False
    assert store.beta_allows(**_beta_fields(beta)) is False
    assert _alpha_ok(store, alpha) is True


def test_beta_mismatches_deny_without_consuming():
    store = _store()
    alpha = _alpha(store)
    _accept(store, alpha)
    beta = _beta(
        store,
        destination_url=URL,
        deadline=alpha.deadline,
        expires_at=alpha.expires_at,
    )
    assert beta.deadline <= alpha.deadline
    _accept(store, beta)
    for extra in (
        {"digest": "wrong-digest"},
        {"run_id": "foreign-run"},
        {"document_origin": "https://other.example"},
        {"destination_origin": "https://other.example"},
        {"destination_url": URL + "/other"},
        {"semantic_fingerprint": "other-fp"},
        {"operation": "annotate"},
        {"operation": "scroll"},
    ):
        assert store.beta_allows(**_beta_fields(beta, **extra)) is False
    assert store.consume_beta(**_beta_fields(beta)) is True


def test_outside_destination_does_not_expand_alpha():
    store = _store()
    alpha = _alpha(store)
    _accept(store, alpha)
    before = frozenset(alpha.origins)
    outside_raw = "https://evil.example/form"
    outside_origin = canonical_origin(outside_raw)
    outside_url = canonical_url(outside_raw)
    assert outside_origin == "https://evil.example:443"
    assert ":443" in outside_origin
    assert outside_origin not in before
    assert outside_url == "https://evil.example/form"
    assert ":443" not in outside_url
    beta = _beta(
        store,
        destination_origin=outside_origin,
        destination_url=outside_url,
        deadline=alpha.deadline,
        expires_at=alpha.expires_at,
    )
    assert beta.deadline <= alpha.deadline
    assert beta.destination_origin == outside_origin
    assert beta.destination_url == outside_url
    assert beta.run_id == alpha.run_id
    assert beta.document_origin == ORIGIN
    assert beta.operation == "fill"
    assert store.is_live(beta.proposal_id) is False
    assert store.beta_allows(**_beta_fields(beta)) is False
    _accept(store, beta)
    assert alpha.origins == before
    assert outside_origin not in alpha.origins
    assert _alpha_ok(store, alpha, destination_origin=outside_origin) is False
    assert store.beta_allows(**_beta_fields(beta)) is True
    assert store.beta_allows(**_beta_fields(beta)) is True
    assert store.consume_beta(**_beta_fields(beta)) is True
    assert store.consume_beta(**_beta_fields(beta)) is False
    assert store.beta_allows(**_beta_fields(beta)) is False
    assert alpha.origins == before
    assert _alpha_ok(store, alpha) is True
    assert _alpha_ok(store, alpha, destination_origin=outside_origin) is False


def test_missing_alpha_denial():
    store = _store()
    beta = _beta(store, destination_url=URL, deadline=100.0)
    _accept(store, beta)
    assert store.is_live(beta.proposal_id) is True
    assert store.get_proposal(beta.proposal_id) == beta
    assert store.beta_allows(**_beta_fields(beta)) is False
    assert store.consume_beta(**_beta_fields(beta)) is False
    assert store.beta_allows(**_beta_fields(beta)) is False


def test_alpha_expiry_denies_beta():
    clock = Clock()
    store = _store(clock)
    alpha = _alpha(store, deadline=30.0, expires_at=10.0)
    _accept(store, alpha)
    beta = _beta(store, destination_url=URL, deadline=alpha.deadline, expires_at=30.0)
    assert beta.deadline <= alpha.deadline
    assert beta.expires_at > alpha.expires_at
    _accept(store, beta)
    assert store.get_proposal(alpha.proposal_id) == alpha
    assert store.is_live(alpha.proposal_id) is True
    assert store.beta_allows(**_beta_fields(beta)) is True
    clock.now = alpha.expires_at
    assert store.is_live(alpha.proposal_id) is False
    assert store.get_proposal(alpha.proposal_id) is None
    assert store.get_proposal(beta.proposal_id) == beta
    assert store.is_live(beta.proposal_id) is True
    assert store.beta_allows(**_beta_fields(beta)) is False
    assert store.consume_beta(**_beta_fields(beta)) is False
    assert store.beta_allows(**_beta_fields(beta)) is False


def test_repeated_alpha_cannot_extend_original_deadline():
    clock = Clock()
    store = _store(clock)
    first = _alpha(store, deadline=10.0, expires_at=10.0)
    with pytest.raises(ValueError):
        _alpha(store, deadline=11.0, expires_at=11.0)
    _accept(store, first)
    assert store.get_proposal(first.proposal_id) == first
    assert _alpha_ok(store, first) is True
    clock.now = first.deadline
    assert store.is_live(first.proposal_id) is False
    assert store.get_proposal(first.proposal_id) is None
    assert _alpha_ok(store, first) is False


def test_beta_beyond_original_deadline_rejects():
    store = _store()
    alpha = _alpha(store, deadline=10.0, expires_at=10.0)
    _accept(store, alpha)
    with pytest.raises(ValueError):
        _beta(store, destination_url=URL, deadline=11.0, expires_at=11.0)
    kept = _beta(store, destination_url=URL, deadline=10.0, expires_at=10.0)
    assert kept.deadline == alpha.deadline == 10.0


def test_clock_rollback_does_not_resurrect_expired_record():
    clock = Clock()
    store = _store(clock)
    proposal = _alpha(store, deadline=10.0, expires_at=10.0)
    _accept(store, proposal)
    assert store.get_proposal(proposal.proposal_id) == proposal
    clock.now = proposal.expires_at
    assert store.is_live(proposal.proposal_id) is False
    assert store.get_proposal(proposal.proposal_id) is None
    clock.now = 0.0
    assert store.is_live(proposal.proposal_id) is False
    assert store.get_proposal(proposal.proposal_id) is None
    assert _alpha_ok(store, proposal) is False


def test_third_pending_proposal_rejected():
    store = _store(Clock(), max_proposals=2, max_runs=2, cleanup_grace=5)
    first = _alpha(store)
    second = _alpha(store)
    with pytest.raises(ValueError):
        _alpha(store)
    assert store.get_proposal(first.proposal_id) == first
    assert store.get_proposal(second.proposal_id) == second
    assert store.is_live(first.proposal_id) is False


def test_cleanup_after_deadline_plus_grace_clears_auxiliary_dicts():
    clock = Clock()
    store = _store(clock, max_proposals=2, max_runs=2, cleanup_grace=5)
    proposal = _alpha(store, deadline=10.0, expires_at=10.0)
    assert store.get_proposal(proposal.proposal_id) == proposal
    assert store.is_live(proposal.proposal_id) is False
    clock.now = 14.0
    assert clock.now < proposal.deadline + 5
    assert store.is_live(proposal.proposal_id) is False
    assert store.get_proposal(proposal.proposal_id) is None
    assert any(
        isinstance(value, (dict, set)) and len(value) > 0
        for value in vars(store).values()
    )
    clock.now = proposal.deadline + 5
    store.now()
    assert store.get_proposal(proposal.proposal_id) is None
    dicts = [value for value in vars(store).values() if isinstance(value, dict)]
    assert dicts
    for value in vars(store).values():
        if isinstance(value, (dict, set)):
            assert len(value) == 0
    again = _alpha(store, deadline=50.0, expires_at=50.0)
    assert store.get_proposal(again.proposal_id) == again


def test_repeated_identical_alpha_scopes_distinct_proposal_id_and_digest():
    store = _store()
    first = _alpha(store)
    second = _alpha(store)
    assert first.proposal_id != second.proposal_id
    assert first.digest != second.digest


def test_repeated_identical_beta_scopes_distinct_proposal_id_and_digest():
    store = _store()
    alpha = _alpha(store)
    _accept(store, alpha)
    first = _beta(store)
    second = _beta(store)
    assert first.proposal_id != second.proposal_id
    assert first.digest != second.digest


@pytest.mark.parametrize("kind", ["alpha", "beta"])
def test_deadline_plus_grace_must_be_finite_before_writes(kind):
    store = _store(cleanup_grace=1e308)
    builder = _alpha if kind == "alpha" else _beta
    with pytest.raises(ValueError):
        builder(store, deadline=1e308, expires_at=1e308)
    for name, value in vars(store).items():
        if isinstance(value, dict) and any(
            part in name for part in ("proposal", "run", "state", "dev", "record")
        ):
            assert value == {}


@pytest.mark.parametrize("failure", [RuntimeError("TEST_ONLY_CLOCK_DETAIL"),
                                     OSError("TEST_ONLY_CLOCK_DETAIL"),
                                     None, True, float("nan"), float("inf"), 10**1000])
def test_clock_failure_is_fixed_and_preserves_time_and_revocation(failure):
    reading = {"value": 0.0}

    def clock():
        value = reading["value"]
        if isinstance(value, Exception):
            raise value
        return value

    store = AdmissionStore(clock=clock)
    proposal = _alpha(store)
    _accept(store, proposal)
    before = (store._last_now, proposal.deadline, proposal.expires_at)
    reading["value"] = failure
    with pytest.raises(ValueError, match="^clock_error$") as error:
        store.now()
    assert str(error.value) == "clock_error"
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    assert (store._last_now, proposal.deadline, proposal.expires_at) == before
    # Revoke/cancel and dropping process-local state do not require a clock.
    store.revoke(proposal.run_id)
    assert store._state[proposal.proposal_id] == "revoked"
    assert proposal.proposal_id not in store._records
    store.restart()
    assert all(not value for value in vars(store).values() if isinstance(value, (dict, set)))


def test_unknown_run_revoke_remains_allocation_free_and_does_not_reserve_identity():
    store = _store()
    for index in range(100):
        store.revoke(f"unknown-{index}")
    assert all(not value for value in vars(store).values() if isinstance(value, (dict, set)))
    proposal = _alpha(store, run_id="unknown-0")
    _accept(store, proposal)
    assert store.is_live(proposal.proposal_id)


def test_terminal_proposals_keep_capacity_and_replay_evidence_until_run_expiry():
    clock = Clock()
    store = _store(clock, max_proposals=1, cleanup_grace=5)
    proposal = _alpha(store, deadline=10.0, expires_at=10.0)
    store.cancel(proposal.proposal_id)
    assert store.get_proposal(proposal.proposal_id) is None
    assert store._proposals[proposal.proposal_id] is proposal
    assert not store.development_desk().accept(proposal.proposal_id, proposal.digest)
    with pytest.raises(ValueError, match="^max_proposals$"):
        _alpha(store, deadline=10.0, expires_at=10.0)
    clock.now = 15.0
    store.cleanup()
    assert not store._proposals
    assert not store.development_desk().accept(proposal.proposal_id, proposal.digest)
    replacement = _alpha(store, deadline=30.0, expires_at=30.0)
    assert replacement.proposal_id != proposal.proposal_id
    _accept(store, replacement)


def test_recovered_clock_does_not_extend_or_resurrect_expired_grant():
    reading = {"value": 0.0}

    def clock():
        value = reading["value"]
        if isinstance(value, Exception):
            raise value
        return value

    store = AdmissionStore(clock=clock, cleanup_grace=5)
    proposal = _alpha(store, deadline=10.0, expires_at=8.0)
    _accept(store, proposal)
    reading["value"] = RuntimeError("TEST_ONLY_CLOCK_DETAIL")
    with pytest.raises(ValueError, match="^clock_error$"):
        store.is_live(proposal.proposal_id)
    assert (proposal.deadline, proposal.expires_at) == (10.0, 8.0)
    reading["value"] = 9.0
    assert store.is_live(proposal.proposal_id) is False
    reading["value"] = 0.0
    assert store.is_live(proposal.proposal_id) is False
    assert store.now() == 9.0
