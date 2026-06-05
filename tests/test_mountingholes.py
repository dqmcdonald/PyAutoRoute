"""Tests for pyautoroute.mountingholes and pcb.make_npth."""

from __future__ import annotations

import pathlib

import pytest

from pyautoroute import geometry, mountingholes, pcb, rules as rules_mod
from pyautoroute.pcb import Board, OutlineShape, Pad

REPO = pathlib.Path(__file__).resolve().parent.parent
PCB = REPO / "TestProjects" / "Test1" / "Test1.kicad_pcb"


def _rect_board(w=50.0, h=30.0, pads=None):
    """A board with a simple rectangular outline and an empty (appendable) tree."""
    return Board(tree=[], copper_layers=["F.Cu", "B.Cu"], pads=list(pads or []),
                 free_vias=[], segments=[], zones=[],
                 outline=[OutlineShape("rect", {"start": (0, 0), "end": (w, h)})])


def _hole(cx, cy, drill=3.2, ref="MH", net="", layers=None):
    """A drilled NPTH pad (defaults to a layerless mounting hole)."""
    return Pad(net=net, pad_type="np_thru_hole", shape="circle", cx=cx, cy=cy,
               w=drill, h=drill, angle=0, copper_layers=layers or [],
               drill=drill, fp_ref=ref)


# --- location-code grammar ---------------------------------------------------

def test_expand_entry_coord_vs_codes():
    assert mountingholes.expand_entry("10,10") == ["10,10"]
    assert mountingholes.expand_entry("TL,TR,BL") == ["TL", "TR", "BL"]
    assert mountingholes.expand_entry("TL") == ["TL"]


def test_resolve_positions_corner_codes_y_down():
    # bounds (0,0,50,30), margin 5 -> top = min y
    coords, warns = mountingholes.resolve_positions(
        (0, 0, 50, 30), ["TL", "TR", "BL", "BR", "C"], margin=5.0)
    assert warns == []
    by_label = {label: (x, y) for x, y, label in coords}
    assert by_label["TL"] == (5, 5)
    assert by_label["TR"] == (45, 5)
    assert by_label["BL"] == (5, 25)
    assert by_label["BR"] == (45, 25)
    assert by_label["C"] == (25, 15)


def test_resolve_positions_edge_codes_and_explicit_xy():
    coords, warns = mountingholes.resolve_positions(
        (0, 0, 50, 30), ["T", "L", "12.5,7.5"], margin=5.0)
    assert warns == []
    by_label = {label: (x, y) for x, y, label in coords}
    assert by_label["T"] == (25, 5)
    assert by_label["L"] == (5, 15)
    assert by_label["12.5,7.5"] == (12.5, 7.5)


def test_resolve_positions_bad_token_warns():
    coords, warns = mountingholes.resolve_positions((0, 0, 50, 30), ["ZZ"], 5.0)
    assert coords == []
    assert len(warns) == 1 and "ZZ" in warns[0]


# --- build / injection -------------------------------------------------------

def test_build_corners_injects_four_holes():
    board = _rect_board()
    rules = rules_mod.default_rules()
    nodes, warns = mountingholes.build(board, rules, diameter=3.2, margin=5.0,
                                       pattern="corners", hole_at=None)
    assert len(nodes) == 4
    assert not warns
    # four NPTH pads added, all with the right drill and no net
    holes = [p for p in board.pads if p.pad_type == "np_thru_hole"]
    assert len(holes) == 4
    assert all(h.drill == 3.2 and h.net == "" for h in holes)
    centres = {(round(h.cx), round(h.cy)) for h in holes}
    assert centres == {(5, 5), (45, 5), (5, 25), (45, 25)}


def test_build_custom_uses_hole_at_only():
    board = _rect_board()
    rules = rules_mod.default_rules()
    nodes, _ = mountingholes.build(board, rules, diameter=3.2, margin=5.0,
                                   pattern="custom", hole_at=["C", "10,10"])
    centres = {(round(p.cx), round(p.cy)) for p in board.pads}
    assert centres == {(25, 15), (10, 10)}
    assert len(nodes) == 2


def test_build_skips_hole_outside_outline():
    board = _rect_board()
    rules = rules_mod.default_rules()
    nodes, warns = mountingholes.build(board, rules, diameter=3.2, margin=5.0,
                                       pattern="custom", hole_at=["100,100"])
    assert nodes == []
    assert any("outside" in w for w in warns)


