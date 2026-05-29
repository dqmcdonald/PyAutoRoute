"""Synthetic board generator for the performance harness.

Builds duck-typed `pyautoroute.pcb` objects (`Board`, `Footprint`, `Pad`) good
enough to drive `placement._Placer` and the routing annealer, without parsing a
real ``.kicad_pcb``. Footprints are laid out on a loose grid and assigned to a
fixed number of nets so the ratsnest / overlap / energy machinery has something
to chew on.
"""

from __future__ import annotations

import random

from pyautoroute import netlist, rules, router, sexpr
from pyautoroute.grid import Grid
from pyautoroute.pcb import Board, Footprint, OutlineShape, Pad


def _pad(net: str, w: float = 1.5, h: float = 1.5) -> Pad:
    return Pad(net=net, pad_type="smd", shape="rect", cx=0.0, cy=0.0, w=w, h=h,
               angle=0.0, copper_layers=["F.Cu", "B.Cu"])


def _fp(ref: str, x: float, y: float, pads_local) -> Footprint:
    """Build a Footprint at ``(x, y)`` from ``pads_local`` = ``[(px, py, net)]``."""
    pads, offsets = [], []
    for (px, py, net) in pads_local:
        pads.append(_pad(net))
        offsets.append((px, py, 0.0))
    fp = Footprint(ref=ref, x=x, y=y, angle=0.0, locked=False, overlap_ok=False,
                   pads=pads, local_offsets=offsets,
                   at_node=sexpr.SList(), fp_node=sexpr.SList(),
                   x0=x, y0=y, angle0=0.0)
    fp.sync_pads()
    return fp


def make_synthetic_board(n_footprints: int, n_nets: int, seed: int = 42) -> Board:
    """Build a synthetic `Board` with ``n_footprints`` 2-pad footprints.

    Footprints are arranged on a near-square grid with a comfortable pitch, and
    each pad is assigned to one of ``n_nets`` nets at random, so the resulting
    ratsnest spans the whole board. The board is large enough to contain the
    layout with margin.

    Args:
        n_footprints: number of (movable) footprints to create.
        n_nets: number of distinct nets pads are assigned to.
        seed: RNG seed for reproducible pad/net assignment and jitter.

    Returns:
        A `Board` whose ``footprints``/``pads`` are populated; suitable for
        `placement._Placer` and for building a netlist.
    """
    rng = random.Random(seed)
    cols = max(1, int(n_footprints ** 0.5 + 0.999))
    pitch = 8.0
    footprints = []
    for i in range(n_footprints):
        r, c = divmod(i, cols)
        x = 10.0 + c * pitch + rng.uniform(-0.5, 0.5)
        y = 10.0 + r * pitch + rng.uniform(-0.5, 0.5)
        nets = [f"N{rng.randrange(n_nets)}", f"N{rng.randrange(n_nets)}"]
        footprints.append(_fp(f"U{i}", x, y,
                              [(-1.5, 0.0, nets[0]), (1.5, 0.0, nets[1])]))
    pads = [p for fp in footprints for p in fp.pads]
    size = 20.0 + cols * pitch
    outline = [OutlineShape("poly",
                            {"pts": [(0, 0), (size, 0), (size, size), (0, size)]})]
    return Board(tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"], pads=pads,
                 free_vias=[], segments=[], zones=[], outline=outline,
                 footprints=footprints)


def make_routing_setup(n_footprints: int, n_nets: int, seed: int = 42,
                       pitch: float = 1.0):
    """Build a synthetic board and greedily route it, ready for `anneal.anneal`.

    Args:
        n_footprints: number of footprints on the synthetic board.
        n_nets: number of distinct nets.
        seed: RNG seed for the board.
        pitch: routing grid pitch (mm); larger grids faster to build/route.

    Returns:
        ``(state, conns, results)`` — the live `RoutingState`, the connection
        list, and the per-connection results from the initial greedy route, as
        `anneal.anneal` expects.
    """
    board = make_synthetic_board(n_footprints, n_nets, seed)
    conns = netlist.build_connections(board)
    state = router.RoutingState(Grid(board, rules.default_rules(), pitch=pitch))
    params = router.RouteParams(max_expansions=200_000)
    result = router.route_all(state, conns, netlist.greedy_order(conns), params)
    return state, conns, list(result.results)
