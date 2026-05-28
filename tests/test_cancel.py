"""Tests for cooperative cancellation of anneal / placement (GUI Stop button)."""

from __future__ import annotations

import threading

from pyautoroute import anneal, netlist, placement, rules, router, sexpr
from pyautoroute.grid import Grid
from pyautoroute.pcb import Board, Footprint, OutlineShape, Pad


def _pad(net, cx, cy, w=1.0, h=1.0, layers=("F.Cu", "B.Cu")):
    return Pad(net=net, pad_type="smd", shape="rect", cx=cx, cy=cy, w=w, h=h,
              angle=0.0, copper_layers=list(layers))


def _routing_setup():
    pads = []
    for k, y in enumerate((6, 12, 18, 24)):
        pads += [_pad(f"N{k}", 5, y), _pad(f"N{k}", 25, y)]
    outline = [OutlineShape("poly", {"pts": [(0, 0), (30, 0), (30, 30), (0, 30)]})]
    board = Board(tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"], pads=pads,
                  free_vias=[], segments=[], zones=[], outline=outline)
    conns = netlist.build_connections(board)
    state = router.RoutingState(Grid(board, rules.default_rules(), pitch=0.5))
    result = router.route_all(state, conns, netlist.greedy_order(conns),
                              router.RouteParams())
    return state, conns, result


def test_anneal_cancel_stops_immediately():
    state, conns, result = _routing_setup()
    cancel = threading.Event()
    cancel.set()                          # already cancelled before the loop runs
    ap = anneal.AnnealParams(iters=1_000_000, seed=1)   # huge budget
    out = anneal.anneal(state, conns, list(result.results), ap, cancel=cancel)
    assert out.iterations == 0            # stopped before any iteration
    assert out.best_energy == out.start_energy


def _fp(ref, x, y, pads_local):
    pads, offsets = [], []
    for (px, py, net) in pads_local:
        pads.append(_pad(net, 0.0, 0.0, w=2.0, h=2.0))
        offsets.append((px, py, 0.0))
    fp = Footprint(ref=ref, x=x, y=y, angle=0.0, locked=False, overlap_ok=False,
                   pads=pads, local_offsets=offsets,
                   at_node=sexpr.SList(), fp_node=sexpr.SList(),
                   x0=x, y0=y, angle0=0.0)
    fp.sync_pads()
    return fp


def _place_board():
    a = _fp("U1", 20.0, 20.0, [(-2.0, 0.0, "A"), (2.0, 0.0, "B")])
    b = _fp("U2", 40.0, 40.0, [(-2.0, 0.0, "A"), (2.0, 0.0, "B")])
    outline = [OutlineShape("poly", {"pts": [(0, 0), (80, 0), (80, 80), (0, 80)]})]
    return Board(tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"],
                 pads=[p for fp in (a, b) for p in fp.pads], free_vias=[],
                 segments=[], zones=[], outline=outline, footprints=[a, b])


def test_placement_cancel_stops_immediately():
    cancel = threading.Event()
    cancel.set()
    res = placement.place(_place_board(),
                          placement.PlaceParams(iters=1_000_000, seed=1),
                          cancel=cancel)
    assert res.iterations == 0
    assert res.best_energy == res.start_energy


def test_placement_runs_cancel_returns_valid_result():
    # cancelling the best-of-N path before any run finishes still returns a result
    cancel = threading.Event()
    cancel.set()
    res = placement.place(_place_board(),
                          placement.PlaceParams(iters=1_000_000, seed=1),
                          runs=3, cancel=cancel)
    assert isinstance(res, placement.PlaceResult)
    assert res.iterations == 0
