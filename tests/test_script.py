"""Smoke tests for the pyautoroute.sh helper menu."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "pyautoroute.sh"
_BASH = shutil.which("bash")


def test_script_is_present_and_executable():
    assert _SCRIPT.exists()
    assert os.access(_SCRIPT, os.X_OK)


@pytest.mark.skipif(_BASH is None, reason="bash not available")
def test_script_syntax_valid():
    assert subprocess.run([_BASH, "-n", str(_SCRIPT)]).returncode == 0


@pytest.mark.skipif(_BASH is None, reason="bash not available")
def test_script_menu_lists_options_and_quits():
    r = subprocess.run([_BASH, str(_SCRIPT)], input="8\n",
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert "PyAutoRoute" in r.stdout
    assert "Run tests" in r.stdout
