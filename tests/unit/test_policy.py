from urllib.parse import urlsplit

import pytest

from decision_browser.admission import AdmissionStore, canonical_origin, canonical_url
from decision_browser.policy import ActionScope, Policy

RUN, ORIGIN, FP, DEADLINE = "run-1", "http://127.0.0.1", "fp-1", 1
INSIDE = "http://127.0.0.1/app"
OUTSIDE = "https://notes.example"
CLEAR_OPS = ("click", "fill", "select", "scroll", "navigate", "enter", "submit")
LEXICON = ("checkout", "send", "publish", "delete", "%63heckout", "se%6ed", "publi%73h", "%64elete")


def _store():
    return AdmissionStore(lambda: 0)


def _accept(store, proposal):
    accepted = store.development_desk().accept(proposal.proposal_id, proposal.digest)
    assert accepted is True
    assert store.is_live(proposal.proposal_id) is True
    return proposal


def _origin_of(url):
    parsed = urlsplit(url)
    if parsed.port:
        return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    return f"{parsed.scheme}://{parsed.hostname}"


def _alpha(store, urls=(INSIDE,), operations=CLEAR_OPS):
    budgets = {op: 1 for op in operations}
    return _accept(store, store.propose_alpha(
        RUN, urls, operations, budgets, DEADLINE, development_only=True))


def _beta(store, destination_url=OUTSIDE, operation="click", fingerprint=FP, run_id=RUN,
          destination_origin=None):
    if destination_origin is None:
        destination_origin = _origin_of(destination_url)
    return _accept(store, store.propose_beta(
        run_id, fingerprint, operation, ORIGIN, destination_origin, destination_url, DEADLINE,
        development_only=True))


def _scope(operation="click", **kw):
    fields = {"signals": (), "signal_conflict": False, "target_key": "field", "destination_url": INSIDE}
    fields.update(kw)
    return ActionScope(
        fields.pop("run_id", RUN), operation,
        fields.pop("document_origin", ORIGIN), fields.pop("destination_origin", ORIGIN),
        fields.pop("semantic_fingerprint", FP), **fields)


def _pair(result):
    return (result.allowed, result.classification)


@pytest.mark.parametrize("operation", CLEAR_OPS)
def test_clear_ops_denied_without_alpha(operation):
    result = Policy(_store()).evaluate(_scope(operation), consume=False)
    assert result.allowed is False


@pytest.mark.parametrize("operation", CLEAR_OPS)
def test_clear_ops_allowed_inside_alpha(operation):
    store = _store()
    _alpha(store)
    result = Policy(store).evaluate(_scope(operation), consume=False)
    assert _pair(result) == (True, "clear_non_match")


@pytest.mark.parametrize("operation", ("screenshot", "wait"))
def test_screenshot_and_wait_observe_driver(operation):
    result = Policy(_store()).evaluate(_scope(operation), consume=False)
    assert (result.allowed, result.admission_class, result.classification) == (True, "observe_driver", "clear_non_match")


@pytest.mark.parametrize("operation", ("annotate", "keyboard"))
def test_unsupported_operations_deny(operation):
    store = _store()
    _alpha(store)
    result = Policy(store).evaluate(_scope(operation), consume=False)
    assert result.allowed is False


@pytest.mark.parametrize("token", LEXICON)
def test_lexicon_blocks_accepted_beta_and_fixture(token):
    url = f"{ORIGIN}/{token}"
    store = _store()
    _alpha(store)
    proposal = _beta(store, destination_url=url)
    policy = Policy(
        store, fixture_origin=ORIGIN, fixture_actions=frozenset({("click", token)}),
        development_only=True)
    result = policy.evaluate(_scope(
        "click", destination_url=url, target_key=token,
        proposal_id=proposal.proposal_id, action_digest=proposal.digest), consume=True)
    assert (result.allowed, result.classification, result.admission_class) == (False, "match", "consequential")
    assert result.reason == "consequential_action_blocked"
    assert store.beta_allows(
        run_id=RUN,
        proposal_id=proposal.proposal_id,
        digest=proposal.digest,
        semantic_fingerprint=FP,
        operation="click",
        document_origin=canonical_origin(ORIGIN),
        destination_origin=canonical_origin(_origin_of(url)),
        destination_url=canonical_url(url),
    ) is True


def test_explicit_signal_conflict_is_uncertain():
    store = _store()
    _alpha(store)
    result = Policy(store).evaluate(
        _scope("click", signals=("shown", "hidden"), signal_conflict=True), consume=False)
    assert result.classification == "uncertain"


