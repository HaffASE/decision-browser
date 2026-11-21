import pytest

from decision_browser.admission import AdmissionStore
from decision_browser.policy import ActionScope, Policy

URL = "https://www.google.com/travel/flights"
ORIGIN = "https://www.google.com"


def alpha(store, **kwargs):
    return store.propose_alpha(
        "task", kwargs.pop("urls", [URL]), ["click", "navigate"],
        {"actions": 10}, 10, **kwargs,
    )


def scope(**kwargs):
    values = {
        "run_id": "task", "operation": "click", "document_origin": ORIGIN,
        "destination_origin": ORIGIN, "semantic_fingerprint": "selected",
        "destination_url": URL,
    }
    values.update(kwargs)
    return ActionScope(**values)


def grant(store, proposal):
    return store.admit_task_scope(proposal.proposal_id, proposal.digest)


def test_disabled_by_default():
    store = AdmissionStore(lambda: 0)
    assert store.task_scope_enabled is False
    assert grant(store, alpha(store)) is False


@pytest.mark.parametrize("enabled", [0, 1, None, "true"])
def test_flag_is_strict_bool(enabled):
    with pytest.raises(TypeError, match="task_scope_enabled"):
        AdmissionStore(task_scope_enabled=enabled)


def test_flag_is_read_only():
    store = AdmissionStore(task_scope_enabled=True)
    with pytest.raises(AttributeError):
        store.task_scope_enabled = False


def test_exact_scope_and_distinct_policy_label(monkeypatch):
    store = AdmissionStore(lambda: 0, task_scope_enabled=True)
    proposal = alpha(store)
    monkeypatch.setattr(store, "accept_standard_form", lambda *_: pytest.fail("manual path"))
    assert grant(store, proposal) is True
    assert grant(store, proposal) is False
    assert store.is_live(proposal.proposal_id)
    assert Policy(store).evaluate(scope()).admission_class == "ordinary_task_scoped"
    assert store.task_scope_allows("task", "click", ORIGIN, ORIGIN)
    assert not store.task_scope_allows("other", "click", ORIGIN, ORIGIN)
    assert not store.task_scope_allows("task", "fill", ORIGIN, ORIGIN)
    assert not store.task_scope_allows("task", "click", ORIGIN, "https://example.com")
    assert not store.task_scope_allows("task", "click", "https://example.com", ORIGIN)
    assert not Policy(store).evaluate(scope(signal_conflict=True)).allowed


def test_manual_label_unchanged():
    store = AdmissionStore(lambda: 0, task_scope_enabled=True)
    proposal = alpha(store)
    assert store.accept_standard_form(proposal.proposal_id, proposal.digest)
    assert not grant(store, proposal)
    assert not store.task_scope_allows("task", "click", ORIGIN, ORIGIN)
    assert Policy(store).evaluate(scope()).admission_class == "ordinary_admitted"


@pytest.mark.parametrize("kwargs", [
    {"development_only": True}, {"urls": [URL, ORIGIN + "/search"]},
    {"urls": [URL, "https://example.com"]},
])
def test_ineligible_alpha(kwargs):
    store = AdmissionStore(lambda: 0, task_scope_enabled=True)
    assert not grant(store, alpha(store, **kwargs))


def test_beta_and_unknown_and_wrong_digest_denied():
    store = AdmissionStore(lambda: 0, task_scope_enabled=True)
    proposal = store.propose_beta("task", "fp", "click", ORIGIN, ORIGIN, URL, 10)
    assert not grant(store, proposal)
    assert not store.admit_task_scope("unknown", "digest")
    proposal = alpha(store)
    assert not store.admit_task_scope(proposal.proposal_id, "incorrect")
    assert not store.admit_task_scope(proposal.proposal_id, None)


@pytest.mark.parametrize("signals", [("Buy",), ("Забронировать",), ("Send",)])
def test_consequential_blocked_after_task_grant(signals):
    store = AdmissionStore(lambda: 0, task_scope_enabled=True)
    assert grant(store, alpha(store))
    decision = Policy(store).evaluate(scope(signals=signals))
    assert not decision.allowed
    assert decision.admission_class == "consequential"
    assert decision.reason == "consequential_action_blocked"


@pytest.mark.parametrize("termination", ["cancel", "revoke", "restart", "cleanup"])
def test_lifecycle_removes_scope_mark_and_record(termination):
    current = [0]
    store = AdmissionStore(lambda: current[0], task_scope_enabled=True, cleanup_grace=0)
    proposal = alpha(store)
    assert grant(store, proposal)
    if termination == "cleanup":
        current[0] = 10
        store.cleanup()
    elif termination == "revoke":
        store.revoke("task")
    elif termination == "cancel":
        store.cancel(proposal.proposal_id)
    else:
        store.restart()
    assert not store._task_scopes
    assert not store._records
    assert not store.task_scope_allows("task", "click", ORIGIN, ORIGIN)
    assert not grant(store, proposal)


@pytest.mark.parametrize("termination", ["expired", "revoked", "cancelled"])
def test_pending_terminated_proposal_cannot_grant(termination):
    current = [0]
    store = AdmissionStore(lambda: current[0], task_scope_enabled=True)
    proposal = alpha(store, expires_at=5)
    if termination == "expired":
        current[0] = 5
    elif termination == "revoked":
        store.revoke("task")
    else:
        store.cancel(proposal.proposal_id)
    assert not grant(store, proposal)


def test_expiry_denies_before_cleanup_grace():
    current = [0]
    store = AdmissionStore(lambda: current[0], task_scope_enabled=True)
    assert grant(store, alpha(store, expires_at=5))
    current[0] = 5
    assert not store.task_scope_allows("task", "click", ORIGIN, ORIGIN)


def test_unrelated_manual_alpha_cannot_borrow_task_label():
    store = AdmissionStore(lambda: 0, task_scope_enabled=True)
    assert grant(store, alpha(store))
    manual = store.propose_alpha(
        "task", ["https://example.com"], ["fill"], {"actions": 1}, 10,
    )
    assert store.accept_standard_form(manual.proposal_id, manual.digest)
    assert not store.task_scope_allows("task", "fill", "https://example.com", "https://example.com")
    decision = Policy(store).evaluate(scope(
        operation="fill", document_origin="https://example.com",
        destination_origin="https://example.com", destination_url="https://example.com",
    ))
    assert decision.allowed
    assert decision.admission_class == "ordinary_admitted"


def test_task_alpha_cannot_supply_manual_beta_basis():
    store = AdmissionStore(lambda: 0, task_scope_enabled=True)
    assert grant(store, alpha(store))
    beta = store.propose_beta("task", "selected", "click", ORIGIN, ORIGIN, URL, 10)
    assert store.accept_standard_form(beta.proposal_id, beta.digest)
    uncertain = scope(signal_conflict=True, proposal_id=beta.proposal_id, action_digest=beta.digest)
    assert not store.beta_allows(
        "task", beta.proposal_id, beta.digest, "selected", "click", ORIGIN, ORIGIN, URL,
    )
    assert not Policy(store).evaluate(uncertain, consume=True).allowed
    assert store.is_live(beta.proposal_id)
    manual = alpha(store)
    assert store.accept_standard_form(manual.proposal_id, manual.digest)
    assert store.beta_allows(
        "task", beta.proposal_id, beta.digest, "selected", "click", ORIGIN, ORIGIN, URL,
    )
    assert Policy(store).evaluate(uncertain, consume=True).admission_class == "ordinary_beta_admitted"
