"""Benchmark: diff-pair coupled A* free-mask precompute vs per-expansion
`RoutingState.is_free` calls (O3).

Routes a diff pair across a moderately cluttered board (fine pitch, several
same-layer obstacle pads) many times and times it. Run this against the
working tree both with and without the O3 change (e.g. restore a backup copy
of diffpair.py/router.py) to compare directly.

Run as::

    python scripts/bench_o3_diffpair_astar.py
"""

from __future__ import annotations

import os
import random
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pyautoroute import rules, sexpr                                # noqa: E402
from pyautoroute.diffpair import route_diff_pair                    # noqa: E402
from pyautoroute.grid import Grid                                   # noqa: E402
from pyautoroute.netlist import DiffPairSpec, build_diff_pair_connections  # noqa: E402
from pyautoroute.pcb import Board, OutlineShape, Pad                # noqa: E402
from pyautoroute.router import RouteParams, RoutingState            # noqa: E402

REPEATS = 30


def _pad(net, cx, cy, fp_ref, w=1.0, h=1.0, layers=("F.Cu",)):
    return Pad(net=net, pad_type="smd", shape="rect", cx=cx, cy=cy, w=w, h=h,
              angle=0.0, copper_layers=list(layers), fp_ref=fp_ref)


def _board(n_obstacles: int, seed: int, size: float = 40.0):
    rng = random.Random(seed)
    sp = _pad("DP+", 4, 20, "U1")
    sn = _pad("DP-", 4, 21, "U1")
    dp = _pad("DP+", size - 4, 20, "U2")
    dn = _pad("DP-", size - 4, 21, "U2")
    pads = [sp, sn, dp, dn]
    for i in range(n_obstacles):
        x = rng.uniform(8, size - 8)
        y = rng.uniform(2, size - 2)
        pads.append(_pad(f"OBS{i}", x, y, f"R{i}", w=1.2, h=1.2,
                         layers=("F.Cu", "B.Cu")))
    outline = [OutlineShape("poly", {"pts": [(0, 0), (size, 0),
                                              (size, size), (0, size)]})]
    return Board(tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"], pads=pads,
                free_vias=[], segments=[], zones=[], outline=outline)


def bench(n_obstacles: int, pitch: float) -> float:
    r = rules.default_rules()
    params = RouteParams()
    t0 = time.perf_counter()
    routed = 0
    for seed in range(REPEATS):
        board = _board(n_obstacles, seed)
        grid = Grid(board, r, pitch=pitch)
        state = RoutingState(grid)
        spec = DiffPairSpec("DP+", "DP-")
        conns = build_diff_pair_connections(board, [spec])
        if not conns:
            continue
        gap = r.dp_gap_for("DP+", "DP-")
        result = route_diff_pair(state, conns[0], gap, params)
        if result is not None:
            routed += 1
    return time.perf_counter() - t0, routed


def main() -> None:
    print(f"{'obstacles':>10}  {'pitch':>6}  {'seconds':>10}  {'ms/route':>10}  {'routed':>7}")
    for n_obs, pitch in ((10, 0.3), (20, 0.2), (30, 0.15)):
        elapsed, routed = bench(n_obs, pitch)
        print(f"{n_obs:>10}  {pitch:>6}  {elapsed:>10.3f}  "
              f"{1000 * elapsed / REPEATS:>10.2f}  {routed:>4}/{REPEATS}")


if __name__ == "__main__":
    main()
