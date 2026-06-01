"""Command-line entry point: parse a board, route it, write a routed copy.

    python -m pyautoroute.autoroute INPUT.kicad_pcb [options]

Orchestrates parse -> grid build -> route -> write, prints a live text progress
display (unless --quiet), reports metrics, and runs an in-repo clearance
self-check on the result.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import configparser
import datetime
import os
import textwrap
import sys
import time
from dataclasses import replace
from pathlib import Path

from . import __version__, anneal, geometry, netlist, pcb, placement, router
from .grid import Grid
from .pipeline import (
    CycleHooks, PipelineHooks, _cycle_worker, run_cycle,
    run_placement, run_routing, select_best,
)
from .rules import load_rules

# Congestion-feedback history weight (--place-feedback): the accumulated field is
# blended `decay·history + (1-decay)·latest` each cycle, so signal builds up over
# cycles without one cycle's routing dominating the next placement.
_FEEDBACK_DECAY = 0.5


class Reporter:
    """Live single-line progress display on a TTY; quiet/plain otherwise.

    If ``log_path`` is given, every phase and a (throttled) trace of routing /
    annealing progress is also appended to that file, regardless of ``quiet``,
    alongside whatever ``log()`` is called with directly (parameter dump, final
    metrics). The log is plain text, one timestamped line per entry.
    """

    def __init__(self, stream=sys.stderr, quiet: bool = False,
                 log_path: Path | None = None):
        self.stream = stream
        self.quiet = quiet
        self.tty = (not quiet) and hasattr(stream, "isatty") and stream.isatty()
        self._t0 = time.time()
        self._c0 = time.process_time()
        self.log_file = open(log_path, "w", encoding="utf-8") if log_path else None
        self.tag = ""

    def phase(self, name: str) -> None:
        """Announce a new phase on the display and in the log.

        Args:
            name: the phase name, e.g. ``"routing 94 connections"``.
        """
        self.log(f"{name} ...")
        if self.quiet:
            return
        self._write(f"[{self._elapsed():6.1f}s] {name} ...")
        if not self.tty:
            self.stream.write("\n")

    def routing(self, done: int, total: int, routed: int, unrouted: int) -> None:
        """Report greedy-routing progress.

        Args:
            done: connections attempted so far.
            total: total connections to route.
            routed: count routed successfully.
            unrouted: count that failed.
        """
        msg = (f"{self.tag}routing {done}/{total}  routed={routed} failed={unrouted}")
        if done == total or done % 10 == 0:
            self.log(msg)
        if self.quiet:
            return
        line = f"[{self._elapsed():6.1f}s] {msg}"
        if self.tty:
            self._write(line)
        elif done == total or done % 10 == 0:
            self.stream.write(line + "\n")

    def annealing(self, it, total, routed, unrouted, energy, best, temp,
                  accept, *, elapsed: float = 0.0, budget: float = 0.0,
                  overall_best: float | None = None) -> None:
        """Report an annealing iteration.

        Args:
            it: iterations completed.
            total: nominal iteration count (for the progress fraction).
            routed: connections currently routed.
            unrouted: connections currently unrouted.
            energy: current energy.
            best: best energy seen so far.
            temp: current annealing temperature.
            accept: fraction of recent moves accepted (0..1); falls as T cools.
            elapsed: seconds elapsed in this run (non-zero enables time display).
            budget: time budget in seconds.
            overall_best: best energy across all runs so far (shown when > 1 run).
        """
        iter_str = (f"{max(0.0, budget - elapsed):.0f}s rem"
                    if budget > 0 else f"{it}/{total}")
        ob_str = (f"  ob={overall_best:7.1f}" if overall_best is not None else "")
        msg = (f"{self.tag}anneal {iter_str}  T={temp:5.2f}  E={energy:7.1f}  "
               f"best={best:7.1f}{ob_str}  acc={accept*100:3.0f}%  "
               f"routed={routed} failed={unrouted}")
        if it % 25 == 0:
            self.log(msg)
        if self.quiet:
            return
        line = f"[{self._elapsed():6.1f}s] {msg}"
        if self.tty:
            self._write(line)
        elif it % 25 == 0:
            self.stream.write(line + "\n")

    def placing(self, it, total, energy, best, temp, accept,
               *, elapsed: float = 0.0, budget: float = 0.0,
               overall_best: float | None = None) -> None:
        """Report a placement-annealing iteration.

        Args:
            it: iterations completed.
            total: nominal iteration count (for the progress fraction).
            energy: current placement energy.
            best: best energy seen so far.
            temp: current annealing temperature.
            accept: fraction of recent moves accepted (0..1); falls as T cools.
            elapsed: seconds elapsed in this run (non-zero enables time display).
            budget: time budget in seconds.
            overall_best: best energy across all runs so far (shown when > 1 run).
        """
        iter_str = (f"{max(0.0, budget - elapsed):.0f}s rem"
                    if budget > 0 else f"{it}/{total}")
        ob_str = (f"  ob={overall_best:8.1f}" if overall_best is not None else "")
        msg = (f"{self.tag}place {iter_str}  T={temp:5.2f}  E={energy:8.1f}  "
               f"best={best:8.1f}{ob_str}  acc={accept*100:3.0f}%")
        if it % 25 == 0:
            self.log(msg)
        if self.quiet:
            return
        line = f"[{self._elapsed():6.1f}s] {msg}"
        if self.tty:
            self._write(line)
        elif it % 25 == 0:
            self.stream.write(line + "\n")

    def log(self, msg: str) -> None:
        """Append a timestamped line to the log file (no-op without one)."""
        if self.log_file is not None:
            self.log_file.write(f"[{self._elapsed():8.2f}s] {msg}\n")
            self.log_file.flush()

    def close(self) -> None:
        """Close the log file, if one is open."""
        if self.log_file is not None:
            self.log_file.close()
            self.log_file = None

    def done(self) -> None:
        """Terminate the live TTY progress line with a newline."""
        if self.tty:
            self.stream.write("\n")
            self.stream.flush()

    def _elapsed(self) -> float:
        """Seconds elapsed since this reporter was created."""
        return time.time() - self._t0

    def runtime(self) -> tuple[float, float]:
        """Wall-clock and process CPU seconds elapsed since this reporter began.

        Returns:
            ``(real, cpu)`` — elapsed wall-clock time and total (user+system)
            CPU time of this process, both in seconds.
        """
        return time.time() - self._t0, time.process_time() - self._c0

    def _write(self, msg: str) -> None:
        """Write a formatted line to the display.

        Args:
            msg: the already-formatted line; emitted in place (``\\r``) on a TTY
                or as its own line otherwise.
        """
        if self.tty:
            self.stream.write("\r\033[K" + msg)
            self.stream.flush()
        else:
            self.stream.write(msg + "\n")


def default_output(input_path: Path, place: bool = False,
                   place_only: bool = False) -> Path:
    """Default output path beside the input, named for what the run produced.

    Args:
        input_path: the input ``.kicad_pcb`` path.
        place: whether the run placed the footprints before routing.
        place_only: whether the run only placed (no routing).

    Returns:
        ``<input>_placed`` (place only), ``<input>_placed_routed`` (place then
        route), or ``<input>_routed`` (route only), with the input's suffix.
    """
    if place_only:
        tag = "_placed"
    elif place:
        tag = "_placed_routed"
    else:
        tag = "_routed"
    return input_path.with_name(input_path.stem + tag + input_path.suffix)


def default_pro(input_path: Path) -> Path:
    """Default project path: the sibling ``.kicad_pro``.

    Args:
        input_path: the input ``.kicad_pcb`` path.
    """
    return input_path.with_suffix(".kicad_pro")


def default_pitch(rules) -> float:
    """Default grid pitch derived from the rules' Default class.

    Args:
        rules: the `pyautoroute.rules.DesignRules`.

    Returns:
        ``track_width/2 + clearance`` (mm), rounded to 4 places.
    """
    dc = rules.default_class
    return round(dc.track_width / 2.0 + dc.clearance, 4)


def default_place_buffer(rules) -> float:
    """Default placement keep-out gap (mm) derived from the design rules.

    The placement pass keeps footprints at least this far apart; a value derived
    from the clearance leaves room for routing between adjacent parts so the
    placed board does not fail DRC.

    Args:
        rules: the `pyautoroute.rules.DesignRules`.

    Returns:
        ``max(2 x max-class-clearance, 0.5)`` mm, rounded to 4 places.
    """
    max_clear = max([c.clearance for c in rules.classes.values()]
                    + [rules.min_clearance])
    return round(max(2.0 * max_clear, 0.5), 4)


# A grid coarser than this multiple of the rules-derived pitch often can't place
# a node in the tight gap beside a pad, forcing vias where a finer grid would
# route on one layer. Empirically a pad-flanking single-layer route survives at
# ~2x the natural pitch but is lost beyond it.
COARSE_GRID_FACTOR = 2.0


def coarse_grid_note(pitch: float, natural: float) -> str | None:
    """Warn when the grid pitch is too coarse relative to the design rules.

    A grid much coarser than the rules-derived pitch (``track/2 + clearance``)
    often cannot fit a node in the clearance gap beside a pad, so the router is
    forced to via under it where a finer grid would route on a single layer.

    Args:
        pitch: the routing grid pitch actually in use (mm).
        natural: the rules-derived pitch (`default_pitch`), in mm.

    Returns:
        A warning string when `pitch` exceeds ``COARSE_GRID_FACTOR x natural``,
        else `None`.
    """
    if natural <= 0 or pitch <= COARSE_GRID_FACTOR * natural:
        return None
    return (f"grid pitch {pitch:g} mm is {pitch / natural:.1f}x the rules-derived "
            f"pitch ({natural:g} mm); a coarse grid can force vias where a finer "
            f"grid would route on one layer — consider --grid {natural:g} "
            f"(or omit --grid)")


def _stamp(board, mode: str) -> None:
    """Add a PyAutoRoute provenance comment to the board's title block."""
    today = datetime.date.today().isoformat()
    pcb.stamp_comment(board, f"PyAutoRoute v{__version__} — {mode} {today}")


