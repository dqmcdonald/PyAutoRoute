"""Tkinter GUI for PyAutoRoute.

Launch with::

    pyautoroute-gui [board.kicad_pcb]

or from Python::

    from pyautoroute.gui import main
    main()

Requires tkinter (stdlib, but packaged separately on some Linux distros)
and matplotlib (``pip install pyautoroute[gui]``).
"""

from __future__ import annotations


def main(argv=None) -> int:
    """GUI entry point — checks for tkinter before importing the window."""
    try:
        import tkinter  # noqa: F401  # pylint: disable=unused-import
    except ImportError:
        print(
            "PyAutoRoute GUI requires tkinter.\n"
            "  Ubuntu/Debian:  sudo apt install python3-tk\n"
            "  macOS Homebrew: brew install python-tk\n",
            flush=True,
        )
        return 1
    try:
        import matplotlib  # noqa: F401  # pylint: disable=unused-import
    except ImportError:
        print(
            "PyAutoRoute GUI requires matplotlib.\n"
            "  pip install pyautoroute[gui]\n",
            flush=True,
        )
        return 1

    from .app import main as _main
    return _main(argv)
