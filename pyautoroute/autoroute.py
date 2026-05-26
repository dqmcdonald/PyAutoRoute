"""Command-line entry point: parse a board, route it, write a routed copy.

    python -m pyautoroute.autoroute INPUT.kicad_pcb [options]

Orchestrates parse -> grid build -> route -> write, prints a live text progress
display (unless --quiet), reports metrics, and runs an in-repo clearance
self-check on the result.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import anneal, geometry, netlist, pcb, router
from .grid import Grid
from .rules import load_rules


class Reporter:
    """Live single-line progress display on a TTY; quiet/plain otherwise."""

    def __init__(self, stream=sys.stderr, quiet: bool = False):
        self.stream = stream
        self.quiet = quiet
        self.tty = (not quiet) and hasattr(stream, "isatty") and stream.isatty()
        self._t0 = time.time()

    def phase(self, name: str) -> None:
        if self.quiet:
            return
        self._write(f"[{self._elapsed():6.1f}s] {name} ...")
        if not self.tty:
            self.stream.write("\n")

    def routing(self, done: int, total: int, routed: int, unrouted: int) -> None:
        if self.quiet:
            return
        msg = (f"[{self._elapsed():6.1f}s] routing {done}/{total}  "
               f"routed={routed} failed={unrouted}")
        if self.tty:
            self._write(msg)
        elif done == total or done % 10 == 0:
            self.stream.write(msg + "\n")

    def annealing(self, it, total, routed, unrouted, energy, best, temp) -> None:
        if self.quiet:
            return
        msg = (f"[{self._elapsed():6.1f}s] anneal {it}/{total}  T={temp:5.2f}  "
               f"E={energy:7.1f}  best={best:7.1f}  routed={routed} failed={unrouted}")
        if self.tty:
            self._write(msg)
        elif it % 25 == 0:
            self.stream.write(msg + "\n")

    def done(self) -> None:
        if self.tty:
            self.stream.write("\n")
            self.stream.flush()

    def _elapsed(self) -> float:
        return time.time() - self._t0

    def _write(self, msg: str) -> None:
        if self.tty:
            self.stream.write("\r\033[K" + msg)
            self.stream.flush()
        else:
            self.stream.write(msg + "\n")


def default_output(input_path: Path) -> Path:
    return input_path.with_name(input_path.stem + "_routed" + input_path.suffix)


def default_pro(input_path: Path) -> Path:
    return input_path.with_suffix(".kicad_pro")


def default_pitch(rules) -> float:
    dc = rules.default_class
    return round(dc.track_width / 2.0 + dc.clearance, 4)


def run(args: argparse.Namespace) -> int:
    rep = Reporter(quiet=args.quiet)
    input_path = Path(args.input)
    out_path = Path(args.output) if args.output else default_output(input_path)
    pro_path = Path(args.pro) if args.pro else default_pro(input_path)

    rep.phase("parsing board + rules")
    board = pcb.load_board(input_path)
    rules = load_rules(pro_path)
    pitch = args.grid if args.grid else default_pitch(rules)

    rep.phase("building netlist (MST rats-nest)")
    conns = netlist.build_connections(board, exclude=args.exclude_net)
    excluded = sorted({p.net for p in board.pads if p.net
                       and netlist.is_excluded(p.net, args.exclude_net)})

    rep.phase(f"building {pitch}mm routing grid")
    grid = Grid(board, rules, pitch)

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
                                 seed=args.seed, route_params=params)
        aout = anneal.anneal(state, conns, list(result.results), ap,
                             on_progress=rep.annealing)
        rep.done()
        final_results = aout.results
        routed, unrouted, length, vias = (aout.routed, aout.unrouted,
                                          aout.total_length, aout.total_vias)
        if not args.quiet:
            print(f"\n  anneal: {aout.iterations} iters, {aout.accepted} accepted, "
                  f"energy {aout.start_energy:.1f} -> {aout.best_energy:.1f}")

    rep.phase("writing routed board")
    nodes = []
    for res in final_results:
        if res is not None:
            nodes += router.path_to_nodes(board, grid, res)
    pcb.write_board(board, out_path, new_nodes=nodes, strip_free_vias=True)

    # reload the written board and self-check clearances
    routed_board = pcb.load_board(out_path)
    violations = geometry.clearance_violations(routed_board, rules)
    rep.done()

    _report(out_path, len(conns), routed, unrouted, length, vias,
            violations, excluded)

    if args.debug_plot:
        from . import visualize
        plot_path = str(out_path.with_suffix(".png"))
        visualize.render(routed_board, plot_path, title=out_path.name)
        if not args.quiet:
            print(f"  debug plot:    {plot_path}")

    return 0 if not violations else 2


def _report(out_path, n_conns, routed, unrouted, length, vias,
            violations, excluded) -> None:
    pct = 100.0 * routed / n_conns if n_conns else 100.0
    print()
    print(f"  output:        {out_path}")
    print(f"  connections:   {routed}/{n_conns} routed ({pct:.0f}%)")
    print(f"  unrouted:      {unrouted}  (reported, not drawn)")
    print(f"  wirelength:    {length:.1f} mm")
    print(f"  vias:          {vias}")
    if excluded:
        print(f"  excluded nets: {len(excluded)} ({', '.join(excluded[:6])}"
              f"{' ...' if len(excluded) > 6 else ''})")
    if violations:
        print(f"  SELF-CHECK:    {len(violations)} clearance violation(s)! "
              f"e.g. {violations[0]}")
    else:
        print(f"  self-check:    clean (0 clearance violations)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pyautoroute",
        description="Autoroute a 2-layer KiCad PCB (writes a routed copy).")
    p.add_argument("input", help="input .kicad_pcb")
    p.add_argument("--pro", help="project .kicad_pro (default: sibling)")
    p.add_argument("-o", "--output", help="output .kicad_pcb (default: INPUT_routed.kicad_pcb)")
    p.add_argument("--grid", type=float, help="grid pitch in mm (default derived from rules)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--iters", type=int, help="optimisation iteration budget")
    g.add_argument("--time", type=float, dest="time_budget", help="optimisation time budget (s)")
    p.add_argument("--exclude-net", action="append", default=[], metavar="PATTERN",
                   help="net name/glob to leave un-routed (repeatable)")
    p.add_argument("--seed", type=int, default=0, help="random seed")
    p.add_argument("--via-weight", type=float, default=2.0, help="via cost (mm-equiv)")
    p.add_argument("--debug-plot", action="store_true", help="write a PNG render")
    p.add_argument("--quiet", action="store_true", help="suppress live progress display")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
