"""Compatibility and live-store contracts preserved by bounded F23 cleanup."""

import pickle
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from decision_browser.admission import AdmissionStore
from decision_browser.owned_fixture_server import OwnedFixtureResource
from decision_browser.provider_config import (
    ProviderConfigError,
    ProviderSettings,
    TextSettings,
    load_provider_settings,
)


@pytest.mark.parametrize("value", [
    ProviderConfigError("invalid_profile"),
    TextSettings("http://localhost:8000/v1/chat/completions", "local-text", None),
    ProviderSettings("local", "http://localhost:8000/v1/systemone", "local", None, 26),
])
def test_provider_public_value_pickle_identity_survives_boundary_cleanup(value):
    assert type(value).__module__ == "decision_browser.provider_config"
    restored = pickle.loads(pickle.dumps(value))
    assert type(restored) is type(value)
    if isinstance(value, ProviderConfigError):
        assert restored.code == value.code and str(restored) == str(value)
    else:
        assert restored == value


def test_explicit_provider_mapping_ignores_saved_profile_without_finding_credentials(monkeypatch):
    from decision_browser import user_config

    def forbidden_profile_read(*args, **kwargs):
        raise AssertionError("explicit mappings must not read a profile")

    monkeypatch.setattr(user_config, "load_profile", forbidden_profile_read)
    actual = load_provider_settings({"DECISION_BROWSER_PROVIDER": "local"})
    assert actual == ProviderSettings(
        "local", "http://127.0.0.1:8000/v1/systemone", "nimble-latest", None, 26,
    )


def alpha(store):
    return store.propose_alpha(
        "cleanup-run", ["https://example.test/"], ["click"], {"steps": 1},
        deadline=30.0, expires_at=20.0, development_only=False,
    )


def test_store_pending_standard_form_preserves_identity_and_one_grant_lifecycle():
    store = AdmissionStore(clock=lambda: 10.0)
    proposal = alpha(store)
    assert store.standard_form_pending(proposal) is True
    assert store.standard_form_pending(replace(proposal)) is False
    assert store.accept_standard_form(proposal.proposal_id, proposal.digest) is True
    assert store.standard_form_pending(proposal) is False
    assert store.accept_standard_form(proposal.proposal_id, proposal.digest) is False


@pytest.mark.parametrize("terminal", ["cancel", "revoke", "expired"])
def test_store_pending_standard_form_rechecks_terminal_state_and_time(terminal):
    clock = [10.0]
    store = AdmissionStore(clock=lambda: clock[0])
    proposal = alpha(store)
    assert store.standard_form_pending(proposal) is True
    if terminal == "cancel":
        store.cancel(proposal.proposal_id)
    elif terminal == "revoke":
        store.revoke(proposal.run_id)
    else:
        clock[0] = 20.0
    assert store.standard_form_pending(proposal) is False
    assert store.accept_standard_form(proposal.proposal_id, proposal.digest) is False


def test_development_proposal_never_becomes_a_standard_form_pending_proposal():
    store = AdmissionStore(clock=lambda: 10.0)
    proposal = store.propose_alpha(
        "development-run", ["https://example.test/"], ["click"], {"steps": 1},
        deadline=30.0, expires_at=20.0, development_only=True,
    )
    assert store.standard_form_pending(proposal) is False


@pytest.mark.parametrize("optimize", [False, True])
def test_factory_explicit_key_checks_and_route_dispatch_survive_optimized_python(optimize):
    # Explicit checks in this child must also survive -O; child assert would
    # silently disappear and make this safety/compatibility control vacuous.
    code = '''
from types import SimpleNamespace
from decision_browser import server
from decision_browser.provider_config import ProviderSettings
for provider in ("openai", "typesafe"):
    server.load_provider_settings = lambda: ProviderSettings(provider, "https://example.test/v1/decisions", "example", None, 255)
    try:
        server.production_factory()
    except server.RunnerError as exc:
        if exc.code != "missing_api_key":
            raise SystemExit("wrong_failure_code")
    else:
        raise SystemExit("missing_key_reached_provider")
expected = SimpleNamespace()
server.load_provider_settings = lambda: ProviderSettings("openai", "https://api.openai.com/v1/decisions", "gpt-6-luna", "TEST_ONLY_KEY", 255)
def factory(*, api_key):
    if api_key != "TEST_ONLY_KEY":
        raise SystemExit("wrong_key")
    return expected
server.OpenAIProvider = factory
if server.production_factory() is not expected:
    raise SystemExit("wrong_provider")
print("explicit_factory_contract_pass")
'''
    result = subprocess.run(
        [sys.executable, *(["-O"] if optimize else []), "-I", "-c", code],
        check=True, capture_output=True, text=True, timeout=30,
    )
    assert result.stdout.strip() == "explicit_factory_contract_pass"


def test_owned_utf8_encoding_keeps_wire_byte_length_and_unicode_payload():
    body = '<style>p{color:black}</style><p>Привет 🙂</p>'
    resource = OwnedFixtureResource()
    resource._arm(SimpleNamespace(body=body, content_id="test-only-pinned-input"))
    header, payload = resource._response.split(b"\r\n\r\n", 1)
    assert payload == body.encode("utf-8")
    assert f"Content-Length: {len(payload)}".encode() in header
    assert len(payload) > len(body)


@pytest.mark.parametrize("codepoint", [0xD800, 0xDFFF])
def test_owned_utf8_encoding_still_rejects_unpaired_surrogates(codepoint):
    resource = OwnedFixtureResource()
    with pytest.raises(UnicodeEncodeError):
        resource._arm(SimpleNamespace(body=f'<style>p{{}}</style>{chr(codepoint)}', content_id="test-only"))
    assert resource._response == b""