def _footprint_constraint_summary(fp) -> str | None:
    """One-line summary of a footprint's placement constraints, or ``None``.

    Args:
        fp: a `pyautoroute.pcb.Footprint`.

    Returns:
        A comma-separated string of the constraints the footprint carries
        (``edge=<side>`` for an `Autoroute-edge` affinity, ``locked``, and/or
        ``overlap`` for `Autoroute-overlap`), or ``None`` when it has none.
    """
    parts = []
    if fp.edge_affinity:
        parts.append(f"edge={fp.edge_affinity}")
    if fp.locked:
        parts.append("locked")
    if fp.overlap_ok:
        parts.append("overlap")
    return ", ".join(parts) if parts else None


def _print_footprint_constraints(board) -> None:
    """Print the footprints that carry placement constraints (silent if none).

    Lists each footprint flagged with an edge affinity, a lock, or an overlap-ok
    flag, with its reference and the constraint(s). Prints nothing when no
    footprint is constrained, so it never adds noise to a plain board.

    Args:
        board: the parsed board.
    """
    items = [(fp.ref, s) for fp in board.footprints
             if (s := _footprint_constraint_summary(fp))]
    if not items:
        return
    items.sort(key=lambda t: t[0])
    width = max(len(ref) for ref, _ in items)
    print("  constraints:")
    for ref, summary in items:
        print(f"    {ref:<{width}}  {summary}")


def _log_footprint_constraints(rep: Reporter, board) -> None:
    """Log the footprints that carry placement constraints (silent if none).

    Args:
        rep: Reporter instance for logging.
        board: the parsed board.
    """
    items = [(fp.ref, s) for fp in board.footprints
             if (s := _footprint_constraint_summary(fp))]
    if not items:
        return
    items.sort(key=lambda t: t[0])
    width = max(len(ref) for ref, _ in items)
    rep.log("constraints:")
    for ref, summary in items:
        rep.log(f"  {ref:<{width}}  {summary}")


def _results_to_nodes(board, grid: Grid, results) -> list:
    """Flatten routed results into the KiCad nodes to append to the board.

    Args:
        board: the board (net-reference style).
        grid: the grid (node -> coordinate conversion).
        results: per-connection `RouteResult` or `None`.

    Returns:
        The concatenated ``(segment ...)`` / ``(via ...)`` nodes for all routed
        connections.
    """
    nodes = []
    for res in results:
        if res is not None:
            nodes += router.path_to_nodes(board, grid, res)
    return nodes


def _resolve_log_path(args, out_path: Path) -> Path | None:
    """Resolve the ``--log`` flag to a path.

    Args:
        args: the parsed CLI namespace (uses ``args.log``).
        out_path: the routed-output path (basis for the default log name).

    Returns:
        `None` if ``--log`` was absent; ``<output>.log`` for a bare ``--log``;
        otherwise the explicit path given.
    """
    if args.log is None:
        return None
    return Path(args.log) if args.log else out_path.with_suffix(".log")


def _log_params(rep: Reporter, args, input_path, out_path, pro_path, pitch,
                board, conns, grid, snap_n: int) -> None:
    """Write the run's input parameters and board stats to the log header.

    Args:
        rep: the reporter owning the log file.
        args: the parsed CLI namespace.
        input_path: the input board path.
        out_path: the routed-output path.
        pro_path: the project (rules) path.
        pitch: the grid pitch (mm).
        board: the parsed board.
        conns: the connection list.
        grid: the routing grid.
        snap_n: the effective snapshot count (0 if disabled).
    """
    rep.log("=" * 64)
    rep.log(f"PyAutoRoute {__version__}  "
            f"{datetime.datetime.now().isoformat(timespec='seconds')}")
    rep.log("=" * 64)
    rep.log(f"input          {input_path}")
    rep.log(f"output         {out_path}")
    rep.log(f"project        {pro_path}")
    rep.log(f"grid pitch     {pitch} mm  (margin {grid.margin:.3f} mm)")
    rep.log(f"grid nodes     {grid.nx} x {grid.ny} x {grid.n_layers} layers")
    if args.place:
        rep.log(f"placement      on  (margin {args.place_margin} mm, "
                f"buffer {args.place_buffer} mm, "
                f"overlap wt {args.place_overlap_weight}, "
                f"compact wt {args.place_compact_weight}, "
                f"edge wt {args.place_edge_weight})")
        if getattr(args, "keep_outline", False):
            rep.log("keep outline   on  (footprints contained within the existing Edge.Cuts)")
        rep.log(f"place temps    {args.place_temps[0]} -> {args.place_temps[1]}")
        rep.log(f"place step     {args.place_step} mm, rotate {args.place_rotate}")
        if args.place_runs > 1:
            rep.log(f"place runs     {args.place_runs}")
        if args.place_iters:
            rep.log(f"place iters    {args.place_iters}")
        if args.place_time:
            rep.log(f"place time     {args.place_time} s")
    rep.log(f"via weight     {args.via_weight}")
    rep.log(f"seed           {args.seed}")
    if args.exclude_net:
        rep.log(f"exclude nets   {', '.join(args.exclude_net)}")
    if args.iters:
        rep.log(f"anneal iters   {args.iters}")
    if args.time_budget:
        rep.log(f"anneal time    {args.time_budget} s")
    if args.iters or args.time_budget:
        rep.log(f"unrouted wt    {args.unrouted_weight}")
        rep.log(f"anneal temps   {args.anneal_temps[0]} -> {args.anneal_temps[1]}")
        if args.runs > 1:
            rep.log(f"runs           {args.runs}")
    if snap_n:
        rep.log(f"snapshots      {snap_n}")
    rep.log(f"copper layers  {', '.join(board.copper_layers)}")
    rep.log(f"pads           {len(board.pads)}")
    rep.log(f"connections    {len(conns)}")
    rep.log("-" * 64)


def _place_params_from_args(args, board, rules, rep):
    """Build `placement.PlaceParams` from the CLI args.

    Resolves the placement-buffer default (from the design rules) and the
    ``--keep-outline`` fallback — a synthesised or absent Edge.Cuts can't be kept,
    so the flag is dropped with a note. Shared by the single-pass placement in
    `run` and the per-cycle placement in `_run_cycles`.

    Args:
        args: the parsed CLI namespace.
        board: the parsed board (for the keep-outline Edge.Cuts check).
        rules: the design rules (for the buffer default).
        rep: the `Reporter` (for the fallback note).

    Returns:
        ``(params, keep_outline)`` — the placement params and the resolved
        keep-outline flag.
    """
    if args.place_buffer is None:
        args.place_buffer = default_place_buffer(rules)
    keep_outline = bool(getattr(args, "keep_outline", False))
    if keep_outline and (not board.outline or board.outline_synthesized):
        keep_outline = False
        msg = ("--keep-outline ignored: the board has no Edge.Cuts to keep; "
               "regenerating a bounding outline instead")
        rep.log(msg)
        if not args.quiet:
            print(f"  note: {msg}")
    pp = placement.PlaceParams(
        iters=args.place_iters, time_budget=args.place_time, seed=args.seed,
        exclude=args.exclude_net, overlap_weight=args.place_overlap_weight,
        compact_weight=args.place_compact_weight, buffer=args.place_buffer,
        edge_weight=args.place_edge_weight, keep_outline=keep_outline,
        t_start=args.place_temps[0], t_end=args.place_temps[1],
        step=args.place_step, rotate_mode=args.place_rotate)
    return pp, keep_outline


