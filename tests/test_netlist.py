"""Tests for pyautoroute.netlist (MST rats-nest decomposition)."""

from __future__ import annotations

from pyautoroute import netlist, sexpr
from pyautoroute.pcb import Board, Footprint, Pad, Segment, Via


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


# ── resolve_decoupling_ic ──────────────────────────────────────────────────────

def _fp(ref, x, y, pad_nets):
    """A footprint at (x, y) with pads at the footprint origin on `pad_nets`."""
    pads = [_pad(net, x, y) for net in pad_nets]
    return Footprint(ref=ref, x=x, y=y, angle=0.0, locked=False, overlap_ok=False,
                     pads=pads, local_offsets=[(0.0, 0.0, 0.0)] * len(pads),
                     at_node=sexpr.SList(), fp_node=sexpr.SList())


def _fp_board(footprints):
    pads = [p for fp in footprints for p in fp.pads]
    return Board(tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"], pads=pads,
                 free_vias=[], segments=[], zones=[], outline=[],
                 footprints=footprints)


def _ic(ref, x, y, nets):
    """An IC-like footprint (>=4 pads) at (x, y); first nets get distinct pads."""
    pads = [_pad(n, x, y) for n in nets] + [_pad("", x, y)] * max(0, 4 - len(nets))
    return Footprint(ref=ref, x=x, y=y, angle=0.0, locked=False, overlap_ok=False,
                     pads=pads, local_offsets=[(0.0, 0.0, 0.0)] * len(pads),
                     at_node=sexpr.SList(), fp_node=sexpr.SList())


def test_resolve_picks_nearest_ic_on_power_net():
    cap = _fp("C1", 10.0, 10.0, ["VCC", "GND"])
    near = _ic("U1", 12.0, 10.0, ["VCC", "GND"])
    far = _ic("U2", 60.0, 60.0, ["VCC", "GND"])
    board = _fp_board([cap, near, far])
    ref, candidates, warning = netlist.resolve_decoupling_ic(board, cap)
    assert ref == "U1"
    assert candidates == ["U1", "U2"]          # nearest first
    assert warning is None


def test_resolve_warns_when_not_two_nets():
    cap = _fp("C1", 0.0, 0.0, ["VCC"])          # single pad-net
    ic = _ic("U1", 1.0, 0.0, ["VCC", "GND"])
    board = _fp_board([cap, ic])
    ref, candidates, warning = netlist.resolve_decoupling_ic(board, cap)
    assert ref is None and candidates == []
    assert warning and "bridge two" in warning


def test_resolve_warns_when_no_power_ground_bridge():
    cap = _fp("C1", 0.0, 0.0, ["SIG_A", "SIG_B"])   # both signal nets
    ic = _ic("U1", 1.0, 0.0, ["SIG_A", "GND"])
    board = _fp_board([cap, ic])
    ref, candidates, warning = netlist.resolve_decoupling_ic(board, cap)
    assert ref is None
    assert warning and "power and ground" in warning


def test_resolve_warns_when_no_ic_on_power_net():
    cap = _fp("C1", 0.0, 0.0, ["VCC", "GND"])
    # only a 2-pad resistor shares VCC — not IC-like, and it is the cap's only
    # company; with no other footprint on VCC the pool falls back to it.
    other_cap = _fp("C2", 50.0, 50.0, ["SIG", "GND"])   # not on VCC
    board = _fp_board([cap, other_cap])
    ref, candidates, warning = netlist.resolve_decoupling_ic(board, cap)
    assert ref is None and candidates == []
    assert warning and "no IC found" in warning


def test_resolve_ambiguous_near_tie_warns_but_chooses():
    cap = _fp("C1", 10.0, 10.0, ["VCC", "GND"])
    a = _ic("U1", 12.0, 10.0, ["VCC", "GND"])
    b = _ic("U2", 8.1, 10.0, ["VCC", "GND"])    # ~ equally close
    board = _fp_board([cap, a, b])
    ref, candidates, warning = netlist.resolve_decoupling_ic(board, cap)
    assert ref in ("U1", "U2")
    assert set(candidates) == {"U1", "U2"}
    assert warning and "could serve" in warning


def test_resolve_power_fallback_on_unrecognised_rail_name():
    # power net not matched by name, but the other net is clearly GND → the
    # non-ground net is taken as the rail (with a note).
    cap = _fp("C1", 0.0, 0.0, ["PWR3", "GND"])
    ic = _ic("U1", 1.0, 0.0, ["PWR3", "GND"])
    board = _fp_board([cap, ic])
    ref, candidates, warning = netlist.resolve_decoupling_ic(board, cap)
    assert ref == "U1"
    assert warning and "not recognised" in warning
