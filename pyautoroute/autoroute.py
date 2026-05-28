"""Command-line entry point: parse a board, route it, write a routed copy.

    python -m pyautoroute.autoroute INPUT.kicad_pcb [options]

Orchestrates parse -> grid build -> route -> write, prints a live text progress
display (unless --quiet), reports metrics, and runs an in-repo clearance
self-check on the result.
"""

from __future__ import annotations

import argparse
import datetime
import sys
import time
from pathlib import Path

from . import __version__, anneal, geometry, netlist, pcb, placement, router
from .grid import Grid
from .rules import load_rules


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
        self.log_file = open(log_path, "w") if log_path else None

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
        msg = (f"routing {done}/{total}  routed={routed} failed={unrouted}")
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
                  accept) -> None:
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
        """
        msg = (f"anneal {it}/{total}  T={temp:5.2f}  E={energy:7.1f}  "
               f"best={best:7.1f}  acc={accept*100:3.0f}%  "
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

    def placing(self, it, total, energy, best, temp, accept) -> None:
        """Report a placement-annealing iteration.

        Args:
            it: iterations completed.
            total: nominal iteration count (for the progress fraction).
            energy: current placement energy.
            best: best energy seen so far.
            temp: current annealing temperature.
            accept: fraction of recent moves accepted (0..1); falls as T cools.
        """
        msg = (f"place {it}/{total}  T={temp:5.2f}  E={energy:8.1f}  "
               f"best={best:8.1f}  acc={accept*100:3.0f}%")
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
                f"compact wt {args.place_compact_weight})")
        rep.log(f"place temps    {args.place_temps[0]} -> {args.place_temps[1]}")
        rep.log(f"place step     {args.place_step} mm, rotate {args.place_rotate}")
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
    if snap_n:
        rep.log(f"snapshots      {snap_n}")
    rep.log(f"copper layers  {', '.join(board.copper_layers)}")
    rep.log(f"pads           {len(board.pads)}")
    rep.log(f"connections    {len(conns)}")
    rep.log("-" * 64)


def run(args: argparse.Namespace) -> int:
    """Execute the full pipeline: parse -> grid -> route -> (anneal) -> write.

    Writes the routed board, optional snapshots and log, runs the clearance
    self-check, and prints the metrics report (including the run's wall-clock
    and CPU time, also mirrored to the log).

    Args:
        args: the parsed CLI namespace (see `build_parser`).

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
    print(f"PyAutoRoute {__version__}")

    rep.phase("parsing board + rules")
    board = pcb.load_board(input_path)
    rules = load_rules(pro_path)
    pitch = args.grid if args.grid else default_pitch(rules)

    if args.place or args.place_only:
        rep.phase(f"placing {len(board.footprints)} footprints (annealing)")
        if args.place_buffer is None:
            args.place_buffer = default_place_buffer(rules)
        pp = placement.PlaceParams(
            iters=args.place_iters, time_budget=args.place_time, seed=args.seed,
            exclude=args.exclude_net, overlap_weight=args.place_overlap_weight,
            compact_weight=args.place_compact_weight, buffer=args.place_buffer,
            t_start=args.place_temps[0], t_end=args.place_temps[1],
            step=args.place_step, rotate_mode=args.place_rotate)
        pout = placement.place(board, pp, on_progress=rep.placing)
        pcb.apply_placement(board, margin=args.place_margin)
        pcb.sync_tree_from_placement(board)
        rep.done()
        summary = (f"place: {pout.iterations} iters, "
                   f"{pout.accepted} accepted ({pout.accept_ratio*100:.0f}%), "
                   f"{pout.moved} moved, energy "
                   f"{pout.start_energy:.1f} -> {pout.best_energy:.1f}")
        breakdown = (f"placement: ratsnest {pout.final_ratsnest:.1f} mm, "
                     f"overlap {pout.final_overlap:.1f} mm2, "
                     f"bbox {pout.final_bbox:.0f} mm2")
        rep.log(summary)
        rep.log(breakdown)
        if not args.quiet:
            print(f"\n  {summary}")
            print(f"  {breakdown}")

    if args.place_only:
        rep.phase("writing placed board")
        pcb.write_board(board, out_path, new_nodes=None, strip_free_vias=True)
        placed_board = pcb.load_board(out_path)
        violations = geometry.clearance_violations(placed_board, rules)
        rep.done()
        _report_placed(rep, out_path, board, violations)
        return _finish(rep, args, out_path, placed_board, violations)

    rep.phase("building netlist (MST rats-nest)")
    conns = netlist.build_connections(board, exclude=args.exclude_net)
    excluded = sorted({p.net for p in board.pads if p.net
                       and netlist.is_excluded(p.net, args.exclude_net)})

    rep.phase(f"building {pitch}mm routing grid")
    grid = Grid(board, rules, pitch)

    # snapshots only make sense during annealing
    snap_n = args.snapshots
    if snap_n and not (args.iters or args.time_budget):
        print("  note: --snapshots needs --iters or --time (annealing); ignoring")
        snap_n = 0
    snap_dir = out_path.parent / "snapshots" if snap_n else None
    if snap_dir is not None:
        snap_dir.mkdir(parents=True, exist_ok=True)

    def on_snapshot(k, n, results):
        sp = snap_dir / f"{input_path.stem}_anneal_{k:02d}of{n:02d}.kicad_pcb"
        pcb.write_board(board, sp, new_nodes=_results_to_nodes(board, grid, results),
                        strip_free_vias=True)
        nrouted = sum(1 for r in results if r is not None)
        rep.log(f"snapshot {k}/{n} -> {sp}  routed={nrouted}/{len(results)}")

    _log_params(rep, args, input_path, out_path, pro_path, pitch,
                board, conns, grid, snap_n)

    note = coarse_grid_note(pitch, default_pitch(rules))
    if note:
        print(f"  warning: {note}")
        rep.log(f"warning: {note}")

    params = router.RouteParams(via_cost=args.via_weight)
    order = netlist.greedy_order(conns)

    rep.phase(f"routing {len(conns)} connections")
    state = router.RoutingState(grid)
    result = router.route_all(state, conns, order, params, on_progress=rep.routing)
    rep.done()

    final_results = result.results
    routed, unrouted, length, vias = (result.routed, result.unrouted,
                                      result.total_length, result.total_vias)

    if args.iters or args.time_budget:
        rep.phase("annealing (rip-up & reroute)")
        ap = anneal.AnnealParams(iters=args.iters, time_budget=args.time_budget,
                                 seed=args.seed, snapshots=snap_n,
                                 unrouted_weight=args.unrouted_weight,
                                 t_start=args.anneal_temps[0], t_end=args.anneal_temps[1],
                                 route_params=params)
        aout = anneal.anneal(state, conns, list(result.results), ap,
                             on_progress=rep.annealing,
                             on_snapshot=on_snapshot if snap_n else None)
        rep.done()
        final_results = aout.results
        routed, unrouted, length, vias = (aout.routed, aout.unrouted,
                                          aout.total_length, aout.total_vias)
        acc_pct = 100 * aout.accepted / max(aout.iterations, 1)
        summary = (f"anneal: {aout.iterations} iters, "
                   f"{aout.accepted} accepted ({acc_pct:.0f}%), "
                   f"energy {aout.start_energy:.1f} -> {aout.best_energy:.1f}")
        rep.log(summary)
        if not args.quiet:
            print(f"\n  {summary}")
        if snap_n:
            print(f"  snapshots:     {snap_n} written to {snap_dir}/")

    rep.phase("writing routed board")
    pcb.write_board(board, out_path,
                    new_nodes=_results_to_nodes(board, grid, final_results),
                    strip_free_vias=True)

    # reload the written board and self-check clearances
    routed_board = pcb.load_board(out_path)
    violations = geometry.clearance_violations(routed_board, rules)
    rep.done()

    _report(rep, out_path, len(conns), routed, unrouted, length, vias,
            violations, excluded)
    return _finish(rep, args, out_path, routed_board, violations)


