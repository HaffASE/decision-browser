"""Redaction collisions must not overwrite an existing audit key."""

import json

from decision_browser.artifacts import ArtifactStore, Journal

_MARK = "[REDACTED]"


def _body(store: ArtifactStore, payload: dict[str, object]) -> dict[str, object]:
    return json.loads(store.read(store.put_json(payload)).content)


def _folded(body: dict[str, object]) -> list[str]:
    return [key for key in body if key == _MARK or key.startswith(f"{_MARK}#")]


def test_occupied_secret_suffixes_are_not_overwritten() -> None:
    store = ArtifactStore(100_000)
    payload = {
        f"{_MARK}#2": "keep-2",
        f"{_MARK}#3": "keep-3",
        "cookie": "left-value",
        "token": "right-value",
        "password": "extra-value",
    }
    raw = store.read(store.put_json(payload)).content.decode()
    body = json.loads(raw)
    assert body[f"{_MARK}#2"] == "keep-2"
    assert body[f"{_MARK}#3"] == "keep-3"
    assert "left-value" not in raw
    assert "right-value" not in raw
    assert "extra-value" not in raw
    assert "cookie" not in raw and "token" not in raw and "password" not in raw
    folded = _folded(body)
    assert len(folded) == 5
    assert all(body[key] == _MARK for key in folded if key not in {f"{_MARK}#2", f"{_MARK}#3"})


def test_occupied_blocked_literal_keys_keep_values() -> None:
    store = ArtifactStore(100_000, blocked_literals=("ALPHA", "BETA", "GAMMA"))
    payload = {
        f"{_MARK}#2": "keep-2",
        f"{_MARK}#3": "keep-3",
        "ALPHA": "left-value",
        "BETA": "right-value",
        "GAMMA": "extra-value",
    }
    raw = store.read(store.put_json(payload)).content.decode()
    body = json.loads(raw)
    assert body[f"{_MARK}#2"] == "keep-2"
    assert body[f"{_MARK}#3"] == "keep-3"
    assert "ALPHA" not in raw and "BETA" not in raw and "GAMMA" not in raw
    folded = _folded(body)
    assert len(folded) == 5
    assert sorted(str(body[key]) for key in folded) == sorted(
        ["keep-2", "keep-3", "left-value", "right-value", "extra-value"]
    )


def test_distinct_keys_round_trip() -> None:
    store = ArtifactStore(10_000)
    assert _body(store, {"plain": "ok", "other": "still"}) == {
        "plain": "ok",
        "other": "still",
    }


def test_two_secret_keys_keep_separate_slots() -> None:
    store = ArtifactStore(10_000)
    raw = store.read(
        store.put_json({"plain": "ok", "cookie": "c-value", "token": "t-value"})
    ).content.decode()
    body = json.loads(raw)
    assert body["plain"] == "ok"
    assert "c-value" not in raw and "t-value" not in raw and "cookie" not in raw
    folded = _folded(body)
    assert len(folded) == 2
    assert all(body[key] == _MARK for key in folded)


def test_journal_append_preserves_occupied_audit_key() -> None:
    store = ArtifactStore(100_000)
    journal = Journal(store)
    artifact_id = journal.append(
        "note",
        "run-1",
        **{
            f"{_MARK}#2": "survivor",
            "cookie": "c-value",
            "token": "t-value",
        },
    )
    raw = store.read(artifact_id).content.decode()
    body = json.loads(raw)
    assert body[f"{_MARK}#2"] == "survivor"
    assert body["event"] == "note"
    assert body["run_id"] == "run-1"
    assert "c-value" not in raw and "t-value" not in raw
