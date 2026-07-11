"""Tests for pyautoroute.groundplane (auto-add ground plane after routing)."""

from __future__ import annotations

import math

from shapely.geometry import box

from pyautoroute import groundplane, pcb, sexpr
from pyautoroute.rules import default_rules


def test_make_zone_node_thermal_bridge_width():
    """thermal_bridge_width is never less than min_thickness."""
    board = pcb.Board(
        tree=sexpr.SList(),
        copper_layers=["F.Cu", "B.Cu"],
        pads=[], free_vias=[], segments=[], zones=[], outline=[],
    )
    pts = [(10, 10), (90, 10), (90, 70), (10, 70)]
    # clearance (0.2) < min_thickness (0.25): bridge must be clamped to 0.25
    zone = pcb.make_zone_node(board, "B.Cu", "GND", pts, clearance=0.2, min_thickness=0.25)
    zone_str = str(zone)
    # Verify thermal_bridge_width is 0.25, not 0.2
    assert "thermal_bridge_width" in zone_str
    idx = zone_str.find("thermal_bridge_width")
    snippet = zone_str[idx:idx+40]
    assert "0.2 " not in snippet and "0.2)" not in snippet, \
        f"thermal_bridge_width should not be 0.2: {snippet}"


def test_make_zone_node_structure():
    """make_zone_node builds a properly-structured zone SList."""
    # Create minimal board for testing
    board = pcb.Board(
        tree=sexpr.SList(),
        copper_layers=["F.Cu", "B.Cu"],
        pads=[],
        free_vias=[],
        segments=[],
        zones=[],
        outline=[],
    )

    pts = [(10, 10), (90, 10), (90, 70), (10, 70)]
    zone = pcb.make_zone_node(board, "B.Cu", "GND", pts, clearance=0.5, min_thickness=0.25)

    # Check it's an SList with "zone" as first element
    assert isinstance(zone, sexpr.SList)
    assert "zone" in str(zone[0])

    # Check it has the expected child nodes
    zone_str = str(zone)
    assert "net" in zone_str
    assert "GND" in zone_str
    assert "B.Cu" in zone_str
    assert "polygon" in zone_str
    assert "fill" in zone_str  # The fill directive should be present


def test_make_zone_node_multiple_layers():
    """make_zone_node can be called with different layers."""
    board = pcb.Board(
        tree=sexpr.SList(),
        copper_layers=["F.Cu", "B.Cu"],
        pads=[],
        free_vias=[],
        segments=[],
        zones=[],
        outline=[],
    )

    pts = [(10, 10), (90, 10), (90, 70), (10, 70)]
    zone_f = pcb.make_zone_node(board, "F.Cu", "GND", pts)
    zone_b = pcb.make_zone_node(board, "B.Cu", "GND", pts)

    # Both should have zone structure with different layers
    assert "F.Cu" in str(zone_f)
    assert "B.Cu" in str(zone_b)
    assert "zone" in str(zone_f)
    assert "zone" in str(zone_b)


def test_make_zone_node_with_numbered_net():
    """make_zone_node handles numbered-net boards."""
    board = pcb.Board(
        tree=sexpr.SList(),
        copper_layers=["F.Cu", "B.Cu"],
        pads=[],
        free_vias=[],
        segments=[],
        zones=[],
        outline=[],
    )
    # Simulate a numbered-net board (KiCad 6-9)
    board.name_only_nets = False
    board.numbered_nets = {3: "GND"}

    pts = [(10, 10), (90, 10), (90, 70), (10, 70)]
    zone = pcb.make_zone_node(board, "B.Cu", "GND", pts)

    # For a numbered board, should use net code instead of name
    zone_str = str(zone)
    assert "zone" in zone_str
    assert "net" in zone_str


# --- stitching vias -----------------------------------------------------------

def _via_xy(via_node):
    at = groundplane._child(via_node, "at")
    return groundplane._float(at, 1), groundplane._float(at, 2)


def test_stitching_vias_avoid_routed_track_geometry():
    """A stitching via must not land on a freshly-routed track for a different
    net, even far from any pad — a pad-centre-only check would miss this."""
    r = default_rules()
    gnd_pad = pcb.Pad(net="GND", pad_type="smd", shape="rect", cx=1, cy=1,
                      w=1, h=1, angle=0.0, copper_layers=["F.Cu", "B.Cu"])
    board = pcb.Board(tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"],
                      pads=[gnd_pad], free_vias=[], segments=[], zones=[],
                      outline=[])
    pour_poly = box(0, 0, 20, 20)
    clearance = r.clearance_for("GND")

    # A SIG track running the full height of the pour at x=10 — the pitch below
    # is chosen so a stitching-via grid column lands exactly on it.
    seg_node = pcb.make_segment(board, 10.0, 0.0, 10.0, 20.0, 0.25, "F.Cu", "SIG")

    vias = groundplane._add_stitching_vias(
        board, r, "GND", "B.Cu", pour_poly, pitch=4.0, clearance=clearance,
        routed_nodes=[seg_node])

    assert vias   # sanity: still placed vias in the rest of the grid
    assert all(abs(_via_xy(v)[0] - 10.0) > 0.3 for v in vias)


def test_stitching_vias_respect_hole_to_hole_spacing():
    """Stitching vias must not violate min_hole_to_hole spacing against a via
    already placed on the board (e.g. a connectivity via)."""
    r = default_rules()
    board = pcb.Board(tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"],
                      pads=[], free_vias=[], segments=[], zones=[], outline=[])
    pour_poly = box(0, 0, 10, 10)
    clearance = r.clearance_for("GND")
    existing = [(5.0, 5.0)]

    vias = groundplane._add_stitching_vias(
        board, r, "GND", "B.Cu", pour_poly, pitch=1.0, clearance=clearance,
        existing_via_points=existing)

    min_gap = r.min_hole_to_hole + r.via_drill_for("GND")
    for v in vias:
        x, y = _via_xy(v)
        dist = ((x - 5.0) ** 2 + (y - 5.0) ** 2) ** 0.5
        assert dist >= min_gap - 1e-6


# --- connectivity-via segment-midpoint fallback --------------------------------

def test_connectivity_via_segment_midpoint_fallback_engages():
    """A GND component that only reaches the pour polygon through a segment's
    *midpoint* (both endpoints lie outside the pour inset) must still get a
    via there — the midpoint tier used to be dead code because midpoints were
    never registered in the union-find, so `_find` on a midpoint always minted
    a fresh singleton that could never equal the component root."""
    r = default_rules()
    board = pcb.Board(tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"],
                      pads=[], free_vias=[], segments=[], zones=[], outline=[])
    # No GND pads at all: a long F.Cu-only GND trace whose endpoints sit
    # outside the (inset) pour polygon but whose midpoint sits inside it.
    board.segments = [pcb.Segment(-5.0, 10.0, 25.0, 10.0, 0.25, "F.Cu", "GND")]
    pour_poly = box(0, 0, 20, 20)
    clearance = r.clearance_for("GND")

    vias = groundplane._add_connectivity_vias(
        board, r, "GND", "B.Cu", pour_poly, clearance)

    assert len(vias) == 1
    x, y = _via_xy(vias[0])
    assert math.isclose(x, 10.0, abs_tol=1e-6)
    assert math.isclose(y, 10.0, abs_tol=1e-6)
