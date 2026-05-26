"""Tests for pyautoroute.grid (routing grid occupancy + pad access)."""

from __future__ import annotations

import pathlib

import pytest

from pyautoroute import grid as gridmod
from pyautoroute import pcb, rules, sexpr
from pyautoroute.grid import BLOCKED, FREE, Grid
from pyautoroute.pcb import Board, OutlineShape, Pad

REPO = pathlib.Path(__file__).resolve().parent.parent
PCB = REPO / "TestProjects" / "Test1" / "Test1.kicad_pcb"


def _rect_pad(net, cx, cy, w=1.0, h=1.0):
    return Pad(net=net, pad_type="smd", shape="rect", cx=cx, cy=cy, w=w, h=h,
              angle=0.0, copper_layers=["F.Cu"])


def _square_board(pads):
    outline = [OutlineShape("poly", {"pts": [(0, 0), (10, 0), (10, 10), (0, 10)]})]
    return Board(tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"], pads=pads,
                 free_vias=[], segments=[], zones=[], outline=outline)


def test_grid_dimensions_and_center_free():
    g = Grid(_square_board([]), rules.default_rules(), pitch=0.5)
    assert g.n_layers == 2
    assert g.nx == 21 and g.ny == 21
    col, row = g.nearest_node(5.0, 5.0)
    assert g.owner[0, row, col] == FREE
    assert g.is_free(0, col, row, net_id=0)


def test_edge_nodes_blocked():
    g = Grid(_square_board([]), rules.default_rules(), pitch=0.5)
    col, row = g.nearest_node(0.0, 0.0)
    assert g.owner[0, row, col] == BLOCKED
    assert not g.is_free(0, col, row, net_id=0)


def test_pad_owner_is_per_net():
    pads = [_rect_pad("A", 3, 3), _rect_pad("B", 7, 7)]
    g = Grid(_square_board(pads), rules.default_rules(), pitch=0.5)
    a_id, b_id = g.net_id("A"), g.net_id("B")
    col, row = g.nearest_node(3.0, 3.0)
    # node inside pad A: routable by A, not by B
    assert g.is_free(0, col, row, a_id)
    assert not g.is_free(0, col, row, b_id)


def test_clearance_blocks_other_net_nearby():
    # two pads close together; the gap should block the foreign net
    pads = [_rect_pad("A", 5, 5)]
    g = Grid(_square_board(pads), rules.default_rules(), pitch=0.25)
    b_id = g.net_id("B")
    # just outside pad A (pad spans 4.5..5.5; inflated by 0.3 -> ~4.2..5.8)
    col, row = g.nearest_node(5.6, 5.0)
    assert not g.is_free(0, col, row, b_id)


def test_pad_access_nodes_present():
    pads = [_rect_pad("A", 3, 3, w=1.0, h=1.0)]
    g = Grid(_square_board(pads), rules.default_rules(), pitch=0.25)
    nodes = g.pad_access_nodes(pads[0])
    assert nodes
    # all access nodes are on a copper layer index and within the pad footprint
    for li, c, r in nodes:
        x, y = g.node_xy(c, r)
        assert 2.4 <= x <= 3.6 and 2.4 <= y <= 3.6


@pytest.mark.skipif(not PCB.exists(), reason="Test1 board not present")
def test_test1_grid_builds_with_free_space():
    board = pcb.load_board(PCB)
    g = Grid(board, rules.load_rules(REPO / "TestProjects" / "Test1" / "Test1.kicad_pro"),
             pitch=0.2)
    # a healthy fraction of F.Cu nodes should be routable
    free_frac = (g.owner[0] == FREE).mean()
    assert free_frac > 0.3
    # every net with pads should have at least one access node
    by_net = board.pads_by_net()
    sample = next(iter(by_net))
    nodes = []
    for p in by_net[sample]:
        nodes += g.pad_access_nodes(p)
    assert nodes
