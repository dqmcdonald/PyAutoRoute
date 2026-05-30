"""Background worker thread: runs the place/route pipeline and posts events.

The worker's only interaction with the UI is to push immutable event objects
onto a ``queue.Queue``.  All board mutations happen here; the snapshot copies
placed in ``BoardSnap`` events are made while the board is in a consistent
state so the main thread can read them safely.

The actual place→route orchestration is the shared `pyautoroute.pipeline`
(`run_placement` / `run_routing`) — the same code the CLI runs — driven here
through a `pipeline.PipelineHooks` whose callbacks translate live progress into
the GUI's ``Phase`` / ``Progress`` / ``BoardSnap`` events (throttled so the UI
queue isn't flooded). This worker only owns parsing, the GUI event translation,
and writing the result.
"""

from __future__ import annotations

import dataclasses
import datetime
import queue
import threading
import time
import traceback
from pathlib import Path

from .events import BoardSnap, Done, Error, Phase, Progress


# Minimum seconds between Progress events posted to the UI queue.
# Keeps the main thread from being overwhelmed by high-iteration-rate callbacks.
_PROGRESS_INTERVAL = 0.125   # ~8 Hz max
# Minimum seconds between board canvas snapshots during greedy routing.
_ROUTE_SNAP_INTERVAL = 1.5
# Routing annealing snapshot count for live board updates.
_ANNEAL_SNAP_COUNT = 40


def _snap_pads(board, margin: float = 2.0):
    """Return a Board copy frozen at a consistent instant for live placement.

    Both ``board.pads`` and ``board.footprints`` are copied so the snapshot is an
    internally consistent moment: ``draw_board`` positions footprint outlines from
    the footprint pose (``fp.x/y/angle``) while pads come from ``board.pads``.  If
    only the pads were frozen, the still-running placer would keep moving the
    shared footprint poses, so the outlines would render offset from their pads.

    The snapshot's outline is also regenerated to bound the current pads (the same
    rectangle ``apply_placement`` produces), so the autoscaled view tracks the
    footprints as they compress instead of staying framed to the original, larger
    board edge.

    Args:
        board: the live board being placed.
        margin: extra space (mm) around the pads when sizing the preview outline;
            pass the placement margin so the preview matches the final outline.
    """
    from pyautoroute import pcb

    pads_copy = [dataclasses.replace(p) for p in board.pads]
    fps_copy = [dataclasses.replace(fp) for fp in board.footprints]
    snap = dataclasses.replace(board, pads=pads_copy, footprints=fps_copy)
    if pads_copy:
        snap.outline = pcb.pad_bounding_outline(pads_copy, margin)
    return snap