def run(args: argparse.Namespace, _print_version: bool = True,
        _startup_log_params: tuple | None = None) -> int:
    """Execute the full pipeline: parse -> grid -> route -> (anneal) -> write.

    Writes the routed board, optional snapshots and log, runs the clearance
    self-check, and prints the metrics report (including the run's wall-clock
    and CPU time, also mirrored to the log).

    Args:
        args: the parsed CLI namespace (see `build_parser`).
        _print_version: if False, suppress the opening ``PyAutoRoute vX.Y``
            line (used by `main` when the settings header already printed it).
        _startup_log_params: optional tuple of (args_cli, parser, pure_defaults,
            proj_ini_path, cfg_path, proj_ini_values, cfg_values) to log the
            startup header. When provided, logs the header to the log file.

    Returns:
        Process exit code: 0 if the self-check is clean, 2 if it finds a
        clearance violation.
    """
    input_path = Path(args.input)
    out_path = (Path(args.output) if args.output
                else default_output(input_path, place=args.place,
                                    place_only=args.place_only))
    pro_path = Path(args.pro) if args.pro else default_pro(input_path)
    rep = Reporter(quiet=args.quiet, log_path=_resolve_log_path(args, out_path))
    if _print_version:
        print(f"PyAutoRoute {__version__}")

    if _startup_log_params:
        (args_cli, parser, pure_defaults, proj_ini_path, cfg_path,
         proj_ini_values, cfg_values) = _startup_log_params
        _log_startup_header(rep, args, args_cli, parser, pure_defaults,
                            proj_ini_path, cfg_path,
                            proj_ini_values, cfg_values)

    rep.phase("parsing board + rules")
    board = pcb.load_board(input_path)
    if board.outline_synthesized:
        print("  note: no Edge.Cuts outline found — default bounding-box outline added")

    fill_nets = pcb.zone_fill_nets(board)
    if fill_nets:
        current_excludes = list(args.exclude_net or [])
        for n in sorted(fill_nets):
            if n not in current_excludes:
                current_excludes.append(n)
                print(f"  fill zone:     auto-excluding net '{n}' (has copper pour)")
        args.exclude_net = current_excludes

    rules = load_rules(pro_path)
    pitch = args.grid if args.grid else default_pitch(rules)

    if getattr(args, "fix_values", False):
        n = pcb.fix_value_layers(board)
        if n:
            print(f"  fix-values:    moved {n} Value text node(s) to silkscreen")

    if board.segments:
        from pyautoroute.report import routing_stats
        init = routing_stats(board, rules)
        print(f"  initial board: {init.summary()}")

    _print_footprint_constraints(board)
    _log_footprint_constraints(rep, board)

    cycles = max(1, getattr(args, "cycles", 1))
    if cycles > 1 and not args.place:
        print("  note: --cycles needs --place (it selects placements by how they "
              "route); ignoring")
        cycles = 1
    if cycles > 1 and args.place_only:
        print("  note: --cycles has no effect with --place-only (no routing); "
              "ignoring")
        cycles = 1
    if getattr(args, "place_feedback", False) and cycles <= 1:
        print("  note: --place-feedback needs --cycles > 1 (it learns from each "
              "cycle's routing); ignoring")
        args.place_feedback = False
    if cycles > 1:
        return _run_cycles(args, rep, input_path, out_path, rules, pitch,
                           board, fill_nets, cycles)

    if args.place or args.place_only:
        pp, keep_outline = _place_params_from_args(args, board, rules, rep)
        place_runs = max(1, args.place_runs)
        n_fps = len(board.footprints)
        _place_t0 = [time.monotonic()]

        def _pl_run(k, n):
            rep.tag = f"run {k + 1}/{n}: " if n > 1 else ""
            _place_t0[0] = time.monotonic()
            if k > 0:
                rep.phase(f"{rep.tag}placing {n_fps} footprints (annealing)")

        def _pl_progress(it, total, energy, best, temp, accept, ob):
            rep.placing(it, total, energy, best, temp, accept,
                        elapsed=time.monotonic() - _place_t0[0],
                        budget=args.place_time or 0.0, overall_best=ob)

        place_hooks = PipelineHooks(
            phase=lambda name: rep.phase(f"{rep.tag}{name}"),
            place_run=_pl_run, place_progress=_pl_progress)
        pout = run_placement(board, place_params=pp, place_runs=place_runs,
                             seed=args.seed, place_margin=args.place_margin,
                             hooks=place_hooks)
        rep.tag = ""
        rep.done()
        if place_runs > 1:
            rep.log(f"best of {place_runs} placement runs: energy {pout.best_energy:.1f}")
        summary = (f"place: {pout.iterations} iters, "
                   f"{pout.accepted} accepted ({pout.accept_ratio*100:.0f}%), "
                   f"{pout.moved} moved, energy "
                   f"{pout.start_energy:.1f} -> {pout.best_energy:.1f}"
                   + (f"  (best of {place_runs})" if place_runs > 1 else ""))
        breakdown = (f"placement: ratsnest {pout.final_ratsnest:.1f} mm, "
                     f"overlap {pout.final_overlap:.1f} mm2, "
                     f"bbox {pout.final_bbox:.0f} mm2")
        if any(fp.edge_affinity for fp in board.footprints):
            breakdown += f", edge {pout.final_edge:.1f} mm"
        rep.log(summary)
        rep.log(breakdown)
        if not args.quiet:
            print(f"\n  {summary}")
            print(f"  {breakdown}")

    if args.place_only:
        rep.phase("writing placed board")
        _stamp(board, "placed")
        # Placement always clears existing routing (moved footprints invalidate old tracks).
        pcb.write_board(board, out_path, new_nodes=None,
                        strip_free_vias=True, strip_segments=True)
        if fill_nets or args.ground_plane:
            ok = pcb.try_refill_zones(out_path)
            if ok:
                rep.log("zones refilled via kicad-cli")
                print("  zones:         copper fill refilled (kicad-cli)")
            else:
                rep.log("zone refill skipped — kicad-cli not found or failed")
                print("  note: kicad-cli not available; open in KiCad to refill copper zones")
        placed_board = pcb.load_board(out_path)
        violations = geometry.clearance_violations(placed_board, rules)
        rep.done()
        _report_placed(rep, out_path, board, violations)
        return _finish(rep, args, out_path, placed_board, violations)

    if args.auto:
        from . import tune
        rep.phase("auto: probing grid/via settings")
        auto_configs = tune.default_grid(time_budget=args.auto_probe_time)

        def _sweep_progress(done, total, cfg, cs):
            if args.quiet:
                return
            if done == 0:
                print(f"  probing {total} grid/via combinations …", flush=True)
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

        scored = tune.sweep(board, rules, auto_configs,
                            seeds=(args.seed,), unrouted_weight=args.unrouted_weight,
                            via_weight=args.via_weight, exclude=args.exclude_net or None,
                            progress=_sweep_progress)
        best = tune.best_config(scored)
        chosen_pitch = round(default_pitch(rules) * best.grid_mult, 4)
        rep.done()
        bm = scored[0].metrics[0]
        total = bm.routed + bm.unrouted

        rep.phase("auto: probing search margin")

        def _margin_progress(done, total_m, cfg, metrics):
            if args.quiet:
                return
            if done == 0:
                print(f"  probing {total_m} search-margin candidates …", flush=True)
                return
            bar_w = 20
            filled = int(bar_w * done / total_m)
            bar = "█" * filled + "░" * (bar_w - filled)
            suffix = ""
            if cfg is not None and metrics is not None:
                margin_str = f"{cfg.search_margin}mm" if cfg.search_margin else "unbounded"
                suffix = (f"  margin={margin_str}"
                          f"  {metrics.routed}/{metrics.routed+metrics.unrouted} routed")
            print(f"\r  [{bar}] {done}/{total_m}{suffix}",
                  end="" if done < total_m else "\n", flush=True)

        suggested_margin = tune.probe_search_margin(board, rules, best, seed=args.seed,
                                                    progress=_margin_progress,
                                                    exclude=args.exclude_net or None)
        rep.done()

        chosen = (f"auto: best probe grid={chosen_pitch} mm (x{best.grid_mult}), "
                  f"via-weight={best.via_weight}, "
                  f"search-margin={suggested_margin if suggested_margin is not None else 'unbounded'}"
                  f" -> {bm.routed}/{total} routed, {bm.length:.0f} mm, {bm.vias} vias")
        print(f"\n  {chosen}")
        rep.log(chosen)
        apply_auto = True
        if sys.stdin.isatty() and not args.auto_yes:
            apply_auto = input("  apply these settings? [Y/n] ").strip().lower() \
                in ("", "y", "yes")
        if apply_auto:
            args.grid, args.via_weight, pitch = chosen_pitch, best.via_weight, chosen_pitch
            if suggested_margin is not None:
                args.search_margin = suggested_margin
        else:
            print("  auto: keeping the given settings")

    existing_routes = getattr(args, "existing_routes", "clear")
    if existing_routes == "preserve" and (args.place or args.place_only):
        print("  note: --existing-routes preserve is ignored with --place "
              "(placement invalidates existing routing); using clear")
        existing_routes = "clear"

    rep.phase("building netlist (MST rats-nest)")
    conns = netlist.build_connections(board, exclude=args.exclude_net)
    excluded = sorted({p.net for p in board.pads if p.net
                       and netlist.is_excluded(p.net, args.exclude_net)})

    n_pre_routed = 0
    if existing_routes == "preserve":
        pre_routed, conns = netlist.pre_routed_connections(board, conns)
        n_pre_routed = len(pre_routed)
        if n_pre_routed:
            rep.log(f"pre-routed:    {n_pre_routed} connection(s) already satisfied "
                    f"by existing copper (skipped)")
            if not args.quiet:
                print(f"  pre-routed:    {n_pre_routed} connection(s) preserved, "
                      f"{len(conns)} remaining")

    rep.phase(f"building {pitch}mm routing grid")
    grid = Grid(board, rules, pitch)

    runs = max(1, args.runs)
    if runs > 1 and not (args.iters or args.time_budget):
        print("  note: --runs > 1 has no effect without --iters/--time "
              "(greedy routing is deterministic); using 1 run")
        runs = 1
    # Worker count for parallel best-of-N: --jobs 0 means "use as many workers
    # as runs, capped at the CPU count". --jobs 1 (the default) keeps the
    # byte-identical sequential path. Never spawn more workers than runs.
    jobs = args.jobs if args.jobs and args.jobs > 0 else (os.cpu_count() or 1)
    jobs = max(1, min(jobs, runs))
    parallel = runs > 1 and jobs > 1
    # snapshots only make sense during a single annealing run
    snap_n = args.snapshots
    if snap_n and not (args.iters or args.time_budget):
        print("  note: --snapshots needs --iters or --time (annealing); ignoring")
        snap_n = 0
    if snap_n and runs > 1:
        print("  note: --snapshots needs a single run; ignoring with --runs > 1")
        snap_n = 0
    snap_dir = out_path.parent / "snapshots" if snap_n else None
    if snap_dir is not None:
        snap_dir.mkdir(parents=True, exist_ok=True)

    def on_snapshot(k, n, results):
        sp = snap_dir / f"{input_path.stem}_anneal_{k:02d}of{n:02d}.kicad_pcb"
        pcb.write_board(board, sp, new_nodes=_results_to_nodes(board, grid, results),
                        strip_free_vias=(existing_routes == "clear"),
                        strip_segments=(existing_routes == "clear"))
        nrouted = sum(1 for r in results if r is not None)
        rep.log(f"snapshot {k}/{n} -> {sp}  routed={nrouted}/{len(results)}")

    _log_params(rep, args, input_path, out_path, pro_path, pitch,
                board, conns, grid, snap_n)

    note = coarse_grid_note(pitch, default_pitch(rules))
    if note:
        print(f"  warning: {note}")
        rep.log(f"warning: {note}")

    params = router.RouteParams(via_cost=args.via_weight,
                                search_margin=args.search_margin)
    order = netlist.greedy_order(conns)
    annealing = bool(args.iters or args.time_budget)
    route_kw = dict(annealing=annealing, iters=args.iters,
                    time_budget=args.time_budget,
                    unrouted_weight=args.unrouted_weight,
                    anneal_temps=args.anneal_temps, via_weight=args.via_weight)
    _anneal_t0 = [0.0]

    def _rt_phase(name):
        rep.done()
        if name.startswith("annealing"):
            _anneal_t0[0] = time.monotonic()
        rep.phase(f"{rep.tag}{name}")

    def _rt_anneal(it, total, r, u, energy, best, temp, accept, ob):
        rep.annealing(it, total, r, u, energy, best, temp, accept,
                      elapsed=time.monotonic() - _anneal_t0[0],
                      budget=args.time_budget or 0.0, overall_best=ob)

    def _rt_run_done(k, n, energy, summary, metrics):
        if parallel:                                   # completion-ordered logging
            rep.log(f"run {k + 1}/{n} done: energy {energy:.1f}"
                    + (f"  ({summary})" if summary else ""))
        elif summary:                                  # sequential annealing summary
            line = f"{rep.tag}{summary}"
            rep.log(line)
            if not args.quiet:
                print(f"\n  {line}")
        elif n > 1:
            rep.log(f"{rep.tag}energy {energy:.1f}")

    route_hooks = PipelineHooks(
        phase=_rt_phase,
        route_run=lambda k, n: setattr(
            rep, "tag", f"run {k + 1}/{n}: " if n > 1 else ""),
        route_progress=rep.routing,
        anneal_progress=_rt_anneal,
        anneal_snapshot=((lambda b, g, res, k, n: on_snapshot(k, n, res))
                         if snap_n else None),
        route_run_done=_rt_run_done)

    res = run_routing(board, rules, pitch, route_params=params, route_kw=route_kw,
                      seed=args.seed, runs=runs, jobs=jobs, snapshots=snap_n,
                      exclude=args.exclude_net, grid=grid, conns=conns, order=order,
                      hooks=route_hooks)
    rep.done()
    rep.tag = ""
    final_results = (res.results if res.results is not None
                     else [None] * len(conns))
    routed, unrouted, length, vias = res.routed, res.unrouted, res.length, res.vias
    best_energy = res.energy

    if runs > 1:
        best_line = f"best of {runs} runs: energy {best_energy:.1f}"
        rep.log(best_line)
        if not args.quiet:
            print(f"\n  {best_line}")
    if snap_n:
        print(f"  snapshots:     {snap_n} written to {snap_dir}/")

    rep.phase("writing routed board")
    mode = "placed + routed" if args.place else "routed"
    _stamp(board, mode)

    # Build node list: routing results + ground plane (if requested)
    new_nodes = _results_to_nodes(board, grid, final_results)
    if args.ground_plane:
        from . import groundplane
        margin = args.ground_plane_margin or rules.default_class.clearance
        layers = ["F.Cu", "B.Cu"] if args.ground_plane_layer == "both" else [args.ground_plane_layer]
        for layer in layers:
            gp_nodes, gp_warns = groundplane.build(
                board, rules, net=args.ground_net, layer=layer, margin=margin,
                stitch_pitch=args.stitch_vias, routed_nodes=new_nodes
            )
            new_nodes.extend(gp_nodes)
            for w in gp_warns:
                rep.log(f"ground-plane: {w}")
                print(f"  ⚠ ground-plane: {w}")

    pcb.write_board(board, out_path,
                    new_nodes=new_nodes,
                    strip_free_vias=(existing_routes == "clear"),
                    strip_segments=(existing_routes == "clear"))

    if fill_nets or args.ground_plane:
        ok = pcb.try_refill_zones(out_path)
        if ok:
            rep.log("zones refilled via kicad-cli")
            print("  zones:         copper fill refilled (kicad-cli)")
        else:
            rep.log("zone refill skipped — kicad-cli not found or failed")
            print("  note: kicad-cli not available; open in KiCad to refill copper zones")

    # reload the written board and self-check clearances
    routed_board = pcb.load_board(out_path)
    violations = geometry.clearance_violations(routed_board, rules)
    rep.done()

    total_conns = len(conns) + n_pre_routed
    _report(rep, out_path, total_conns, routed + n_pre_routed, unrouted, length, vias,
            violations, excluded)
    return _finish(rep, args, out_path, routed_board, violations)


