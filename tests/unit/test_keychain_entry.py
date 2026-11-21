"""Synthetic Keychain-entry checks, including the actual child exec boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from decision_browser import keychain_entry

FAKE_KEY = "placeholder-keychain-value"
INHERITED = "placeholder-inherited-value"
SECURITY_ARGV = [
    "/usr/bin/security",
    "find-generic-password",
    "-s",
    "OPENAI_API_KEY",
    "-w",
]


def _assert_security_call(args: tuple[object, ...], kwargs: dict[str, object]) -> None:
    assert args == (SECURITY_ARGV,)
    assert isinstance(args[0], list)
    assert kwargs["shell"] is False
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] == 20


def test_security_invocation_and_process_replacement(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("OPENAI_API_KEY", INHERITED)
    monkeypatch.setenv("DECISION_BROWSER_TEST_RUNTIME", "placeholder-runtime")
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            SECURITY_ARGV,
            0,
            stdout=b"placeholder-keychain-value\n",
            stderr=b"placeholder-security-stderr",
        )

    def fail_popen(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("popen-used")

    def fake_exec(
        executable: str,
        argv: list[str],
        env: dict[str, str],
    ) -> None:
        assert executable == sys.executable
        assert argv == [sys.executable, "-I", "-m", "decision_browser.cli", "serve"]
        assert FAKE_KEY not in argv
        assert env is not os.environ
        assert os.environ["OPENAI_API_KEY"] == INHERITED
        assert env["OPENAI_API_KEY"] == FAKE_KEY
        assert env["DECISION_BROWSER_TEST_RUNTIME"] == "placeholder-runtime"
        assert all(
            FAKE_KEY not in value
            for name, value in env.items()
            if name != "OPENAI_API_KEY"
        )
        raise SystemExit(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", fail_popen)
    monkeypatch.setattr(os, "execve", fake_exec)
    with pytest.raises(SystemExit) as caught:
        keychain_entry.main()
    assert caught.value.code == 0
    assert len(calls) == 1
    _assert_security_call(calls[0][0], calls[0][1])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert FAKE_KEY not in captured.out
    assert FAKE_KEY not in captured.err


def test_unsupported_platform_never_calls_security_or_exec(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("security-called")

    def fail_exec(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("exec-called")

    monkeypatch.setattr(subprocess, "run", fail_run)
    monkeypatch.setattr(os, "execve", fail_exec)
    assert keychain_entry.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: unsupported platform\n"


@pytest.mark.parametrize(
    ("stdout", "stderr", "code"),
    [
        (b"", b"placeholder-security-stderr", 0),
        (b"\n", b"placeholder-security-stderr", 0),
        (b"\r\n", b"placeholder-security-stderr", 0),
        (b"placeholder-security-stdout", b"placeholder-security-stderr", 1),
        (b"", b"placeholder-security-stderr", 2),
    ],
)
def test_missing_empty_or_nonzero_fails_without_leaks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stdout: bytes,
    stderr: bytes,
    code: int,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("OPENAI_API_KEY", INHERITED)

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        _assert_security_call(args, kwargs)
        return subprocess.CompletedProcess(
            SECURITY_ARGV,
            code,
            stdout=stdout,
            stderr=stderr,
        )

    def fail_exec(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("exec-called")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(os, "execve", fail_exec)
    assert keychain_entry.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: keychain lookup failed\n"
    assert os.environ["OPENAI_API_KEY"] == INHERITED


@pytest.mark.parametrize(
    "boom",
    [
        subprocess.TimeoutExpired(
            SECURITY_ARGV,
            20,
            output=b"placeholder-timeout-stdout",
            stderr=b"placeholder-timeout-stderr",
        ),
        FileNotFoundError("placeholder-missing-security"),
        OSError("placeholder-security-oserror"),
    ],
)
def test_timeout_missing_binary_and_oserror_are_generic(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    boom: BaseException,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    def fake_run(*args: object, **kwargs: object) -> None:
        _assert_security_call(args, kwargs)
        raise boom

    def fail_exec(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("exec-called")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(os, "execve", fail_exec)
    assert keychain_entry.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: keychain lookup failed\n"


def test_exec_failure_does_not_leak_key_or_exception(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            SECURITY_ARGV,
            0,
            stdout=b"placeholder-keychain-value\n",
            stderr=b"placeholder-security-stderr",
        )

    def fake_exec(
        _executable: str,
        argv: list[str],
        env: dict[str, str],
    ) -> None:
        raise OSError(
            "placeholder-exec-oserror " + env["OPENAI_API_KEY"] + " " + " ".join(argv)
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", fake_exec)
    monkeypatch.setattr(os, "execve", fake_exec)
    assert keychain_entry.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: exec failed\n"


@pytest.mark.parametrize("shadow_source", ["cwd", "pythonpath"])
def test_replacement_isolates_module_resolution(tmp_path: Path, shadow_source: str) -> None:
    """The actual exec boundary must use the trusted installation after key injection."""
    runtime = tmp_path / "runtime"
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(runtime)
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    purelib = Path(subprocess.check_output(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        text=True,
    ).strip())
    trusted = purelib / "decision_browser"
    trusted.mkdir()
    (trusted / "__init__.py").write_text("")
    (trusted / "cli.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['TRUSTED_RESULT']).write_text(json.dumps({"
        "'isolated': sys.flags.isolated, 'has_key': "
        "os.environ.get('OPENAI_API_KEY') == 'placeholder-keychain-value', "
        "'args': sys.argv[1:]}))\n"
    )
    hostile = tmp_path / "hostile"
    shadow = hostile / "decision_browser"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("")
    (shadow / "cli.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "Path(os.environ['SHADOW_RESULT']).write_text('executed')\n"
    )
    clean_cwd = tmp_path / "clean"
    clean_cwd.mkdir()
    result = tmp_path / "trusted.json"
    shadow_result = tmp_path / "shadow.txt"
    env = {
        **{name: os.environ[name] for name in (
            "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL"
        ) if name in os.environ},
        "TRUSTED_RESULT": str(result),
        "SHADOW_RESULT": str(shadow_result),
        "OPENAI_API_KEY": INHERITED,
        "PYTHONPATH": str(hostile) if shadow_source == "pythonpath" else "",
    }
    source = Path(keychain_entry.__file__).resolve()
    launcher = (
        "import importlib.util; "
        f"spec = importlib.util.spec_from_file_location('entry', {str(source)!r}); "
        "entry = importlib.util.module_from_spec(spec); spec.loader.exec_module(entry); "
        f"entry._replace_with_serve({FAKE_KEY!r}, 'task_scope')"
    )
    child = subprocess.run(
        [str(python), "-I", "-c", launcher],
        cwd=hostile if shadow_source == "cwd" else clean_cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    assert not shadow_result.exists()
    assert json.loads(result.read_text()) == {
        "isolated": 1,
        "has_key": True,
        "args": ["serve", "--admission", "task_scope"],
    }
    assert FAKE_KEY not in child.stdout + child.stderr
