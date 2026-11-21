"""Unit tests for bounded artifacts and the evidence journal."""

import json

import pytest

from decision_browser.artifacts import ArtifactError, ArtifactStore, Journal

_PNG = "image/png"
_BIN = "application/octet-stream"
_SECRET = "seed-secret"
_MARK = "[REDACTED]"


def _raises(fn, code: str) -> None:
    with pytest.raises(ArtifactError) as caught:
        fn()
    assert caught.value.code == code


def test_bytecap_atomic_and_blocked_binary() -> None:
    store = ArtifactStore(10, blocked_literals=(_SECRET,))
    first = store.put_bytes(b"12345", _BIN)
    _raises(lambda: store.put_bytes(b"123456", _BIN), "over_bytes")
    _raises(
        lambda: store.put_bytes(b"xx" + _SECRET.encode() + b"yy", _BIN), "blocked_bytes"
    )
    assert store.stored_bytes == 5
    assert store.read(first).content == b"12345"


def test_path_ids_rejected() -> None:
    store = ArtifactStore(100)
    made = store.put_bytes(b"ab", _PNG)
    assert store.read(made).sha256
    for bad in ("../" + made, made + "/x", "/tmp/" + made, "A" * 32, "g" * 32, ""):
        _raises(lambda bad=bad: store.read(bad), "bad_id")
    _raises(lambda: store.read("ab" * 16), "unknown_id")


def test_nested_secrets_keys_and_values() -> None:
    store = ArtifactStore(100_000, blocked_literals=(_SECRET,))
    payload = {
        "cookie": "header-value",
        "Authorization": "Bearer z",
        "note": "cookie",
        "token": {"n": _SECRET},
        "wrap": {"k": _SECRET, "plain": "ok", "nested": [{"h": f"pre-{_SECRET}-post"}]},
        "label": f"id-{_SECRET}",
    }
    raw = store.read(store.put_json(payload)).content.decode()
    body = json.loads(raw)
    assert _SECRET not in raw
    assert "cookie" not in body and "token" not in body and "Authorization" not in raw
    assert "Bearer" not in raw
    assert body["note"] == "cookie"
    assert body["wrap"]["plain"] == "ok"
    assert _MARK in raw


def test_urls_userinfo_query_fragment_and_bad_port() -> None:
    store = ArtifactStore(100_000, blocked_literals=("portsecret",))
    good = "http://user:pw@host.example/a/b?q=1&x=2#frag"
    bad = "http://user:portsecret@example.com:99999/admin"
    worse = "https://example.com:not-a-port/hidden"
    text = store.read(store.put_text(f"G {good} B {bad} W {worse}")).content.decode()
    assert "http://host.example/a/b" in text
    assert "user:pw" not in text and "q=1" not in text and "frag" not in text
    assert "99999" not in text and "not-a-port" not in text and "portsecret" not in text
    assert text.count(_MARK) >= 2


def test_nonfinite_floats_atomic() -> None:
    store = ArtifactStore(1000)
    for payload in ({"v": float("nan")}, [float("inf")], float("-inf")):
        _raises(lambda payload=payload: store.put_json(payload), "non_finite")
    assert store.stored_bytes == 0
    kept = json.loads(store.read(store.put_json({"v": 1.5})).content)
    assert kept["v"] == 1.5


def test_journal_unknown_attempt_retained() -> None:
    store = ArtifactStore(500_000)
    journal = Journal(store)
    attempt = journal.before_dispatch("run-1", "digest", "finger", "read")
    other = journal.before_dispatch("run-2", "digest", "finger", "read")
    outcome = journal.outcome("run-1", attempt, "acknowledged")
    failure = journal.post_observe_failure("run-1", attempt)
    baseline = store.stored_bytes
    _raises(
        lambda: journal.outcome("run-1", "not-an-attempt", "acknowledged"),
        "bad_attempt",
    )
    _raises(lambda: journal.outcome("run-2", attempt, "rejected"), "bad_attempt")
    _raises(
        lambda: journal.outcome("run-1", "ab" * 16, "outcome_unknown"), "bad_attempt"
    )
    _raises(lambda: journal.post_observe_failure("run-1", "cd" * 16), "bad_attempt")
    assert store.stored_bytes == baseline
    assert store.read(attempt).id == attempt
    assert store.read(other).id == other
    assert json.loads(store.read(attempt).content)["event"] == "before_dispatch"
    assert json.loads(store.read(outcome).content)["status"] == "acknowledged"
    last = json.loads(store.read(failure).content)
    assert last["event"] == "post_observe_failure"
    assert last["status"] == "outcome_unknown"
    assert last["attempt_id"] == attempt
    assert last["run_id"] == "run-1"