def _run_cycles(args, rep, input_path, out_path, rules, pitch, board, fill_nets,
                cycles) -> int:
    """Best-of-cycles: keep the best-*routing* of N independent place+route runs.

    Each cycle re-loads the board and runs one placement + one routing through
    `pipeline.run_cycle` (seed ``args.seed + k``), so cycles are independent and
    selected on the true routed objective — fewest unrouted, then lowest energy —
    rather than on placement energy. Cycles run sequentially with live progress,
    or across ``--jobs`` worker processes (progress suppressed, one line per
    finished cycle). The winning cycle's board is written, zone-refilled,
    self-checked and reported exactly as `run` does.

    Args:
        args: the parsed CLI namespace.
        rep: the reporter.
        input_path: the source board path (re-read each cycle).
        out_path: the output board path.
        rules: the design rules.
        pitch: routing-grid pitch (mm).
        board: the already-parsed board (for param resolution + exclude reporting).
        fill_nets: nets with copper pours (for the zone refill).
        cycles: number of cycles (> 1).

    Returns:
        Process exit code (0 clean, 2 on a clearance violation).
    """
    pp, _keep = _place_params_from_args(args, board, rules, rep)
    route_params = router.RouteParams(via_cost=args.via_weight,
                                      search_margin=args.search_margin)
    route_kw = dict(annealing=bool(args.iters or args.time_budget),
                    iters=args.iters, time_budget=args.time_budget,
                    unrouted_weight=args.unrouted_weight,
                    anneal_temps=args.anneal_temps, via_weight=args.via_weight)
    base_seed = args.seed
    jobs = args.jobs if args.jobs and args.jobs > 0 else (os.cpu_count() or 1)
    jobs = max(1, min(jobs, cycles))
    # Congestion feedback couples each cycle to the previous one's routing, so it
    # cannot run across independent workers — it forces the sequential path.
    feedback = bool(getattr(args, "place_feedback", False))
    parallel = jobs > 1 and not feedback

    def _cycle_line(k, cr):
        return (f"cycle {k}/{cycles}: routed {cr.routed}/{cr.n_conns}, "
                f"energy {cr.energy:.1f}, {cr.vias} vias"
                + (f", {cr.unrouted} unrouted" if cr.unrouted else ""))

    results: list = []
    if parallel:
        rep.phase(f"best-of-cycles: {cycles} place+route cycles "
                  f"across {jobs} workers")
        payloads = [(str(input_path), rules, pitch, pp, route_params,
                     route_kw, args.place_margin, base_seed + k)
                    for k in range(cycles)]
        try:
            with concurrent.futures.ProcessPoolExecutor(max_workers=jobs) as ex:
                futs = [ex.submit(_cycle_worker, p) for p in payloads]
                for fut in concurrent.futures.as_completed(futs):
                    cr = fut.result()
                    results.append(cr)
                    line = _cycle_line(len(results), cr)
                    rep.log(line)
                    if not args.quiet:
                        print(f"  {line}")
        except KeyboardInterrupt:
            rep.log("interrupted — keeping best cycle so far")
            if not results:
                raise
        rep.done()
    else:
        if feedback:
            if args.jobs and args.jobs != 1:
                print("  note: --place-feedback couples each cycle to the last, "
                      "so cycles run sequentially (--jobs ignored)")
            rep.phase(f"best-of-cycles: {cycles} place+route cycles with "
                      f"congestion feedback (weight {args.congestion_weight:g})")
        # Congestion feedback (--place-feedback): a fixed board-wide field frame is
        # built once, then each cycle (after cycle 0) re-places under the field
        # accumulated from the previous cycles' routing, decayed so signal builds
        # without one cycle dominating. The best-routing cycle is still selected,
        # so feedback can only help or be discarded.
        frame = router.congestion_frame(board, pitch) if feedback else None
        field = None
        for k in range(cycles):
            rep.tag = f"cycle {k + 1}/{cycles}: "
            hooks = CycleHooks(
                phase_cb=lambda n: rep.phase(rep.tag + n),
                place_progress=(lambda it, total, e, b, t, acc:
                                rep.placing(it, total, e, b, t, acc)),
                route_progress=rep.routing,
                anneal_progress=(lambda it, total, r, u, e, b, t, acc:
                                 rep.annealing(it, total, r, u, e, b, t, acc)),
            )
            pp_k = pp
            if feedback and field is not None:
                pp_k = replace(pp, congestion_field=field,
                               congestion_weight=args.congestion_weight)
            cr = run_cycle(input_path, rules, pitch, pp_k, route_params,
                           route_kw=route_kw, place_margin=args.place_margin,
                           seed=base_seed + k, hooks=hooks)
            rep.done()
            results.append(cr)
            line = _cycle_line(k + 1, cr)
            rep.log(line)
            if not args.quiet:
                print(f"  {line}")
            if feedback and k + 1 < cycles:
                new_field = router.congestion_heatmap(cr.conns, cr.results,
                                                      cr.grid, frame)
                field = (new_field if field is None
                         else field.blended(new_field, _FEEDBACK_DECAY))
        rep.tag = ""

    best = select_best(results)
    best_line = (f"best of {cycles} cycles: seed {best.seed} — "
                 f"routed {best.routed}/{best.n_conns}, energy {best.energy:.1f}, "
                 f"{best.vias} vias"
                 + (f", {best.unrouted} unrouted" if best.unrouted else ""))
    rep.log(best_line)
    if not args.quiet:
        print(f"\n  {best_line}")

    sel_board, grid, final_results = best.board, best.grid, best.results
    excluded = sorted({p.net for p in sel_board.pads if p.net
                       and netlist.is_excluded(p.net, args.exclude_net)})

    rep.phase("writing placed + routed board")
    _stamp(sel_board, "placed + routed")
    new_nodes = _results_to_nodes(sel_board, grid, final_results)
    if args.ground_plane:
        from . import groundplane
        margin = args.ground_plane_margin or rules.default_class.clearance
        layers = ["F.Cu", "B.Cu"] if args.ground_plane_layer == "both" else [args.ground_plane_layer]
        for layer in layers:
            gp_nodes, gp_warns = groundplane.build(
                sel_board, rules, net=args.ground_net, layer=layer, margin=margin,
                stitch_pitch=args.stitch_vias, routed_nodes=new_nodes
            )
            new_nodes.extend(gp_nodes)
            for w in gp_warns:
                rep.log(f"ground-plane: {w}")
                print(f"  ⚠ ground-plane: {w}")
    # cycles always uses --place, which forces clear mode
    pcb.write_board(sel_board, out_path, new_nodes=new_nodes,
                    strip_free_vias=True, strip_segments=True)

    if fill_nets or args.ground_plane:
        ok = pcb.try_refill_zones(out_path)
        if ok:
            rep.log("zones refilled via kicad-cli")
            print("  zones:         copper fill refilled (kicad-cli)")
        else:
            rep.log("zone refill skipped — kicad-cli not found or failed")
            print("  note: kicad-cli not available; open in KiCad to refill copper zones")

    routed_board = pcb.load_board(out_path)
    violations = geometry.clearance_violations(routed_board, rules)
    rep.done()

    _report(rep, out_path, best.n_conns, best.routed, best.unrouted, best.length,
            best.vias, violations, excluded)
    return _finish(rep, args, out_path, routed_board, violations)


