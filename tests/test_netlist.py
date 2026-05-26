"""Tests for pyautoroute.netlist (MST rats-nest decomposition)."""

from __future__ import annotations

from pyautoroute import netlist, sexpr
from pyautoroute.pcb import Board, Pad


def _pad(net, cx, cy):
    return Pad(net=net, pad_type="smd", shape="rect", cx=cx, cy=cy, w=1, h=1,
              angle=0.0, copper_layers=["F.Cu"])


def _board(pads):
    return Board(tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"], pads=pads,
                 free_vias=[], segments=[], zones=[], outline=[])


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
