"""Tests for pyautoroute.groundplane (auto-add ground plane after routing)."""

from __future__ import annotations

import math

from shapely.geometry import box

from pyautoroute import groundplane, pcb, rules as rules_mod, sexpr
from pyautoroute.pcb import Board, OutlineShape, Pad, Segment
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


def _gnd_pad(cx, cy, ref="C1"):
    """An SMD-only GND pad (needs a via to reach the B.Cu pour)."""
    return Pad(net="GND", pad_type="smd", shape="roundrect", cx=cx, cy=cy,
               w=1.2, h=1.4, angle=0, copper_layers=["F.Cu"], fp_ref=ref)


def _gnd_pad_on_pour_layer(cx, cy, ref="C1", layer="B.Cu"):
    """An SMD GND pad already on the pour layer (no via needed — in theory)."""
    return Pad(net="GND", pad_type="smd", shape="roundrect", cx=cx, cy=cy,
               w=1.2, h=1.4, angle=0, copper_layers=[layer], fp_ref=ref)


def _ring_segments(cx, cy, half=4.0, net="SIG", layer="B.Cu", width=0.3):
    """Four B.Cu segments forming a closed square moat around (cx, cy)."""
    x0, x1 = cx - half, cx + half
    y0, y1 = cy - half, cy + half
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [
        Segment(x1=corners[i][0], y1=corners[i][1],
                x2=corners[(i + 1) % 4][0], y2=corners[(i + 1) % 4][1],
                width=width, layer=layer, net=net)
        for i in range(4)
    ]


def test_connectivity_via_rejects_isolated_pocket():
    """A GND pad moated off by other-net copper must not get a via stranded
    in the pocket — it should be left unconnected (with a warning) rather
    than anchored to copper that KiCad's fill can never join to the main
    plane. Reproduces the isolated-ground-island failure mode: the local
    clearance check around a candidate via can pass even though the whole
    pocket it sits in is cut off from the rest of the pour."""
    pad = _gnd_pad(10, 20)
    board = Board(
        tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"],
        pads=[pad], free_vias=[], segments=_ring_segments(10, 20),
        zones=[], outline=[OutlineShape("rect", {"start": (0, 0), "end": (60, 40)})],
    )
    pour_poly = box(0, 0, 60, 40)
    rules = rules_mod.default_rules()
    clearance = rules.clearance_for("GND")

    vias, warnings = groundplane._add_connectivity_vias(
        board, rules, "GND", "B.Cu", pour_poly, clearance,
    )

    assert vias == [], "via must not be placed inside the isolated pocket"
    assert len(warnings) == 1
    assert "C1" in warnings[0]


def test_connectivity_via_reaches_open_plane():
    """Without a moat, an SMD GND pad still gets tied to the pour as before."""
    pad = _gnd_pad(10, 20)
    board = Board(
        tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"],
        pads=[pad], free_vias=[], segments=[],
        zones=[], outline=[OutlineShape("rect", {"start": (0, 0), "end": (60, 40)})],
    )
    pour_poly = box(0, 0, 60, 40)
    rules = rules_mod.default_rules()
    clearance = rules.clearance_for("GND")

    vias, warnings = groundplane._add_connectivity_vias(
        board, rules, "GND", "B.Cu", pour_poly, clearance,
    )

    assert warnings == []
    assert len(vias) >= 1
    assert any(_node_head(v) == "via" for v in vias)


def _node_head(node) -> str:
    return node[0].raw if node and hasattr(node[0], "raw") else ""


def test_connectivity_via_numbered_net_routed_gnd_not_treated_as_obstacle():
    """On numbered-net boards (KiCad 6-9), a freshly-routed GND segment is
    written as ``(net 3)``, not ``(net "GND")``. _obstacles_from_nodes must
    resolve that back to the GND net before comparing, or it misclassifies
    the routed GND copper as an other-net obstacle — which, fed into the
    main-plane connectivity check, can fragment the whole pour around the
    router's own GND traces and falsely reject a reachable via position."""
    pad = _gnd_pad(10, 20)
    board = Board(
        tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"],
        pads=[pad], free_vias=[], segments=[],
        zones=[], outline=[OutlineShape("rect", {"start": (0, 0), "end": (60, 40)})],
    )
    board.name_only_nets = False
    board.numbered_nets = {3: "GND", 7: "SIG"}

    # A ring of GND copper (not SIG) the router just placed, expressed as
    # routed_nodes SList — the same input build() passes from a routing run.
    ring = _ring_segments(10, 20, net="GND")
    routed_nodes = [
        pcb.make_segment(board, s.x1, s.y1, s.x2, s.y2, s.width, s.layer, s.net)
        for s in ring
    ]

    pour_poly = box(0, 0, 60, 40)
    rules = rules_mod.default_rules()
    clearance = rules.clearance_for("GND")

    vias, warnings = groundplane._add_connectivity_vias(
        board, rules, "GND", "B.Cu", pour_poly, clearance,
        routed_nodes=routed_nodes,
    )

    assert warnings == [], "routed GND copper must not be misread as an other-net obstacle"
    assert len(vias) >= 1
    assert any(_node_head(v) == "via" for v in vias)


def test_pad_already_on_pour_layer_in_isolated_pocket_warns():
    """A GND pad whose copper is already on the pour layer is normally
    skipped (it doesn't need a connectivity via) — but if same-layer
    other-net traces moat it off from the rest of the plane, that pad
    anchors an isolated fill island exactly like a stranded via would.
    KiCad's island_removal_mode 0 only deletes fill with no zone-net
    connection, so the pad's own copper keeps the pocket alive without it
    ever reaching the rest of GND. Must warn rather than silently doing
    nothing."""
    pad = _gnd_pad_on_pour_layer(10, 20, layer="B.Cu")
    board = Board(
        tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"],
        pads=[pad], free_vias=[], segments=_ring_segments(10, 20, layer="B.Cu"),
        zones=[], outline=[OutlineShape("rect", {"start": (0, 0), "end": (60, 40)})],
    )
    pour_poly = box(0, 0, 60, 40)
    rules = rules_mod.default_rules()
    clearance = rules.clearance_for("GND")

    vias, warnings = groundplane._add_connectivity_vias(
        board, rules, "GND", "B.Cu", pour_poly, clearance,
    )

    assert vias == [], "must not silently leave broken copper unreported"
    assert len(warnings) == 1
    assert "C1" in warnings[0]


def test_pad_on_pour_layer_in_open_plane_needs_no_via():
    """Regression guard: a GND pad already on the pour layer with no moat
    around it must still be left alone (no via, no warning) — the new
    pad-anchored-pocket check must not fire on ordinary boards."""
    pad = _gnd_pad_on_pour_layer(10, 20, layer="B.Cu")
    board = Board(
        tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"],
        pads=[pad], free_vias=[], segments=[],
        zones=[], outline=[OutlineShape("rect", {"start": (0, 0), "end": (60, 40)})],
    )
    pour_poly = box(0, 0, 60, 40)
    rules = rules_mod.default_rules()
    clearance = rules.clearance_for("GND")

    vias, warnings = groundplane._add_connectivity_vias(
        board, rules, "GND", "B.Cu", pour_poly, clearance,
    )

    assert vias == []
    assert warnings == []


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

    vias, warnings = groundplane._add_connectivity_vias(
        board, r, "GND", "B.Cu", pour_poly, clearance)

    assert warnings == []
    assert len(vias) == 1
    x, y = _via_xy(vias[0])
    assert math.isclose(x, 10.0, abs_tol=1e-6)
    assert math.isclose(y, 10.0, abs_tol=1e-6)