def _finish(rep: Reporter, args, out_path: Path, board, violations) -> int:
    """Report timing and close the log; the shared tail of the routing and
    place-only paths.

    Args:
        rep: the reporter.
        args: the parsed CLI namespace.
        out_path: the output board path (basis for the log name).
        board: the reloaded output board (unused; kept for signature symmetry).
        violations: the self-check violations (empty == clean) for the exit code.

    Returns:
        Process exit code: 0 if `violations` is empty, else 2.
    """
    real, cpu = rep.runtime()
    timing = f"runtime:       {real:.2f}s real, {cpu:.2f}s cpu"
    print(f"  {timing}")
    rep.log(timing)

    if rep.log_file is not None and not args.quiet:
        print(f"  log:           {_resolve_log_path(args, out_path)}")
    rep.close()
    return 0 if not violations else 2


def _report(rep: Reporter, out_path, n_conns, routed, unrouted, length, vias,
            violations, excluded) -> None:
    """Print the final metrics summary and mirror it to the log.

    Args:
        rep: the reporter (for logging the same lines).
        out_path: the routed-output path.
        n_conns: total connections.
        routed: connections routed.
        unrouted: connections not routed.
        length: total wirelength (mm).
        vias: total via count.
        violations: clearance-violation tuples from the self-check (empty ==
            clean).
        excluded: net names excluded from routing.
    """
    pct = 100.0 * routed / n_conns if n_conns else 100.0
    lines = [
        f"output:        {out_path}",
        f"connections:   {routed}/{n_conns} routed ({pct:.0f}%)",
        f"unrouted:      {unrouted}  (reported, not drawn)",
        f"wirelength:    {length:.1f} mm",
        f"vias:          {vias}",
    ]
    if excluded:
        lines.append(f"excluded nets: {len(excluded)} ({', '.join(excluded[:6])}"
                     f"{' ...' if len(excluded) > 6 else ''})")
    if violations:
        lines.append(f"SELF-CHECK:    {len(violations)} clearance violation(s)! "
                     f"e.g. {violations[0]}")
    else:
        lines.append("self-check:    clean (0 clearance violations)")
    print()
    for ln in lines:
        print(f"  {ln}")
        rep.log(ln)


