"""Shared place→route orchestration for the CLI and GUI.

Two shared units, both binding no `Reporter` or Tk objects (live progress flows
through optional, no-op-by-default hook callbacks) so the same code backs the
command line and the GUI worker, removing the orchestration that used to be
duplicated between them:

- `run_pipeline` — the **full** place(best-of-`place_runs`) → route(best-of-`runs`,
  optionally across `--jobs` worker processes) → select-best pipeline, driven by
  `PipelineHooks`. This is what `autoroute.run` and the GUI worker share.
- `run_cycle` — one self-contained **cycle** (fresh `load_board`, one placement,
  one routing) scored by the **routed** outcome ``(unrouted, routed_energy)``;
  `select_best` keeps the lowest. Picklable, so ``--cycles N`` runs independent
  cycles inline or in a `ProcessPoolExecutor` and keeps the one that actually
  routes best — a signal a placement-energy proxy can't see.

The routing half of both (`_route_one_run`, greedy route + optional anneal) also
backs the older ``--runs`` best-of-N routing loop, and is dispatched to worker
processes via `_route_run_worker`.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, replace

from . import anneal, netlist, pcb, placement, router
from .grid import Grid


def _call(cb, *args):
    """Invoke an optional hook callback (no-op when ``cb`` is ``None``)."""
    if cb is not None:
        cb(*args)


def _anneal_summary(aout) -> str:
    """Build the one-line annealing summary, with a diagnostic note when the
    routing appears already optimal (every reroute produces equal energy)."""
    acc_pct = 100 * aout.accepted / max(aout.iterations, 1)
    note = ""
    if acc_pct >= 99 and aout.best_energy >= aout.start_energy - 1e-6:
        note = "  ← routing appears already optimal (no reroute improved energy)"
    return (f"anneal: {aout.iterations} iters, "
            f"{aout.accepted} accepted ({acc_pct:.0f}%), "
            f"energy {aout.start_energy:.1f} -> {aout.best_energy:.1f}{note}")


def _route_one_run(grid, conns, order, params, run_idx, *, annealing,
                   iters, time_budget, seed, unrouted_weight, anneal_temps,
                   via_weight, stall_patience=0, stall_ratio=0.02, flat_window=0,
                   on_route_progress=None, on_anneal_progress=None,
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
        A dict ``{energy, results, metrics, summary, iters}`` where ``metrics``
        is ``(routed, unrouted, length, vias)``, ``summary`` is a log line (or
        ``None``), and ``iters`` is the number of annealing iterations completed
        (0 when not annealing). All values are picklable so the dict can cross
        a process boundary.
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
                                 route_params=params,
                                 stall_patience=stall_patience,
                                 stall_ratio=stall_ratio,
                                 flat_window=flat_window)
        aout = anneal.anneal(state, conns, list(result.results), ap,
                             on_progress=on_anneal_progress,
                             on_snapshot=on_snapshot, cancel=cancel)
        run_results = aout.results
        run_metrics = (aout.routed, aout.unrouted,
                       aout.total_length, aout.total_vias)
        run_energy = aout.best_energy
        run_iters = aout.iterations
        summary = _anneal_summary(aout)
    else:
        run_energy = anneal._energy(run_results, via_weight, unrouted_weight)
        run_iters = 0

    return {"energy": run_energy, "results": run_results,
            "metrics": run_metrics, "summary": summary, "iters": run_iters}


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
        conns: the connection list (parallel to `results`); carried so congestion
            feedback (``--place-feedback``) can find the endpoints of the
            connections left unrouted.
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
    conns: list
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
                       conns=conns, results=out["results"], routed=routed,
                       unrouted=unrouted, length=length, vias=vias,
                       energy=out["energy"], summary=out["summary"])


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


@dataclass
class PipelineHooks:
    """Optional live-progress callbacks for `run_pipeline`.

    Every field defaults to ``None`` (a no-op), so ``hooks=None`` runs the whole
    pipeline silently. The CLI builds one backed by its `Reporter`; the GUI worker
    builds one that posts events to its queue. Callbacks that need to render the
    board are handed the live ``board`` and ``grid`` so the caller can snapshot
    them without re-deriving anything.

    Attributes:
        phase: ``f(name)`` — a new phase began (placing / routing / annealing …).
        place_run: ``f(k, n)`` — placement run ``k`` of ``n`` (0-based) started.
        place_progress: ``f(it, total, energy, best, temp, accept, overall_best)``
            per placement iteration (``overall_best`` is ``None`` when one run).
        placed: ``f(board)`` — placement finalised into the board (ready to draw).
        route_run: ``f(k, n)`` — routing run ``k`` of ``n`` (0-based) started.
        route_progress: ``f(done, total, routed, unrouted)`` greedy-route progress.
        route_partial: ``f(board, grid, partial_results)`` — partial routing as it
            builds (for a live canvas); fired per connection.
        anneal_progress: ``f(it, total, r, u, energy, best, temp, accept, overall_best)``
            per rip-up/reroute iteration.
        anneal_snapshot: ``f(board, grid, results, k, n)`` — an annealing snapshot.
        anneal_best: ``f(board, grid, results)`` — a new best routing during anneal.
        route_run_done: ``f(k, n, energy, summary, metrics, is_best, iters)`` — a
            routing run (or a parallel worker, ``k`` = completion index) finished;
            ``is_best`` is ``True`` when this run set a new overall-best energy;
            ``iters`` is the number of annealing iterations completed (0 if
            no annealing was run).
        overall_best: ``f(board, grid, results, energy)`` — a new lowest-energy
            routing across runs was adopted.
    """
    phase: object = None
    place_run: object = None
    place_progress: object = None
    placed: object = None
    route_run: object = None
    route_progress: object = None
    route_partial: object = None
    anneal_progress: object = None
    anneal_snapshot: object = None
    anneal_best: object = None
    route_run_done: object = None
    overall_best: object = None


@dataclass
class PipelineResult:
    """Outcome of `run_pipeline`: the placed+routed board and its metrics.

    Attributes:
        board: the placed (and, unless ``placed_only``, routed-ready) board.
        grid: the routing grid, or ``None`` when ``placed_only``.
        n_conns: number of connections (0 when ``placed_only``).
        results: per-connection results, or ``None`` when ``placed_only``.
        routed/unrouted/length/vias: routed metrics (0 when ``placed_only``).
        energy: best routed energy (``inf`` when ``placed_only`` or nothing routed).
        place_stats: the `placement.PlaceResult` (for reporting), or ``None``.
        placed_only: whether routing was skipped (``--place-only``).
        cancelled: whether the run was cancelled before any routing completed.
    """
    board: object
    grid: object
    n_conns: int
    results: object
    routed: int
    unrouted: int
    length: float
    vias: int
    energy: float
    place_stats: object
    placed_only: bool
    cancelled: bool = False


def run_placement(board, *, place_params, place_runs: int, seed: int,
                  place_margin: float, hooks=None, cancel=None):
    """Place a board's footprints (best-of-`place_runs`) and finalise the layout.

    Runs `placement.place(runs=place_runs)` with seed `seed`, then
    `pcb.apply_placement` + `pcb.sync_tree_from_placement` so the board carries the
    chosen placement (outline regenerated, or kept under ``keep_outline``) and is
    ready to route or write. Mutates `board` in place. Live progress — including
    per-run boundaries and the running overall-best — flows through `hooks`.

    Args:
        board: the parsed board, mutated in place.
        place_params: base `placement.PlaceParams` (seed overridden with `seed`).
        place_runs: number of placement runs (best by placement energy is kept).
        seed: placement seed.
        place_margin: outline margin (mm) for `pcb.apply_placement`.
        hooks: optional `PipelineHooks`.
        cancel: optional cancellation `Event`.

    Returns:
        The `placement.PlaceResult` (for reporting).
    """
    h = hooks or PipelineHooks()
    n_fps = len(board.footprints)
    prog = {"run": 0, "last_it": -1, "ob": float("inf")}

    def on_place(it, total, energy, best, temp, accept):
        if prog["last_it"] >= 0 and it < prog["last_it"]:
            prog["run"] += 1
            _call(h.place_run, prog["run"], place_runs)
        prog["last_it"] = it
        if best < prog["ob"]:
            prog["ob"] = best
        ob = prog["ob"] if place_runs > 1 else None
        _call(h.place_progress, it, total, energy, best, temp, accept, ob)

    _call(h.phase, f"placing {n_fps} footprints")
    _call(h.place_run, 0, place_runs)
    place_stats = placement.place(board, replace(place_params, seed=seed),
                                  on_progress=on_place, runs=place_runs,
                                  cancel=cancel)
    kept = pcb.apply_placement(board, margin=place_margin,
                               keep_outline=place_params.keep_outline)
    pcb.sync_tree_from_placement(board, keep_outline=kept)
    _call(h.placed, board)
    return place_stats


def run_routing(board, rules, pitch: float, *, route_params, route_kw: dict,
                seed: int, runs: int, jobs: int, snapshots: int, exclude,
                grid=None, conns=None, order=None,
                hooks=None, cancel=None) -> PipelineResult:
    """Route a (placed) board best-of-`runs` and keep the lowest-energy result.

    Builds the connections, greedy order, and grid over `board` (unless passed in
    pre-built — the CLI builds them early for its parameter dump), then routes
    `runs` times (each greedy route + optional anneal). With ``runs > 1`` and
    ``jobs > 1`` the runs are dispatched across a `ProcessPoolExecutor` (progress
    suppressed); otherwise they run sequentially with live `hooks`. Selection —
    lowest routed energy — matches both paths.

    Args:
        board: the placed board to route.
        rules: the design rules (for the grid).
        pitch: routing-grid pitch (mm).
        route_params: the `router.RouteParams`.
        route_kw: ``annealing``/``iters``/``time_budget``/``unrouted_weight``/
            ``anneal_temps``/``via_weight`` (the seed is the separate `seed` arg).
        seed: base seed (routing run ``k`` keys off ``seed + k``).
        runs: number of routing runs (best kept).
        jobs: worker processes for parallel routing (``> 1`` enables it).
        snapshots: annealing snapshot count (single-run only; 0 disables).
        exclude: net patterns to leave unrouted.
        grid: optional pre-built routing grid (built here, with a phase event, if
            ``None``).
        conns: optional pre-built connection list (built here if ``None``).
        order: optional pre-built greedy order (built here if ``None``).
        hooks: optional `PipelineHooks`.
        cancel: optional cancellation `Event`.

    Returns:
        The `PipelineResult` (``place_stats`` is ``None``; the caller fills it).
    """
    h = hooks or PipelineHooks()
    if seed is None:
        import time as _time
        seed = int(_time.time()) & 0x7FFF_FFFF
    if conns is None:
        _call(h.phase, "building netlist (MST rats-nest)")
        conns = netlist.build_connections(board, exclude=exclude or [])
    if order is None:
        order = netlist.greedy_order(conns)
    if grid is None:
        _call(h.phase, f"building {pitch}mm routing grid")
        grid = Grid(board, rules, pitch)
    annealing = route_kw["annealing"]

    best_energy = float("inf")
    final_results = None
    routed = unrouted = length = vias = 0
    parallel = runs > 1 and jobs > 1

    if parallel:
        _call(h.phase, f"routing {len(conns)} connections — "
                       f"{runs} runs across {jobs} workers")
        kw = dict(route_kw, seed=seed)
        payloads = [(grid, conns, order, route_params, k, kw) for k in range(runs)]
        done_n = 0
        try:
            with concurrent.futures.ProcessPoolExecutor(max_workers=jobs) as ex:
                futs = [ex.submit(_route_run_worker, p) for p in payloads]
                for fut in concurrent.futures.as_completed(futs):
                    out = fut.result()
                    is_best = out["energy"] < best_energy
                    if is_best:
                        best_energy = out["energy"]
                        final_results = out["results"]
                        routed, unrouted, length, vias = out["metrics"]
                    _call(h.route_run_done, done_n, runs, out["energy"],
                          out["summary"], out["metrics"], is_best,
                          out.get("iters", 0))
                    done_n += 1
        except KeyboardInterrupt:
            if final_results is None:
                raise
    else:
        for k in range(runs):
            if cancel is not None and cancel.is_set():
                break
            _call(h.route_run, k, runs)
            _call(h.phase, f"routing {len(conns)} connections")
            partial = [None] * len(conns)

            def on_partial(idx, res):
                partial[idx] = res
                _call(h.route_partial, board, grid, partial)

            state = router.RoutingState(grid)
            result = router.route_all(
                state, conns, order, route_params,
                on_progress=h.route_progress,
                on_partial=on_partial if h.route_partial is not None else None)
            run_results = result.results
            run_metrics = (result.routed, result.unrouted,
                           result.total_length, result.total_vias)
            summary = None

            if annealing:
                _call(h.phase, "annealing (rip-up & reroute)")
                ob_e = best_energy if runs > 1 and best_energy < float("inf") else None

                def on_anneal(it, total, r, u, energy, best, temp, accept):
                    _call(h.anneal_progress, it, total, r, u, energy, best, temp,
                          accept, ob_e)

                def on_snap(kk, nn, res):
                    _call(h.anneal_snapshot, board, grid, res, kk, nn)

                def on_best(be, br):
                    _call(h.anneal_best, board, grid, br)

                ap = anneal.AnnealParams(
                    iters=route_kw["iters"], time_budget=route_kw["time_budget"],
                    seed=seed + k, snapshots=snapshots,
                    unrouted_weight=route_kw["unrouted_weight"],
                    t_start=route_kw["anneal_temps"][0],
                    t_end=route_kw["anneal_temps"][1], route_params=route_params,
                    stall_patience=route_kw.get("stall_patience", 0),
                    stall_ratio=route_kw.get("stall_ratio", 0.02),
                    flat_window=route_kw.get("flat_window", 0))
                aout = anneal.anneal(
                    state, conns, list(result.results), ap,
                    on_progress=on_anneal if h.anneal_progress is not None else None,
                    on_snapshot=on_snap if snapshots else None,
                    cancel=cancel,
                    on_best=on_best if h.anneal_best is not None else None)
                run_results = aout.results
                run_metrics = (aout.routed, aout.unrouted,
                               aout.total_length, aout.total_vias)
                run_energy = aout.best_energy
                run_iters = aout.iterations
                summary = _anneal_summary(aout)
            else:
                run_energy = anneal._energy(run_results, route_kw["via_weight"],
                                            route_kw["unrouted_weight"])
                run_iters = 0

            is_best = run_energy < best_energy
            if is_best:
                best_energy = run_energy
                final_results = run_results
                routed, unrouted, length, vias = run_metrics
            _call(h.route_run_done, k, runs, run_energy, summary, run_metrics,
                  is_best, run_iters)
            if is_best:
                _call(h.overall_best, board, grid, final_results, best_energy)

    return PipelineResult(board=board, grid=grid, n_conns=len(conns),
                          results=final_results, routed=routed, unrouted=unrouted,
                          length=length, vias=vias, energy=best_energy,
                          place_stats=None, placed_only=False,
                          cancelled=(final_results is None))


def run_pipeline(board, rules, pitch: float, *, do_place: bool, place_only: bool,
                 place_params, place_runs: int, route_params, route_kw: dict,
                 seed: int, runs: int, jobs: int, snapshots: int, exclude,
                 place_margin: float, hooks=None, cancel=None) -> PipelineResult:
    """Place then route a board — the full shared pipeline (`run_placement` +
    `run_routing`).

    Convenience composer for callers that place and route in one go with no step
    in between (the GUI worker; the cycle unit). `autoroute.run` instead calls
    `run_placement` and `run_routing` separately so it can slot ``--auto`` (grid/
    via probing on the placed board) between them. Mutates `board` in place.

    Args:
        board: the parsed board, mutated in place.
        rules: the design rules.
        pitch: routing-grid pitch (mm).
        do_place: run placement before routing.
        place_only: place and return without routing.
        place_params: base `placement.PlaceParams`.
        place_runs: placement runs (best kept).
        route_params: the `router.RouteParams`.
        route_kw: the routing-run knobs (see `run_routing`).
        seed: base seed.
        runs: routing runs (best kept).
        jobs: worker processes for parallel routing.
        snapshots: annealing snapshot count.
        exclude: net patterns to leave unrouted.
        place_margin: outline margin (mm) for placement finalisation.
        hooks: optional `PipelineHooks`.
        cancel: optional cancellation `Event`.

    Returns:
        The `PipelineResult` (with `place_stats` filled when placement ran).
    """
    place_stats = None
    if do_place or place_only:
        place_stats = run_placement(board, place_params=place_params,
                                    place_runs=place_runs, seed=seed,
                                    place_margin=place_margin, hooks=hooks,
                                    cancel=cancel)
    if place_only:
        return PipelineResult(board=board, grid=None, n_conns=0, results=None,
                              routed=0, unrouted=0, length=0.0, vias=0,
                              energy=float("inf"), place_stats=place_stats,
                              placed_only=True)
    if cancel is not None and cancel.is_set():
        return PipelineResult(board=board, grid=None, n_conns=0, results=None,
                              routed=0, unrouted=0, length=0.0, vias=0,
                              energy=float("inf"), place_stats=place_stats,
                              placed_only=False, cancelled=True)
    res = run_routing(board, rules, pitch, route_params=route_params,
                      route_kw=route_kw, seed=seed, runs=runs, jobs=jobs,
                      snapshots=snapshots, exclude=exclude, hooks=hooks,
                      cancel=cancel)
    res.place_stats = place_stats
    return res