def test_exact_beta_outside_alpha_previews_once():
    store = _store()
    _alpha(store, urls=(INSIDE,))
    outside_origin = _origin_of(OUTSIDE)
    assert outside_origin == "https://notes.example"
    assert OUTSIDE == "https://notes.example"
    proposal = _beta(store, destination_url=OUTSIDE, destination_origin=outside_origin)
    assert store.alpha_allows(
        run_id=RUN,
        operation="click",
        document_origin=canonical_origin(ORIGIN),
        destination_origin=canonical_origin(ORIGIN),
    ) is True
    assert store.alpha_allows(
        run_id=RUN,
        operation="click",
        document_origin=canonical_origin(ORIGIN),
        destination_origin=canonical_origin(outside_origin),
    ) is False
    blocked = Policy(store).evaluate(_scope(
        "click", destination_url=OUTSIDE, destination_origin=outside_origin), consume=False)
    assert (blocked.allowed, blocked.classification) == (False, "clear_non_match")
    scope = _scope(
        "click",
        destination_url=OUTSIDE,
        destination_origin=outside_origin,
        proposal_id=proposal.proposal_id,
        action_digest=proposal.digest,
        signal_conflict=True,
    )
    policy = Policy(store)
    assert _pair(policy.evaluate(scope, consume=False)) == (True, "uncertain")
    assert _pair(policy.evaluate(scope, consume=True)) == (True, "uncertain")
    replay = policy.evaluate(scope, consume=True)
    consumed = store.consume_beta(
        run_id=RUN,
        proposal_id=proposal.proposal_id,
        digest=proposal.digest,
        semantic_fingerprint=FP,
        operation="click",
        document_origin=canonical_origin(ORIGIN),
        destination_origin=canonical_origin(outside_origin),
        destination_url=canonical_url(OUTSIDE),
    )
    assert (replay.allowed, replay.classification, consumed) == (False, "uncertain", False)


@pytest.mark.parametrize("patch", (
    {"semantic_fingerprint": "fp-other"},
    {"run_id": "run-other"},
    {"proposal_id": "id-other"},
    {"action_digest": "digest-other"},
    {"operation": "fill"},
    {"destination_url": "http://127.0.0.1/elsewhere"},
))
def test_inexact_beta_denies_and_stays_uncertain(patch):
    store = _store()
    _alpha(store, urls=(INSIDE,))
    proposal = _beta(store)
    destination_url = patch.get("destination_url", OUTSIDE)
    scope = _scope(
        patch.get("operation", "click"),
        destination_url=destination_url,
        destination_origin=_origin_of(destination_url),
        proposal_id=patch.get("proposal_id", proposal.proposal_id),
        action_digest=patch.get("action_digest", proposal.digest),
        semantic_fingerprint=patch.get("semantic_fingerprint", FP),
        run_id=patch.get("run_id", RUN),
        signal_conflict=True)
    assert _pair(Policy(store).evaluate(scope, consume=False)) == (False, "uncertain")


def test_beta_without_alpha_denies():
    store = _store()
    proposal = _beta(store)
    result = Policy(store).evaluate(_scope(
        "click",
        destination_url=OUTSIDE,
        destination_origin=_origin_of(OUTSIDE),
        proposal_id=proposal.proposal_id,
        action_digest=proposal.digest,
        signal_conflict=True), consume=False)
    assert (result.allowed, result.classification) == (False, "uncertain")


def test_fixture_allows_exact_op_and_target_on_both_origins():
    policy = Policy(
        _store(), fixture_origin=ORIGIN, fixture_actions=frozenset({("click", "field")}),
        development_only=True)
    assert policy.evaluate(_scope("click"), consume=False).allowed is True


@pytest.mark.parametrize(("operation", "document_origin", "destination_origin", "target_key"), (
    ("click", "http://127.0.0.2", ORIGIN, "field"),
    ("click", ORIGIN, "http://127.0.0.2", "field"),
    ("fill", ORIGIN, ORIGIN, "field"),
    ("click", ORIGIN, ORIGIN, "other"),
))
def test_fixture_blocks_wrong_origin_op_or_target(
        operation, document_origin, destination_origin, target_key):
    policy = Policy(
        _store(), fixture_origin=ORIGIN, fixture_actions=frozenset({("click", "field")}),
        development_only=True)
    scope = _scope(
        operation, document_origin=document_origin,
        destination_origin=destination_origin, target_key=target_key)
    assert policy.evaluate(scope, consume=False).allowed is False


def test_default_configuration_with_fixture_map_raises():
    with pytest.raises(ValueError):
        Policy(_store(), fixture_origin=ORIGIN, fixture_actions=frozenset({("click", "field")}))


def test_public_fixture_origin_raises():
    with pytest.raises(ValueError):
        Policy(
            _store(), fixture_origin="https://example.com",
            fixture_actions=frozenset({("click", "field")}), development_only=True)