def _report_placed(rep: Reporter, out_path, board, violations) -> None:
    """Print the place-only metrics summary and mirror it to the log.

    Args:
        rep: the reporter (for logging the same lines).
        out_path: the placed-output path.
        board: the placed board (for the moved-footprint count and outline size).
        violations: clearance-violation tuples from the self-check (empty ==
            clean).
    """
    moved = sum(1 for fp in board.footprints if fp.moved)
    rect = next((s for s in board.outline if s.kind == "rect"), None)
    lines = [
        f"output:        {out_path}",
        f"footprints:    {moved}/{len(board.footprints)} moved",
    ]
    if rect is not None:
        (x0, y0), (x1, y1) = rect.data["start"], rect.data["end"]
        lines.append(f"board outline: {abs(x1 - x0):.1f} x {abs(y1 - y0):.1f} mm")
    if violations:
        lines.append(f"SELF-CHECK:    {len(violations)} clearance violation(s)! "
                     f"e.g. {violations[0]}")
    else:
        lines.append("self-check:    clean (0 clearance violations)")
    print()
    for ln in lines:
        print(f"  {ln}")
        rep.log(ln)


# --- settings file (INI) -----------------------------------------------------

_CONFIG_SECTION = "pyautoroute"
# options that are not part of the persisted settings (meta / positional)
_CONFIG_SKIP = {"help", "version", "config", "write_config", "input"}


def _configurable_actions(parser: argparse.ArgumentParser) -> dict:
    """Map each persisted option's ``dest`` to its argparse action.

    Args:
        parser: the CLI parser.

    Returns:
        ``{dest: action}`` for every optional argument that round-trips through
        the settings file (the meta/positional ones in `_CONFIG_SKIP` excluded).
    """
    return {a.dest: a for a in parser._actions
            if a.option_strings and a.dest not in _CONFIG_SKIP}


def _parse_bool(text: str) -> bool:
    """Parse a boolean from a settings-file value.

    Args:
        text: the raw value (e.g. ``"true"``, ``"0"``, ``"yes"``).

    Returns:
        The boolean.

    Raises:
        ValueError: if `text` is not a recognised boolean.
    """
    low = text.strip().lower()
    if low in ("true", "1", "yes", "on"):
        return True
    if low in ("false", "0", "no", "off"):
        return False
    raise ValueError(f"not a boolean: {text!r}")


def _coerce_config_value(action, raw: str):
    """Coerce a raw settings-file string to the option's Python type.

    Args:
        action: the argparse action for the option.
        raw: the raw string from the settings file.

    Returns:
        The typed value (bool for flags, list for append/2-tuple options,
        otherwise the action's ``type`` applied to `raw`).
    """
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        return _parse_bool(raw)
    conv = action.type or str
    if isinstance(action, argparse._AppendAction):
        return [conv(s.strip()) for s in raw.split(",") if s.strip()]
    if action.nargs == 2:
        parts = [s for s in raw.replace(",", " ").split() if s]
        if len(parts) != 2:
            raise ValueError(f"expected 2 values, got {raw!r}")
        return [conv(p) for p in parts]
    return conv(raw)


def load_project_config(path: str | Path,
                        parser: argparse.ArgumentParser) -> dict:
    """Load a project INI file if it has a ``[pyautoroute]`` section.

    Like `load_config` but silently returns ``{}`` when the file does not
    exist or lacks the ``[pyautoroute]`` section — so a generic ``*.ini``
    used by other tools in the project directory is skipped harmlessly.
    Bad values still raise via ``parser.error``.

    Args:
        path: path to the project INI file (e.g. ``myboard.ini``).
        parser: the CLI parser.

    Returns:
        Settings dict (possibly empty) suitable for ``parser.set_defaults``.
    """
    path = Path(path)
    if not path.exists():
        return {}
    cp = configparser.ConfigParser()
    cp.read(path)
    if not cp.has_section(_CONFIG_SECTION):
        return {}
    return load_config(path, parser)


def load_config(path: str | Path, parser: argparse.ArgumentParser) -> dict:
    """Read an INI settings file into a ``{dest: typed_value}`` mapping.

    Args:
        path: the settings file path.
        parser: the CLI parser (for option names and types).

    Returns:
        The settings as a dict suitable for ``parser.set_defaults``.

    Raises:
        SystemExit: via ``parser.error`` if the file is missing, lacks the
            ``[pyautoroute]`` section, names an unknown key, or has a bad value.
    """
    path = Path(path)
    if not path.exists():
        parser.error(f"--config file not found: {path}")
    cp = configparser.ConfigParser()
    cp.read(path)
    if not cp.has_section(_CONFIG_SECTION):
        parser.error(f"--config file has no [{_CONFIG_SECTION}] section: {path}")
    actions = _configurable_actions(parser)
    out = {}
    for key, raw in cp.items(_CONFIG_SECTION):
        dest = key.replace("-", "_")
        if dest not in actions:
            parser.error(f"--config: unknown option {key!r} in {path}")
        try:
            out[dest] = _coerce_config_value(actions[dest], raw)
        except (ValueError, TypeError) as exc:
            parser.error(f"--config: bad value for {key!r}: {exc}")
    return out


