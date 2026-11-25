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


@pytest.mark.parametrize('system_certs', [None, 'true', 'false'])
@pytest.mark.parametrize('index', [None, 'https://private-mirror.example/simple'])
def test_package_children_preserve_explicit_ca_settings_and_filter_secrets(
    monkeypatch, tmp_path, system_certs, index,
):
    monkeypatch.delenv('UV_SYSTEM_CERTS', raising=False)
    monkeypatch.delenv('DECISION_BROWSER_INSTALL_INDEX_URL', raising=False)
    if system_certs is not None:
        monkeypatch.setenv('UV_SYSTEM_CERTS', system_certs)
    if index is not None:
        monkeypatch.setenv('DECISION_BROWSER_INSTALL_INDEX_URL', index)
    certificate_settings = {
        'SSL_CERT_FILE': '/private/corporate-ca.pem',
        'SSL_CERT_DIR': '/private/corporate-cas',
        'NODE_EXTRA_CA_CERTS': '/private/node-corporate-ca.pem',
    }
    for name, value in certificate_settings.items():
        monkeypatch.setenv(name, value)
    for name in ['OPENAI_API_KEY', 'DECISION_BROWSER_API_KEY', 'UV_INSECURE_HOST',
                 'UV_NATIVE_TLS', 'NODE_OPTIONS', 'PYTHONPATH']:
        monkeypatch.setenv(name, 'FILTERED-CANARY')
    source = tmp_path / 'source'
    (source / 'prebuilt').mkdir(parents=True)
    (source / 'prebuilt/fixture.whl').touch()
    calls = []

    def record(command, env, stage, cwd):
        calls.append((command, env.copy(), stage))

    monkeypatch.setattr(installer, 'run', record)
    installer.prepare(source, tmp_path, tmp_path / 'runtime', 'uv')
    package_calls = [(command, env) for command, env, stage in calls
                     if stage == 'locked Python package']
    assert len(package_calls) == 1
    command, _ = package_calls[0]
    if index is None:
        assert 'sync' in command and '--frozen' in command
        assert '--default-index' not in command
    else:
        assert 'pip' in command and '--require-hashes' in command
        assert command[command.index('--default-index') + 1] == index
        assert any(stage == 'locked dependency export' for _, _, stage in calls)
    assert any(stage == 'Chromium download' for _, _, stage in calls)
    for _, env, _ in calls:
        assert {name: env[name] for name in certificate_settings} == certificate_settings
        assert env.get('UV_SYSTEM_CERTS') == system_certs
        assert ('UV_SYSTEM_CERTS' in env) == (system_certs is not None)
        assert env.get('UV_DEFAULT_INDEX') == index
        assert 'FILTERED-CANARY' not in env.values()


@pytest.mark.parametrize('mirror', [False, True])
def test_setup_tls_failure_reports_safe_reason_and_preserves_blocked_project(
    monkeypatch, tmp_path, capsys, mirror,
):
    source = tmp_path / 'source'
    skill = source / 'skills/decision-browser/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('fixture skill')
    project = tmp_path / 'project'
    config = project / '.codex/config.toml'
    config.parent.mkdir(parents=True)
    original = '[mcp_servers.other]\ncommand = "retained-client"\n'
    config.write_text(original)
    monkeypatch.setattr(installer, 'source_id', lambda _: 'f' * 20)
    monkeypatch.setattr(installer, 'check_uv', lambda _: None)
    monkeypatch.setattr(installer.shutil, 'which', lambda _: '/fixture/uv')
    monkeypatch.delenv('DECISION_BROWSER_INSTALL_INDEX_URL', raising=False)
    if mirror:
        monkeypatch.setenv('DECISION_BROWSER_INSTALL_INDEX_URL',
                           'https://private-mirror.canary.invalid/simple')
    log = (
        'private mirror https://private-mirror.canary.invalid/simple/package\n'
        'proxy http://proxy-user-canary:proxy-password-canary@proxy.canary.invalid:8080\n'
        'SSL_CERT_FILE=/private/certificate-canary.pem\n'
        'Failed to download https://username-canary:password-canary@files.pythonhosted.org/'
        'packages/wheel.whl?token=query-canary\n'
        'invalid peer certificate: UnknownIssuer\n'
    )

    def fail_prepare(source, project, runtime, uv):
        runtime.mkdir(parents=True)
        (runtime / 'retained-evidence').write_text('owned runtime')
        command = [sys.executable, '-I', '-c', f'import sys;print({log!r});sys.exit(1)']
        installer.run(command, installer.clean_environment(), 'locked Python package', project)

    monkeypatch.setattr(installer, 'prepare', fail_prepare)
    arguments = ['codex', '--source', str(source), '--project', str(project)]
    assert installer.main(arguments) == 1
    output = capsys.readouterr()
    records = [json.loads(line) for line in output.err.splitlines() if line.startswith('{')]
    assert len(records) == 1
    record = records[0]
    assert record['stage'] == 'locked Python package'
    assert record['code'] == 'stage_failed'
    assert record['hint'] == 'tls_unknown_issuer'
    assert record['target'] == ('configured mirror' if mirror else 'files.pythonhosted.org')
    assert 'UV_SYSTEM_CERTS=true' in record['next_action']
    combined = output.out + output.err
    for private in ['username-canary', 'password-canary', 'query-canary',
                    'private-mirror.canary.invalid', 'proxy-user-canary',
                    'proxy-password-canary', 'proxy.canary.invalid',
                    '/private/certificate-canary.pem', 'https://', 'http://']:
        assert private not in combined
        assert private not in json.dumps(record)
    assert record['cleanup'] == 'unknown_retained'
    assert any(line.lower().startswith('preparation blocked')
               for line in output.err.splitlines())
    assert 'Do not repeat setup in this directory.' in output.err
    assert 'Client settings were not changed.' in output.err
    assert config.read_text() == original
    assert (project / installer._RESIDUAL).is_file()
    assert (project / '.decision-browser-install.lease').is_file()
    assert not (project / installer.STATE / 'manifest.json').exists()
    assert 'retry' not in output.err.lower()
    evidence = list(project.rglob('retained-evidence'))
    assert len(evidence) == 1
    monkeypatch.setattr(installer, 'prepare', lambda *args: pytest.fail('blocked preparation restarted'))
    assert installer.main(arguments) == 1
    blocked = capsys.readouterr().err
    assert 'Preparation blocked' in blocked
    assert 'retry' not in blocked.lower()
    assert config.read_text() == original
    assert evidence[0].read_text() == 'owned runtime'


