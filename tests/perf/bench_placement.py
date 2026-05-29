"""Placement performance benchmarks (plain `time.perf_counter`, no deps).

Times two hot operations of the placement annealer across board sizes:

- **energy**: one full `_Placer._rebuild_cache` + `_cached_energy` (the cost paid
  on init / accept), and
- **sa_step**: one proposed move + incremental delta + revert (the cost paid on
  every simulated-annealing iteration).

Run standalone to print the scaling curve::

    python tests/perf/bench_placement.py

The budgets asserted are deliberately loose — they pass on current hardware and
document a baseline so a future regression (e.g. the incremental energy reverting
to a full O(P^2) recompute per step) trips them.
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

from pyautoroute import placement                                  # noqa: E402
from board_factory import make_synthetic_board                     # noqa: E402

SIZES = [10, 25, 50, 100]

# Per-call wall-clock budgets (seconds), generous vs. current hardware.
ENERGY_BUDGET = 0.05      # one full cache rebuild + energy read
STEP_BUDGET = 0.005       # one incremental SA step (move + delta + revert)


def _placer(n_footprints: int) -> placement._Placer:
    board = make_synthetic_board(n_footprints, n_nets=max(2, n_footprints // 3))
    pl = placement._Placer(board, placement.PlaceParams(seed=0))
    for fp in pl.boxed:
        fp.sync_pads()
    pl._rebuild_cache()
    return pl


def bench_energy(n_footprints: int, repeats: int = 200) -> float:
    """Average seconds for one full energy (cache rebuild) at this size."""
    pl = _placer(n_footprints)
    t0 = time.perf_counter()
    for _ in range(repeats):
        pl._rebuild_cache()
        pl._cached_energy()
    return (time.perf_counter() - t0) / repeats


def bench_sa_step(n_footprints: int, repeats: int = 2000) -> float:
    """Average seconds for one incremental SA step (move + delta + revert)."""
    pl = _placer(n_footprints)
    t0 = time.perf_counter()
    for _ in range(repeats):
        snap, idxs = pl._move(0.5)
        save = (pl._rats, pl._overlap, pl._bbox,
                {i: (pl._boxes[i], pl._bounds[i]) for i in idxs},
                {ci: pl._conn_len[ci] for i in idxs
                 for ci in pl._fp_conns.get(i, ())})
        pl._move_delta(idxs)
        pl._cached_energy()
        # revert (mirror _Placer.run's reject path)
        pl._restore(snap)
        pl._rats, pl._overlap, pl._bbox, boxes, lens = save
        for i, (b, bnd) in boxes.items():
            pl._boxes[i] = b
            pl._bounds[i] = bnd
        for ci, ln in lens.items():
            pl._conn_len[ci] = ln
    return (time.perf_counter() - t0) / repeats


def run() -> None:
    print(f"{'N':>5} {'energy ms':>12} {'sa_step us':>12}")
    for n in SIZES:
        e = bench_energy(n)
        s = bench_sa_step(n)
        print(f"{n:>5} {e * 1e3:>12.3f} {s * 1e6:>12.2f}")
        assert e < ENERGY_BUDGET, f"energy {e:.4f}s over budget at N={n}"
        assert s < STEP_BUDGET, f"sa_step {s:.4f}s over budget at N={n}"
    print("OK: all sizes within budget")


# pytest entry points -------------------------------------------------------

def test_bench_placement_within_budget():
    run()


if __name__ == "__main__":
    run()
