"""PyAutoRoute — simulated-annealing autorouter for 2-layer KiCad PCBs.

The overview below is the developer architecture guide (kept in sync with
`docs/architecture.md`); the submodule list links to the per-module API
reference, where each function and method documents its parameters.

.. include:: ../docs/architecture.md
"""

import re
from importlib.metadata import PackageNotFoundError, version as _version
from pathlib import Path


def _read_pyproject_version(pyproject: Path | None = None) -> str | None:
    """Read ``[project].version`` straight from ``pyproject.toml``.

    An editable install (``pip install -e .``) snapshots package metadata at
    install time, so ``importlib.metadata.version()`` silently drifts from the
    source across version bumps unless the install is re-run. When developing
    from a checkout, reading the version straight from the adjacent
    ``pyproject.toml`` keeps ``__version__`` live; a real (non-editable)
    install has no ``pyproject.toml`` next to the package, so it falls back to
    the recorded metadata below.

    Args:
        pyproject: path to the ``pyproject.toml`` to read; defaults to the one
            next to the repo root (two levels up from this file).

    Returns:
        The declared version, or `None` if the file is absent or has no
        ``version`` key inside ``[project]``.
    """
    if pyproject is None:
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject.is_file():
        return None
    in_project = False
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_project = stripped == "[project]"
            continue
        if in_project and stripped.startswith("version"):
            m = re.match(r'version\s*=\s*"([^"]+)"', stripped)
            if m:
                return m.group(1)
    return None


__version__ = _read_pyproject_version()
if __version__ is None:
    try:
        __version__ = _version("pyautoroute")
    except PackageNotFoundError:  # running from a source tree without an install
        __version__ = "unknown"

# Whether the optional native A* core (`pyautoroute._astar_c`) is built and in
# use. When True, `router.astar` dispatches to the Cython search (5-20x faster);
# when False it falls back transparently to the optimised pure-Python search.
# Build the extension with ``pip install -e ".[fast]" && python setup.py
# build_ext --inplace``.
from .router import _USE_C_ASTAR as HAS_C_ASTAR  # noqa: E402