from decision_browser.budget import Budget, BudgetExceeded
from decision_browser.contracts import Limits

_MIB = 1024 * 1024


def test_eleven_mib_binary_fits_hundred_mib_cap() -> None:
    payload = bytes(11 * _MIB)
    store = ArtifactStore(100 * _MIB)
    made = store.put_bytes(payload, _BIN)
    assert store.read(made).content == payload
    assert store.stored_bytes == 11 * _MIB


def test_constructor_hundred_mib_bound() -> None:
    ArtifactStore(100 * _MIB)
    _raises(lambda: ArtifactStore(100 * _MIB + 1), "bad_max_bytes")


def test_shared_budget_matches_retained_and_keeps_deadline() -> None:
    clock: list[float] = [0.0]
    budget = Budget(
        Limits(max_seconds=10.0, max_stored_bytes=4096), clock=lambda: clock[0]
    )
    opened = budget.original_deadline
    store = ArtifactStore(
        4096, blocked_literals=("secret-token",), reserve_bytes=budget.reserve_stored
    )
    journal = Journal(store)
    body = store.put_json({"note": "café secret-token", "token": "hidden"})
    attempt = journal.before_dispatch("run-1", "digest", "finger", "ordinary_admitted")
    outcome = journal.outcome("run-1", attempt, "outcome_unknown")
    measured = sum(len(store.read(item).content) for item in (body, attempt, outcome))
    assert budget.usage().stored_bytes == store.stored_bytes == measured
    assert opened == 10.0 and budget.original_deadline == opened
    clock[0] = opened
    held_store, held_usage, held_items = (
        store.stored_bytes,
        budget.usage(),
        len(store._items),
    )
    with pytest.raises(BudgetExceeded) as caught:
        store.put_text("after-deadline")
    assert caught.value.code == "deadline_expired"
    assert store.stored_bytes == held_store and len(store._items) == held_items
    assert budget.usage() == held_usage and budget.original_deadline == opened


def test_budget_stored_reject_does_not_commit() -> None:
    budget = Budget(Limits(max_stored_bytes=1))
    store = ArtifactStore(64, reserve_bytes=budget.reserve_stored)
    held = budget.usage()
    with pytest.raises(BudgetExceeded) as caught:
        store.put_bytes(b"ab", _BIN)
    assert caught.value.code == "stored_bytes"
    assert store.stored_bytes == 0 and len(store._items) == 0
    assert budget.usage() == held


@pytest.mark.parametrize(
    ("setup", "code"), [("bytes", "over_bytes"), ("count", "over_count")]
)
def test_store_cap_rejects_before_callback(setup: str, code: str) -> None:
    calls: list[int] = []
    budget = Budget(Limits(max_stored_bytes=64))

    def reserve(n: int) -> None:
        calls.append(n)
        budget.reserve_stored(n)

    if setup == "bytes":
        store = ArtifactStore(1, reserve_bytes=reserve)
        attempt = lambda: store.put_bytes(b"ab", _BIN)
    else:
        store = ArtifactStore(64, max_artifacts=1, reserve_bytes=reserve)
        store.put_bytes(b"a", _BIN)
        calls.clear()
        attempt = lambda: store.put_bytes(b"b", _BIN)
    held_calls, held_usage = list(calls), budget.usage()
    held_stored, held_items = store.stored_bytes, len(store._items)
    _raises(attempt, code)
    assert calls == held_calls and budget.usage() == held_usage
    assert store.stored_bytes == held_stored and len(store._items) == held_items
