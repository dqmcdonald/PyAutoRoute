"""Parameter sweep + scoring to find good routing settings.

Simulated annealing is stochastic and several knobs (grid pitch, via weight,
schedule) trade completion against wirelength, vias, and runtime. This module
scores a routing with a single objective and sweeps the critical parameters over
one or more boards to find the best setting — the engine behind the
``pyautoroute-tune`` command and the ``--auto`` probe.

The objective (lower is better) is

    score = unrouted_weight*unrouted + length + via_weight*vias + time_weight*runtime

i.e. the annealer's own energy (completion, then wirelength, then vias) plus a
small runtime tiebreaker, so among settings of equal quality the fastest wins.
Because the router is DRC-clean by construction, correctness is not part of the
score — only completion / length / vias / time.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass

from . import anneal, netlist, pcb, router
from .autoroute import default_pitch
from .grid import Grid
from .rules import load_rules


@dataclass
class TuneMetrics:
    routed: int
    unrouted: int
    length: float
    vias: int
    runtime: float


def score(m: TuneMetrics, unrouted_weight: float = 100.0,
          via_weight: float = 2.0, time_weight: float = 0.0) -> float:
    """Scalar quality of a routing (lower is better).

    Args:
        m: the routing metrics.
        unrouted_weight: penalty per unrouted connection (dominant term).
        via_weight: cost per via (mm-equivalent).
        time_weight: cost per second of runtime (a small tiebreaker; 0 ignores it).

    Returns:
        ``unrouted_weight*unrouted + length + via_weight*vias + time_weight*runtime``.
    """
    return (unrouted_weight * m.unrouted + m.length + via_weight * m.vias
            + time_weight * m.runtime)


@dataclass
class Config:
    """One point in the routing-parameter search space."""
    grid_mult: float = 1.0            # pitch as a multiple of the rules-derived pitch
    via_weight: float = 2.0
    unrouted_weight: float = 100.0
    temps: tuple[float, float] = (4.0, 0.05)
    iters: int | None = None
    time_budget: float | None = None
    search_margin: float | None = None   # A* search box margin (mm); None = unbounded


def evaluate(board, rules, cfg: Config, seed: int, grid: Grid | None = None,
             exclude: list[str] | None = None) -> TuneMetrics:
    """Route a board under one config + seed and measure it.

    Args:
        board: the parsed board.
        rules: its design rules.
        cfg: the parameter config to evaluate.
        seed: the annealing seed.
        grid: a prebuilt grid for ``cfg.grid_mult`` to reuse (built if `None`).
        exclude: net name/glob patterns to leave unrouted (e.g. ``["GND"]``).

    Returns:
        The `TuneMetrics` for the run.
    """
    pitch = default_pitch(rules) * cfg.grid_mult
    if grid is None:
        grid = Grid(board, rules, pitch)
    conns = netlist.build_connections(board, exclude=exclude or [])
    params = router.RouteParams(via_cost=cfg.via_weight,
                                search_margin=cfg.search_margin)
    state = router.RoutingState(grid)
    t0 = time.time()
    res = router.route_all(state, conns, netlist.greedy_order(conns), params)
    results = res.results
    if cfg.iters or cfg.time_budget:
        ap = anneal.AnnealParams(iters=cfg.iters, time_budget=cfg.time_budget,
                                 seed=seed, unrouted_weight=cfg.unrouted_weight,
                                 t_start=cfg.temps[0], t_end=cfg.temps[1],
                                 route_params=params)
        results = anneal.anneal(state, conns, list(res.results), ap).results
    runtime = time.time() - t0
    routed = sum(1 for r in results if r is not None)
    length = sum(r.length for r in results if r is not None)
    vias = sum(r.vias for r in results if r is not None)
    return TuneMetrics(routed, len(results) - routed, length, vias, runtime)


@dataclass
class ConfigScore:
    config: Config
    median_score: float
    metrics: list[TuneMetrics]


def sweep(board, rules, configs: list[Config], seeds=(0, 1, 2),
          unrouted_weight: float = 100.0, via_weight: float = 2.0,
          time_weight: float = 0.0,
          exclude: list[str] | None = None,
          progress=None) -> list[ConfigScore]:
    """Evaluate every config over several seeds on a board, scored by median.

    Args:
        board: the parsed board (reused across configs; grids cached per pitch).
        rules: its design rules.
        configs: the parameter configs to try.
        seeds: annealing seeds per config (the median score is used so a lucky
            seed doesn't win).
        unrouted_weight: scoring weight per unrouted connection.
        via_weight: scoring weight per via.
        time_weight: scoring weight per second.
        exclude: net name/glob patterns to leave unrouted (e.g. ``["GND"]``).
        progress: optional ``progress(done, total, cfg, cs)`` callback invoked
            after each config is evaluated; ``cs`` is the `ConfigScore` for
            that config (unsorted), or ``None`` on the very first call with
            ``done=0``.

    Returns:
        One `ConfigScore` per config, sorted best (lowest median score) first.
    """
    grids: dict[float, Grid] = {}
    out: list[ConfigScore] = []
    total = len(configs)
    if progress:
        progress(0, total, None, None)
    for i, cfg in enumerate(configs):
        if cfg.grid_mult not in grids:
            grids[cfg.grid_mult] = Grid(board, rules,
                                        default_pitch(rules) * cfg.grid_mult)
        ms = [evaluate(board, rules, cfg, s, grid=grids[cfg.grid_mult],
                       exclude=exclude) for s in seeds]
        scores = [score(m, unrouted_weight, via_weight, time_weight) for m in ms]
        cs = ConfigScore(cfg, statistics.median(scores), ms)
        out.append(cs)
        if progress:
            progress(i + 1, total, cfg, cs)
    out.sort(key=lambda cs: cs.median_score)
    return out


def sweep_board(board_path, pro_path, configs: list[Config], seeds=(0, 1, 2),
                unrouted_weight: float = 100.0, via_weight: float = 2.0,
                time_weight: float = 0.0) -> list[ConfigScore]:
    """Load a board + rules from disk and `sweep` them.

    Args:
        board_path: the ``.kicad_pcb`` to route.
        pro_path: its ``.kicad_pro`` (design rules).
        configs: the parameter configs to try.
        seeds: annealing seeds per config.
        unrouted_weight: scoring weight per unrouted connection.
        via_weight: scoring weight per via.
        time_weight: scoring weight per second.

    Returns:
        One `ConfigScore` per config, sorted best first.
    """
    return sweep(pcb.load_board(board_path), load_rules(pro_path), configs, seeds,
                 unrouted_weight, via_weight, time_weight)


def best_config(scored: list[ConfigScore]) -> Config:
    """Return the lowest-median-score config from a sweep.

    Args:
        scored: the `ConfigScore` list (any order).

    Returns:
        The best `Config`.
    """
    return min(scored, key=lambda cs: cs.median_score).config


def probe_search_margin(board, rules, cfg: Config, seed: int,
                        tolerance: float = 0.02, progress=None,
                        exclude: list[str] | None = None) -> float | None:
    """Find the smallest search_margin that matches unbounded routing quality.

    Runs the router once with ``search_margin=None`` (unbounded) to establish a
    baseline score, then tries progressively tighter margins — derived from the
    board diagonal — returning the smallest that stays within *tolerance* of the
    baseline. A smaller margin speeds up routing without sacrificing quality.

    Args:
        board: the parsed board.
        rules: its design rules.
        cfg: the best config from the main sweep (grid_mult and via_weight used;
            its own search_margin is ignored).
        seed: the routing seed.
        tolerance: accept a margin if its score is within this fraction above the
            unbounded baseline (default 0.02 = 2%).

    Returns:
        The recommended search_margin in mm, or ``None`` if unbounded is best
        (e.g. the board is too small for a margin to matter, or every tested
        margin degrades quality).
    """
    import math

    pitch = default_pitch(rules) * cfg.grid_mult
    grid = Grid(board, rules, pitch)

    # Derive candidate margins from the board diagonal.
    minx, miny, maxx, maxy = grid.outline.bounds
    diagonal = math.hypot(maxx - minx, maxy - miny)

    # Fractions of diagonal: from loose (50%) down to tight (5%).
    # Also always include a few absolute minimums in case the board is tiny.
    fractions = [0.5, 0.25, 0.15, 0.1, 0.05]
    candidates = sorted({max(2.0, round(diagonal * f, 1)) for f in fractions},
                        reverse=True)  # largest first

    # Baseline: unbounded search
    base_cfg = Config(grid_mult=cfg.grid_mult, via_weight=cfg.via_weight,
                      iters=cfg.iters, time_budget=cfg.time_budget,
                      search_margin=None)

    total = 1 + len(candidates)   # baseline + one per candidate
    done = 0
    if progress:
        progress(done, total, None, None)
    base_metrics = evaluate(board, rules, base_cfg, seed, grid=grid, exclude=exclude)
    done += 1
    base_score = score(base_metrics)
    threshold = base_score * (1 + tolerance)
    if progress:
        progress(done, total, None, base_metrics)

    best_margin = None   # None = unbounded is the fallback
    for m in candidates:
        test_cfg = Config(grid_mult=cfg.grid_mult, via_weight=cfg.via_weight,
                          iters=cfg.iters, time_budget=cfg.time_budget,
                          search_margin=m)
        m_metrics = evaluate(board, rules, test_cfg, seed, grid=grid, exclude=exclude)
        m_score = score(m_metrics)
        done += 1
        if progress:
            progress(done, total, test_cfg, m_metrics)
        if m_score <= threshold:
            best_margin = m   # this margin is acceptable; try something tighter

    return best_margin


# Default search space for the CLI / --auto probe: a few grid pitches and via
# weights around the rules-derived defaults. Kept small so a sweep is tractable.
def default_grid(iters: int | None = None,
                 time_budget: float | None = None) -> list[Config]:
    """Build the default coarse search grid.

    Args:
        iters: annealing iterations per config (or `None`).
        time_budget: annealing seconds per config (or `None`).

    Returns:
        Configs over grid multipliers {0.75, 1.0, 1.5} x via weights {1, 2, 4}.
    """
    return [Config(grid_mult=g, via_weight=v, iters=iters, time_budget=time_budget)
            for g in (0.75, 1.0, 1.5) for v in (1.0, 2.0, 4.0)]


def _format_report(board_path, scored: list[ConfigScore]) -> str:
    """Render a plain-text table of the top configs for one board.

    Args:
        board_path: the board the sweep ran on.
        scored: the sorted `ConfigScore` list.

    Returns:
        A formatted string suitable for terminal output.
    """
    top = scored[:6]
    best = best_config(scored)

    # Build rows: (grid, via_w, completion, length, vias, score, is_best)
    rows = []
    for cs in top:
        m = cs.metrics[0]
        total = m.routed + m.unrouted
        rows.append((
            f"×{cs.config.grid_mult}",
            f"{cs.config.via_weight}",
            f"{m.routed}/{total}",
            f"{m.length:.0f}",
            f"{m.vias}",
            f"{cs.median_score:.0f}",
            cs.config is best or (cs.config.grid_mult == best.grid_mult
                                  and cs.config.via_weight == best.via_weight),
        ))

    headers = ("grid", "via_w", "completion", "len (mm)", "vias", "score")
    col_ws = [max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

    def _hline():
        return "─┼─".join("─" * w for w in col_ws)

    def _row(cells, marker="  "):
        return " │ ".join(c.rjust(w) for c, w in zip(cells, col_ws)) + marker

    lines = [str(board_path)]
    lines.append(" │ ".join(h.rjust(w) for h, w in zip(headers, col_ws)))
    lines.append(_hline())
    for r in rows:
        marker = " ★" if r[-1] else "  "
        lines.append(_row(r[:-1], marker))
    lines.append(_hline())

    lines.append(f"  best:  grid×{best.grid_mult}  via-weight={best.via_weight}")
    return "\n".join(lines)


def main(argv=None) -> int:
    """CLI: sweep the given boards and print a recommended-settings report.

    Args:
        argv: argument list; `None` uses ``sys.argv``.

    Returns:
        Process exit code (0).
    """
    import argparse
    from pathlib import Path

    p = argparse.ArgumentParser(
        prog="pyautoroute-tune",
        description="Sweep routing parameters over boards to find good settings.")
    p.add_argument("boards", nargs="+", help="input .kicad_pcb files")
    p.add_argument("--time", type=float, default=5.0, metavar="S",
                   help="annealing seconds per config (default %(default)s)")
    p.add_argument("--seeds", type=int, default=3, metavar="N",
                   help="seeds per config; the median score is used (default %(default)s)")
    p.add_argument("--exclude-net", action="append", default=[], metavar="PATTERN",
                   help="net name/glob to leave unrouted during tuning (repeatable); "
                        "use for pour nets like GND that pyautoroute will exclude anyway")
    p.add_argument("--save-ini", nargs="?", const="", default=None, metavar="FILE",
                   help="write optimal settings to an INI file and exit "
                        "(bare: <first_board>.ini — the file auto-loaded by pyautoroute)")
    args = p.parse_args(argv)

    configs = default_grid(time_budget=args.time)
    total_evals = len(configs) * args.seeds

    def _progress(done, total, cfg, cs):
        if done == 0:
            print(f"  probing {total} configs × {args.seeds} seed(s) "
                  f"= {total_evals} eval(s) …", flush=True)
            return
        bar_w = 20
        filled = int(bar_w * done / total)
        bar = "█" * filled + "░" * (bar_w - filled)
        suffix = ""
        if cfg is not None and cs is not None:
            m = cs.metrics[0]
            suffix = (f"  grid×{cfg.grid_mult} via={cfg.via_weight}"
                      f"  {m.routed}/{m.routed+m.unrouted} routed"
                      f"  score={cs.median_score:.0f}")
        print(f"\r  [{bar}] {done}/{total}{suffix}",
              end="" if done < total else "\n", flush=True)

    all_scored: list[tuple[Path, list[ConfigScore]]] = []
    for b in args.boards:
        bp = Path(b)
        if len(args.boards) > 1:
            print(f"\n{bp.name}")
        board = pcb.load_board(bp)
        rules = load_rules(bp.with_suffix(".kicad_pro"))
        scored = sweep(board, rules, configs, seeds=tuple(range(args.seeds)),
                       exclude=args.exclude_net or None, progress=_progress)
        report = _format_report(bp, scored)
        print(report + "\n")
        all_scored.append((bp, scored))

    if args.save_ini is not None:
        # Use the first board as the reference for grid pitch and margin probe.
        first_bp, first_scored = all_scored[0]
        first_board = pcb.load_board(first_bp)
        first_rules = load_rules(first_bp.with_suffix(".kicad_pro"))
        best = best_config(first_scored)
        chosen_pitch = round(default_pitch(first_rules) * best.grid_mult, 4)

        print("  probing search margin for best config …", flush=True)
        suggested_margin = probe_search_margin(first_board, first_rules, best, seed=0,
                                               exclude=args.exclude_net or None)

        ini_path = Path(args.save_ini) if args.save_ini else first_bp.with_suffix(".ini")

        from .autoroute import build_parser, write_config
        par = build_parser()
        # Parse a minimal namespace using the first board as input, then
        # override only the settings the sweep determined.
        ini_args = par.parse_args([str(first_bp)])
        ini_args.grid = chosen_pitch
        ini_args.via_weight = best.via_weight
        if suggested_margin is not None:
            ini_args.search_margin = suggested_margin
        if args.exclude_net:
            ini_args.exclude_net = args.exclude_net

        write_config(par, ini_args, ini_path)
        margin_str = f"{suggested_margin} mm" if suggested_margin is not None else "unbounded"
        print(f"  wrote {ini_path}")
        print(f"    grid={chosen_pitch} mm  via-weight={best.via_weight}"
              f"  search-margin={margin_str}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
