import importlib.util
import json
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('network_installer', Path(__file__).resolve().parents[2] / 'scripts/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_network_environment_excludes_keys(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy:8080')
    monkeypatch.setenv('NODE_EXTRA_CA_CERTS', '/path/to/corporate.pem')
    monkeypatch.setenv('NODE_OPTIONS', '--require=/untrusted.js')
    monkeypatch.setenv('OPENAI_API_KEY', 'secret')
    monkeypatch.setenv('DECISION_BROWSER_INSTALL_INDEX_URL', 'https://mirror.example/simple')
    env = installer.clean_environment()
    assert env['HTTPS_PROXY'] == 'http://proxy:8080'
    assert env['NODE_EXTRA_CA_CERTS'] == '/path/to/corporate.pem'
    assert env['UV_DEFAULT_INDEX'] == 'https://mirror.example/simple'
    assert 'OPENAI_API_KEY' not in env
    assert 'NODE_OPTIONS' not in env


@pytest.mark.parametrize('url', ['http://mirror/simple', 'https://user:secret@mirror/simple', 'https://mirror/simple?key=secret'])
def test_reject_unsafe_index(monkeypatch, url):
    monkeypatch.setenv('DECISION_BROWSER_INSTALL_INDEX_URL', url)
    with pytest.raises(installer.InstallError, match='index_url'):
        installer.clean_environment()


def test_safe_failure_report(monkeypatch, tmp_path, capsys):
    with pytest.raises(installer.InstallError):
        installer.run([sys.executable, '-I', '-c', "import sys;print('secret-command');sys.exit(7)"],
                      {}, 'package build', tmp_path)
    record = json.loads(capsys.readouterr().err)
    assert record['code'] == 'stage_failed'
    assert record['exit_code'] == 7
    assert 'secret-command' not in str(record)


def test_mirror_uses_locked_hashes(monkeypatch, tmp_path):
    monkeypatch.setenv('DECISION_BROWSER_INSTALL_INDEX_URL', 'https://mirror.example/simple')
    calls = []
    def run(command, env, stage, cwd):
        calls.append(command)
        if stage == 'package build':
            dist = tmp_path / 'runtime/dist'
            dist.mkdir(parents=True)
            (dist / 'fixture.whl').touch()
    monkeypatch.setattr(installer, 'run', run)
    installer.prepare(tmp_path, tmp_path, tmp_path / 'runtime', 'uv')
    assert any('export' in c and '--frozen' in c for c in calls)
    assert any('sync' in c and '--require-hashes' in c for c in calls)
