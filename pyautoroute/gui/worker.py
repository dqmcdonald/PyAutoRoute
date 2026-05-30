"""Background worker thread: runs the place/route pipeline and posts events.

The worker's only interaction with the UI is to push immutable event objects
onto a ``queue.Queue``.  All board mutations happen here; the snapshot copies
placed in ``BoardSnap`` events are made while the board is in a consistent
state so the main thread can read them safely.
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

    def _pipeline(self, cfg) -> None:
        from pathlib import Path as _P

        from pyautoroute import __version__, anneal, netlist, pcb
        from pyautoroute import placement as place_mod
        from pyautoroute import router
        from pyautoroute.autoroute import (
            default_output, default_pitch, default_place_buffer,
            _results_to_nodes,
        )
        from pyautoroute import geometry
        from pyautoroute.grid import Grid
        from pyautoroute.rules import load_rules

        input_path = _P(cfg.input)
        place = cfg.place
        place_only = cfg.place_only
        out_path = default_output(input_path, place=place,
                                  place_only=place_only)

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

        pro_path = (_P(cfg.pro) if cfg.pro
                    else input_path.with_suffix(".kicad_pro"))
        if not pro_path.exists():
            pro_path = input_path.with_name(
                input_path.stem + ".kicad_pro")
        rules = load_rules(pro_path)
        pitch = cfg.grid if cfg.grid else default_pitch(rules)

        # ---- placement -----------------------------------------------
        if place or place_only:
            place_runs = max(1, cfg.place_runs)
            n_fps = len(board.footprints)
            run_suffix = f" — run 1/{place_runs}" if place_runs > 1 else ""
            self._post(Phase(f"placing {n_fps} footprints{run_suffix}"))
            if cfg.place_buffer is None:
                buf = default_place_buffer(rules)
            else:
                buf = cfg.place_buffer

            last_place_prog = [0.0]
            last_place_snap = [float("inf"), 0.0]  # [best_energy, timestamp]
            place_run_idx = [0]       # 0-based current run number
            last_place_it = [-1]      # previous it value; reset detection
            place_run_t0 = [time.monotonic()]

            def on_place(it, total, energy, best, temp, accept):
                if self._cancel.is_set():
                    return
                now = time.monotonic()
                # Detect SA run reset: iteration counter went back down → new run.
                if last_place_it[0] >= 0 and it < last_place_it[0]:
                    place_run_idx[0] += 1
                    place_run_t0[0] = now
                    if place_runs > 1:
                        k = place_run_idx[0] + 1
                        self._post(Phase(
                            f"placing {n_fps} footprints — run {k}/{place_runs}"))
                last_place_it[0] = it
                if now - last_place_prog[0] >= _PROGRESS_INTERVAL:
                    last_place_prog[0] = now
                    budget = cfg.place_time or 0.0
                    elapsed = now - place_run_t0[0]
                    self._post(Progress("placing", it, total, energy, best,
                                        temp, accept, 0, 0,
                                        elapsed=elapsed, budget=budget))
                new_best = best < last_place_snap[0]
                min_interval = 0.5 if new_best else 2.0
                if now - last_place_snap[1] >= min_interval:
                    if new_best:
                        last_place_snap[0] = best
                    last_place_snap[1] = now
                    snap_kind = "best" if new_best else "current"
                    self._post(BoardSnap(_snap_pads(board, cfg.place_margin),
                                        kind=snap_kind))
                    if new_best:
                        self._post(BoardSnap(_snap_pads(board, cfg.place_margin),
                                            kind="overall_best"))

            pp = place_mod.PlaceParams(
                iters=cfg.place_iters,
                time_budget=cfg.place_time,
                seed=cfg.seed,
                exclude=cfg.exclude_net or [],
                overlap_weight=cfg.place_overlap_weight,
                compact_weight=cfg.place_compact_weight,
                edge_weight=cfg.place_edge_weight,
                keep_outline=getattr(cfg, "keep_outline", False),
                buffer=buf,
                t_start=cfg.place_temps[0],
                t_end=cfg.place_temps[1],
                step=cfg.place_step,
                rotate_mode=cfg.place_rotate,
            )
            place_mod.place(board, pp,
                            on_progress=on_place,
                            runs=place_runs,
                            cancel=self._cancel)
            kept = pcb.apply_placement(
                board, margin=cfg.place_margin,
                keep_outline=getattr(cfg, "keep_outline", False))
            pcb.sync_tree_from_placement(board, keep_outline=kept)
            # Final placement snapshot
            self._post(BoardSnap(_snap_pads(board, cfg.place_margin)))

        if place_only:
            self._post(Phase("writing placed board"))
            pcb.stamp_comment(board,
                f"PyAutoRoute v{__version__} — placed "
                f"{datetime.date.today().isoformat()}")
            pcb.write_board(board, out_path, new_nodes=None,
                            strip_free_vias=True)
            if fill_nets:
                pcb.try_refill_zones(out_path)
            placed = pcb.load_board(out_path)
            violations = geometry.clearance_violations(placed, rules)
            self._post(Done(str(out_path), 0, 0, 0, 0.0, 0,
                            violations, placed))
            return

        if self._cancel.is_set():
            self._post(Phase("cancelled"))
            return

        # ---- routing -------------------------------------------------
        self._post(Phase("building netlist"))
        conns = netlist.build_connections(board, exclude=cfg.exclude_net or [])

        self._post(Phase(f"building {pitch}mm routing grid"))
        grid = Grid(board, rules, pitch)

        annealing = bool(cfg.iters or cfg.time_budget)
        runs = max(1, cfg.runs)
        if runs > 1 and not annealing:
            runs = 1  # greedy is deterministic

        params = router.RouteParams(via_cost=cfg.via_weight)
        order = netlist.greedy_order(conns)

        best_energy = float("inf")
        final_results = None
        routed = unrouted = length = vias = 0

        for k in range(runs):
            if self._cancel.is_set():
                break
            tag = f"run {k + 1}/{runs}: " if runs > 1 else ""
            self._post(Phase(f"{tag}routing {len(conns)} connections"))

            partial_results: list = [None] * len(conns)
            last_route_prog = [0.0]
            last_route_snap = [0.0]

            def on_partial(idx, res):
                partial_results[idx] = res

            def on_route(done, total, r, u):
                now = time.monotonic()
                if now - last_route_prog[0] >= _PROGRESS_INTERVAL:
                    last_route_prog[0] = now
                    self._post(Progress("routing", done, total, 0.0, 0.0,
                                        0.0, 0.0, r, u))
                if now - last_route_snap[0] >= _ROUTE_SNAP_INTERVAL:
                    last_route_snap[0] = now
                    self._post(BoardSnap(
                        dataclasses.replace(board),
                        results=list(partial_results),
                        grid=grid,
                    ))

            state = router.RoutingState(grid)
            result = router.route_all(state, conns, order, params,
                                      on_progress=on_route,
                                      on_partial=on_partial)
            # Board snap after greedy routing
            self._post(BoardSnap(
                dataclasses.replace(board),
                results=list(result.results),
                grid=grid,
            ))
            run_results = result.results
            run_metrics = (result.routed, result.unrouted,
                           result.total_length, result.total_vias)

            if annealing:
                self._post(Phase(f"{tag}annealing"))

                last_anneal_prog = [0.0]
                anneal_t0 = [time.monotonic()]

                def on_anneal(it, total, r, u, energy, best, temp, accept):
                    now = time.monotonic()
                    if now - last_anneal_prog[0] >= _PROGRESS_INTERVAL:
                        last_anneal_prog[0] = now
                        elapsed = now - anneal_t0[0]
                        self._post(Progress("annealing", it, total, energy,
                                            best, temp, accept, r, u,
                                            elapsed=elapsed,
                                            budget=cfg.time_budget or 0.0))

                def on_snap(snap_k, snap_n, snap_results):
                    self._post(BoardSnap(
                        dataclasses.replace(board),
                        results=snap_results,
                        grid=grid,
                    ))

                def on_best_ann(best_e, best_results):
                    self._post(BoardSnap(
                        dataclasses.replace(board),
                        results=best_results,
                        grid=grid,
                        kind="best",
                    ))

                ap = anneal.AnnealParams(
                    iters=cfg.iters,
                    time_budget=cfg.time_budget,
                    seed=cfg.seed + k,
                    snapshots=_ANNEAL_SNAP_COUNT,
                    unrouted_weight=cfg.unrouted_weight,
                    t_start=cfg.anneal_temps[0],
                    t_end=cfg.anneal_temps[1],
                    route_params=params,
                )
                aout = anneal.anneal(
                    state, conns, list(result.results), ap,
                    on_progress=on_anneal,
                    on_snapshot=on_snap,
                    cancel=self._cancel,
                    on_best=on_best_ann,
                )
                run_results = aout.results
                run_metrics = (aout.routed, aout.unrouted,
                               aout.total_length, aout.total_vias)
                run_energy = aout.best_energy
            else:
                run_energy = anneal._energy(run_results, cfg.via_weight,
                                            cfg.unrouted_weight)

            if run_energy < best_energy:
                best_energy = run_energy
                final_results = run_results
                routed, unrouted, length, vias = run_metrics
                self._post(BoardSnap(
                    dataclasses.replace(board),
                    results=list(run_results),
                    grid=grid,
                    kind="overall_best",
                ))

        if final_results is None:
            self._post(Phase("cancelled before routing completed"))
            return

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