@pytest.mark.parametrize(("destination_url", "destination_origin"), (
    ("http://user:secret@127.0.0.1/app", ORIGIN),
    ("http://[::1/app", ORIGIN),
    ("http://127.0.0.1:99999/app", "http://127.0.0.1:99999"),
    (INSIDE, "http://127.0.0.1:8080"),
))
def test_invalid_urls_fail_closed(destination_url, destination_origin):
    store = _store()
    _alpha(store)
    result = Policy(store).evaluate(_scope(
        "click", destination_url=destination_url, destination_origin=destination_origin),
        consume=False)
    assert result.allowed is False


@pytest.mark.parametrize(("operation", "signals"), (
    ("select", ("Sort order",)),
    ("click", ("Apply order",)),
    ("enter", ("Sort order", "Apply order")),
    ("select", ("  SORT\tORDER  ",)),
))
def test_ordering_control_labels_are_ordinary_with_admission(operation, signals):
    store = _store()
    _alpha(store)
    decision = Policy(store).evaluate(_scope(operation, signals=signals))
    assert _pair(decision) == (True, "clear_non_match")


@pytest.mark.parametrize("signals", (
    ("Order",), ("Place order",), ("Apply purchase order",),
    ("Sort order and buy",), ("Apply order now",),
    ("Sort order", "Purchase"), ("Apply order", "Send"),
    ("Sort order", "Order"), ("Apply order", "Place order"),
))
def test_ordering_words_do_not_exempt_consequential_signals(signals):
    store = _store()
    _alpha(store)
    decision = Policy(store).evaluate(_scope(signals=signals))
    assert decision.allowed is False
    assert decision.reason == "consequential_action_blocked"


@pytest.mark.parametrize("route", ("/order", "/checkout", "/search?order=newest", "/%6frder"))
def test_ordering_label_never_exempts_destination_route(route):
    store = _store()
    _alpha(store)
    decision = Policy(store).evaluate(_scope(
        signals=("Apply order",), destination_url=ORIGIN + route))
    assert decision.allowed is False
    assert decision.reason == "consequential_action_blocked"


def test_ordering_control_still_requires_admission_and_unconflicted_signals():
    scope = _scope(signals=("Apply order",))
    assert Policy(_store()).evaluate(scope).reason == "no_independent_admission_basis"
    store = _store()
    _alpha(store)
    decision = Policy(store).evaluate(_scope(signals=("Apply order",), signal_conflict=True))
    assert decision.allowed is False
    assert decision.classification == "uncertain"


@pytest.mark.parametrize("identifier", (
    "checkout_now", "CHECKOUT_NOW", "placeOrder", "deleteAccount",
    "confirmPurchase", "pay_now", "HTMLPayment", "payments", "orders",
    "purchases", "checkouts", "transfers", "bookings",
))
@pytest.mark.parametrize("source", ("signal", "path", "query"))
def test_consequential_identifiers_block_with_ordinary_admission(identifier, source):
    store = _store()
    _alpha(store)
    fields = (
        {"signals": (identifier,)} if source == "signal" else
        {"destination_url": f"{ORIGIN}/{identifier}"} if source == "path" else
        {"destination_url": f"{INSIDE}?action={identifier}"}
    )
    decision = Policy(store).evaluate(_scope(**fields))
    assert decision.allowed is False
    assert decision.classification == "match"
    assert decision.admission_class == "consequential"
    assert decision.reason == "consequential_action_blocked"


@pytest.mark.parametrize("identifier", (
    "checkoutable", "purchasable", "buyer", "prepaymentInfoText", "payload",
    "sender", "publisher", "deletedness", "remover", "transferable", "border",
    "reorder", "orderly", "ordinary_anchor", "openDetails", "HTMLPreview",
))
@pytest.mark.parametrize("source", ("signal", "path", "query"))
def test_identifier_normalization_preserves_ordinary_near_matches(identifier, source):
    store = _store()
    _alpha(store)
    fields = (
        {"signals": (identifier,)} if source == "signal" else
        {"destination_url": f"{ORIGIN}/{identifier}"} if source == "path" else
        {"destination_url": f"{INSIDE}?action={identifier}"}
    )
    decision = Policy(store).evaluate(_scope(**fields))
    assert _pair(decision) == (True, "clear_non_match")


@pytest.mark.parametrize("signal", ("sort_order", "SortOrder", "applyOrder", "APPLY_ORDER"))
def test_normalized_ordering_labels_remain_ordinary(signal):
    store = _store()
    _alpha(store)
    decision = Policy(store).evaluate(_scope(signals=(signal,)))
    assert _pair(decision) == (True, "clear_non_match")


@pytest.mark.parametrize("signals", (
    ("SortOrderAndBuy",), ("apply_order_now",), ("sort_order", "placeOrder"),
))
def test_normalized_ordering_words_do_not_exempt_other_consequential_signals(signals):
    store = _store()
    _alpha(store)
    decision = Policy(store).evaluate(_scope(signals=signals))
    assert decision.reason == "consequential_action_blocked"
