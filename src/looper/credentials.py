"""API key storage. Keys never go in looper.json.

Lookup order for a key named e.g. ``ANTHROPIC_API_KEY``:

1. the process environment
2. the OS keyring (only if the optional ``keyring`` package is installed)
3. ``<looper home>/credentials.env`` (``$LOOPER_HOME`` or ``~/.config/looper``), mode 0600

Nothing in this module ever prints a key; ``mask`` shows only the last 4 chars.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from looper.config import user_config_dir

KEYRING_SERVICE = "looper"
_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def credentials_path() -> Path:
    return user_config_dir() / "credentials.env"


def _keyring():
    try:
        import keyring  # type: ignore[import-not-found]
        from keyring.backends import fail  # type: ignore[import-not-found]

        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:  # noqa: BLE001 - optional dependency, any failure = unavailable
        return None


def keyring_available() -> bool:
    return _keyring() is not None


def _read_file() -> dict[str, str]:
    p = credentials_path()
    if not p.is_file():
        return {}
    out: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def _write_file(data: dict[str, str]) -> Path:
    p = credentials_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p.parent, 0o700)
    except OSError:
        pass
    body = "# looper API keys - do not commit. Managed by `looper keys` / `looper setup`.\n" + "".join(
        f"{k}={v}\n" for k, v in sorted(data.items()))
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".credentials.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    os.chmod(p, 0o600)
    return p


def validate_name(name: str) -> str:
    if not _NAME.match(name or ""):
        raise ValueError(f"invalid env var name {name!r}")
    return name


def set_key(name: str, value: str, *, use_keyring: bool = False) -> str:
    """Store a key. Returns where it went: 'keyring' or the file path."""
    validate_name(name)
    value = (value or "").strip()
    if not value or "\n" in value:
        raise ValueError("empty or multi-line key")
    if use_keyring:
        kr = _keyring()
        if kr is None:
            raise RuntimeError("keyring is not installed/usable; pip install 'looper[keyring]'")
        kr.set_password(KEYRING_SERVICE, name, value)
        return "keyring"
    data = _read_file()
    data[name] = value
    return str(_write_file(data))


def get_key(name: str | None) -> str | None:
    if not name:
        return None
    v = os.environ.get(name)
    if v:
        return v
    kr = _keyring()
    if kr is not None:
        try:
            v = kr.get_password(KEYRING_SERVICE, name)
        except Exception:  # noqa: BLE001
            v = None
        if v:
            return v
    return _read_file().get(name) or None


def key_source(name: str | None) -> str:
    if not name:
        return "n/a"
    if os.environ.get(name):
        return "env"
    kr = _keyring()
    if kr is not None:
        try:
            if kr.get_password(KEYRING_SERVICE, name):
                return "keyring"
        except Exception:  # noqa: BLE001
            pass
    if name in _read_file():
        return "file"
    return "missing"


def remove_key(name: str) -> list[str]:
    """Remove from keyring and file (never touches the process env). Returns places removed."""
    validate_name(name)
    removed = []
    kr = _keyring()
    if kr is not None:
        try:
            if kr.get_password(KEYRING_SERVICE, name):
                kr.delete_password(KEYRING_SERVICE, name)
                removed.append("keyring")
        except Exception:  # noqa: BLE001
            pass
    data = _read_file()
    if name in data:
        del data[name]
        _write_file(data)
        removed.append("file")
    return removed


def stored_names() -> list[str]:
    return sorted(_read_file())


def mask(value: str | None) -> str:
    if not value:
        return "-"
    return "••••" + value[-4:] if len(value) >= 12 else "••••"


@dataclass
class KeyStatus:
    name: str
    providers: list[str]
    source: str
    masked: str


def status(names_to_providers: dict[str, list[str]]) -> list[KeyStatus]:
    names = dict(names_to_providers)
    for n in stored_names():
        names.setdefault(n, [])
    return [KeyStatus(n, sorted(p), key_source(n), mask(get_key(n))) for n, p in sorted(names.items())]
