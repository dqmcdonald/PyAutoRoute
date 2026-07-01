"""Tests for pyautoroute.groundplane (auto-add ground plane after routing)."""

from __future__ import annotations

from shapely.geometry import box

from pyautoroute import groundplane, pcb, rules as rules_mod, sexpr
from pyautoroute.pcb import Board, OutlineShape, Pad, Segment


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
