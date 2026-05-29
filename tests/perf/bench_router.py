"""Routing-annealer performance benchmark (plain `time.perf_counter`, no deps).

Reports two columns across board sizes: the average wall-clock per annealing
iteration (`anneal.anneal`, dominated by A* rip-up/reroute on top of the
incremental-energy + KD-tree cluster machinery of Optimisation A) and the
average per-`router.astar` call doing a greedy route (Optimisation B's optimised
A* core). Stall detection is left disabled so the full iteration budget is timed
(a fair, schedule-only baseline).

Run standalone to print the scaling curve::

    python tests/perf/bench_router.py

Budgets are loose: they pass on current hardware and document a baseline.
"""

from __future__ import annotations

import os
import sys
import time

# Make both the repo root (for `pyautoroute`) and this dir (for `board_factory`)
# importable, so the bench runs both under pytest and as a standalone script.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))

from pyautoroute import anneal, netlist, router                   # noqa: E402
from board_factory import make_routing_setup, make_synthetic_board  # noqa: E402
from pyautoroute import rules                                     # noqa: E402
from pyautoroute.grid import Grid                                 # noqa: E402

# Routing grids are far more expensive to build/route than placement boards, so
# the router bench uses smaller sizes and a coarse pitch.
SIZES = [10, 25, 50]
ITERS = 20

# Per-iteration wall-clock budget (seconds), generous vs. current hardware.
STEP_BUDGET = 2.0

# Per-A*-call wall-clock budget (seconds). A single greedy route of one
# connection on these coarse synthetic grids is sub-millisecond on current
# hardware; the budget is loose to document a baseline, not gate fine timing.
ASTAR_BUDGET = 0.5


def bench_anneal_step(n_footprints: int, iters: int = ITERS) -> float:
    """Average seconds per annealing iteration at this board size.

    The routing SA step is dominated by A* rip-up/reroute, but each iteration
    also runs the incremental-energy update and the centroid KD-tree cluster
    query (Optimisation A); this times the whole step end to end.
    """
    state, conns, results = make_routing_setup(
        n_footprints, n_nets=max(2, n_footprints // 3), pitch=1.0)
    ap = anneal.AnnealParams(
        iters=iters, seed=1,
        route_params=router.RouteParams(max_expansions=100_000))
    t0 = time.perf_counter()
    out = anneal.anneal(state, conns, results, ap)
    dt = time.perf_counter() - t0
    return dt / max(1, out.iterations)


def bench_astar(n_footprints: int) -> float:
    """Average seconds per `router.astar` call doing a greedy route of a board.

    Times the optimised A* core directly (Optimisation B: integer state keys,
    per-net free mask, precomputed heuristic field, hoisted via neighbourhood)
    on a fresh grid, so the number reflects the inner search loop rather than
    the annealer's bookkeeping.
    """
    board = make_synthetic_board(n_footprints,
                                 n_nets=max(2, n_footprints // 3))
    conns = netlist.build_connections(board)
    grid = Grid(board, rules.default_rules(), pitch=1.0)
    state = router.RoutingState(grid)
    params = router.RouteParams(max_expansions=100_000)
    calls = 0
    t0 = time.perf_counter()
    for idx in netlist.greedy_order(conns):
        c = conns[idx]
        res = router.route_connection(
            state, c.net, grid.pad_access_nodes(c.a),
            grid.pad_access_nodes(c.b), params,
            src_xy=(c.a.cx, c.a.cy), dst_xy=(c.b.cx, c.b.cy))
        calls += 1
        if res is not None:
            state.commit(idx, res)
    dt = time.perf_counter() - t0
    return dt / max(1, calls)


def run() -> None:
    has_c = getattr(router, "_USE_C_ASTAR", False)
    if has_c:
        # Time both the native and pure-Python A* by toggling the dispatch flag,
        # so the speedup of the optional Cython core is visible side by side.
        print(f"{'N':>5} {'step ms':>12} {'astar(C) ms':>12} "
              f"{'astar(py) ms':>13} {'speedup':>9}")
        for n in SIZES:
            s = bench_anneal_step(n)
            router._USE_C_ASTAR = True
            a_c = bench_astar(n)
            router._USE_C_ASTAR = False
            a_py = bench_astar(n)
            router._USE_C_ASTAR = True
            spd = a_py / a_c if a_c else float("inf")
            print(f"{n:>5} {s * 1e3:>12.2f} {a_c * 1e3:>12.3f} "
                  f"{a_py * 1e3:>13.3f} {spd:>8.1f}x")
            assert s < STEP_BUDGET, f"anneal step {s:.4f}s over budget at N={n}"
            assert a_c < ASTAR_BUDGET, f"astar call {a_c:.4f}s over budget at N={n}"
    else:
        print(f"{'N':>5} {'step ms':>12} {'astar ms':>12}")
        for n in SIZES:
            s = bench_anneal_step(n)
            a = bench_astar(n)
            print(f"{n:>5} {s * 1e3:>12.2f} {a * 1e3:>12.2f}")
            assert s < STEP_BUDGET, f"anneal step {s:.4f}s over budget at N={n}"
            assert a < ASTAR_BUDGET, f"astar call {a:.4f}s over budget at N={n}"
    print("OK: all sizes within budget")


# pytest entry points -------------------------------------------------------

def test_bench_router_within_budget():
    run()


if __name__ == "__main__":
    run()
