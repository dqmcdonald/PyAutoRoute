"""cProfile the placement annealer on a small synthetic board.

Runs `placement.place` on a synthetic board, writes the profile to
``/tmp/profile_anneal.prof``, and prints the top-20 lines by cumulative time —
the quickest way to confirm where the placement SA spends its time (and that the
incremental energy keeps `build_connections` / `_overlap_area` out of the hot
path).

Run as::

    python scripts/profile_anneal.py
"""

from __future__ import annotations

import cProfile
import os
import pstats
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "tests", "perf")))

from pyautoroute import placement                                  # noqa: E402
from board_factory import make_synthetic_board                     # noqa: E402

PROF_PATH = "/tmp/profile_anneal.prof"
N_FOOTPRINTS = 30
N_NETS = 10
ITERS = 5000


def _workload() -> None:
    board = make_synthetic_board(N_FOOTPRINTS, N_NETS, seed=0)
    placement.place(board, placement.PlaceParams(iters=ITERS, seed=0))


def main() -> None:
    print(f"Profiling placement.place "
          f"(N={N_FOOTPRINTS}, nets={N_NETS}, iters={ITERS}) ...")
    profiler = cProfile.Profile()
    profiler.enable()
    _workload()
    profiler.disable()

    profiler.dump_stats(PROF_PATH)
    print(f"Wrote profile to {PROF_PATH}\n")

    stats = pstats.Stats(profiler)
    stats.sort_stats(pstats.SortKey.CUMULATIVE)
    print("Top 20 by cumulative time:")
    stats.print_stats(20)


if __name__ == "__main__":
    main()
