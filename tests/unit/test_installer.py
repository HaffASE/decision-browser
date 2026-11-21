"""Installer tests use disposable projects and fake setup, never global profiles."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("installer", ROOT / "scripts/install.py")
assert spec and spec.loader
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    source = tmp_path / 'checkout with spaces'
    source.mkdir()
    for rel, data in {
        'pyproject.toml': '[project]\nname="fixture"', 'uv.lock': '# lock',
        'scripts/build-constraints.txt': '# constraints',
        'scripts/install.py': '# installer', 'README.md': '# readme',
        'THIRD_PARTY_NOTICES.md': '# notices',
        'src/decision_browser/cli.py': '# source',
        'skills/decision-browser/SKILL.md': '---\nname: decision-browser\n---\nSafe skill',
        'skills/decision-browser/scripts/doctor.py': '# doctor',
    }.items():
        path = source / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
    project = tmp_path / 'project with spaces " unicode-é'
    project.mkdir()
    monkeypatch.setattr(installer.shutil, 'which', lambda name: '/bin/' + name)
    monkeypatch.setattr(installer, 'check_uv', lambda uv: None)
    calls = []
    monkeypatch.setattr(installer, 'prepare', lambda *args: calls.append(args))
    return source, project, calls


def configs(project):
    return (tomllib.loads((project / '.codex/config.toml').read_text())['mcp_servers'],
            json.loads((project / '.mcp.json').read_text())['mcpServers'])


@pytest.mark.parametrize('client,expected', [('codex', ['codex']), ('claude', ['claude']),
                                          ('both', ['codex', 'claude'])])
def test_short_command_uses_current_project(setup, monkeypatch, client, expected):
    source, project, calls = setup
    monkeypatch.chdir(project)
    assert installer.main([client, '--source', str(source)]) == 0
    assert len(calls) == 1
    assert calls[0][1] == project
    state = json.loads((project / installer.STATE / 'manifest.json').read_text())
    assert set(state['clients']) == set(expected)


@pytest.mark.parametrize('args', [[], ['unknown'], ['codex', '--client', 'claude'],
                                 ['codex', '--client', 'codex'],
                                 ['codex', '--upgrade', '--uninstall']])
def test_invalid_short_command_never_changes_settings(setup, monkeypatch, args):
    _, project, calls = setup
    monkeypatch.chdir(project)
    with pytest.raises(SystemExit) as caught:
        installer.main(args)
    assert caught.value.code == 2
    assert not calls and not list(project.iterdir())


@pytest.mark.parametrize('clients', [['codex'], ['claude'], ['codex', 'claude']])
def test_install_repeat_and_remove(setup, clients):
    source, project, calls = setup
    installer.install(source, project, clients)
    first = {p.relative_to(project): p.read_bytes() for p in project.rglob('*') if p.is_file()}
    installer.install(source, project, clients)
    second = {p.relative_to(project): p.read_bytes() for p in project.rglob('*') if p.is_file()}
    for name, value in first.items():
        if str(name).endswith(('SKILL.md', 'doctor.py')):
            assert value == second[name]
    assert calls[0][2] != calls[1][2]  # never mutate an active runtime
    assert len(calls) == 2
    for client in clients:
        root = '.agents' if client == 'codex' else '.claude'
        assert (project / root / 'skills/decision-browser/scripts/doctor.py').is_file()
    installer.install(source, project, clients, uninstall=True)
    assert not json.loads((project / installer.STATE / 'manifest.json').read_text())['clients']
    installer.install(source, project, clients)  # reinstall after uninstall


def test_preserves_other_settings_and_explicit_policy(setup, isolated_provider_profile):
    source, project, _ = setup
    (project / '.codex').mkdir()
    (project / '.codex/config.toml').write_text('# keep comment\nmodel="example"\n[mcp_servers.other]\ncommand="other"\n')
    (project / '.mcp.json').write_text('{"mcpServers":{"other":{"command":"other"}},"custom":true}')
    installer.install(source, project, ['codex', 'claude'])
    for server in configs(project):
        assert server['other'] == {'command': 'other'}
        ours = server['decision_browser']
        assert ours['args'][-2:] == ['--admission', 'standard_form']
        assert ours['args'][0] == '-I'
        assert ' ' in ours['command']
        assert set(ours['env']) == {'PLAYWRIGHT_BROWSERS_PATH', 'DECISION_BROWSER_CONFIG_FILE'}
        assert ours['env']['DECISION_BROWSER_CONFIG_FILE'] == str(isolated_provider_profile)
    assert '# keep comment' in (project / '.codex/config.toml').read_text()
    assert json.loads((project / '.mcp.json').read_text())['custom'] is True


@pytest.mark.parametrize('relative,body', [
    ('.codex/config.toml', '[mcp_servers.decision_browser]\ncommand="other"'),
    ('.mcp.json', '{"mcpServers":{"decision_browser":{"command":"other"}}}'),
    ('.agents/skills/decision-browser/SKILL.md', 'custom'),
    ('.claude/skills/decision-browser/SKILL.md', 'custom'),
])
def test_conflicts_before_setup(setup, relative, body):
    source, project, calls = setup
    p = project / relative
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    with pytest.raises(installer.InstallError, match='Conflicting'):
        installer.install(source, project, ['codex', 'claude'])
    assert p.read_text() == body
    assert not calls
    assert not (project / installer.STATE).exists()


@pytest.mark.parametrize('missing', ['uv', 'codex', 'claude'])
def test_missing_prerequisite(setup, monkeypatch, missing):
    source, project, calls = setup
    monkeypatch.setattr(installer.shutil, 'which', lambda name: None if name == missing else '/bin/' + name)
    with pytest.raises(installer.InstallError):
        installer.install(source, project, ['codex', 'claude'])
    assert not calls and not list(project.iterdir())


def test_old_python(setup, monkeypatch):
    source, project, _ = setup
    monkeypatch.setattr(installer.sys, 'version_info', (3, 10))
    with pytest.raises(installer.InstallError, match='Python 3.11'):
        installer.install(source, project, ['codex'])


@pytest.mark.parametrize('stage', ['locked Python package', 'Chromium download', 'sandboxed Chromium smoke test'])
def test_setup_failure_retry(setup, monkeypatch, stage):
    source, project, _ = setup
    def fail(*args):
        raise installer.InstallError(stage + ' failed')
    monkeypatch.setattr(installer, 'prepare', fail)
    with pytest.raises(installer.InstallError, match=stage):
        installer.install(source, project, ['codex', 'claude'])
    assert not (project / '.codex/config.toml').exists()
    assert not (project / '.mcp.json').exists()
    assert not (project / installer.STATE / 'install.lock').exists()
    monkeypatch.setattr(installer, 'prepare', lambda *args: None)
    installer.install(source, project, ['codex', 'claude'])


def test_write_failure_rolls_back(setup, monkeypatch):
    source, project, _ = setup
    (project / '.mcp.json').write_text('{"custom":true}')
    write = installer.atomic_write
    def fail_once(path, data):
        if str(path).endswith('.claude/skills/decision-browser/SKILL.md'):
            raise OSError('fixture')
        write(path, data)
    monkeypatch.setattr(installer, 'atomic_write', fail_once)
    with pytest.raises(OSError):
        installer.install(source, project, ['codex', 'claude'])
    assert (project / '.mcp.json').read_text() == '{"custom":true}'
    assert not (project / '.codex/config.toml').exists()
    assert not (project / '.agents/skills/decision-browser/SKILL.md').exists()
    monkeypatch.setattr(installer, 'atomic_write', write)
    installer.install(source, project, ['codex', 'claude'])


@pytest.mark.parametrize('relative', ['.codex/config.toml', '.claude/skills/decision-browser/SKILL.md'])
def test_edited_managed_file_is_preserved(setup, relative):
    source, project, _ = setup
    installer.install(source, project, ['codex', 'claude'])
    path = project / relative
    path.write_text(path.read_text().replace('standard_form', 'task_scope') + '\n# user edit\n')
    before = path.read_bytes()
    with pytest.raises(installer.InstallError):
        installer.install(source, project, ['codex', 'claude'], uninstall=True)
    assert path.read_bytes() == before


def test_upgrade_requires_explicit_all_clients(setup):
    source, project, _ = setup
    installer.install(source, project, ['codex', 'claude'])
    old = configs(project)[0]['decision_browser']['command']
    (source / 'src/decision_browser/cli.py').write_text('# new version')
    with pytest.raises(installer.InstallError, match='Checkout changed'):
        installer.install(source, project, ['codex', 'claude'])
    with pytest.raises(installer.InstallError, match='all previously'):
        installer.install(source, project, ['codex'], upgrade=True)
    installer.install(source, project, ['codex', 'claude'], upgrade=True)
    assert configs(project)[0]['decision_browser']['command'] != old


def test_concurrent_change_during_setup_is_preserved(setup, monkeypatch):
    source, project, _ = setup
    def change(*args):
        (project / '.mcp.json').write_text('{"changed":true}')
    monkeypatch.setattr(installer, 'prepare', change)
    with pytest.raises(installer.InstallError, match='Target changed'):
        installer.install(source, project, ['codex', 'claude'])
    assert not (project / '.codex/config.toml').exists()
    assert (project / '.mcp.json').read_text() == '{"changed":true}'


def test_symlink_target_rejected(setup, tmp_path):
    source, project, _ = setup
    elsewhere = tmp_path / 'outside'
    elsewhere.mkdir()
    (project / '.codex').symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(installer.InstallError, match='Symlink'):
        installer.install(source, project, ['codex'])
    assert not list(elsewhere.iterdir())


def test_unknown_runtime_not_reused(setup):
    source, project, _ = setup
    (project / installer.STATE).mkdir()
    with pytest.raises(installer.InstallError, match='Unrecognized'):
        installer.install(source, project, ['codex'])


def test_child_failure_never_exposes_output(tmp_path, monkeypatch, capsys):
    with pytest.raises(installer.InstallError, match='Chromium download failed'):
        installer.run([sys.executable, '-I', '-c',
                       "import sys;print('fixture-secret https://user:password@private');sys.exit(1)"],
                      {}, 'Chromium download', tmp_path)
    output = capsys.readouterr()
    assert all(value not in output.out + output.err for value in ['fixture-secret', 'password', 'https://'])


def test_no_provider_values_in_setup_environment(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-not-a-secret')
    monkeypatch.setenv('HTTPS_PROXY', 'synthetic-not-a-url')
    assert 'OPENAI_API_KEY' not in installer.clean_environment()
    assert installer.clean_environment()['HTTPS_PROXY'] == 'synthetic-not-a-url'


def test_packaging_inputs_and_bootstrap_help():
    metadata = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    assert metadata['project']['readme'] == 'README.md'
    assert metadata['project']['urls']['Repository'].endswith('/HaffASE/decision-browser')
    hatch = metadata['tool']['hatch']['build']['targets']
    assert hatch['wheel']['force-include']['scripts/install.py'] == 'decision_browser/install.py'
    for name in ['scripts/install.py', 'docs/installation.md', 'docs/task-scoped-admission.md',
                 'docs/action-classification.md', 'THIRD_PARTY_NOTICES.md']:
        assert name in hatch['sdist']['only-include'] and (ROOT / name).is_file()
    result = subprocess.run([sys.executable, '-I', str(ROOT / 'scripts/install.py'), '--help'],
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0 and '--project' in result.stdout


@pytest.mark.parametrize('version,valid', [('0.12.6', False), ('0.13.7', True), ('0.14.0', True)])
def test_uv_version_guard(monkeypatch, version, valid):
    monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **k:
                        subprocess.CompletedProcess(a[0], 0, 'uv ' + version + '\n'))
    if valid:
        installer.check_uv('uv')
    else:
        with pytest.raises(installer.InstallError, match='uv 0.13.7'):
            installer.check_uv('uv')


def test_null_server_is_still_a_conflict():
    with pytest.raises(installer.InstallError, match='Conflicting'):
        installer.config_change('claude', b'{"mcpServers":{"decision_browser":null}}', None,
                                {'command': 'fixture'})


def test_setup_commands_preserve_sandbox_and_paths(tmp_path, monkeypatch):
    source = tmp_path / 'source with spaces'
    project = tmp_path / 'project with spaces'
    runtime = project / 'runtime'
    source.mkdir()
    project.mkdir()
    (runtime / 'dist').mkdir(parents=True)
    (runtime / 'dist/fixture.whl').write_bytes(b'fixture')
    calls = []
    monkeypatch.setattr(installer, 'run', lambda *a: calls.append(a))
    installer.prepare(source, project, runtime, '/fake/uv')
    assert len(calls) == 6
    assert '--frozen' in calls[0][0]
    assert str(source) in calls[0][0]
    assert '--no-install-project' in calls[0][0]
    assert '--build-constraints' in calls[1][0]
    assert '--no-deps' in calls[2][0]
    assert calls[3][0][-3:] == ['install', '--no-shell', 'chromium']
    assert 'chromium_sandbox=True' in calls[4][0][-1]
    assert 'channel="chromium"' in calls[4][0][-1]
    assert 'screenshot(timeout=5000)' in calls[4][0][-1]
    assert '.click(timeout=5000)' in calls[4][0][-1]
    assert '--no-sandbox' not in str(calls)
    assert calls[0][1]['PLAYWRIGHT_BROWSERS_PATH'] == str(runtime / 'browsers')


def test_stale_lock_stops_before_setup(setup):
    source, project, calls = setup
    installer.install(source, project, ['codex'])
    (project / installer.STATE / 'install.lock').write_text('')
    with pytest.raises(installer.InstallError, match='install.lock'):
        installer.install(source, project, ['codex'])
    assert len(calls) == 1



def test_concurrent_change_between_commit_writes_is_preserved(setup, monkeypatch):
    source, project, _ = setup
    write = installer.atomic_write
    def concurrent(path, data):
        write(path, data)
        if str(path).endswith('.codex/config.toml'):
            (project / '.mcp.json').write_text('{"concurrent":true}')
    monkeypatch.setattr(installer, 'atomic_write', concurrent)
    with pytest.raises(installer.InstallError, match='Target changed during commit'):
        installer.install(source, project, ['codex', 'claude'])
    assert (project / '.mcp.json').read_text() == '{"concurrent":true}'
    assert not (project / '.codex/config.toml').exists()


def test_packaged_default_stages_inputs_before_runtime_changes(tmp_path, monkeypatch):
    package = tmp_path / 'package'
    bundled = package / 'bootstrap'
    bundled.mkdir(parents=True)
    (bundled / 'uv.lock').write_text('locked')
    monkeypatch.setattr(installer, '__file__', str(package / 'install.py'))
    monkeypatch.setattr(installer, 'packaged_wheel', lambda target: target.mkdir())
    observed = []

    def install(source, *args):
        observed.append(source)
        assert source != bundled
        assert (source / 'uv.lock').read_text() == 'locked'
        (bundled / 'uv.lock').unlink()  # model replacement of the original installation
        assert (source / 'uv.lock').read_text() == 'locked'

    monkeypatch.setattr(installer, 'install', install)
    assert installer.main(['--client', 'both', '--project', str(tmp_path)]) == 0
    assert not observed[0].exists()


@pytest.mark.parametrize("failure", [installer.InstallError, KeyboardInterrupt])
@pytest.mark.parametrize("upgrade", [False, True])
def test_prepare_failure_preserves_live_runtime(setup, monkeypatch, failure, upgrade):
    source, project, calls = setup
    installer.install(source, project, ["codex", "claude"])
    active = calls[0][2]
    active.mkdir(parents=True)
    (active / "live-package").write_bytes(b"working")
    before = {p.relative_to(project): p.read_bytes() for p in project.rglob("*") if p.is_file()}
    if upgrade:
        (source / "README.md").write_text("changed")
    def fail(source, project, runtime, uv):
        assert runtime != active
        runtime.mkdir(parents=True)
        (runtime / "partial").write_text("incomplete")
        raise failure("build interrupted")
    monkeypatch.setattr(installer, "prepare", fail)
    with pytest.raises(failure):
        installer.install(source, project, ["codex", "claude"], upgrade=upgrade)
    for name, data in before.items():
        assert (project / name).read_bytes() == data
    assert not (project / installer.STATE / "install.lock").exists()


def test_non_bmp_toml_roundtrip_and_lifecycle(setup):
    source, project, _ = setup
    target = project / "emoji-😀-𐐀"
    target.mkdir()
    for _ in range(2):
        installer.install(source, target, ["codex", "claude"])
        codex, claude = configs(target)
        assert "😀" in codex[installer.SERVER]["command"]
        assert codex[installer.SERVER]["command"] == claude[installer.SERVER]["command"]
    installer.install(source, target, ["codex", "claude"], uninstall=True)


@pytest.mark.parametrize("relative", ["src/decision_browser/nested", "skills/decision-browser/nested", "src/decision_browser/__pycache__"])
def test_nested_source_symlink_rejected(setup, tmp_path, relative):
    source, project, calls = setup
    elsewhere = tmp_path / "external"
    elsewhere.mkdir()
    (elsewhere / "unfingerprinted.py").write_text("code")
    (source / relative).symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(installer.InstallError, match="symlinks"):
        installer.install(source, project, ["codex", "claude"])
    assert not calls


def test_manifest_paths_use_portable_separators(setup):
    from pathlib import PureWindowsPath
    source, project, _ = setup
    installer.install(source, project, ["codex", "claude"])
    state = json.loads((project / installer.STATE / "manifest.json").read_text())
    for record in state["clients"].values():
        for key in record["files"]:
            assert PureWindowsPath(key).as_posix() == key
    installer.install(source, project, ["codex", "claude"])
    installer.install(source, project, ["codex", "claude"], uninstall=True)


def test_legacy_escaped_codex_block_can_be_replaced(setup):
    source, project, _ = setup
    installer.install(source, project, ["codex"])
    state = json.loads((project / installer.STATE / "manifest.json").read_text())
    path = project / ".codex/config.toml"
    path.write_text(installer.codex_block(state["clients"]["codex"]["config"], legacy=True))
    installer.install(source, project, ["codex"])
    installer.install(source, project, ["codex"], uninstall=True)


@pytest.mark.parametrize("target_name", ["wheel", "sdist"])
@pytest.mark.parametrize("linked_input", ["src/decision_browser/nested", "scripts/install.py"])
def test_build_hook_rejects_nested_directory_symlink(tmp_path, monkeypatch, target_name, linked_input):
    import shutil
    import types
    fake = types.ModuleType("hatchling.builders.hooks.plugin.interface")
    fake.BuildHookInterface = object
    monkeypatch.setitem(sys.modules, fake.__name__, fake)
    hook_spec = importlib.util.spec_from_file_location("bootstrap_hook", ROOT / "hatch_build.py")
    hook_module = importlib.util.module_from_spec(hook_spec)
    hook_spec.loader.exec_module(hook_module)
    source = tmp_path / "source"
    source.mkdir()
    for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "hatch_build.py", "scripts", "src/decision_browser", "skills/decision-browser"):
        origin, target = ROOT / name, source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if origin.is_dir():
            shutil.copytree(origin, target)
        else:
            shutil.copyfile(origin, target)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = source / linked_input
    link.unlink(missing_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    hook = hook_module.CustomBuildHook()
    hook.root, hook.target_name = str(source), target_name
    with pytest.raises(Exception, match="Source symlinks"):
        hook.initialize("standard", {"force_include": {}})


def test_legacy_windows_manifest_repeat_and_uninstall(setup):
    source, project, _ = setup
    installer.install(source, project, ["codex", "claude"])
    manifest = project / installer.STATE / "manifest.json"
    state = json.loads(manifest.read_text())
    for record in state["clients"].values():
        record["files"] = {key.replace("/", "\\"): value for key, value in record["files"].items()}
    manifest.write_text(json.dumps(state))
    installer.install(source, project, ["codex", "claude"])
    installer.install(source, project, ["codex", "claude"], uninstall=True)


def test_prebuilt_wheel_is_fingerprinted_and_symlinks_rejected(setup):
    source, _, _ = setup
    before = installer.source_id(source)
    folder = source / 'prebuilt'
    folder.mkdir()
    wheel = folder / 'fixture.whl'
    from zipfile import ZipFile

    with ZipFile(wheel, 'w') as archive:
        archive.writestr('code.py', b'first')
    first = installer.source_id(source)
    with ZipFile(wheel, 'w') as archive:
        archive.writestr('code.py', b'second')
    assert before != first != installer.source_id(source)
    wheel.unlink()
    wheel.symlink_to(source / 'README.md')
    with pytest.raises(installer.InstallError, match='symlinks'):
        installer.source_id(source)
    wheel.unlink()
    folder.rmdir()
    folder.symlink_to(source / 'scripts', target_is_directory=True)
    with pytest.raises(installer.InstallError, match='symlinks'):
        installer.source_id(source)


def test_packaged_uninstall_does_not_repack(tmp_path, monkeypatch):
    package = tmp_path / 'package'
    (package / 'bootstrap').mkdir(parents=True)
    monkeypatch.setattr(installer, '__file__', str(package / 'install.py'))
    monkeypatch.setattr(installer, 'packaged_wheel', lambda path: pytest.fail('unnecessary repack'))
    monkeypatch.setattr(installer, 'install', lambda *args: None)
    assert installer.main(['codex', '--uninstall']) == 0


def test_prebuilt_fingerprint_ignores_zip_layout_and_record(setup):
    from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

    source, _, _ = setup
    folder = source / 'prebuilt'
    folder.mkdir()
    wheel = folder / 'fixture.whl'
    with ZipFile(wheel, 'w') as archive:
        archive.writestr('code.py', b'code')
        archive.writestr('fixture.dist-info/RECORD', b'old record')
    original = installer.source_id(source)
    with ZipFile(wheel, 'w', ZIP_DEFLATED) as archive:
        archive.writestr('fixture.dist-info/RECORD', b'new record')
        archive.writestr(ZipInfo('code.py', (1980, 1, 1, 0, 0, 0)), b'code')
    assert installer.source_id(source) == original
