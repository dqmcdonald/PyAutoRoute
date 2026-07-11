"""Benchmark: persistent overlap-query tree vs. rebuild-every-call (O1).

Times `placement.place` on synthetic boards of increasing size. Run this
against the working tree both with and without the O1 change (e.g. `git
stash` / restore a backup copy of placement.py) to compare directly, or just
read the printed per-move-delta cost, which is the O1-sensitive number.

Run as::

    python scripts/bench_o1_overlap_tree.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "tests", "perf")))

from pyautoroute import placement                                  # noqa: E402
from board_factory import make_synthetic_board                     # noqa: E402

ITERS = 4000


def bench(n_footprints: int, n_nets: int) -> float:
    board = make_synthetic_board(n_footprints, n_nets, seed=0)
    t0 = time.perf_counter()
    placement.place(board, placement.PlaceParams(iters=ITERS, seed=0))
    return time.perf_counter() - t0


def main() -> None:
    print(f"{'N footprints':>12}  {'iters':>6}  {'seconds':>10}  {'us/iter':>10}")
    for n in (20, 50, 100, 150, 200, 300, 400, 600, 800, 1200, 1600):
        elapsed = bench(n, max(4, n // 5))
        print(f"{n:>12}  {ITERS:>6}  {elapsed:>10.3f}  {1e6 * elapsed / ITERS:>10.2f}")


if __name__ == "__main__":
    main()
