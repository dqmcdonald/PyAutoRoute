"""Tests for pyautoroute.geometry (pad/outline shapely geometry)."""

from __future__ import annotations

import math
import pathlib

import pytest

from pyautoroute import geometry, pcb, rules as rules_mod
from pyautoroute.pcb import Board, OutlineShape, Pad, Via

REPO = pathlib.Path(__file__).resolve().parent.parent
PCB = REPO / "TestProjects" / "Test1" / "Test1.kicad_pcb"


def _pad(shape, w, h, cx=0, cy=0, angle=0, rratio=None, delta=None):
    return Pad(net="N", pad_type="smd", shape=shape, cx=cx, cy=cy, w=w, h=h,
               angle=angle, copper_layers=["F.Cu"], roundrect_rratio=rratio,
               rect_delta=delta)


def _hole(cx, cy, drill=3.2, pad_type="np_thru_hole", net="", layers=None, ref="MH"):
    """A drilled through-hole pad (defaults to a layerless NPTH mounting hole)."""
    return Pad(net=net, pad_type=pad_type, shape="circle", cx=cx, cy=cy,
               w=drill, h=drill, angle=0, copper_layers=layers or [],
               drill=drill, fp_ref=ref)


def _board(pads, copper=("F.Cu", "B.Cu"), free_vias=None):
    """Minimal Board carrying just the pads/vias the geometry helpers read."""
    return Board(tree=None, copper_layers=list(copper), pads=pads,
                 free_vias=free_vias or [], segments=[], zones=[], outline=[])


def _via(cx, cy, drill=0.3, size=0.6, net="GND"):
    return Via(cx=cx, cy=cy, size=size, drill=drill,
              layers=("F.Cu", "B.Cu"), net=net)


def test_rect_pad_area_and_center():
    poly = geometry.pad_polygon(_pad("rect", 2.0, 1.0, cx=5, cy=7))
    assert math.isclose(poly.area, 2.0, rel_tol=1e-6)
    assert math.isclose(poly.centroid.x, 5.0, abs_tol=1e-6)
    assert math.isclose(poly.centroid.y, 7.0, abs_tol=1e-6)


def test_circle_pad_area():
    poly = geometry.pad_polygon(_pad("circle", 2.0, 2.0))
    assert math.isclose(poly.area, math.pi, rel_tol=1e-2)


def test_roundrect_area_between_rect_and_inscribed():
    poly = geometry.pad_polygon(_pad("roundrect", 2.0, 2.0, rratio=0.25))
    assert poly.area < 4.0           # rounded corners removed area
    assert poly.area > 3.0


def test_rotated_rect_bounds():
    # 2x1 rect rotated 90deg -> bounds become 1 wide, 2 tall
    poly = geometry.pad_polygon(_pad("rect", 2.0, 1.0, angle=90))
    minx, miny, maxx, maxy = poly.bounds
    assert math.isclose(maxx - minx, 1.0, abs_tol=1e-6)
    assert math.isclose(maxy - miny, 2.0, abs_tol=1e-6)


def test_outline_poly_area_and_bounds():
    shapes = [OutlineShape("poly", {"pts": [(0, 0), (10, 0), (10, 5), (0, 5)]})]
    poly = geometry.outline_to_polygon(shapes)
    assert math.isclose(poly.area, 50.0, rel_tol=1e-9)


def test_outline_from_lines_stitched():
    pts = [(0, 0), (4, 0), (4, 3), (0, 3)]
    lines = []
    for i in range(4):
        a, b = pts[i], pts[(i + 1) % 4]
        lines.append(OutlineShape("line", {"start": a, "end": b}))
    poly = geometry.outline_to_polygon(lines)
    assert math.isclose(poly.area, 12.0, rel_tol=1e-9)


