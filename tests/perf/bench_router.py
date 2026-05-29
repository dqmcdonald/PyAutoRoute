"""Routing-annealer performance benchmark (plain `time.perf_counter`, no deps).

Times a short fixed-iteration `anneal.anneal` run across board sizes — the SA
step here is dominated by A* rip-up/reroute, so the bench reports the average
wall-clock per annealing iteration. Stall detection is left disabled so the full
iteration budget is timed (a fair, schedule-only baseline).

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

from pyautoroute import anneal, router                            # noqa: E402
from board_factory import make_routing_setup                      # noqa: E402

# Routing grids are far more expensive to build/route than placement boards, so
# the router bench uses smaller sizes and a coarse pitch.
SIZES = [10, 25, 50]
ITERS = 20

# Per-iteration wall-clock budget (seconds), generous vs. current hardware.
STEP_BUDGET = 2.0


def bench_anneal_step(n_footprints: int, iters: int = ITERS) -> float:
    """Average seconds per annealing iteration at this board size."""
    state, conns, results = make_routing_setup(
        n_footprints, n_nets=max(2, n_footprints // 3), pitch=1.0)
    ap = anneal.AnnealParams(
        iters=iters, seed=1,
        route_params=router.RouteParams(max_expansions=100_000))
    t0 = time.perf_counter()
    out = anneal.anneal(state, conns, results, ap)
    dt = time.perf_counter() - t0
    return dt / max(1, out.iterations)


def run() -> None:
    print(f"{'N':>5} {'step ms':>12}")
    for n in SIZES:
        s = bench_anneal_step(n)
        print(f"{n:>5} {s * 1e3:>12.2f}")
        assert s < STEP_BUDGET, f"anneal step {s:.4f}s over budget at N={n}"
    print("OK: all sizes within budget")


# pytest entry points -------------------------------------------------------

def test_bench_router_within_budget():
    run()


if __name__ == "__main__":
    run()
