"""Tests for pyautoroute.__version__ (must not drift from pyproject.toml)."""

from __future__ import annotations

import pathlib

import pyautoroute


def test_read_pyproject_version_parses_project_section(tmp_path):
    text = (
        '[build-system]\n'
        'requires = ["setuptools"]\n'
        '\n'
        '[project]\n'
        'name = "x"\n'
        'version = "1.2.3"\n'
        '\n'
        '[project.optional-dependencies]\n'
        'version = "9.9.9"\n'   # decoy 'version' key outside [project]
    )
    p = tmp_path / "pyproject.toml"
    p.write_text(text)
    assert pyautoroute._read_pyproject_version(p) == "1.2.3"


def test_read_pyproject_version_missing_file_returns_none(tmp_path):
    assert pyautoroute._read_pyproject_version(tmp_path / "nope.toml") is None


def test_version_matches_repo_pyproject_toml():
    """Regression: __version__ used to come from the editable install's
    captured metadata, which silently drifted from pyproject.toml across
    version bumps unless `pip install -e .` was re-run every time."""
    repo_root = pathlib.Path(pyautoroute.__file__).resolve().parent.parent
    declared = pyautoroute._read_pyproject_version(repo_root / "pyproject.toml")
    assert declared is not None
    assert pyautoroute.__version__ == declared