def _format_config_value(action, value) -> str:
    """Render an effective option value as a settings-file string.

    Args:
        action: the argparse action for the option.
        value: the value from the parsed namespace.

    Returns:
        The INI-ready string (``true``/``false`` for flags, comma-joined for
        list/2-tuple options).
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def _config_comment(action) -> str:
    """Return the help text for `action` with %(default)s expanded."""
    if not action.help:
        return ""
    try:
        return action.help % {"default": action.default,
                               "metavar": action.metavar or ""}
    except (KeyError, TypeError):
        return action.help


def _append_option(lines: list, dest: str, action, value,
                   commented: bool) -> None:
    """Append one INI entry (comment + key = value) to *lines*.

    If *commented* is True the key=value line is prefixed with ``#`` so it
    acts as a placeholder the user can uncomment.
    """
    help_text = _config_comment(action)
    if help_text:
        for chunk in textwrap.wrap(help_text, width=76,
                                   initial_indent="# ",
                                   subsequent_indent="# "):
            lines.append(chunk + "\n")
    if commented:
        val_str = _format_config_value(action, value) if value is not None else ""
        lines.append(f"# {dest} = {val_str}\n")
    else:
        lines.append(f"{dest} = {_format_config_value(action, value)}\n")
    lines.append("\n")


def _print_settings_header(
    args: argparse.Namespace,
    args_cli: argparse.Namespace,
    parser: argparse.ArgumentParser,
    pure_defaults: dict,
    proj_ini_path: "Path | None",
    cfg_path: "Path | None",
    proj_ini_values: dict,
    cfg_values: dict,
) -> None:
    """Print the startup header: version, config files, non-default settings table.

    Args:
        args: effective parsed namespace (defaults + ini + CLI).
        args_cli: parsed namespace with no ini defaults applied (CLI-only).
        parser: the CLI parser (for action metadata).
        pure_defaults: ``{dest: default}`` captured before any ``set_defaults``.
        proj_ini_path: path to the auto-loaded project ini, or ``None``.
        cfg_path: path to the ``--config`` ini, or ``None``.
        proj_ini_values: settings loaded from the project ini.
        cfg_values: settings loaded from ``--config``.
    """
    print(f"PyAutoRoute {__version__}")
    if proj_ini_path is not None:
        print(f"  Project ini:  {proj_ini_path}")
    if cfg_path is not None:
        print(f"  --config:     {cfg_path}")

    actions = _configurable_actions(parser)
    rows: list[tuple[str, str, str]] = []
    for dest, action in actions.items():
        effective = getattr(args, dest, None)
        pure_def = pure_defaults.get(dest, action.default)
        if effective == pure_def:
            continue                        # still at default — skip

        long_opt = next((s for s in action.option_strings if s.startswith("--")),
                        action.option_strings[0])
        val_str = _format_config_value(action, effective)

        cli_val = getattr(args_cli, dest, pure_def)
        if cli_val != pure_def:
            source = "cli"
        elif dest in cfg_values and effective == cfg_values[dest]:
            source = cfg_path.name if cfg_path else "ini"
        elif dest in proj_ini_values:
            source = proj_ini_path.name if proj_ini_path else "ini"
        else:
            source = "ini"

        rows.append((long_opt, val_str, source))

    w_opt = max((len(r[0]) for r in rows), default=len("Option"))
    w_val = max((len(r[1]) for r in rows), default=len("Value"))
    w_opt = max(w_opt, len("Option"))
    w_val = max(w_val, len("Value"))
    if rows:
        hdr = f"  {'Option':<{w_opt}}  {'Value':<{w_val}}  Source"
        sep = f"  {'-' * w_opt}  {'-' * w_val}  ------"
        print()
        print(hdr)
        print(sep)
        for opt, val, src in rows:
            print(f"  {opt:<{w_opt}}  {val:<{w_val}}  {src}")
        print()
    sys.stdout.flush()


def _log_startup_header(
    rep: Reporter,
    args: argparse.Namespace,
    args_cli: argparse.Namespace,
    parser: argparse.ArgumentParser,
    pure_defaults: dict,
    proj_ini_path: "Path | None" = None,
    cfg_path: "Path | None" = None,
    proj_ini_values: dict | None = None,
    cfg_values: dict | None = None,
) -> None:
    """Log the startup header: version, config files, non-default settings table.

    Args:
        rep: Reporter instance for logging.
        args: effective parsed namespace (defaults + ini + CLI).
        args_cli: parsed namespace with no ini defaults applied (CLI-only).
        parser: the CLI parser (for action metadata).
        pure_defaults: ``{dest: default}`` captured before any ``set_defaults``.
        proj_ini_path: path to the auto-loaded project ini, or ``None``.
        cfg_path: path to the ``--config`` ini, or ``None``.
        proj_ini_values: settings loaded from the project ini.
        cfg_values: settings loaded from ``--config``.
    """
    rep.log(f"PyAutoRoute {__version__}")
    if proj_ini_path is not None:
        rep.log(f"  Project ini:  {proj_ini_path}")
    if cfg_path is not None:
        rep.log(f"  --config:     {cfg_path}")

    actions = _configurable_actions(parser)
    rows: list[tuple[str, str, str]] = []
    for dest, action in actions.items():
        effective = getattr(args, dest, None)
        pure_def = pure_defaults.get(dest, action.default)
        if effective == pure_def:
            continue

        long_opt = next((s for s in action.option_strings if s.startswith("--")),
                        action.option_strings[0])
        val_str = _format_config_value(action, effective)

        cli_val = getattr(args_cli, dest, pure_def)
        if cli_val != pure_def:
            source = "cli"
        elif (cfg_values and dest in cfg_values and
              effective == cfg_values[dest]):
            source = cfg_path.name if cfg_path else "ini"
        elif proj_ini_values and dest in proj_ini_values:
            source = proj_ini_path.name if proj_ini_path else "ini"
        else:
            source = "ini"

        rows.append((long_opt, val_str, source))

    if rows:
        rep.log("")
        w_opt = max((len(r[0]) for r in rows), default=len("Option"))
        w_val = max((len(r[1]) for r in rows), default=len("Value"))
        w_opt = max(w_opt, len("Option"))
        w_val = max(w_val, len("Value"))
        rep.log(f"Option{' ' * (w_opt - 6)}  Value{' ' * (w_val - 5)}  Source")
        rep.log(f"{'-' * w_opt}  {'-' * w_val}  ------")
        for opt, val, src in rows:
            rep.log(f"{opt:<{w_opt}}  {val:<{w_val}}  {src}")
        rep.log("")


def write_config(parser: argparse.ArgumentParser, args, path: str | Path) -> None:
    """Write the effective settings to an INI file with per-option comments.

    Every configurable option is included: options that have a value are
    written as ``key = value``; options that are unset (``None``) are written
    as ``# key =`` so they act as documented placeholders.  Within mutually
    exclusive groups only the active member is written un-commented; the
    alternatives appear as commented-out placeholders below it.

    Args:
        parser: the CLI parser (for the option set and help strings).
        args: the parsed namespace whose effective values are written.
        path: destination settings-file path.
    """
    skip = _CONFIG_SKIP
    ordered = [(a.dest, a) for a in parser._actions
               if a.option_strings and a.dest not in skip]
    action_map = dict(ordered)

    # Build a map from dest -> sibling dests for mutually exclusive groups.
    mutex_siblings: dict[str, list[str]] = {}
    for group in parser._mutually_exclusive_groups:
        dests = [a.dest for a in group._group_actions if a.dest not in skip]
        for dest in dests:
            mutex_siblings[dest] = [d for d in dests if d != dest]

    lines = [
        "# PyAutoRoute settings — pass with --config FILE.\n",
        "# CLI options override these. Lists are comma-separated.\n",
        "\n",
        f"[{_CONFIG_SECTION}]\n",
        "\n",
    ]
    written: set[str] = set()
    for dest, action in ordered:
        if dest in written:
            continue
        written.add(dest)
        value = getattr(args, dest, None)
        _append_option(lines, dest, action, value, commented=(value is None))

        # Immediately follow with any mutex siblings, always commented out.
        for sib in mutex_siblings.get(dest, []):
            if sib in written:
                continue
            written.add(sib)
            sib_val = getattr(args, sib, None)
            _append_option(lines, sib, action_map[sib], sib_val,
                           commented=True)

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser.

    Returns:
        The configured `argparse.ArgumentParser` for the ``pyautoroute`` CLI.
    """
    p = argparse.ArgumentParser(
        prog="pyautoroute",
        description="Autoroute a 2-layer KiCad PCB (writes a routed copy).")
    p.add_argument("--version", action="version", version=f"PyAutoRoute {__version__}")
    p.add_argument("input", help="input .kicad_pcb")
    p.add_argument("--config", metavar="FILE",
                   help="read options from an INI settings file (a [pyautoroute] "
                        "section); options given on the command line override it")
    p.add_argument("--write-config", nargs="?", const="", default=None, metavar="FILE",
                   help="write the effective settings to an INI file and exit "
                        "(bare: <input>.ini beside the board — the same file "
                        "auto-loaded on the next run)")
    p.add_argument("--pro", help="project .kicad_pro (default: sibling)")
    p.add_argument("-o", "--output", help="output .kicad_pcb (default: INPUT_routed, "
                                          "or _placed_routed / _placed when placing)")
    p.add_argument("--grid", type=float, help="grid pitch in mm (default derived from rules)")
    p.add_argument("--place", action="store_true",
                   help="experimental: place footprints (simulated annealing) before "
                        "routing — honours locked footprints and the Autoroute-overlap "
                        "property, and regenerates the Edge.Cuts outline")
    p.add_argument("--place-only", action="store_true",
                   help="place the footprints and write the placed board "
                        "(<input>_placed.kicad_pcb) without routing")
    pg = p.add_mutually_exclusive_group()
    pg.add_argument("--place-iters", type=int, metavar="N",
                    help="placement iteration budget (with --place)")
    pg.add_argument("--place-time", type=float, metavar="S",
                    help="placement time budget in seconds (with --place)")
    p.add_argument("--place-margin", type=float,
                   default=2.0, metavar="MM",
                   help="margin (mm) around the parts for the regenerated outline "
                        "(default %(default)s)")
    p.add_argument("--keep-outline", action="store_true",
                   help="during --place, keep the board's existing Edge.Cuts and "
                        "contain the footprints within it, instead of regenerating "
                        "a bounding-box outline (needs a closed Edge.Cuts; ignored "
                        "otherwise). Edge-flagged parts then snap to the real edge.")
    p.add_argument("--place-buffer", type=float, default=None, metavar="MM",
                   help="keep-out gap (mm) enforced between footprints during "
                        "placement, so the routed board stays DRC-clean "
                        "(default: derived from the design-rule clearance)")
    p.add_argument("--place-overlap-weight", type=float,
                   default=placement.PlaceParams.overlap_weight, metavar="W",
                   help="placement cost per mm² of footprint overlap (default %(default)s)")
    p.add_argument("--place-compact-weight", type=float,
                   default=placement.PlaceParams.compact_weight, metavar="W",
                   help="placement cost per mm² of layout bounding box, pulling the "
                        "parts together (default %(default)s)")
    p.add_argument("--place-edge-weight", type=float,
                   default=placement.PlaceParams.edge_weight, metavar="W",
                   help="placement cost per mm a footprint flagged "
                        "Autoroute-edge=<side> sits from its target board edge; "
                        "higher pulls edge parts (e.g. connectors) out harder and "
                        "aligns them flat against the edge (default %(default)s)")
    p.add_argument("--place-temps", nargs=2, type=float, metavar=("START", "END"),
                   default=(placement.PlaceParams.t_start, placement.PlaceParams.t_end),
                   help="placement annealing start/end temperature for the geometric "
                        "cooling schedule; START>END>0 (default %(default)s)")
    p.add_argument("--place-step", type=float,
                   default=placement.PlaceParams.step, metavar="MM",
                   help="max placement translate step (mm) at the start temperature "
                        "(default %(default)s)")
    p.add_argument("--place-rotate", choices=("ortho", "free", "none"),
                   default=placement.PlaceParams.rotate_mode,
                   help="placement rotation moves: ortho (+/-90/180), free (any "
                        "angle), or none (default %(default)s)")
    p.add_argument("--place-runs", type=int, default=1, metavar="N",
                   help="run placement N times (different seeds) and keep the "
                        "lowest-energy placement (default 1)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--iters", type=int, help="optimisation iteration budget")
    g.add_argument("--time", type=float, dest="time_budget", help="optimisation time budget (s)")
    p.add_argument("--runs", type=int, default=1, metavar="N",
                   help="route N times with different annealing seeds and keep the "
                        "lowest-energy result (default 1; only varies with "
                        "--iters/--time)")
    p.add_argument("--cycles", type=int, default=1, metavar="N",
                   help="with --place: run N independent place+route cycles and "
                        "keep the one that *routes* best (fewest unrouted, then "
                        "lowest energy) — selecting on the true objective rather "
                        "than placement energy. The recommended knob for a better "
                        "board; parallelised by --jobs. 1 (default) = today's "
                        "behaviour. Each cycle does one placement and one routing; "
                        "--place-runs/--runs remain available as inner loops")
    p.add_argument("--place-feedback", action="store_true",
                   help="with --cycles > 1: feed each cycle's routing back into "
                        "the next placement as a congestion field, spreading "
                        "footprints out of the cells where routing struggled "
                        "(PathFinder-style). Cycles then run sequentially "
                        "(feedback is inherent); the best-routing cycle is still "
                        "kept, so feedback can only help. Opt-in and experimental")
    p.add_argument("--congestion-weight", type=float, default=5.0, metavar="W",
                   help="with --place-feedback: mm-cost per unit congestion at a "
                        "footprint centroid; higher spreads parts harder out of "
                        "the routed hot zones (default %(default)s)")
    p.add_argument("-j", "--jobs", type=int, default=1, metavar="N",
                   help="run --runs trials (or --cycles cycles) across N worker "
                        "processes (best-of-N in parallel); 0 uses every CPU "
                        "(capped at the trial/cycle count). 1 (default) keeps the "
                        "sequential path with live per-trial progress (default 1)")
    p.add_argument("--auto", action="store_true",
                   help="probe a few grid/via settings on this board, pick the best, "
                        "and (on a terminal) ask to confirm before routing with them")
    p.add_argument("--auto-yes", action="store_true",
                   help="with --auto, apply the chosen settings without prompting")
    p.add_argument("--auto-probe-time", type=float, default=3.0, metavar="S",
                   help="annealing seconds per probed setting under --auto (default %(default)s)")
    p.add_argument("--exclude-net", action="append", default=[], metavar="PATTERN",
                   help="net name/glob to leave un-routed (repeatable)")
    p.add_argument("--ground-plane", action="store_true",
                   help="add a GND copper pour zone after routing")
    p.add_argument("--ground-net", default=None, metavar="NET",
                   help="net name for --ground-plane (default: auto-detect GND)")
    p.add_argument("--ground-plane-layer", default="B.Cu", metavar="LAYER",
                   choices=["B.Cu", "F.Cu", "both"],
                   help="layer(s) for the ground pour (default: %(default)s)")
    p.add_argument("--ground-plane-margin", type=float, default=None, metavar="MM",
                   help="inset margin from board outline (mm; default: board clearance)")
    p.add_argument("--stitch-vias", type=float, nargs="?", const=5.0, metavar="PITCH",
                   help="add stitching vias at PITCH mm intervals (default 5.0 mm; "
                        "most useful with --ground-plane-layer both)")
    p.add_argument("--seed", type=int, default=0, help="random seed")
    p.add_argument("--via-weight", type=float, default=2.0, help="via cost (mm-equiv)")
    p.add_argument("--search-margin", type=float, default=None, metavar="MM",
                   help="bound each connection's A* search to a box around its "
                        "endpoints, grown by MM on every side (widening and "
                        "retrying on failure). Speeds up routing on large boards "
                        "at a small cost to path optimality; unset = search the "
                        "whole grid (default)")
    p.add_argument("--unrouted-weight", type=float,
                   default=anneal.AnnealParams.unrouted_weight, metavar="W",
                   help="annealing penalty per unrouted connection — higher tries "
                        "harder to complete routes at the expense of length/vias "
                        "(default %(default)s)")
    p.add_argument("--anneal-temps", nargs=2, type=float, metavar=("START", "END"),
                   default=(anneal.AnnealParams.t_start, anneal.AnnealParams.t_end),
                   help="annealing start/end temperature for the geometric cooling "
                        "schedule; START>END>0 (default %(default)s)")
    p.add_argument("--snapshots", type=int, default=0, metavar="N",
                   help="during annealing, save N board snapshots to a snapshots/ "
                        "subdir (requires --iters or --time)")
    p.add_argument("--log", nargs="?", const="", default=None, metavar="FILE",
                   help="write a verbose log of parameters and routing/anneal "
                        "progress (bare --log uses <output>.log)")
    p.add_argument("--fix-values", action="store_true",
                   help="move footprint Value text to the silkscreen layer before routing")
    p.add_argument("--existing-routes", choices=("clear", "preserve"), default="clear",
                   metavar="{clear,preserve}",
                   help="clear (default): strip all existing tracks and vias before routing "
                        "so re-routing a board never doubles tracks. "
                        "preserve: keep existing copper, detect which connections are already "
                        "satisfied, route only the remainder, treating existing copper as "
                        "obstacles — enabling partial routing of a partially hand-routed board.")
    p.add_argument("--quiet", action="store_true", help="suppress live progress display")
    return p


