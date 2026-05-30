"""Shared place→route→score *cycle* unit for the CLI and GUI.

A **cycle** is one self-contained attempt at a board: load a fresh copy, place
its footprints, route the result, and score it by the **routed** outcome —
``(unrouted_connections, routed_energy)`` — rather than by placement energy
alone. `run_cycle` is the single implementation of that unit; `select_best`
keeps the lowest-scoring cycle of several.

Running ``--cycles N`` independent cycles and keeping the best routed one selects
placements on how they *actually* route (more vias, or nets left incomplete),
which a placement-energy proxy can't see. Because a cycle re-loads the board from
disk it carries no state between attempts, and `run_cycle` binds no `Reporter` or
Tk objects, so it is picklable and runs unchanged in a `ProcessPoolExecutor`
worker (parallel cycles) or inline (sequential), and backs both the CLI and the
GUI worker — removing the orchestration that used to be duplicated between them.

The routing half of a cycle (`_route_one_run`, greedy route + optional anneal)
also backs the older ``--runs`` best-of-N routing loop in `autoroute`, which
imports it from here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from . import anneal, netlist, pcb, placement, router
from .grid import Grid


def _route_one_run(grid, conns, order, params, run_idx, *, annealing,
                   iters, time_budget, seed, unrouted_weight, anneal_temps,
                   via_weight, on_route_progress=None, on_anneal_progress=None,
                   on_snapshot=None, snapshots=0, cancel=None):
    """Run one independent route (+ optional anneal) and return its outcome.

    This is the shared body of the sequential and parallel best-of-N routing
    paths and of a `run_cycle`'s routing half. It builds a fresh `RoutingState`
    over `grid` (which it never mutates), greedily routes, then optionally anneals
    with seed ``seed + run_idx`` — matching the sequential loop's seeding exactly.

    Args:
        grid: the (read-only) routing grid; each run gets its own `RoutingState`.
        conns: the connection list.
        order: the greedy routing order.
        params: the `RouteParams`.
        run_idx: zero-based run index; offsets the anneal seed.
        annealing: whether to run the rip-up/reroute annealer.
        iters: anneal iteration budget.
        time_budget: anneal time budget (s).
        seed: base seed (the anneal seed is ``seed + run_idx``).
        unrouted_weight: anneal unrouted-connection penalty.
        anneal_temps: (start, end) anneal temperatures.
        via_weight: via cost used for the non-annealing energy.
        on_route_progress: optional greedy-route progress callback.
        on_anneal_progress: optional anneal progress callback.
        on_snapshot: optional snapshot callback (single-run only).
        snapshots: number of snapshots to emit during annealing.
        cancel: optional cancellation `Event`.

    Returns:
        A dict ``{energy, results, metrics, summary}`` where ``metrics`` is
        ``(routed, unrouted, length, vias)`` and ``summary`` is a log line (or
        ``None``). All values are picklable so the dict can cross a process
        boundary.
    """
    state = router.RoutingState(grid)
    result = router.route_all(state, conns, order, params,
                              on_progress=on_route_progress)
    run_results = result.results
    run_metrics = (result.routed, result.unrouted,
                   result.total_length, result.total_vias)
    summary = None

    if annealing:
        ap = anneal.AnnealParams(iters=iters, time_budget=time_budget,
                                 seed=seed + run_idx, snapshots=snapshots,
                                 unrouted_weight=unrouted_weight,
                                 t_start=anneal_temps[0], t_end=anneal_temps[1],
                                 route_params=params)
        aout = anneal.anneal(state, conns, list(result.results), ap,
                             on_progress=on_anneal_progress,
                             on_snapshot=on_snapshot, cancel=cancel)
        run_results = aout.results
        run_metrics = (aout.routed, aout.unrouted,
                       aout.total_length, aout.total_vias)
        run_energy = aout.best_energy
        acc_pct = 100 * aout.accepted / max(aout.iterations, 1)
        summary = (f"anneal: {aout.iterations} iters, "
                   f"{aout.accepted} accepted ({acc_pct:.0f}%), "
                   f"energy {aout.start_energy:.1f} -> {aout.best_energy:.1f}")
    else:
        run_energy = anneal._energy(run_results, via_weight, unrouted_weight)

    return {"energy": run_energy, "results": run_results,
            "metrics": run_metrics, "summary": summary}


def _route_run_worker(payload):
    """Picklable `ProcessPoolExecutor` entry point for one routing run.

    Unpacks a ``(grid, conns, order, params, run_idx, kwargs)`` tuple and calls
    `_route_one_run` with all progress callbacks suppressed (per-run live
    progress does not interleave cleanly across processes).

    Args:
        payload: the packed argument tuple.

    Returns:
        The result dict from `_route_one_run`.
    """
    grid, conns, order, params, run_idx, kw = payload
    return _route_one_run(grid, conns, order, params, run_idx, **kw)


@dataclass
class CycleResult:
    """Outcome of one place→route→score cycle.

    Attributes:
        seed: the cycle's seed (placement and routing both keyed off it).
        board: the placed board, with the placement synced into its tree and the
            outline finalised — ready to write once routing nodes are attached.
        grid: the routing grid built over `board` (node ↔ coordinate mapping,
            needed to flatten `results` into KiCad nodes).
        n_conns: number of connections routed (the rats-nest size).
        results: per-connection `router.RouteResult` (or ``None`` for unrouted).
        routed: connections completed.
        unrouted: connections left open.
        length: total routed track length (mm).
        vias: total vias placed.
        energy: routed energy (``length + via_weight·vias + unrouted_weight·unrouted``).
        summary: the annealing summary line, or ``None`` when not annealed.

    All fields are picklable, so a worker process can return the whole result.
    """
    seed: int
    board: object
    grid: object
    n_conns: int
    results: list
    routed: int
    unrouted: int
    length: float
    vias: int
    energy: float
    summary: str | None

    @property
    def score(self) -> tuple[int, float]:
        """Selection key: fewest unrouted first, then lowest routed energy.

        Lexicographic, so completing connections always dominates — a placement
        that routes one more net beats any that routes fewer, regardless of
        length/vias; ties break on energy.
        """
        return (self.unrouted, self.energy)


def run_cycle(input_path, rules, pitch: float, place_params, route_params, *,
              route_kw: dict, place_margin: float, seed: int,
              cancel=None, hooks=None) -> CycleResult:
    """Place and route a fresh copy of a board once, and score the result.

    Loads `input_path` anew (so no placement/routing state leaks between cycles),
    places it with ``replace(place_params, seed=seed)`` — a single placement run —
    finalises the placement into the board tree, builds the routing grid, then
    routes (with optional annealing) via `_route_one_run` keyed off the same
    `seed`. The returned `CycleResult` is ready to write and carries the routed
    score.

    Args:
        input_path: the source ``.kicad_pcb`` path (re-read each call).
        rules: the parsed design rules (for the grid).
        pitch: routing-grid pitch in mm.
        place_params: base `placement.PlaceParams`; the seed is overridden with
            `seed`, the placement runs once.
        route_params: the `router.RouteParams`.
        route_kw: keyword dict forwarded to `_route_one_run` (``annealing``,
            ``iters``, ``time_budget``, ``unrouted_weight``, ``anneal_temps``,
            ``via_weight``); its own ``seed`` is supplied here.
        place_margin: outline margin (mm) for `pcb.apply_placement`.
        seed: this cycle's seed (placement and routing).
        cancel: optional cancellation `Event`, threaded into place and route.
        hooks: optional `CycleHooks` for live progress; ``None`` suppresses it
            (the parallel-worker and headless default).

    Returns:
        The `CycleResult` for this cycle.
    """
    h = hooks or CycleHooks()
    board = pcb.load_board(input_path)

    pp = replace(place_params, seed=seed)
    h.phase(f"placing {len(board.footprints)} footprints")
    placement.place(board, pp, on_progress=h.place_progress, cancel=cancel)
    kept = pcb.apply_placement(board, margin=place_margin,
                               keep_outline=pp.keep_outline)
    pcb.sync_tree_from_placement(board, keep_outline=kept)
    h.board_snap(board)

    grid = Grid(board, rules, pitch)
    conns = netlist.build_connections(board, exclude=pp.exclude)
    order = netlist.greedy_order(conns)

    h.phase(f"routing {len(conns)} connections")
    out = _route_one_run(grid, conns, order, route_params, 0, seed=seed,
                         on_route_progress=h.route_progress,
                         on_anneal_progress=h.anneal_progress,
                         cancel=cancel, **route_kw)
    routed, unrouted, length, vias = out["metrics"]
    return CycleResult(seed=seed, board=board, grid=grid, n_conns=len(conns),
                       results=out["results"], routed=routed, unrouted=unrouted,
                       length=length, vias=vias, energy=out["energy"],
                       summary=out["summary"])


def _cycle_worker(payload):
    """Picklable `ProcessPoolExecutor` entry point for one cycle.

    Unpacks ``(input_path, rules, pitch, place_params, route_params, route_kw,
    place_margin, seed)`` and calls `run_cycle` with progress suppressed (live
    progress does not interleave cleanly across processes).

    Args:
        payload: the packed argument tuple.

    Returns:
        The `CycleResult` for the cycle.
    """
    (input_path, rules, pitch, place_params, route_params,
     route_kw, place_margin, seed) = payload
    return run_cycle(input_path, rules, pitch, place_params, route_params,
                     route_kw=route_kw, place_margin=place_margin, seed=seed)


def select_best(cycles):
    """Return the lowest-scoring `CycleResult` (see `CycleResult.score`).

    Args:
        cycles: an iterable of `CycleResult`.

    Returns:
        The cycle with the fewest unrouted connections, ties broken by routed
        energy; ``None`` if `cycles` is empty.
    """
    best = None
    for c in cycles:
        if best is None or c.score < best.score:
            best = c
    return best


@dataclass
class CycleHooks:
    """Optional live-progress callbacks for `run_cycle`.

    Every field defaults to ``None`` (a no-op), so a bare ``CycleHooks()`` — or
    ``hooks=None`` — runs the cycle silently, as the parallel workers do. The CLI
    `Reporter` and the GUI worker each build one to surface their own progress.

    Attributes:
        phase: ``f(name)`` — a new phase began (placing / routing …).
        place_progress: placement SA per-iteration callback (see `placement.place`).
        route_progress: greedy-route progress ``f(done, total, routed, unrouted)``.
        anneal_progress: rip-up/reroute per-iteration callback (see `anneal.anneal`).
        board_snap: ``f(board)`` — the placement is finalised (for a live redraw).
    """
    phase_cb: object = None
    place_progress: object = None
    route_progress: object = None
    anneal_progress: object = None
    board_snap_cb: object = None

    def phase(self, name: str) -> None:
        if self.phase_cb is not None:
            self.phase_cb(name)

    def board_snap(self, board) -> None:
        if self.board_snap_cb is not None:
            self.board_snap_cb(board)
