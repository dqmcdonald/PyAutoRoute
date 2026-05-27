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
