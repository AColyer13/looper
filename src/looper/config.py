"""Config discovery, loading, and merging.

Search order (first hit wins, merged over the bundled default unless the file
sets ``"inherit_defaults": false``):

1. ``--config PATH`` flag
2. ``$LOOPER_CONFIG``
3. ``./looper.json``
4. ``~/.config/looper/looper.json`` (honours ``$LOOPER_HOME`` / ``$XDG_CONFIG_HOME``)
5. bundled default (``looper/data/default_config.json``)
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from importlib import resources
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when a config file is missing, unreadable, or invalid."""


def bundled_default_text() -> str:
    return resources.files("looper").joinpath("data/default_config.json").read_text(encoding="utf-8")


def bundled_default() -> dict[str, Any]:
    return json.loads(bundled_default_text())


def user_config_dir() -> Path:
    """``$LOOPER_HOME`` if set, else ``$XDG_CONFIG_HOME/looper`` (default ``~/.config/looper``)."""
    home = os.environ.get("LOOPER_HOME")
    if home:
        return Path(home).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "looper"


def candidate_paths(explicit: Path | str | None = None) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    if explicit:
        out.append(("--config", Path(explicit).expanduser()))
    env = os.environ.get("LOOPER_CONFIG")
    if env:
        out.append(("$LOOPER_CONFIG", Path(env).expanduser()))
    out.append(("./looper.json", Path.cwd() / "looper.json"))
    out.append(("user", user_config_dir() / "looper.json"))
    return out


def find_config(explicit: Path | str | None = None) -> tuple[str, Path | None]:
    """Return (source label, path) of the config to use; path None = bundled."""
    for label, path in candidate_paths(explicit):
        if path.is_file():
            return label, path
        if label in ("--config", "$LOOPER_CONFIG"):
            raise ConfigError(f"{label} points to a missing file: {path}")
    return "bundled", None


def merge_config(base: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    """Merge a user config over the defaults.

    * ``defaults``/``router``: deep merge
    * ``agents``: per-agent shallow merge (override just ``binary`` if you like)
    * ``strategies``: per-role replace
    * ``loops``: per-loop replace (a loop is defined as a whole); set a loop to
      ``null`` to remove it.
    """
    out = deepcopy(base)
    for key, value in user.items():
        if key == "loops" and isinstance(value, dict):
            loops = out.setdefault("loops", {})
            for name, spec in value.items():
                if spec is None:
                    loops.pop(name, None)
                else:
                    loops[name] = deepcopy(spec)
        elif key == "agents" and isinstance(value, dict):
            agents = out.setdefault("agents", {})
            for name, spec in value.items():
                if spec is None:
                    agents.pop(name, None)
                elif isinstance(spec, dict):
                    agents[name] = {**agents.get(name, {}), **deepcopy(spec)}
        elif key in ("defaults", "router") and isinstance(value, dict):
            out[key] = _deep_merge(out.get(key, {}), value)
        elif key in ("tiers", "roles", "providers") and isinstance(value, dict):
            sect = out.setdefault(key, {})
            for name, spec in value.items():
                if spec is None:
                    sect.pop(name, None)
                elif isinstance(spec, dict):
                    sect[name] = {**sect.get(name, {}), **deepcopy(spec)}
        elif key == "strategies" and isinstance(value, dict):
            out.setdefault("strategies", {}).update(deepcopy(value))
        else:
            out[key] = deepcopy(value)
    return out


def _deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


def load_raw_config(explicit: Path | str | None = None) -> tuple[dict[str, Any], str, Path | None]:
    """Load the merged raw config dict. Returns (config, source label, path)."""
    label, path = find_config(explicit)
    base = bundled_default()
    if path is None:
        return base, label, None
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(user, dict):
        raise ConfigError(f"{path}: top level must be a JSON object")
    if user.pop("inherit_defaults", True) is False:
        return user, label, path
    return merge_config(base, user), label, path
