"""Tests for pyautoroute.netlist (MST rats-nest decomposition)."""

from __future__ import annotations

from pyautoroute import netlist, sexpr
from pyautoroute.pcb import Board, Pad, Segment, Via


def _pad(net, cx, cy, layers=None):
    return Pad(net=net, pad_type="smd", shape="rect", cx=cx, cy=cy, w=1, h=1,
              angle=0.0, copper_layers=layers or ["F.Cu"])


def _tht_pad(net, cx, cy):
    return Pad(net=net, pad_type="thru_hole", shape="circle", cx=cx, cy=cy, w=1, h=1,
              angle=0.0, copper_layers=["F.Cu", "B.Cu"])


def _board(pads, segments=None, free_vias=None):
    return Board(tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"], pads=pads,
                 free_vias=free_vias or [], segments=segments or [],
                 zones=[], outline=[])


def test_two_pad_net_one_connection():
    conns = netlist.build_connections(_board([_pad("A", 0, 0), _pad("A", 5, 0)]))
    assert len(conns) == 1
    assert conns[0].net == "A"


def test_n_pad_net_yields_n_minus_1_connections():
    pads = [_pad("A", 0, 0), _pad("A", 5, 0), _pad("A", 10, 0), _pad("A", 15, 0)]
    conns = netlist.build_connections(_board(pads))
    assert len(conns) == 3


def test_mst_picks_nearest_neighbours():
    # collinear pads: MST should chain adjacent ones, not the far pair
    pads = [_pad("A", 0, 0), _pad("A", 1, 0), _pad("A", 2, 0)]
    conns = netlist.build_connections(_board(pads))
    lengths = sorted(round(c.est_length, 3) for c in conns)
    assert lengths == [1.0, 1.0]      # never the length-2 chord


def test_single_pad_net_skipped():
    conns = netlist.build_connections(_board([_pad("A", 0, 0)]))
    assert conns == []


def test_exclude_pattern_drops_net():
    pads = [_pad("GND", 0, 0), _pad("GND", 5, 0), _pad("DATA", 1, 1), _pad("DATA", 6, 1)]
    conns = netlist.build_connections(_board(pads), exclude=["GND"])
    assert {c.net for c in conns} == {"DATA"}


def test_greedy_order_shortest_first():
    pads = [_pad("A", 0, 0), _pad("A", 10, 0), _pad("B", 0, 0), _pad("B", 2, 0)]
    conns = netlist.build_connections(_board(pads))
    order = netlist.greedy_order(conns)
    assert conns[order[0]].est_length <= conns[order[-1]].est_length


# ── pre_routed_connections ─────────────────────────────────────────────────────

def test_pre_routed_no_existing_copper():
    """No existing segments → all connections are unrouted."""
    pads = [_pad("A", 0, 0), _pad("A", 5, 0)]
    board = _board(pads)
    conns = netlist.build_connections(board)
    pre, unrouted = netlist.pre_routed_connections(board, conns)
    assert pre == []
    assert len(unrouted) == 1


def test_pre_routed_segment_joins_pads():
    """A segment connecting two pads marks their connection as pre-routed."""
    pads = [_pad("A", 0, 0), _pad("A", 5, 0)]
    seg = Segment(x1=0, y1=0, x2=5, y2=0, width=0.2, layer="F.Cu", net="A")
    board = _board(pads, segments=[seg])
    conns = netlist.build_connections(board)
    pre, unrouted = netlist.pre_routed_connections(board, conns)
    assert len(pre) == 1
    assert unrouted == []


def test_pre_routed_via_bridges_layers():
    """A via at the right position bridges F.Cu → B.Cu, satisfying the connection."""
    pad_f = _pad("A", 0, 0, layers=["F.Cu"])
    pad_b = _pad("A", 0, 0, layers=["B.Cu"])  # same position, different layer
    via = Via(cx=0, cy=0, size=0.6, drill=0.3, layers=("F.Cu", "B.Cu"), net="A")
    board = _board([pad_f, pad_b], free_vias=[via])
    conns = netlist.build_connections(board)
    pre, unrouted = netlist.pre_routed_connections(board, conns)
    assert len(pre) == 1
    assert unrouted == []


def test_pre_routed_partial_three_pads():
    """One segment satisfies one of two MST connections; the other remains."""
    pads = [_pad("A", 0, 0), _pad("A", 5, 0), _pad("A", 10, 0)]
    # Segment only joining pads at x=0 and x=5
    seg = Segment(x1=0, y1=0, x2=5, y2=0, width=0.2, layer="F.Cu", net="A")
    board = _board(pads, segments=[seg])
    conns = netlist.build_connections(board)
    pre, unrouted = netlist.pre_routed_connections(board, conns)
    assert len(pre) + len(unrouted) == len(conns)
    assert len(pre) >= 1
    assert len(unrouted) >= 1


def test_pre_routed_tht_pad_bridges_layers():
    """A THT pad (F.Cu + B.Cu) connects its position across both layers."""
    tht = _tht_pad("A", 5, 0)
    smd = _pad("A", 0, 0, layers=["B.Cu"])
    seg = Segment(x1=0, y1=0, x2=5, y2=0, width=0.2, layer="B.Cu", net="A")
    board = _board([tht, smd], segments=[seg])
    conns = netlist.build_connections(board)
    pre, unrouted = netlist.pre_routed_connections(board, conns)
    assert len(pre) == 1
    assert unrouted == []
