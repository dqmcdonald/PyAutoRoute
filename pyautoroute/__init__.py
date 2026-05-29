"""PyAutoRoute — simulated-annealing autorouter for 2-layer KiCad PCBs.

The overview below is the developer architecture guide (kept in sync with
`docs/architecture.md`); the submodule list links to the per-module API
reference, where each function and method documents its parameters.

.. include:: ../docs/architecture.md
"""

from importlib.metadata import PackageNotFoundError, version as _version

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