class Worker:
    """Runs the pipeline in a daemon thread; cancel via ``cancel_event``."""

    def __init__(self, event_queue: queue.Queue,
                 cancel_event: threading.Event):
        self._q = event_queue
        self._cancel = cancel_event
        self._thread: threading.Thread | None = None

    def start(self, cfg) -> None:
        """Launch the pipeline for the given ``RunConfig`` *cfg*."""
        self._thread = threading.Thread(
            target=self._run, args=(cfg,), daemon=True)
        self._thread.start()

    def join(self, timeout: float = 0) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    # ------------------------------------------------------------------
    def _post(self, event) -> None:
        self._q.put(event)

    def _run(self, cfg) -> None:
        try:
            self._pipeline(cfg)
        except Exception as exc:
            self._post(Error(exc, traceback.format_exc()))

    # ------------------------------------------------------------------
    def _build_hooks(self, cfg, margin, board):
        """Build a `pipeline.PipelineHooks` that posts throttled GUI events.

        The shared `run_placement` / `run_routing` call these at each progress
        point; here they translate into `Phase` / `Progress` / `BoardSnap` events.
        Snapshot copies are taken from the live ``board`` at a consistent instant.

        Args:
            cfg: the run configuration (for budgets shown in the progress events).
            margin: placement preview margin (mm), passed to `_snap_pads`.
            board: the live board the pipeline mutates (snapshotted for the canvas).

        Returns:
            A `pyautoroute.pipeline.PipelineHooks`.
        """
        from pyautoroute import pipeline

        post = self._post
        cancel = self._cancel
        n_fps = len(board.footprints)
        # mutable throttle / timer state shared across the callbacks
        st = {"tag": "", "place_t0": time.monotonic(), "anneal_t0": time.monotonic(),
              "prog": 0.0, "psnap_t": 0.0, "psnap_best": float("inf"),
              "route_prog": 0.0, "route_snap": 0.0, "anneal_prog": 0.0}

        def phase(name):
            if name.startswith("annealing"):
                st["anneal_t0"] = time.monotonic()
            post(Phase(f"{st['tag']}{name}"))

        def place_run(k, n):
            st["tag"] = f"run {k + 1}/{n} " if n > 1 else ""
            st["place_t0"] = time.monotonic()
            if k > 0:                                   # later runs get a new phase
                post(Phase(f"{st['tag']}placing {n_fps} footprints"))

        def place_progress(it, total, energy, best, temp, accept, ob):
            if cancel.is_set():
                return
            now = time.monotonic()
            if now - st["prog"] >= _PROGRESS_INTERVAL:
                st["prog"] = now
                post(Progress("placing", it, total, energy, best, temp, accept,
                              0, 0, elapsed=now - st["place_t0"],
                              budget=cfg.place_time or 0.0))
            new_best = best < st["psnap_best"]
            if now - st["psnap_t"] >= (0.5 if new_best else 2.0):
                if new_best:
                    st["psnap_best"] = best
                st["psnap_t"] = now
                post(BoardSnap(_snap_pads(board, margin),
                               kind="best" if new_best else "current"))
                if new_best:
                    post(BoardSnap(_snap_pads(board, margin), kind="overall_best"))

        def placed(_board):
            post(BoardSnap(_snap_pads(board, margin)))

        def route_run(k, n):
            st["tag"] = f"run {k + 1}/{n} " if n > 1 else ""

        def route_progress(done, total, routed, unrouted):
            now = time.monotonic()
            if now - st["route_prog"] >= _PROGRESS_INTERVAL:
                st["route_prog"] = now
                post(Progress("routing", done, total, 0.0, 0.0, 0.0, 0.0,
                              routed, unrouted))

        def route_partial(b, g, partial):
            now = time.monotonic()
            if now - st["route_snap"] >= _ROUTE_SNAP_INTERVAL:
                st["route_snap"] = now
                post(BoardSnap(dataclasses.replace(b), results=list(partial),
                               grid=g))

        def anneal_progress(it, total, r, u, energy, best, temp, accept, ob):
            now = time.monotonic()
            if now - st["anneal_prog"] >= _PROGRESS_INTERVAL:
                st["anneal_prog"] = now
                post(Progress("annealing", it, total, energy, best, temp, accept,
                              r, u, elapsed=now - st["anneal_t0"],
                              budget=cfg.time_budget or 0.0))

        def anneal_snapshot(b, g, results, k, n):
            post(BoardSnap(dataclasses.replace(b), results=results, grid=g))

        def anneal_best(b, g, results):
            post(BoardSnap(dataclasses.replace(b), results=results, grid=g,
                           kind="best"))

        def overall_best(b, g, results, energy):
            post(BoardSnap(dataclasses.replace(b), results=list(results), grid=g,
                           kind="overall_best"))

        return pipeline.PipelineHooks(
            phase=phase, place_run=place_run, place_progress=place_progress,
            placed=placed, route_run=route_run, route_progress=route_progress,
            route_partial=route_partial, anneal_progress=anneal_progress,
            anneal_snapshot=anneal_snapshot, anneal_best=anneal_best,
            overall_best=overall_best)

    def _pipeline(self, cfg) -> None:
        from pyautoroute import __version__, geometry, pcb, pipeline
        from pyautoroute import placement as place_mod
        from pyautoroute import router
        from pyautoroute.autoroute import (
            _results_to_nodes, default_output, default_pitch,
            default_place_buffer,
        )
        from pyautoroute.rules import load_rules

        input_path = Path(cfg.input)
        place = cfg.place
        place_only = cfg.place_only
        out_path = default_output(input_path, place=place, place_only=place_only)

        self._post(Phase("parsing board + rules"))
        board = pcb.load_board(input_path)
        if getattr(cfg, "fix_values", False):
            pcb.fix_value_layers(board)

        fill_nets = pcb.zone_fill_nets(board)
        if fill_nets:
            current_excludes = list(cfg.exclude_net or [])
            for n in sorted(fill_nets):
                if n not in current_excludes:
                    current_excludes.append(n)
            cfg.exclude_net = current_excludes

        pro_path = (Path(cfg.pro) if cfg.pro
                    else input_path.with_suffix(".kicad_pro"))
        if not pro_path.exists():
            pro_path = input_path.with_name(input_path.stem + ".kicad_pro")
        rules = load_rules(pro_path)
        pitch = cfg.grid if cfg.grid else default_pitch(rules)

        margin = cfg.place_margin
        hooks = self._build_hooks(cfg, margin, board)

        # ---- placement -----------------------------------------------
        if place or place_only:
            buf = (default_place_buffer(rules) if cfg.place_buffer is None
                   else cfg.place_buffer)
            pp = place_mod.PlaceParams(
                iters=cfg.place_iters, time_budget=cfg.place_time, seed=cfg.seed,
                exclude=cfg.exclude_net or [],
                overlap_weight=cfg.place_overlap_weight,
                compact_weight=cfg.place_compact_weight,
                edge_weight=cfg.place_edge_weight,
                keep_outline=getattr(cfg, "keep_outline", False), buffer=buf,
                t_start=cfg.place_temps[0], t_end=cfg.place_temps[1],
                step=cfg.place_step, rotate_mode=cfg.place_rotate)
            pipeline.run_placement(board, place_params=pp,
                                   place_runs=max(1, cfg.place_runs),
                                   seed=cfg.seed, place_margin=margin,
                                   hooks=hooks, cancel=self._cancel)

        if place_only:
            self._post(Phase("writing placed board"))
            pcb.stamp_comment(board,
                f"PyAutoRoute v{__version__} — placed "
                f"{datetime.date.today().isoformat()}")
            pcb.write_board(board, out_path, new_nodes=None, strip_free_vias=True)
            if fill_nets:
                pcb.try_refill_zones(out_path)
            placed = pcb.load_board(out_path)
            violations = geometry.clearance_violations(placed, rules)
            self._post(Done(str(out_path), 0, 0, 0, 0.0, 0, violations, placed))
            return

        if self._cancel.is_set():
            self._post(Phase("cancelled"))
            return

        # ---- routing -------------------------------------------------
        annealing = bool(cfg.iters or cfg.time_budget)
        runs = max(1, cfg.runs)
        if runs > 1 and not annealing:
            runs = 1                                    # greedy is deterministic
        route_params = router.RouteParams(via_cost=cfg.via_weight)
        route_kw = dict(annealing=annealing, iters=cfg.iters,
                        time_budget=cfg.time_budget,
                        unrouted_weight=cfg.unrouted_weight,
                        anneal_temps=cfg.anneal_temps, via_weight=cfg.via_weight)
        res = pipeline.run_routing(
            board, rules, pitch, route_params=route_params, route_kw=route_kw,
            seed=cfg.seed, runs=runs, jobs=1, snapshots=_ANNEAL_SNAP_COUNT,
            exclude=cfg.exclude_net or [], hooks=hooks, cancel=self._cancel)

        if res.results is None:
            self._post(Phase("cancelled before routing completed"))
            return

        grid = res.grid
        final_results = res.results
        routed, unrouted = res.routed, res.unrouted
        length, vias = res.length, res.vias

        self._post(Phase("writing routed board"))
        _mode = "placed + routed" if place else "routed"
        pcb.stamp_comment(board,
            f"PyAutoRoute v{__version__} — {_mode} "
            f"{datetime.date.today().isoformat()}")
        pcb.write_board(board, out_path,
                        new_nodes=_results_to_nodes(board, grid, final_results),
                        strip_free_vias=True)
        if fill_nets:
            pcb.try_refill_zones(out_path)
        routed_board = pcb.load_board(out_path)
        violations = geometry.clearance_violations(routed_board, rules)
        total = routed + unrouted
        self._post(BoardSnap(routed_board))
        self._post(Done(str(out_path), total, routed, unrouted,
                        length, vias, violations, routed_board))