def test_outline_interior_cutout_becomes_a_hole():
    """A closed shape wholly inside the outer outline (a milled slot or a
    large drilled hole drawn as its own Edge.Cuts loop) must be subtracted as
    an interior hole, not merged in as extra board area."""
    shapes = [
        OutlineShape("rect", {"start": (0, 0), "end": (20, 20)}),   # outer, area 400
        OutlineShape("rect", {"start": (8, 8), "end": (12, 12)}),   # cutout, area 16
    ]
    poly = geometry.outline_to_polygon(shapes)
    assert math.isclose(poly.area, 400.0 - 16.0, rel_tol=1e-9)
    assert len(poly.interiors) == 1
    # the cutout's centre is now outside the board area
    assert not poly.contains(geometry.Point(10, 10))
    # a point elsewhere in the board is still inside
    assert poly.contains(geometry.Point(2, 2))


def test_outline_non_contained_shapes_still_union():
    """Two overlapping-but-not-contained shapes (an L-shaped board built from
    two rects) still add together — only wholly-contained shapes are holes."""
    shapes = [
        OutlineShape("rect", {"start": (0, 0), "end": (10, 10)}),
        OutlineShape("rect", {"start": (5, 5), "end": (15, 15)}),
    ]
    poly = geometry.outline_to_polygon(shapes)
    assert math.isclose(poly.area, 175.0, rel_tol=1e-9)   # 100 + 100 - 25 overlap
    assert len(poly.interiors) == 0


def test_inflate_grows_area():
    poly = geometry.pad_polygon(_pad("rect", 2.0, 2.0))
    grown = geometry.inflate(poly, 0.5)
    assert grown.area > poly.area
    assert grown.contains(poly)


@pytest.mark.skipif(not PCB.exists(), reason="Test1 board not present")
def test_test1_pads_inside_outline():
    board = pcb.load_board(PCB)
    outline = geometry.outline_to_polygon(board.outline)
    grown = outline.buffer(0.5)   # tolerate pads near the edge
    inside = sum(1 for p in board.pads if grown.contains(geometry.pad_polygon(p).centroid))
    # the vast majority of pad centres must lie within the board area;
    # this catches a wrong rotation convention (which throws pads far away)
    assert inside >= int(0.95 * len(board.pads))


@pytest.mark.skipif(not PCB.exists(), reason="Test1 board not present")
def test_obstacle_index_layers():
    board = pcb.load_board(PCB)
    idx = geometry.ObstacleIndex(geometry.board_obstacles(board))
    assert set(idx.layers()) <= {"F.Cu", "B.Cu"}
    assert "F.Cu" in idx.layers()


# --- drill geometry / hole-to-hole DRC ---------------------------------------

def test_board_drills_collects_through_holes_only():
    board = _board([
        _hole(0, 0, drill=3.2, pad_type="np_thru_hole"),
        _hole(10, 0, drill=1.0, pad_type="thru_hole", net="GND"),
        _pad("rect", 1.0, 1.0, cx=20, cy=0),            # SMD: no drill
    ])
    drills = geometry.board_drills(board)
    assert len(drills) == 2
    assert {round(d.radius, 3) for d in drills} == {1.6, 0.5}
    assert {d.plated for d in drills} == {True, False}


def test_drill_violations_flags_close_holes():
    rules = rules_mod.default_rules()       # min_hole_to_hole == 0.25
    # edge-to-edge gap = 3.3 - 1.6 - 1.6 = 0.1 mm < 0.25 -> violation
    near = _board([_hole(0, 0), _hole(3.3, 0)])
    assert len(geometry.drill_violations(near, rules)) == 1
    # spaced well apart -> clean
    far = _board([_hole(0, 0), _hole(20, 0)])
    assert geometry.drill_violations(far, rules) == []


def test_drill_violations_no_same_net_exemption():
    """Two holes on the same net still must respect hole-to-hole spacing."""
    rules = rules_mod.default_rules()
    board = _board([_hole(0, 0, net="GND"), _hole(3.3, 0, net="GND")])
    assert len(geometry.drill_violations(board, rules)) == 1


def test_board_drills_includes_free_vias():
    """Vias (routing vias, and ground-plane connectivity/stitching vias — all
    plain `(via ...)` nodes by the time the board is reloaded) must join the
    drill set, or via-to-via and via-to-pad hole spacing is never
    self-checked."""
    board = _board([], free_vias=[_via(0, 0), _via(10, 10)])
    drills = geometry.board_drills(board)
    assert len(drills) == 2
    assert all(d.plated for d in drills)


