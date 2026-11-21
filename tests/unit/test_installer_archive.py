"""Distribution bootstrapping without a source checkout or third-party imports."""
import importlib.util
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('archive_builder', ROOT / 'scripts/build_installer.py')
assert spec and spec.loader
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def wheel(tmp_path, extra=None):
    path = tmp_path / 'fixture.whl'
    files = {'pyproject.toml': '', 'uv.lock': '', 'LICENSE': 'MIT',
             'skills/decision-browser/SKILL.md': 'skill', 'src/decision_browser/cli.py': '',
             'scripts/install.py': (ROOT / 'scripts/install.py').read_text(),
             'scripts/prepare_tool_runtime.py': (ROOT / 'scripts/prepare_tool_runtime.py').read_text()}
    for name in ('README.md', 'THIRD_PARTY_NOTICES.md', 'hatch_build.py',
                 'scripts/build-constraints.txt', 'scripts/build_metadata.py'):
        files[name] = (ROOT / name).read_text(encoding='utf-8')
    files.update(extra or {})
    with ZipFile(path, 'w') as archive:
        for name, data in files.items():
            archive.writestr('decision_browser/bootstrap/' + name, data)
    return path


def test_archive_runs_help_from_empty_directory_with_isolated_python(tmp_path):
    output = tmp_path / 'installer with spaces.pyz'
    builder.build(wheel(tmp_path), output)
    cwd = tmp_path / 'empty'; cwd.mkdir()
    result = subprocess.run([sys.executable, '-I', str(output), '--help'], cwd=cwd,
                            capture_output=True, text=True, check=True)
    assert '--client' in result.stdout and '--project' in result.stdout
    assert not list(cwd.iterdir())
    with pytest.raises(FileExistsError):
        builder.build(tmp_path / 'fixture.whl', output)


def test_archive_rejects_traversal_before_output(tmp_path):
    output = tmp_path / 'installer.pyz'
    with pytest.raises(ValueError, match='Invalid bootstrap path'):
        builder.build(wheel(tmp_path, {'../escape': 'no'}), output)
    assert not output.exists()


@pytest.mark.parametrize('name', ['D:escape', 'scripts/install.py::$DATA', 'scripts/C:relative', 'C:/absolute'])
def test_archive_rejects_windows_drive_and_ads_names_before_output(tmp_path, name):
    output = tmp_path / 'installer.pyz'
    with pytest.raises(ValueError, match='Invalid bootstrap path'):
        builder.build(wheel(tmp_path, {name: 'not executable'}), output)
    assert not output.exists()


@pytest.mark.parametrize('name', ['D:escape', 'scripts/install.py::$DATA'])
def test_archive_entrypoint_rechecks_windows_names(tmp_path, name):
    output = tmp_path / 'installer.pyz'
    builder.build(wheel(tmp_path), output)
    with ZipFile(output, 'a') as archive:
        archive.writestr('payload/' + name, 'not executable')
    result = subprocess.run([sys.executable, '-I', str(output), '--help'], cwd=tmp_path,
                            capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert 'Invalid installer archive path' in result.stderr


def test_archive_requires_complete_package(tmp_path):
    path = tmp_path / 'empty.whl'
    with ZipFile(path, 'w'):
        pass
    with pytest.raises(ValueError, match='complete bootstrap'):
        builder.build(path, tmp_path / 'installer.pyz')


def test_archive_tool_runtime_help_is_isolated_and_side_effect_free(tmp_path):
    output = tmp_path / 'installer.pyz'
    builder.build(wheel(tmp_path), output)
    cwd = tmp_path / 'empty'; cwd.mkdir()
    result = subprocess.run([sys.executable, '-I', str(output), '--prepare-runtime', '--help'],
                            cwd=cwd, capture_output=True, text=True, check=True)
    assert '--cache' in result.stdout and '--client' not in result.stdout
    assert not list(cwd.iterdir())


def test_archive_dispatches_runtime_preparation_not_project_install(tmp_path):
    output = tmp_path / 'installer.pyz'
    payload = {'scripts/prepare_tool_runtime.py': 'print(\'{"python":"fixture"}\')\n',
               'scripts/install.py': 'raise SystemExit("wrong entry")\n'}
    builder.build(wheel(tmp_path, payload), output)
    result = subprocess.run([sys.executable, '-I', str(output), '--prepare-runtime', '--cache', str(tmp_path)],
                            capture_output=True, text=True, check=True)
    assert result.stdout == '{"python":"fixture"}\n'


def test_archive_contains_original_wheel_without_rebuilding(tmp_path):
    original = wheel(tmp_path)
    output = tmp_path / 'installer.pyz'
    builder.build(original, output)
    with ZipFile(output) as archive:
        assert archive.read('payload/prebuilt/' + original.name) == original.read_bytes()


@pytest.mark.parametrize('tampered', [False, True])
def test_repack_installed_wheel_checks_record(tmp_path, monkeypatch, tampered):
    import base64
    import hashlib
    import importlib.metadata
    from types import SimpleNamespace

    install_spec = importlib.util.spec_from_file_location('repack_installer', ROOT / 'scripts/install.py')
    installer = importlib.util.module_from_spec(install_spec)
    install_spec.loader.exec_module(installer)
    names = ['decision_browser/install.py', 'decision_browser-0.1.0.dist-info/WHEEL']
    entries = []
    for name, data in zip(names, [b'original', b'Wheel-Version: 1.0\nTag: py3-none-any\n']):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        entry = importlib.metadata.PackagePath(name)
        entry.hash = SimpleNamespace(mode='sha256', value=base64.urlsafe_b64encode(
            hashlib.sha256(data).digest()).rstrip(b'=').decode())
        entries.append(entry)
    # uv writes its own mutable cache metadata without a portable wheel hash.
    generated = importlib.metadata.PackagePath('decision_browser-0.1.0.dist-info/uv_cache.json')
    generated.hash = None
    (tmp_path / generated).write_text('{"timestamp": 123}')
    entries.append(generated)
    monkeypatch.setattr(importlib.metadata, 'distribution', lambda name: SimpleNamespace(
        files=entries, version='0.1.0', locate_file=lambda entry: tmp_path / entry))
    if tampered:
        (tmp_path / names[0]).write_bytes(b'changed')
        with pytest.raises(installer.InstallError, match='modified'):
            installer.packaged_wheel(tmp_path / 'prebuilt')
        assert not (tmp_path / 'prebuilt').exists()
    else:
        installer.packaged_wheel(tmp_path / 'prebuilt')
        with ZipFile(next((tmp_path / 'prebuilt').glob('*.whl'))) as archive:
            assert archive.read(names[0]) == b'original'
            assert str(generated) not in archive.namelist()
            assert 'decision_browser-0.1.0.dist-info/RECORD' in archive.namelist()
