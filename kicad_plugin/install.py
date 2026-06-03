"""Console script: ``pyautoroute-install-plugin``.

Creates a symlink from the KiCad user-plugins directory to this plugin package,
so KiCad discovers it automatically on the next launch.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _kicad_plugins_dir() -> Path | None:
    """Return the best KiCad user plugins directory, or ``None`` if not found."""
    kicad_base = Path.home() / "Documents" / "KiCad"
    if not kicad_base.is_dir():
        return None
    # Pick the highest version number found.
    def _ver_key(name: str) -> tuple:
        try:
            return tuple(int(p) for p in name.split("."))
        except ValueError:
            return (0,)

    versions = sorted(
        [d.name for d in kicad_base.iterdir() if d.is_dir() and d.name[:1].isdigit()],
        key=_ver_key, reverse=True,
    )
    if not versions:
        return None
    return kicad_base / versions[0] / "scripting" / "plugins"


def main() -> int:
    """Entry point for ``pyautoroute-install-plugin``."""
    plugin_src = Path(__file__).parent.resolve()
    plugins_dir = _kicad_plugins_dir()

    if plugins_dir is None:
        print(
            "Could not find ~/Documents/KiCad/<version>/ — "
            "is KiCad installed?",
            file=sys.stderr,
        )
        return 1

    plugins_dir.mkdir(parents=True, exist_ok=True)
    target = plugins_dir / "pyautoroute"

    if target.is_symlink():
        target.unlink()
    elif target.exists():
        print(
            f"{target} already exists and is not a symlink.\n"
            "Remove it manually and re-run to install.",
            file=sys.stderr,
        )
        return 1

    target.symlink_to(plugin_src)
    print(f"Installed: {target}")
    print(f"       → {plugin_src}")
    print("Restart KiCad (or reload scripting) to activate the plugin.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
