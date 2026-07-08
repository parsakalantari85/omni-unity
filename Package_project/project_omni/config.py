"""Persistent user config stored in ~/.omni/config.json."""
from __future__ import annotations

import json
from pathlib import Path

_CONFIG_DIR = Path.home() / ".omni"
_CONFIG_FILE = _CONFIG_DIR / "config.json"


def _load() -> dict:
    try:
        return json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get(key: str, default=None):
    return _load().get(key, default)


def set(key: str, value) -> None:
    # Silent on purpose: callers report success. A bare print() here would
    # corrupt the full-screen prompt_toolkit UI when invoked mid-session.
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = _load()
    data[key] = value
    _CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def path() -> Path:
    """Location of the config file (for callers that report where they saved)."""
    return _CONFIG_FILE
