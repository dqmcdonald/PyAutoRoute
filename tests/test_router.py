"""Tests for pyautoroute.router (A* maze routing + path conversion)."""

from __future__ import annotations

import math

from pyautoroute import rules, sexpr
from pyautoroute.grid import Grid
from pyautoroute.pcb import Board, OutlineShape, Pad
from pyautoroute.router import RoutingState, path_to_nodes, route_connection


def _pad(net, cx, cy, w=1.0, h=1.0, layers=("F.Cu",)):
    return Pad(net=net, pad_type="smd", shape="rect", cx=cx, cy=cy, w=w, h=h,
              angle=0.0, copper_layers=list(layers))


def _board(pads, size=20):
    outline = [OutlineShape("poly", {"pts": [(0, 0), (size, 0), (size, size), (0, size)]})]
    return Board(tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"], pads=pads,
                 free_vias=[], segments=[], zones=[], outline=outline)


def _state(board):
    return RoutingState(Grid(board, rules.default_rules(), pitch=0.25))


def _access(state, pad):
    return state.grid.pad_access_nodes(pad)


def test_straight_route_same_layer():
    a, b = _pad("A", 4, 10), _pad("A", 16, 10)
    s = _state(_board([a, b]))
    res = route_connection(s, "A", _access(s, a), _access(s, b))
    assert res is not None
    assert res.vias == 0
    assert res.length < 14.0          # ~12 mm straight run
    assert all(li == 0 for (li, _, _) in res.path)


def test_no_path_when_fully_walled():
    a, b = _pad("A", 4, 10), _pad("A", 16, 10)
    # wall of foreign net spanning full height on BOTH layers
    wall_f = _pad("X", 10, 10, w=0.8, h=20, layers=("F.Cu", "B.Cu"))
    s = _state(_board([a, b, wall_f]))
    res = route_connection(s, "A", _access(s, a), _access(s, b))
    assert res is None


def test_via_used_to_cross_foreign_wall():
    a, b = _pad("A", 4, 10), _pad("A", 16, 10)
    # wall blocks F.Cu only -> router must dive to B.Cu and back (2 vias)
    wall = _pad("X", 10, 10, w=0.8, h=20, layers=("F.Cu",))
    s = _state(_board([a, b, wall]))
    res = route_connection(s, "A", _access(s, a), _access(s, b))
    assert res is not None
    assert res.vias == 2
    assert any(li == 1 for (li, _, _) in res.path)   # used the back layer


def test_diagonal_preferred_over_manhattan():
    a, b = _pad("A", 3, 3), _pad("A", 15, 15)
    s = _state(_board([a, b]))
    res = route_connection(s, "A", _access(s, a), _access(s, b))
    assert res is not None
    euclid = math.hypot(12, 12)        # ~16.97
    manhattan = 24.0
    # a 45-degree run is far closer to the straight-line distance
    assert res.length < (euclid + manhattan) / 2


def test_path_to_nodes_emits_segments_and_via():
    a, b = _pad("A", 4, 10), _pad("A", 16, 10)
    wall = _pad("X", 10, 10, w=0.8, h=20, layers=("F.Cu",))
    board = _board([a, b, wall])
    s = _state(board)
    res = route_connection(s, "A", _access(s, a), _access(s, b))
    nodes = path_to_nodes(board, s.grid, res)
    heads = [sexpr.head_symbol(n) for n in nodes]
    assert heads.count("via") == 2
    assert "segment" in heads


def test_commit_blocks_other_nets_and_ripup_restores():
    a, b = _pad("A", 4, 10), _pad("A", 16, 10)
    s = _state(_board([a, b]))
    res = route_connection(s, "A", _access(s, a), _access(s, b))
    s.commit(0, res)
    col, row = s.grid.nearest_node(10.0, 10.0)
    b_id, a_id = s.grid.net_id("B"), s.grid.net_id("A")
    # a foreign net can no longer occupy a node on the committed centreline
    assert not s.is_free(0, col, row, b_id)
    assert s.is_free(0, col, row, a_id)        # net A owns the track
    # ripping the connection restores the node to free for everyone
    s.ripup(0)
    assert s.is_free(0, col, row, b_id)
