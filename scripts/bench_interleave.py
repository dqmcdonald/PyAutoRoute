"""Benchmark interleaved polish (--place-polish-interleave) against plain SA.

For each test board and seed, runs the placement annealer twice from the same
scattered start under the same wall-clock budget — once plain (anneal then
final polish) and once with a descent sweep interleaved every K iterations —
and compares the final best energies. Equal time budgets make the comparison
fair: the interleaved run pays for its sweeps with fewer Metropolis iterations.

Usage:
    python scripts/bench_interleave.py [--budget S] [--seeds N] [--interleave K]
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyautoroute import placement
from pyautoroute.pcb import load_board
from pyautoroute.placement import PlaceParams, scatter_footprints

ROOT = Path(__file__).resolve().parent.parent
BOARDS = [
    ROOT / "TestProjects" / "Test1" / "Test1.kicad_pcb",
    ROOT / "TestProjects" / "Test2" / "Test2.kicad_pcb",
    ROOT / "TestProjects" / "Test3" / "Test3.kicad_pcb",
    ROOT / "TestProjects" / "Test4" / "Test4.kicad_pcb",
    ROOT / "TestProjects" / "Test5" / "Test5.kicad_pcb",
]


def run_once(path: Path, seed: int, budget: float, interleave: int,
             start_frac: float = 0.0):
    """One placement run from a scattered start; returns (best_E, iters, sweeps)."""
    board = load_board(path)
    scatter_footprints(board, seed)
    params = PlaceParams(time_budget=budget, seed=seed, polish=True,
                         polish_interleave=interleave,
                         polish_interleave_start=start_frac)
    result = placement.place(board, params)
    return result.best_energy, result.iterations, result.interleave_sweeps


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=8.0,
                    help="wall-clock budget (s) per placement run")
    ap.add_argument("--seeds", type=int, default=5, help="seeds per board")
    ap.add_argument("--interleave", type=int, default=100,
                    help="descent-sweep period K for the interleaved arm")
    ap.add_argument("--start-frac", type=float, default=0.0,
                    help="schedule fraction before interleaved sweeps begin")
    args = ap.parse_args()

    print(f"budget {args.budget}s/run, {args.seeds} seeds, K={args.interleave}, "
          f"start={args.start_frac}")
    print(f"{'board':<8} {'seed':>4} {'base E':>10} {'intlv E':>10} "
          f"{'delta%':>7} {'base it':>8} {'intlv it':>8} {'sweeps':>6}")

    for path in BOARDS:
        name = path.stem
        base_es, intlv_es = [], []
        for seed in range(args.seeds):
            t0 = time.time()
            be, bi, _ = run_once(path, seed, args.budget, 0)
            ie, ii, sw = run_once(path, seed, args.budget, args.interleave,
                                  args.start_frac)
            base_es.append(be)
            intlv_es.append(ie)
            delta = 100.0 * (ie - be) / be if be else 0.0
            print(f"{name:<8} {seed:>4} {be:>10.1f} {ie:>10.1f} {delta:>+6.1f}% "
                  f"{bi:>8} {ii:>8} {sw:>6}   ({time.time()-t0:.0f}s)",
                  flush=True)
        mb, mi = statistics.mean(base_es), statistics.mean(intlv_es)
        wins = sum(1 for b, i in zip(base_es, intlv_es) if i < b)
        print(f"{name:<8} mean {mb:>10.1f} {mi:>10.1f} "
              f"{100.0 * (mi - mb) / mb:>+6.1f}%   "
              f"interleave wins {wins}/{len(base_es)}\n", flush=True)


if __name__ == "__main__":
    main()
