import copy
import importlib.util
import json
import tomllib
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('provider_installer', Path(__file__).resolve().parents[2] / 'scripts/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def encode(client, config):
    return (installer.codex_block(config) if client == 'codex' else json.dumps({'mcpServers': {'decision_browser': config}})).encode()


@pytest.mark.parametrize('client', ['codex', 'claude'])
def test_provider_settings_survive_repeated_upgrade_and_uninstall(client, tmp_path):
    expected = installer.configuration(client, tmp_path, tmp_path / 'old')
    current = copy.deepcopy(expected)
    current['env'].update(DECISION_BROWSER_PROVIDER='local', DECISION_BROWSER_MODEL='nimble:latest', DECISION_BROWSER_ENDPOINT='http://127.0.0.1:11434/v1/systemone')
    data = encode(client, current)
    for name in ['new', 'newer']:
        desired = installer.configuration(client, tmp_path, tmp_path / name)
        data = installer.config_change(client, data, expected, desired)
        parsed = tomllib.loads(data.decode()) if client == 'codex' else json.loads(data)
        result = parsed['mcp_servers' if client == 'codex' else 'mcpServers']['decision_browser']
        assert result['env']['DECISION_BROWSER_MODEL'] == 'nimble:latest'
        assert result['command'] == desired['command']
        assert 'DECISION_BROWSER_MODEL' not in desired['env']  # manifest stays installer-owned
        expected = desired
    installer.config_change(client, data, expected, None)


@pytest.mark.parametrize('field,value', [('command', '/unowned'), ('args', ['unsafe'])])
def test_owned_fields_still_conflict(tmp_path, field, value):
    expected = installer.configuration('codex', tmp_path, tmp_path / 'old')
    current = copy.deepcopy(expected)
    current[field] = value
    with pytest.raises(installer.InstallError):
        installer.config_change('codex', encode('codex', current), expected, expected)


@pytest.mark.parametrize('key', ['OPENAI_API_KEY', 'DECISION_BROWSER_API_KEY', 'UNKNOWN', 'PLAYWRIGHT_BROWSERS_PATH'])
def test_secret_or_unknown_env_edits_fail_closed(tmp_path, key):
    expected = installer.configuration('codex', tmp_path, tmp_path / 'old')
    current = copy.deepcopy(expected)
    current['env'][key] = 'changed'
    with pytest.raises(installer.InstallError):
        installer.config_change('codex', encode('codex', current), expected, expected)


def test_codex_formatting_and_forwarded_names_survive(tmp_path):
    expected = installer.configuration('codex', tmp_path, tmp_path / 'old')
    current = copy.deepcopy(expected)
    current['env_vars'] = ['OPENAI_API_KEY', 'DECISION_BROWSER_PROVIDER']
    current['env']['DECISION_BROWSER_PROVIDER'] = 'local'
    data = encode('codex', current).replace(b'DECISION_BROWSER_PROVIDER =', b'# personal preference\nDECISION_BROWSER_PROVIDER =')
    desired = installer.configuration('codex', tmp_path, tmp_path / 'new')
    result = tomllib.loads(installer.config_change('codex', data, expected, desired).decode())
    assert result['mcp_servers']['decision_browser']['env_vars'] == current['env_vars']


@pytest.mark.parametrize('forwarded', [None, [], ['OPENAI_API_KEY']])
def test_codex_forwarding_choice_survives_repeated_updates(tmp_path, forwarded):
    expected = installer.configuration('codex', tmp_path, tmp_path / 'old')
    current = copy.deepcopy(expected)
    if forwarded is None:
        current.pop('env_vars')
    else:
        current['env_vars'] = forwarded
    data = encode('codex', current)
    for name in ['new', 'newer']:
        desired = installer.configuration('codex', tmp_path, tmp_path / name)
        data = installer.config_change('codex', data, expected, desired)
        current = tomllib.loads(data.decode())['mcp_servers']['decision_browser']
        if forwarded is None:
            assert 'env_vars' not in current
        else:
            assert current['env_vars'] == forwarded
        assert current['command'] == desired['command']
        expected = desired
    removed = installer.config_change('codex', data, expected, None)
    assert 'decision_browser' not in tomllib.loads(removed.decode()).get('mcp_servers', {})


def test_endpoint_credentials_are_not_preserved(tmp_path):
    expected = installer.configuration('codex', tmp_path, tmp_path / 'old')
    current = copy.deepcopy(expected)
    current['env']['DECISION_BROWSER_ENDPOINT'] = 'https://user:secret@example.com/'
    with pytest.raises(installer.InstallError):
        installer.config_change('codex', encode('codex', current), expected, expected)
