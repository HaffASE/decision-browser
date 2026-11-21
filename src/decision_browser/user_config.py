"""Private, user-owned provider profile; no network or credential discovery."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from decision_browser.provider_values import (
    ProviderConfigError,
    provider_names,
    settings_from_environment,
)

if TYPE_CHECKING:
    from decision_browser.provider_values import ProviderSettings

_MAX_BYTES = 16_384
_FIELDS = {
    "provider": "DECISION_BROWSER_PROVIDER",
    "endpoint": "DECISION_BROWSER_ENDPOINT",
    "model": "DECISION_BROWSER_MODEL",
    "max_choices": "DECISION_BROWSER_MAX_CHOICES",
    "api_key": "DECISION_BROWSER_API_KEY",
}


def profile_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    override = env.get("DECISION_BROWSER_CONFIG_FILE")
    if override is None:
        return Path.home() / ".config" / "decision-browser" / "provider.json"
    if not override or "\x00" in override:
        raise ProviderConfigError("invalid_profile_path")
    path = Path(override)
    if not path.is_absolute() or ".." in path.parts:
        raise ProviderConfigError("invalid_profile_path")
    return path


def _parents(path: Path) -> None:
    for parent in path.parents:
        if parent.is_symlink():
            raise ProviderConfigError("unsafe_profile_path")


def _private(info: os.stat_result, *, directory: bool = False) -> None:
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct_type:
        raise ProviderConfigError("unsafe_profile_path")
    if os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise ProviderConfigError("unsafe_profile_permissions")
    if not directory and info.st_nlink != 1:
        raise ProviderConfigError("unsafe_profile_path")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderConfigError("invalid_profile")
        result[key] = value
    return result


def _environment(data: Any) -> dict[str, str]:
    if (type(data) is not dict or set(data) - {"version", *_FIELDS}
            or type(data.get("version")) is not int or data["version"] != 1
            or type(data.get("provider")) is not str or data["provider"] not in provider_names()):
        raise ProviderConfigError("invalid_profile")
    env: dict[str, str] = {}
    for field, name in _FIELDS.items():
        if field not in data:
            continue
        value = data[field]
        if field == "max_choices":
            if type(value) is not int:
                raise ProviderConfigError("invalid_profile")
            value = str(value)
        elif field == "api_key" and value is None:
            continue
        if type(value) is not str:
            raise ProviderConfigError("invalid_profile")
        env[name] = value
    try:
        settings_from_environment(env)
    except ProviderConfigError:
        raise ProviderConfigError("invalid_profile") from None
    if os.name == "nt" and env.get("DECISION_BROWSER_API_KEY"):
        raise ProviderConfigError("profile_credentials_unsupported")
    return env


def load_profile(environ: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """Read only our bounded, private profile. Missing configuration is normal."""
    path = profile_path(environ)
    try:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        _parents(path)
        _private(info)
        _private(path.parent.stat(), directory=True)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            _private(opened)
            _parents(path)
            current = path.lstat()
            if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                    or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
                raise ProviderConfigError("unsafe_profile_path")
            raw = handle.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ProviderConfigError("invalid_profile")
        return _environment(json.loads(raw, object_pairs_hook=_unique))
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise ProviderConfigError("profile_unreadable") from None


def _save_destination(path: Path) -> None:
    _parents(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _parents(path)
    _private(path.parent.stat(), directory=True)
    try:
        _private(path.lstat())
    except FileNotFoundError:
        pass


def prepare_profile_destination(
    environ: Mapping[str, str] | None = None, *, credentials: bool = False,
) -> Path:
    """Validate and probe the private destination before requesting a secret."""
    if credentials and os.name == "nt":
        raise ProviderConfigError("profile_credentials_unsupported")
    path = profile_path(environ)
    try:
        _save_destination(path)
        fd, temporary = tempfile.mkstemp(prefix=".provider-check-", dir=path.parent)
        try:
            os.close(fd)
        finally:
            os.unlink(temporary)
        _save_destination(path)
    except OSError:
        raise ProviderConfigError("profile_write_failed") from None
    return path


def save_profile(settings: ProviderSettings, environ: Mapping[str, str] | None = None) -> Path:
    """Atomically replace one profile; never print its plaintext credential."""
    from decision_browser.provider_values import ProviderSettings

    if type(settings) is not ProviderSettings or settings.text is not None:
        raise ProviderConfigError("invalid_profile")
    data = {"version": 1, "provider": settings.provider, "endpoint": settings.endpoint,
            "model": settings.model, "max_choices": settings.max_choices, "api_key": settings.api_key}
    _environment(data)
    payload = (json.dumps(data, ensure_ascii=True, indent=2) + "\n").encode()
    if len(payload) > _MAX_BYTES:
        raise ProviderConfigError("invalid_profile")
    path = profile_path(environ)
    temporary: str | None = None
    try:
        _save_destination(path)
        fd, temporary = tempfile.mkstemp(prefix=".provider-", dir=path.parent)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _save_destination(path)
        os.replace(temporary, path)
        temporary = None
    except OSError:
        raise ProviderConfigError("profile_write_failed") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return path
