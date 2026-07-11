"""Benchmark: `_IndexPool.choice()` vs `random.choice(tuple(some_set))` (O2).

`_Annealer._propose` draws a random routed/unrouted connection index on every
SA iteration. The old code did `rng.choice(tuple(self._routed))`, which
materialises the *entire* pool into a tuple just to draw one element — O(pool
size) per draw. This isolates that specific cost (routing itself dominates a
full `anneal()` run, which would dilute the comparison) across pool sizes.

Run as::

    python scripts/bench_o2_index_pool.py
"""

from __future__ import annotations

import os
import random
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pyautoroute.anneal import _IndexPool                          # noqa: E402

DRAWS = 200_000


def bench_set(n: int) -> float:
    rng = random.Random(0)
    pool = set(range(n))
    t0 = time.perf_counter()
    for _ in range(DRAWS):
        rng.choice(tuple(pool))
    return time.perf_counter() - t0


def bench_index_pool(n: int) -> float:
    rng = random.Random(0)
    pool = _IndexPool(range(n))
    t0 = time.perf_counter()
    for _ in range(DRAWS):
        pool.choice(rng)
    return time.perf_counter() - t0


def main() -> None:
    print(f"{'pool size':>10}  {'set+tuple (s)':>14}  {'_IndexPool (s)':>15}  {'speedup':>8}")
    for n in (50, 200, 1000, 5000, 20000, 50000):
        t_set = bench_set(n)
        t_pool = bench_index_pool(n)
        print(f"{n:>10}  {t_set:>14.3f}  {t_pool:>15.3f}  {t_set / t_pool:>7.1f}x")


if __name__ == "__main__":
    main()
