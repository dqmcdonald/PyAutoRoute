"""Tests for pyautoroute.anneal (simulated-annealing optimisation)."""

from __future__ import annotations

from pyautoroute import anneal, netlist, rules, router, sexpr
from pyautoroute.grid import Grid
from pyautoroute.pcb import Board, OutlineShape, Pad


def _pad(net, cx, cy, w=1.0, h=1.0):
    return Pad(net=net, pad_type="smd", shape="rect", cx=cx, cy=cy, w=w, h=h,
              angle=0.0, copper_layers=["F.Cu", "B.Cu"])


def _board(pads, size=30):
    outline = [OutlineShape("poly", {"pts": [(0, 0), (size, 0), (size, size), (0, size)]})]
    return Board(tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"], pads=pads,
                 free_vias=[], segments=[], zones=[], outline=outline)


def _setup():
    pads = []
    for k, y in enumerate((6, 12, 18, 24)):
        pads += [_pad(f"N{k}", 5, y), _pad(f"N{k}", 25, y)]
    board = _board(pads)
    conns = netlist.build_connections(board)
    state = router.RoutingState(Grid(board, rules.default_rules(), pitch=0.5))
    params = router.RouteParams(max_expansions=200_000)
    result = router.route_all(state, conns, netlist.greedy_order(conns), params)
    return state, conns, result


def test_anneal_never_worsens_best_energy():
    state, conns, result = _setup()
    ap = anneal.AnnealParams(iters=25, seed=1,
                             route_params=router.RouteParams(max_expansions=200_000))
    out = anneal.anneal(state, conns, list(result.results), ap)
    assert out.best_energy <= out.start_energy + 1e-6
    assert out.routed >= result.routed
    assert out.iterations == 25


def test_anneal_keeps_routing_clean():
    # after annealing, no two different-net committed connections share a node
    state, conns, result = _setup()
    ap = anneal.AnnealParams(iters=20, seed=2)
    anneal.anneal(state, conns, list(result.results), ap)
    for node, idxs in state.cover.items():
        nets = {state.conn_net[i] for i in idxs}
        assert len(nets) == 1, f"node {node} shared by nets {nets}"