def test_build_skips_hole_overlapping_copper():
    pad = Pad(net="N", pad_type="smd", shape="rect", cx=5, cy=5, w=3, h=3,
              angle=0, copper_layers=["F.Cu"])
    board = _rect_board(pads=[pad])
    rules = rules_mod.default_rules()
    nodes, warns = mountingholes.build(board, rules, diameter=3.2, margin=5.0,
                                       pattern="corners", hole_at=None)
    # the TL corner (5,5) collides with the pad -> 3 of 4 placed
    assert len(nodes) == 3
    assert any("overlaps copper" in w for w in warns)


def test_build_no_outline_warns():
    board = _rect_board()
    board.outline = []
    rules = rules_mod.default_rules()
    nodes, warns = mountingholes.build(board, rules, diameter=3.2, margin=5.0)
    assert nodes == []
    assert any("outline" in w for w in warns)


# --- placement interaction ---------------------------------------------------

def test_positions_known_preplacement():
    f = mountingholes.positions_known_preplacement
    # corner codes need the outline -> only known if it's kept fixed
    assert f("corners", None, keep_outline=False) is False
    assert f("corners", None, keep_outline=True) is True
    # explicit coords are always known up front
    assert f("custom", ["10,10", "20,20"], keep_outline=False) is True
    # a code under custom still needs the outline
    assert f("custom", ["TL"], keep_outline=False) is False
    assert f("custom", ["TL"], keep_outline=True) is True
    assert f("custom", None, keep_outline=False) is False


def test_build_lock_adds_locked_footprints():
    board = _rect_board()
    rules = rules_mod.default_rules()
    nodes, _ = mountingholes.build(board, rules, diameter=3.2, margin=5.0,
                                   pattern="corners", hole_at=None, lock=True)
    assert len(nodes) == 4
    holes = [fp for fp in board.footprints if fp.locked]
    assert len(holes) == 4
    for fp in holes:
        assert len(fp.pads) == 1 and fp.pads[0].drill == 3.2
        assert fp.at_node is not None and fp.fp_node is not None    # wired to tree


# --- boards that already have holes -------------------------------------------

def test_build_skips_coincident_existing_hole_and_keeps_refs_unique():
    from pyautoroute.pcb import Footprint
    existing = _hole(5, 5, drill=3.2, ref="MH1")        # already-drilled TL corner
    board = _rect_board(pads=[existing])
    board.footprints.append(Footprint(
        ref="MH1", x=5, y=5, angle=0, locked=True, overlap_ok=False,
        pads=[existing], local_offsets=[(0, 0, 0)], at_node=None, fp_node=None))
    rules = rules_mod.default_rules()
    nodes, warns = mountingholes.build(board, rules, diameter=3.2, margin=5.0,
                                       pattern="corners", hole_at=None)
    # TL is already drilled -> reported as existing, the other 3 corners added
    assert len(nodes) == 3
    assert any("already exists" in w for w in warns)
    new_refs = [p.fp_ref for p in board.pads
                if p.pad_type == "np_thru_hole" and p is not existing]
    assert "MH1" not in new_refs                         # no refdes collision
    assert len(set(new_refs)) == 3                        # all unique


def test_build_is_idempotent_on_rerun():
    board = _rect_board()
    rules = rules_mod.default_rules()
    n1, _ = mountingholes.build(board, rules, diameter=3.2, margin=5.0)
    assert len(n1) == 4
    n2, warns = mountingholes.build(board, rules, diameter=3.2, margin=5.0)
    assert n2 == []                                       # nothing added a 2nd time
    assert warns and all("already exists" in w for w in warns)


# --- make_npth round-trip ----------------------------------------------------

@pytest.mark.skipif(not PCB.exists(), reason="Test1 board not present")
def test_make_npth_round_trips(tmp_path):
    board = pcb.load_board(PCB)
    n_before = sum(1 for p in board.pads if p.pad_type == "np_thru_hole")
    minx, miny, _, _ = geometry.outline_to_polygon(board.outline).bounds
    node = pcb.make_npth(minx + 5, miny + 5, 3.2, ref="MH1")
    board.tree.append(node)
    out = tmp_path / "with_hole.kicad_pcb"
    pcb.write_board(board, out)

    reloaded = pcb.load_board(out)
    npth = [p for p in reloaded.pads if p.pad_type == "np_thru_hole"]
    assert len(npth) == n_before + 1
    hole = next(p for p in npth if abs(p.cx - (minx + 5)) < 1e-6)
    assert hole.drill == 3.2
    assert hole.net == ""
    # picked up by the drill-geometry helpers
    refs = {d.ref for d in geometry.board_drills(reloaded)}
    assert "MH1" in refs
