from __future__ import annotations

import hashlib
import socket
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

import decision_browser.owned_fixture as catalog
from decision_browser.owned_fixture import (
    CONTENT_IDS,
    DOCUMENTS,
    resolve,
    validate_literals,
)

_PINS = {
    "clean": "02ba761051ac15bd4b5c1762266efe1fba9fb40d8cef50b9871a112765ff172c",
    "clipping": "2014082eb93e8f11ffb8b51a0086ef2446df740c51a781003466db01ddc63ff6",
    "overlap": "f6c992423ebfc1f34cbff1693c64ddea1d59d7c01883274fefb1cae0df7dc47f",
    "responsive": "adf76193d6ebd2c5e10095f8b78e104528b4ecc13ffd11c109cb0f5737be7648",
}
_SIGNALS = {
    "clean": ("Clean control", "Search", "atlas", "Search"),
    "clipping": ("Clipping", "Clipped action"),
    "overlap": ("Overlap", "Alpha", "Beta", "Ignore A", "Ignore B"),
    "responsive": ("Responsive", "Overflows every owned viewport"),
}
_SOURCE = Path(__file__).parents[2] / "src" / "decision_browser" / "owned_fixture.py"
_FORBIDDEN = [
    "http://user:pw@owned-fixture.invalid/v1/clean",
    "http://user@owned-fixture.invalid/v1/clean",
    "http://owned-fixture.invalid/v1/clean?q=1",
    "http://owned-fixture.invalid/v1/clean#part",
    "HTTP://owned-fixture.invalid/v1/clean",
    "http://OWNED-FIXTURE.invalid/v1/clean",
    "http://owned-fixture.invalid/v1/cle%61n",
    "http://owned-fixture.invalid/v1/clean/",
    "http://owned-fixture.invalid:80/v1/clean",
    "https://owned-fixture.invalid/v1/clean",
    "http://localhost/v1/clean",
    "http://127.0.0.1/v1/clean",
    "http://127.0.0.1:9/v1/clean",
    "http://owned-fixture.invalid/v1/other",
]


def _arm(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    hits = [0]

    def boom(*_args: object, **_kwargs: object) -> None:
        hits[0] += 1
        raise AssertionError("network")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    return hits


def test_independent_pins_match_bytes() -> None:
    assert set(DOCUMENTS) == set(_PINS)
    for key, pin in _PINS.items():
        raw = DOCUMENTS[key].encode("utf-8")
        assert raw and hashlib.sha256(raw).hexdigest() == pin


def test_validate_mismatch_and_missing() -> None:
    with pytest.raises(RuntimeError, match="owned fixture catalog mismatch"):
        validate_literals({"clean": ""})
    changed = dict(DOCUMENTS)
    changed.pop("overlap")
    with pytest.raises(RuntimeError, match="owned fixture catalog mismatch"):
        validate_literals(changed)
    changed = dict(DOCUMENTS)
    changed["clean"] = changed["clean"].replace("Clean control", "Cleaned control", 1)
    with pytest.raises(RuntimeError, match="owned fixture catalog mismatch"):
        validate_literals(changed)


def test_tampered_import_raises_with_no_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hits = _arm(monkeypatch)
    source = _SOURCE.read_text(encoding="utf-8").replace("Clean control", "Cleaned control", 1)
    path = tmp_path / "tampered_owned_fixture.py"
    path.write_text(source, encoding="utf-8")
    module = types.ModuleType("tampered_owned_fixture")
    with pytest.raises(RuntimeError, match="owned fixture catalog mismatch"):
        SourceFileLoader(module.__name__, str(path)).exec_module(module)
    assert hits[0] == 0


@pytest.mark.parametrize("state", ["match", "uncertain"])
def test_classify_refusal(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    monkeypatch.setattr(catalog, "classify", lambda _scope: state)
    with pytest.raises(RuntimeError, match="owned fixture catalog mismatch"):
        resolve(CONTENT_IDS["clean"])


def test_exact_ids_signals_and_no_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    hits = _arm(monkeypatch)
    for key, content_id in CONTENT_IDS.items():
        found = resolve(content_id)
        assert found is not None
        assert found.content_id == content_id
        assert found.body.encode("utf-8") == DOCUMENTS[key].encode("utf-8")
        assert found.signals == _SIGNALS[key]
    assert hits[0] == 0


@pytest.mark.parametrize("content_id", _FORBIDDEN)
def test_forbidden_variants_are_none(monkeypatch: pytest.MonkeyPatch, content_id: str) -> None:
    hits = _arm(monkeypatch)
    assert resolve(content_id) is None
    assert hits[0] == 0


def test_no_forbidden_exports() -> None:
    banned = {"CaptureEvidence", "sanitized", "admission", "fixture_mapped"}
    assert banned.isdisjoint(vars(catalog))