def test_drill_violations_flags_close_vias():
    rules = rules_mod.default_rules()       # min_hole_to_hole == 0.25
    # edge-to-edge gap = 0.4 - 0.15 - 0.15 = 0.1 mm < 0.25 -> violation
    close = _board([], free_vias=[_via(0, 0), _via(0.4, 0)])
    assert len(geometry.drill_violations(close, rules)) == 1
    far = _board([], free_vias=[_via(0, 0), _via(20, 0)])
    assert geometry.drill_violations(far, rules) == []


def test_drill_violations_flags_via_too_close_to_pad_hole():
    rules = rules_mod.default_rules()
    # THT pad drill 3.2 (r=1.6) and a via 0.3 away from its edge -> violation
    board = _board([_hole(0, 0, drill=3.2, pad_type="thru_hole", net="GND")],
                   free_vias=[_via(1.7, 0)])
    assert len(geometry.drill_violations(board, rules)) == 1


def _two_class_rules(tight=0.1, wide=0.5):
    """DesignRules with net "A" on a tight-clearance Default class and net "B"
    on a wide-clearance "Wide" class (assignment mirrors a KiCad net-class
    override), so `pair_clearance("A", "B") == wide` while `clearance_for("A")
    == tight`."""
    default_cls = rules_mod.NetClass(name="Default", clearance=tight,
                                     track_width=0.2, via_diameter=0.6, via_drill=0.3)
    wide_cls = rules_mod.NetClass(name="Wide", clearance=wide,
                                  track_width=0.2, via_diameter=0.6, via_drill=0.3)
    return rules_mod.DesignRules(
        classes={"Default": default_cls, "Wide": wide_cls},
        default_class=default_cls,
        assignments={"B": "Wide"},
        patterns=[],
        min_clearance=0.0, min_track_width=0.2,
        min_via_diameter=0.6, min_via_drill=0.3,
        min_copper_edge_clearance=0.5, min_hole_to_hole=0.25,
    )


def test_clearance_violations_catches_cross_netclass_gap():
    """A gap that clears the tight net's own clearance but not the pair's
    (larger, cross-class) required clearance must still be flagged.

    Net "A" (Default, clearance 0.1) and net "B" (Wide, clearance 0.5) are
    0.3 mm apart: outside A's own clearance but inside the 0.5 mm pair
    requirement (the larger of the two), so this is a real DRC violation.
    """
    rules = _two_class_rules(tight=0.1, wide=0.5)
    pad_a = _pad("rect", 1.0, 1.0, cx=0, cy=0)
    pad_a.net = "A"
    pad_b = _pad("rect", 1.0, 1.0, cx=1.3, cy=0)   # edge-to-edge gap = 0.3 mm
    pad_b.net = "B"
    board = _board([pad_a, pad_b])
    violations = geometry.clearance_violations(board, rules)
    assert len(violations) == 1
    layer, net_a, net_b, gap = violations[0]
    assert {net_a, net_b} == {"A", "B"}
    assert math.isclose(gap, 0.3, abs_tol=1e-6)


def test_board_obstacles_reserve_npth_barrel_all_layers():
    """A layerless NPTH hole becomes an all-layer barrel keep-out."""
    board = _board([_hole(5, 5, drill=3.2)])
    obs = geometry.board_obstacles(board)
    # one barrel disk per copper layer, net-agnostic (empty net)
    assert {o.layer for o in obs} == {"F.Cu", "B.Cu"}
    for o in obs:
        assert o.net == ""
        assert o.geom.distance(geometry.Point(5, 5)) == 0    # disk covers centre
        assert math.isclose(o.geom.area, math.pi * 1.6 ** 2, rel_tol=1e-2)


def test_board_obstacles_no_duplicate_barrel_where_copper_exists():
    """A plated THT pad coppered on all layers needs no extra barrel disk."""
    board = _board([_hole(0, 0, drill=1.0, pad_type="thru_hole", net="GND",
                          layers=["F.Cu", "B.Cu"])])
    obs = geometry.board_obstacles(board)
    # exactly the two copper-ring obstacles, no bare barrels added
    assert len(obs) == 2
    assert all(o.net == "GND" for o in obs)