def _finish(rep: Reporter, args, out_path: Path, board, violations) -> int:
    """Render the optional debug plot, report timing, and close the log.

    Shared tail of both the routing and place-only paths.

    Args:
        rep: the reporter.
        args: the parsed CLI namespace.
        out_path: the output board path (basis for the plot/log names).
        board: the reloaded output board (rendered when ``--debug-plot``).
        violations: the self-check violations (empty == clean) for the exit code.

    Returns:
        Process exit code: 0 if `violations` is empty, else 2.
    """
    if args.debug_plot:
        from . import visualize
        plot_path = str(out_path.with_suffix(".png"))
        visualize.render(board, plot_path, title=out_path.name)
        if not args.quiet:
            print(f"  debug plot:    {plot_path}")
        rep.log(f"debug plot -> {plot_path}")

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
    p.add_argument("--pro", help="project .kicad_pro (default: sibling)")
    p.add_argument("-o", "--output", help="output .kicad_pcb (default: INPUT_routed, "
                                          "or _placed_routed / _placed when placing)")
    p.add_argument("--grid", type=float, help="grid pitch in mm (default derived from rules)")
    p.add_argument("--place", action="store_true",
                   help="experimental: place footprints (simulated annealing) before "
                        "routing — honours locked footprints and the Autoroute=overlap "
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
    g = p.add_mutually_exclusive_group()
    g.add_argument("--iters", type=int, help="optimisation iteration budget")
    g.add_argument("--time", type=float, dest="time_budget", help="optimisation time budget (s)")
    p.add_argument("--exclude-net", action="append", default=[], metavar="PATTERN",
                   help="net name/glob to leave un-routed (repeatable)")
    p.add_argument("--seed", type=int, default=0, help="random seed")
    p.add_argument("--via-weight", type=float, default=2.0, help="via cost (mm-equiv)")
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
    p.add_argument("--debug-plot", action="store_true", help="write a PNG render")
    p.add_argument("--quiet", action="store_true", help="suppress live progress display")
    return p


def main(argv=None) -> int:
    """CLI entry point: parse arguments, validate them, and run the router.

    Args:
        argv: argument list to parse; `None` uses ``sys.argv``.

    Returns:
        The process exit code from `run` (0 clean, 2 on a self-check violation).
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    t_start, t_end = args.anneal_temps
    if not (t_start > t_end > 0):
        parser.error("--anneal-temps requires START > END > 0")
    if args.unrouted_weight < 0:
        parser.error("--unrouted-weight must be >= 0")
    if args.place_margin < 0:
        parser.error("--place-margin must be >= 0")
    if args.place_buffer is not None and args.place_buffer < 0:
        parser.error("--place-buffer must be >= 0")
    pt_start, pt_end = args.place_temps
    if not (pt_start > pt_end > 0):
        parser.error("--place-temps requires START > END > 0")
    if args.place_step <= 0:
        parser.error("--place-step must be > 0")
    if (args.place_iters or args.place_time) and not (args.place or args.place_only):
        parser.error("--place-iters/--place-time require --place or --place-only")
    if args.place_only and (args.iters or args.time_budget):
        parser.error("--place-only does not route; drop --iters/--time")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