@pytest.mark.parametrize('log,reason', [
    ('invalid peer certificate: UnknownIssuer', 'tls_unknown_issuer'),
    ('certificate verify failed: unable to get local issuer certificate', 'tls_unknown_issuer'),
    ('invalid peer certificate: CertificateExpired', 'tls_certificate_expired'),
    ('error sending request; operation timed out', 'timeout'),
    ('error sending request; connection refused', 'network_error'),
    ('error sending request; dns error: name resolution failed', 'dns_resolution'),
    ('No solution found when resolving dependencies', 'package_error'),
    ('Hash mismatch for package wheel', 'package_error'),
    ('no space left on device', 'disk_space'),
    ('read /private/certificate-canary.pem failed', 'check_prerequisites'),
])
def test_stage_failure_reason_is_derived_from_child_evidence(tmp_path, capsys, log, reason):
    command = [sys.executable, '-I', '-c', f'import sys;print({log!r});sys.exit(1)']
    with pytest.raises(installer.InstallError) as error:
        installer.run(command, {}, 'locked Python package', tmp_path)
    record = json.loads(capsys.readouterr().err)
    assert record['hint'] == reason
    assert reason in str(error.value)
    assert record['target'] == 'unknown'
    assert record['next_action'] != 'none'


def test_installer_timeout_takes_precedence_over_partial_tls_output(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(installer, '_STAGE_TIMEOUT', 0.1)
    command = [sys.executable, '-I', '-c',
               "import time;print('invalid peer certificate: UnknownIssuer', flush=True);time.sleep(10)"]
    with pytest.raises(installer.InstallError, match='stage_timeout'):
        installer.run(command, {}, 'locked Python package', tmp_path)
    record = json.loads(capsys.readouterr().err)
    assert record['hint'] == 'timeout'
    assert record['code'] == 'stage_timeout'


def test_chromium_tls_failure_names_its_own_client(tmp_path, capsys):
    log = ('Download failed https://cdn.playwright.dev/path?token=chromium-canary '
           'invalid peer certificate: UnknownIssuer')
    command = [sys.executable, '-I', '-c', f'import sys;print({log!r});sys.exit(1)']
    with pytest.raises(installer.InstallError):
        installer.run(command, {'UV_SYSTEM_CERTS': 'true'}, 'Chromium download', tmp_path)
    record = json.loads(capsys.readouterr().err)
    assert record['stage'] == 'Chromium download'
    assert record['hint'] == 'tls_certificate'
    assert record['target'] == 'cdn.playwright.dev'
    assert 'NODE_EXTRA_CA_CERTS' in record['next_action']
    assert 'UV_SYSTEM_CERTS applies to uv only' in record['next_action']
    assert 'chromium-canary' not in json.dumps(record)


@pytest.mark.parametrize('url', [
    'https://files.pythonhosted.org.private-canary.invalid/wheel',
    'https://private-canary.invalid/wheel?redirect=files.pythonhosted.org',
    'https://files.pythonhosted.org@private-canary.invalid/wheel',
])
def test_failure_target_does_not_infer_public_host_from_private_url(url):
    assert installer._stage_target(url.encode(), {}, 'locked Python package') == 'unknown'


def test_package_failure_ignores_reason_words_inside_download_url(tmp_path, capsys):
    log = ('Failed to build package from '
           'https://files.pythonhosted.org/packages/timeout.whl?hint=certificate%20verify%20failed')
    command = [sys.executable, '-I', '-c', f'import sys;print({log!r});sys.exit(1)']
    with pytest.raises(installer.InstallError):
        installer.run(command, {}, 'locked Python package', tmp_path)
    assert json.loads(capsys.readouterr().err)['hint'] == 'package_error'


@pytest.mark.parametrize('separator', [b' ', b':\n  |   '])
def test_failure_target_uses_failing_wheel_after_index_response(separator):
    log = (b'Resolved package index https://pypi.org/simple/package\n'
           b'Failed to download' + separator + b'https://files.pythonhosted.org/packages/wheel.whl\n'
           b'invalid peer certificate: UnknownIssuer')
    assert installer._stage_target(log, {}, 'locked Python package') == 'files.pythonhosted.org'


@pytest.mark.parametrize('log,reason', [
    ('invalid peer certificate: Expired', 'tls_certificate_expired'),
    ('invalid peer certificate: NotValidForName', 'tls_hostname_mismatch'),
])
def test_server_certificate_errors_require_server_fix(tmp_path, capsys, log, reason):
    command = [sys.executable, '-I', '-c', f'import sys;print({log!r});sys.exit(1)']
    with pytest.raises(installer.InstallError):
        installer.run(command, {}, 'locked Python package', tmp_path)
    record = json.loads(capsys.readouterr().err)
    assert record['hint'] == reason
    assert 'administrator' in record['next_action']
    assert 'UV_SYSTEM_CERTS' not in record['next_action']
