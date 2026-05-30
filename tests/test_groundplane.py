"""Tests for pyautoroute.groundplane (auto-add ground plane after routing)."""

from __future__ import annotations

from pyautoroute import pcb, sexpr


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
