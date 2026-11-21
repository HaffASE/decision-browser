"""Store-unit coverage for AdmissionStore.accept_standard_form.

Memory only. Not a native human, MCP, or browser proof.
"""

from decision_browser.admission import (
    AdmissionStore,
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


def _store(clock=None):
    return AdmissionStore(clock=clock or Clock())


def _alpha(store, **kwargs):
    fields = {
        "run_id": "run-1",
        "urls": [RAW],
        "operations": ["fill"],
        "budgets": {"steps": 1},
        "deadline": 100.0,
        "expires_at": 100.0,
        "development_only": False,
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
        "development_only": False,
    }
    fields.update(kwargs)
    return store.propose_beta(**fields)


def _beta_kwargs(proposal):
    return {
        "run_id": proposal.run_id,
        "proposal_id": proposal.proposal_id,
        "digest": proposal.digest,
        "semantic_fingerprint": proposal.semantic_fingerprint,
        "operation": proposal.operation,
        "document_origin": proposal.document_origin,
        "destination_origin": proposal.destination_origin,
        "destination_url": proposal.destination_url,
    }


def _unchanged(store, state, records, dev):
    assert dict(store._state) == state
    assert dict(store._records) == records
    assert dict(store._dev) == dev


def test_production_alpha_pending_becomes_live_once():
    store = _store()
    proposal = _alpha(store)
    pid = proposal.proposal_id
    assert store._dev[pid] is False
    assert store._state[pid] == "pending"
    assert store.is_live(pid) is False
    assert (
        store.alpha_allows(proposal.run_id, "fill", ORIGIN, ORIGIN, proposal.digest)
        is False
    )
    assert store.accept_standard_form(pid, proposal.digest) is True
    assert store.is_live(pid) is True
    assert store._state[pid] == "live"
    assert len(store._records) == 1
    record = store._records[pid]
    assert isinstance(record, str) and record != ""
    assert (
        store.alpha_allows(proposal.run_id, "fill", ORIGIN, ORIGIN, proposal.digest)
        is True
    )
    assert store.accept_standard_form(pid, proposal.digest) is False
    assert store._records == {pid: record}
    assert store._state[pid] == "live"
    assert store.is_live(pid) is True


def test_wrong_and_malformed_arguments_do_not_admit():
    store = _store()
    proposal = _alpha(store)
    state = dict(store._state)
    records = dict(store._records)
    dev = dict(store._dev)
    digest = proposal.digest
    pid = proposal.proposal_id
    pairs = [
        ("other-id", digest),
        (pid, "wrong-digest"),
        ("", digest),
        (pid, ""),
        (None, digest),
        (pid, None),
        (True, digest),
        (pid, False),
        (["id"], digest),
        (pid, ["digest"]),
        ({"id": 1}, digest),
        (pid, {"d": 1}),
        (b"id", digest),
        (pid, b"digest"),
        (1, digest),
        (pid, 1),
    ]
    for proposal_id, given in pairs:
        assert store.accept_standard_form(proposal_id, given) is False
    _unchanged(store, state, records, dev)
    assert store.is_live(pid) is False
    assert store.get_proposal(pid) == proposal


def test_development_form_rejected_and_private_accept_refuses_production():
    store = _store()
    dev = _alpha(store, development_only=True)
    prod = _alpha(store)
    assert store._dev[dev.proposal_id] is True
    assert store.accept_standard_form(dev.proposal_id, dev.digest) is False
    assert store.is_live(dev.proposal_id) is False
    assert dev.proposal_id not in store._records
    assert store._state[dev.proposal_id] == "pending"
    assert store._accept_development(prod.proposal_id, prod.digest) is False
    assert store.is_live(prod.proposal_id) is False
    assert prod.proposal_id not in store._records
    assert ProductionAuthority().attest(prod.proposal_id, prod.digest) is False
    assert store.development_desk().accept(dev.proposal_id, dev.digest) is True
    assert store.is_live(dev.proposal_id) is True
    assert len(store._records) == 1


def test_cancelled_revoked_and_expired_stay_closed():
    clock = Clock()
    store = _store(clock)
    cancelled = _alpha(store, deadline=10.0, expires_at=10.0)
    store.cancel(cancelled.proposal_id)
    assert store.accept_standard_form(cancelled.proposal_id, cancelled.digest) is False
    assert cancelled.proposal_id not in store._records
    assert store._state[cancelled.proposal_id] == "cancelled"
    revoked = _alpha(store, run_id="run-2", deadline=10.0, expires_at=10.0)
    store.revoke(revoked.run_id)
    assert store.accept_standard_form(revoked.proposal_id, revoked.digest) is False
    assert revoked.proposal_id not in store._records
    assert store._state[revoked.proposal_id] == "revoked"
    expired = _alpha(store, run_id="run-3", deadline=10.0, expires_at=10.0)
    clock.now = expired.expires_at
    assert store.accept_standard_form(expired.proposal_id, expired.digest) is False
    assert expired.proposal_id not in store._records
    assert store.is_live(expired.proposal_id) is False
    assert store._state[expired.proposal_id] != "live"
    assert len(store._records) == 0


def test_production_beta_accepts_then_consume_requires_live_alpha():
    store = _store()
    alpha = _alpha(store)
    beta = _beta(store, deadline=alpha.deadline, expires_at=alpha.expires_at)
    fields = _beta_kwargs(beta)
    assert store._dev[beta.proposal_id] is False
    assert store.accept_standard_form(beta.proposal_id, beta.digest) is True
    assert store.is_live(beta.proposal_id) is True
    assert len(store._records) == 1
    assert store.beta_allows(**fields) is False
    assert store.consume_beta(**fields) is False
    assert store.is_live(beta.proposal_id) is True
    assert beta.proposal_id in store._records
    assert store.accept_standard_form(alpha.proposal_id, alpha.digest) is True
    assert store.alpha_allows(alpha.run_id, "fill", ORIGIN, ORIGIN) is True
    assert len(store._records) == 2
    assert store.beta_allows(**fields) is True
    assert store.consume_beta(**fields) is True
    assert store.consume_beta(**fields) is False
    assert store.beta_allows(**fields) is False
    assert store.is_live(beta.proposal_id) is False
    assert store.is_live(alpha.proposal_id) is True
    assert list(store._records) == [alpha.proposal_id]
    assert store.accept_standard_form(beta.proposal_id, beta.digest) is False
    assert list(store._records) == [alpha.proposal_id]
