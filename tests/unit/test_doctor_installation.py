"""Doctor distinguishes registration on disk from a connected client."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('installation_doctor', ROOT / 'skills/decision-browser/scripts/doctor.py')
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)


def test_missing_registration_has_action(tmp_path):
    result = doctor._installation(str(tmp_path))
    assert result['codex']['registration'] == 'missing'
    assert result['client_connection'] == 'NOT_RUN'
    assert result['next_steps']


def test_registered_does_not_claim_connection_or_leak_config(tmp_path):
    folder = tmp_path / '.codex'
    folder.mkdir()
    (folder / 'config.toml').write_text('[mcp_servers.decision_browser]\ncommand="/missing/python"\n[mcp_servers.decision_browser.env]\nOPENAI_API_KEY="SECRET-CANARY"\n')
    result = doctor._installation(str(tmp_path))
    assert result['codex']['registration'] == 'present'
    assert result['codex']['runtime_exists'] is False
    assert result['client_connection'] == 'NOT_RUN'
    assert 'SECRET-CANARY' not in json.dumps(result)


def test_malformed_config_reports_fixed_error(tmp_path):
    (tmp_path / '.mcp.json').write_text('secret invalid json')
    result = doctor._installation(str(tmp_path))
    assert result['claude']['registration'] == 'invalid'
    assert 'secret' not in json.dumps(result)


def test_old_python_still_reports_prerequisites(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, 'tomllib', None)
    (tmp_path / '.codex').mkdir()
    (tmp_path / '.codex/config.toml').write_text('[mcp_servers]')
    result = doctor._installation(str(tmp_path))
    assert result['codex']['registration'] == 'unavailable_python'
    assert result['client_connection'] == 'NOT_RUN'