def main(argv=None) -> int:
    """CLI entry point: parse arguments, validate them, and run the router.

    Args:
        argv: argument list to parse; `None` uses ``sys.argv``.

    Returns:
        The process exit code from `run` (0 clean, 2 on a self-check violation).
    """
    # Layer config sources so the final priority is:
    #   defaults  <  project ini  <  --config  <  CLI options
    # The project ini is <input_stem>.ini beside the board file and is loaded
    # automatically when present (no flag needed).  --config overrides it.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config")
    pre.add_argument("input", nargs="?")
    known, _ = pre.parse_known_args(argv)
    parser = build_parser()

    # Capture pure argparse defaults before any set_defaults calls.
    pure_defaults = {a.dest: a.default for a in parser._actions
                     if a.option_strings and a.dest not in _CONFIG_SKIP}

    proj_ini_path: Path | None = None
    proj_ini_values: dict = {}
    if known.input:
        proj_ini = Path(known.input).with_suffix(".ini")
        d = load_project_config(proj_ini, parser)
        if d:
            proj_ini_path = proj_ini
            proj_ini_values = d
            parser.set_defaults(**d)

    cfg_path: Path | None = None
    cfg_values: dict = {}
    if known.config:
        cfg_values = load_config(known.config, parser)
        cfg_path = Path(known.config)
        parser.set_defaults(**cfg_values)

    args = parser.parse_args(argv)

    # CLI-only namespace (no ini defaults) used for source detection in header.
    args_cli = build_parser().parse_args(argv)

    if args.write_config is not None:
        cfg_path = (Path(args.write_config) if args.write_config
                    else Path(args.input).with_suffix(".ini"))
        write_config(parser, args, cfg_path)
        print(f"wrote settings to {cfg_path}")
        return 0

    t_start, t_end = args.anneal_temps
    if not (t_start > t_end > 0):
        parser.error("--anneal-temps requires START > END > 0")
    if args.unrouted_weight < 0:
        parser.error("--unrouted-weight must be >= 0")
    if args.search_margin is not None and args.search_margin < 0:
        parser.error("--search-margin must be >= 0")
    if args.place_margin < 0:
        parser.error("--place-margin must be >= 0")
    if args.place_buffer is not None and args.place_buffer < 0:
        parser.error("--place-buffer must be >= 0")
    pt_start, pt_end = args.place_temps
    if not (pt_start > pt_end > 0):
        parser.error("--place-temps requires START > END > 0")
    if args.place_step <= 0:
        parser.error("--place-step must be > 0")
    if args.runs < 1:
        parser.error("--runs must be >= 1")
    if args.jobs < 0:
        parser.error("--jobs must be >= 0 (0 means use every CPU)")
    if args.place_runs < 1:
        parser.error("--place-runs must be >= 1")
    if (args.place_iters or args.place_time) and not (args.place or args.place_only):
        parser.error("--place-iters/--place-time require --place or --place-only")
    if args.place_only and (args.iters or args.time_budget):
        parser.error("--place-only does not route; drop --iters/--time")
    _print_settings_header(args, args_cli, parser, pure_defaults,
                           proj_ini_path, cfg_path,
                           proj_ini_values, cfg_values)
    startup_log_params = (args_cli, parser, pure_defaults,
                          proj_ini_path, cfg_path,
                          proj_ini_values, cfg_values)
    return run(args, _print_version=False,
               _startup_log_params=startup_log_params)


if __name__ == "__main__":
    raise SystemExit(main())
