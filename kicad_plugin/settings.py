"""Persistent settings for the PyAutoRoute KiCad plugin.

Stores the pyautoroute executable path in ``~/.config/pyautoroute_plugin.json``
and provides auto-detection across common venv locations.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

_SETTINGS_FILE = Path.home() / ".config" / "pyautoroute_plugin.json"

# Common venv locations to probe when pyautoroute isn't on PATH.
_VENV_CANDIDATES: list[Path] = [
    Path.home() / "venvs" / "tf" / "bin" / "pyautoroute",
    Path.home() / ".venv" / "bin" / "pyautoroute",
    Path.home() / "venv" / "bin" / "pyautoroute",
    Path.home() / ".virtualenvs" / "pyautoroute" / "bin" / "pyautoroute",
]


def find_executable() -> str:
    """Return the path to the pyautoroute executable, or ``''`` if not found.

    Search order: saved settings → PATH → common venv locations.
    """
    saved = load_settings().get("executable", "")
    if saved and Path(saved).is_file():
        return saved
    found = shutil.which("pyautoroute")
    if found:
        return found
    for p in _VENV_CANDIDATES:
        if p.is_file():
            return str(p)
    return ""


def load_settings() -> dict:
    """Load plugin settings from disk; return ``{}`` on any error."""
    try:
        return json.loads(_SETTINGS_FILE.read_text())
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    """Persist plugin settings to disk."""
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_FILE.write_text(json.dumps(data, indent=2))
