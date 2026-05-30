"""Tests for the CLI startup printout of footprint placement constraints."""

from __future__ import annotations

import types

from pyautoroute import autoroute


def _fp(ref, *, edge=None, locked=False, overlap=False):
    return types.SimpleNamespace(ref=ref, edge_affinity=edge,
                                 locked=locked, overlap_ok=overlap)


def _board(footprints):
    return types.SimpleNamespace(footprints=footprints)


def test_summary_combines_constraints():
    assert autoroute._footprint_constraint_summary(_fp("U1")) is None
    assert autoroute._footprint_constraint_summary(_fp("J1", edge="left")) == "edge=left"
    assert autoroute._footprint_constraint_summary(_fp("U2", locked=True)) == "locked"
    assert autoroute._footprint_constraint_summary(_fp("SH1", overlap=True)) == "overlap"
    assert autoroute._footprint_constraint_summary(
        _fp("J2", edge="any", locked=True)) == "edge=any, locked"


def test_print_lists_constrained_footprints_sorted(capsys):
    board = _board([
        _fp("U3", locked=True),
        _fp("J1", edge="left"),
        _fp("C5"),                       # unconstrained — must not appear
        _fp("SH1", overlap=True),
        _fp("J2", edge="any", locked=True),
    ])
    autoroute._print_footprint_constraints(board)
    out = capsys.readouterr().out
    assert "constraints:" in out
    # constrained refs appear with their values; the plain one does not
    assert "J1" in out and "edge=left" in out
    assert "J2" in out and "edge=any, locked" in out
    assert "U3" in out and "locked" in out
    assert "SH1" in out and "overlap" in out
    assert "C5" not in out
    # sorted by ref: J1, J2, SH1, U3
    body = out[out.index("constraints:"):]
    assert (body.index("J1") < body.index("J2") < body.index("SH1")
            < body.index("U3"))


def test_print_silent_when_no_constraints(capsys):
    autoroute._print_footprint_constraints(_board([_fp("U1"), _fp("R2")]))
    assert capsys.readouterr().out == ""


def test_print_silent_when_no_footprints(capsys):
    autoroute._print_footprint_constraints(_board([]))
    assert capsys.readouterr().out == ""
