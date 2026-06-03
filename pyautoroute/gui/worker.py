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

from .events import BoardSnap, Done, Error, Phase, Progress, SelfCheck


# Minimum seconds between Progress events posted to the UI queue.
# Keeps the main thread from being overwhelmed by high-iteration-rate callbacks.
_PROGRESS_INTERVAL = 0.125   # ~8 Hz max
# Minimum seconds between board canvas snapshots during greedy routing.
_ROUTE_SNAP_INTERVAL = 1.5
# Routing annealing snapshot count for live board updates.
_ANNEAL_SNAP_COUNT = 40


def _quick_violations(board, grid, results, rules) -> int:
    """Run an in-memory DRC check on routing results without writing to disk.

    Converts routing results to SList nodes, builds Obstacle objects from them,
    and runs clearance_violations against the board pads + new routing.

    Args:
        board: the board (pads/existing copper; routing not yet applied).
        grid: the routing grid.
        results: list of RouteResult from the router.
        rules: design rules for clearance lookup.

    Returns:
        Number of clearance violations found.
    """
    from pyautoroute import geometry, pcb
    from pyautoroute.autoroute import _results_to_nodes
    from pyautoroute.sexpr import SList, Atom

    nodes = _results_to_nodes(board, grid, results)

    # Parse SList segment/via nodes into Obstacle objects for all layers.
    extra_obs: list[geometry.Obstacle] = []
    for node in nodes:
        if not isinstance(node, SList) or not node or not isinstance(node[0], Atom):
            continue
        head = node[0].raw
        if head == "segment":
            start = pcb.floats(pcb.child(node, "start"))
            end = pcb.floats(pcb.child(node, "end"))
            widths = pcb.floats(pcb.child(node, "width"))
            layers = pcb.strings(pcb.child(node, "layer"))
            net_node = pcb.child(node, "net")
            net = pcb.strings(net_node)[0] if net_node and pcb.strings(net_node) else ""
            if len(start) < 2 or len(end) < 2 or not widths or not layers:
                continue
            from shapely.geometry import LineString
            poly = LineString([start[:2], end[:2]]).buffer(widths[0] / 2)
            extra_obs.append(geometry.Obstacle(poly, net, layers[0]))
        elif head == "via":
            at = pcb.floats(pcb.child(node, "at"))
            sizes = pcb.floats(pcb.child(node, "size"))
            layers = pcb.strings(pcb.child(node, "layers"))
            net_node = pcb.child(node, "net")
            net = pcb.strings(net_node)[0] if net_node and pcb.strings(net_node) else ""
            if len(at) < 2 or not sizes or not layers:
                continue
            from shapely.geometry import Point
            poly = Point(at[:2]).buffer(sizes[0] / 2)
            for layer in layers:
                extra_obs.append(geometry.Obstacle(poly, net, layer))

    # Build combined obstacle list: existing board copper + new routing.
    all_obs = geometry.board_obstacles(board) + extra_obs
    by_layer: dict[str, list] = {}
    for o in all_obs:
        by_layer.setdefault(o.layer, []).append(o)

    from shapely.strtree import STRtree
    violations = 0
    for layer, items in by_layer.items():
        tree = STRtree([o.geom for o in items])
        for i, o in enumerate(items):
            need = max(rules.clearance_for(o.net), 0.0)
            ring = o.geom.buffer(need)
            for idx in tree.query(ring):
                if idx <= i:
                    continue
                other = items[idx]
                if other.net == o.net:
                    continue
                if o.geom.distance(other.geom) < need - 1e-6:
                    violations += 1
    return violations


def _snap_pads(board, margin: float = 2.0, strip_vias: bool = False):
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
    kw = dict(pads=pads_copy, footprints=fps_copy)
    if strip_vias:
        kw["free_vias"] = []
    snap = dataclasses.replace(board, **kw)
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
    def _build_hooks(self, cfg, margin, board, rules=None):
        """Build a `pipeline.PipelineHooks` that posts throttled GUI events.

        The shared `run_placement` / `run_routing` call these at each progress
        point; here they translate into `Phase` / `Progress` / `BoardSnap` events.
        Snapshot copies are taken from the live ``board`` at a consistent instant.

        Args:
            cfg: the run configuration (for budgets shown in the progress events).
            margin: placement preview margin (mm), passed to `_snap_pads`.
            board: the live board the pipeline mutates (snapshotted for the canvas).
            rules: design rules; when provided, a `SelfCheck` event is posted
                whenever a new best routing is found.

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
        # Strip pre-existing free vias from live previews when they'll be cleared
        _strip_vias = getattr(cfg, "existing_routes", "clear") == "clear"

        def _snap(b=None):
            return _snap_pads(b or board, margin, strip_vias=_strip_vias)

        def _route_snap(b):
            return dataclasses.replace(b, free_vias=[] if _strip_vias else b.free_vias)

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
                post(BoardSnap(_snap(),
                               kind="best" if new_best else "current"))
                if new_best:
                    post(BoardSnap(_snap(), kind="overall_best"))

        def placed(_board):
            post(BoardSnap(_snap()))

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
                post(BoardSnap(_route_snap(b), results=list(partial), grid=g))

        def anneal_progress(it, total, r, u, energy, best, temp, accept, ob):
            now = time.monotonic()
            if now - st["anneal_prog"] >= _PROGRESS_INTERVAL:
                st["anneal_prog"] = now
                post(Progress("annealing", it, total, energy, best, temp, accept,
                              r, u, elapsed=now - st["anneal_t0"],
                              budget=cfg.time_budget or 0.0))

        def anneal_snapshot(b, g, results, k, n):
            post(BoardSnap(_route_snap(b), results=results, grid=g))

        def anneal_best(b, g, results):
            post(BoardSnap(_route_snap(b), results=results, grid=g, kind="best"))

        def overall_best(b, g, results, energy):
            post(BoardSnap(_route_snap(b), results=list(results), grid=g,
                           kind="overall_best"))
            if rules is not None and results:
                try:
                    n = _quick_violations(b, g, list(results), rules)
                    post(SelfCheck(n))
                except Exception:
                    pass

        def route_run_done(k, n, energy, summary, metrics, is_best=False, iters=0):
            if summary:
                post(Phase(f"{st['tag']}{summary}"))

        return pipeline.PipelineHooks(
            phase=phase, place_run=place_run, place_progress=place_progress,
            placed=placed, route_run=route_run, route_progress=route_progress,
            route_partial=route_partial, anneal_progress=anneal_progress,
            anneal_snapshot=anneal_snapshot, anneal_best=anneal_best,
            overall_best=overall_best, route_run_done=route_run_done)

    def _ground_plane_nodes(self, cfg, board, rules, out_path,
                            routed_nodes=None):
        """Build ground plane zone nodes if configured.

        Returns a list of zone nodes (possibly empty). Posts warnings to the queue.

        Args:
            cfg: the run configuration (must have ground_plane, ground_net, etc).
            board: the board (already routed).
            rules: the design rules.
            out_path: the output path (for refill check).
            routed_nodes: freshly-routed SList nodes not yet in board (forwarded
                to `groundplane.build` so connectivity via clearance checks see
                new tracks).

        Returns:
            A list of zone SList nodes.
        """
        if not getattr(cfg, "ground_plane", False):
            return []

        try:
            from pyautoroute import groundplane
        except ImportError:
            self._post(Phase("⚠ ground-plane: not available"))
            return []

        nodes = []
        margin = (cfg.ground_plane_margin
                  if cfg.ground_plane_margin is not None
                  else rules.default_class.clearance)
        layers = (["F.Cu", "B.Cu"] if cfg.ground_plane_layer == "both"
                  else [cfg.ground_plane_layer])

        if len(layers) == 1 and cfg.stitch_vias:
            self._post(Phase(
                "  ⚠ ground-plane: stitching vias skipped — requires "
                "--ground-plane-layer both (vias would float on the non-pour layer)"))
        for i, layer in enumerate(layers):
            sp = cfg.stitch_vias if (len(layers) > 1 and i == 0) else None
            gp_nodes, gp_warns = groundplane.build(
                board, rules, net=cfg.ground_net, layer=layer, margin=margin,
                stitch_pitch=sp, routed_nodes=routed_nodes or [])
            nodes.extend(gp_nodes)
            for w in gp_warns:
                self._post(Phase(f"  ⚠ ground-plane: {w}"))

        return nodes

    def _place_params(self, cfg, rules):
        """Build the `placement.PlaceParams` from the run config.

        Shared by the single-pass and best-of-cycles paths so the placement knobs
        stay in lock-step.

        Args:
            cfg: the run configuration.
            rules: the design rules (for the default keep-out buffer).

        Returns:
            A `pyautoroute.placement.PlaceParams`.
        """
        from pyautoroute import placement as place_mod
        from pyautoroute.autoroute import default_place_buffer

        buf = (default_place_buffer(rules) if cfg.place_buffer is None
               else cfg.place_buffer)
        return place_mod.PlaceParams(
            iters=cfg.place_iters, time_budget=cfg.place_time, seed=cfg.seed,
            exclude=cfg.exclude_net or [],
            overlap_weight=cfg.place_overlap_weight,
            compact_weight=cfg.place_compact_weight,
            edge_weight=cfg.place_edge_weight,
            keep_outline=getattr(cfg, "keep_outline", False), buffer=buf,
            t_start=cfg.place_temps[0], t_end=cfg.place_temps[1],
            step=cfg.place_step, rotate_mode=cfg.place_rotate,
            swap_prob=getattr(cfg, "place_swap_prob", 0.2) or 0.2)

    def _cycle_hooks(self, cfg, margin, tag):
        """Build a `pipeline.CycleHooks` posting throttled GUI events for one cycle.

        `run_cycle` exposes a leaner hook set than the single-pass pipeline (no live
        per-iteration board during placement, no route partials / anneal snapshots),
        so the live canvas updates once per cycle — when that cycle's placement is
        finalised (`board_snap_cb`) — while phase/progress events stream throughout,
        each prefixed with ``tag`` (e.g. ``"cycle 2/5: "``).

        Args:
            cfg: the run configuration (for the progress budgets).
            margin: placement preview margin (mm), passed to `_snap_pads`.
            tag: a per-cycle phase prefix.

        Returns:
            A `pyautoroute.pipeline.CycleHooks`.
        """
        from pyautoroute import pipeline

        post = self._post
        cancel = self._cancel
        st = {"place_t0": time.monotonic(), "anneal_t0": time.monotonic(),
              "prog": 0.0, "route_prog": 0.0, "anneal_prog": 0.0}

        def phase(name):
            if name.startswith("routing"):
                st["anneal_t0"] = time.monotonic()
            post(Phase(f"{tag}{name}"))

        def place_progress(it, total, energy, best, temp, accept):
            if cancel.is_set():
                return
            now = time.monotonic()
            if now - st["prog"] >= _PROGRESS_INTERVAL:
                st["prog"] = now
                post(Progress("placing", it, total, energy, best, temp, accept,
                              0, 0, elapsed=now - st["place_t0"],
                              budget=cfg.place_time or 0.0))

        def route_progress(done, total, routed, unrouted):
            now = time.monotonic()
            if now - st["route_prog"] >= _PROGRESS_INTERVAL:
                st["route_prog"] = now
                post(Progress("routing", done, total, 0.0, 0.0, 0.0, 0.0,
                              routed, unrouted))

        def anneal_progress(it, total, r, u, energy, best, temp, accept):
            now = time.monotonic()
            if now - st["anneal_prog"] >= _PROGRESS_INTERVAL:
                st["anneal_prog"] = now
                post(Progress("annealing", it, total, energy, best, temp, accept,
                              r, u, elapsed=now - st["anneal_t0"],
                              budget=cfg.time_budget or 0.0))

        def board_snap(b):
            post(BoardSnap(_snap_pads(b, margin)))

        return pipeline.CycleHooks(
            phase_cb=phase, place_progress=place_progress,
            route_progress=route_progress, anneal_progress=anneal_progress,
            board_snap_cb=board_snap)

    def _pipeline(self, cfg) -> None:
        from pyautoroute import __version__, geometry, pcb, pipeline
        from pyautoroute import router
        from pyautoroute.autoroute import (
            _results_to_nodes, default_output, default_pitch,
        )
        from pyautoroute.rules import load_rules

        input_path = Path(cfg.input)
        place = cfg.place
        place_only = cfg.place_only
        out_path = default_output(input_path, place=place, place_only=place_only)

        self._post(Phase("parsing board + rules"))
        board = pcb.load_board(input_path)
        if getattr(cfg, "silk_labels", False):
            pcb.move_values_to_silk(board)
            pcb.move_refs_to_fab(board)

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

        # ---- best-of-cycles (+ optional congestion feedback) ---------
        # When placing and routing with cycles > 1, run the place→route→score
        # cycle the CLI's `--cycles` runs (and `--place-feedback` chains), keeping
        # the cycle that *routes* best. This shares `pipeline.run_cycle`, so the
        # orchestration isn't duplicated — only the thin per-cycle driver differs.
        cycles = max(1, getattr(cfg, "cycles", 1) or 1)
        if place and not place_only and cycles > 1:
            self._run_cycles(cfg, board, rules, pitch, margin, input_path,
                             out_path, fill_nets, cycles)
            return

        hooks = self._build_hooks(cfg, margin, board, rules=rules)

        # ---- placement -----------------------------------------------
        if place or place_only:
            pp = self._place_params(cfg, rules)
            pipeline.run_placement(board, place_params=pp,
                                   place_runs=max(1, cfg.place_runs),
                                   seed=cfg.seed, place_margin=margin,
                                   hooks=hooks, cancel=self._cancel)

        if place_only:
            self._post(Phase("writing placed board"))
            pcb.stamp_comment(board,
                f"PyAutoRoute v{__version__} — placed "
                f"{datetime.date.today().isoformat()}")
            # Placement always clears existing routing.
            pcb.write_board(board, out_path, new_nodes=None,
                            strip_free_vias=True, strip_segments=True)
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
        existing_routes = getattr(cfg, "existing_routes", "clear") or "clear"
        if existing_routes == "preserve" and (place or place_only):
            existing_routes = "clear"   # placement invalidates existing routing

        # For preserve mode: build the full connection list, filter out pre-routed
        # connections, and pass only the remainder to run_routing.
        n_pre_routed = 0
        conns = None
        if existing_routes == "preserve":
            from pyautoroute import netlist
            all_conns = netlist.build_connections(board, exclude=cfg.exclude_net or [])
            pre_routed_conns, conns = netlist.pre_routed_connections(board, all_conns)
            n_pre_routed = len(pre_routed_conns)

        route_params = router.RouteParams(via_cost=cfg.via_weight)
        route_kw = dict(annealing=annealing, iters=cfg.iters,
                        time_budget=cfg.time_budget,
                        unrouted_weight=cfg.unrouted_weight,
                        anneal_temps=cfg.anneal_temps, via_weight=cfg.via_weight)
        res = pipeline.run_routing(
            board, rules, pitch, route_params=route_params, route_kw=route_kw,
            seed=cfg.seed, runs=runs, jobs=1, snapshots=_ANNEAL_SNAP_COUNT,
            exclude=cfg.exclude_net or [], hooks=hooks, cancel=self._cancel,
            conns=conns,
            greedy_order_mode=getattr(cfg, "greedy_order", "short") or "short")

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
        new_nodes = _results_to_nodes(board, grid, final_results)
        new_nodes.extend(self._ground_plane_nodes(cfg, board, rules, out_path,
                                                  routed_nodes=new_nodes))
        pcb.write_board(board, out_path, new_nodes=new_nodes,
                        strip_free_vias=(existing_routes == "clear"),
                        strip_segments=(existing_routes == "clear"))
        if fill_nets or getattr(cfg, "ground_plane", False):
            pcb.try_refill_zones(out_path)
        routed_board = pcb.load_board(out_path)
        violations = geometry.clearance_violations(routed_board, rules)
        total = routed + unrouted + n_pre_routed
        self._post(BoardSnap(routed_board))
        self._post(Done(str(out_path), total, routed + n_pre_routed, unrouted,
                        length, vias, violations, routed_board))

    def _run_cycles(self, cfg, board, rules, pitch, margin, input_path,
                    out_path, fill_nets, cycles) -> None:
        """Best-of-cycles place+route, mirroring the CLI's ``--cycles`` loop.

        Runs ``cycles`` independent place→route cycles through `pipeline.run_cycle`
        (seed ``cfg.seed + k``), keeping the one that *routes* best
        (`pipeline.select_best`). With ``cfg.place_feedback`` each later cycle
        re-places under a congestion field accumulated from the previous cycles'
        routing (decayed by `autoroute._FEEDBACK_DECAY`), spreading footprints out
        of the hot zones; the keep-best gate means feedback can only help. The
        winning cycle's board is written, zone-refilled, self-checked and reported
        exactly as the single-pass path. Live progress is per-cycle (see
        `_cycle_hooks`).

        Args:
            cfg: the run configuration.
            board: the parsed board (used only to frame the congestion field).
            rules: the design rules.
            pitch: routing-grid pitch (mm).
            margin: placement outline margin (mm).
            input_path: the source board path (re-read each cycle).
            out_path: the output board path.
            fill_nets: nets with copper pours (for the zone refill).
            cycles: number of cycles (> 1).
        """
        from dataclasses import replace

        from pyautoroute import __version__, geometry, pcb, router
        from pyautoroute.autoroute import _FEEDBACK_DECAY, _results_to_nodes
        from pyautoroute.pipeline import run_cycle, select_best

        pp = self._place_params(cfg, rules)
        route_params = router.RouteParams(via_cost=cfg.via_weight)
        route_kw = dict(annealing=bool(cfg.iters or cfg.time_budget),
                        iters=cfg.iters, time_budget=cfg.time_budget,
                        unrouted_weight=cfg.unrouted_weight,
                        anneal_temps=cfg.anneal_temps, via_weight=cfg.via_weight)
        feedback = bool(getattr(cfg, "place_feedback", False))
        frame = router.congestion_frame(board, pitch) if feedback else None
        field = None
        results: list = []

        for k in range(cycles):
            if self._cancel.is_set():
                break
            tag = f"cycle {k + 1}/{cycles}: "
            pp_k = pp
            if feedback and field is not None:
                pp_k = replace(pp, congestion_field=field,
                               congestion_weight=cfg.congestion_weight)
            hooks = self._cycle_hooks(cfg, margin, tag)
            cr = run_cycle(input_path, rules, pitch, pp_k, route_params,
                           route_kw=route_kw, place_margin=margin,
                           seed=cfg.seed + k, hooks=hooks, cancel=self._cancel,
                           greedy_order_mode=getattr(cfg, "greedy_order", "short") or "short")
            results.append(cr)
            self._post(Phase(
                f"cycle {k + 1}/{cycles} done: routed {cr.routed}/{cr.n_conns}, "
                f"energy {cr.energy:.1f}, {cr.vias} vias"
                + (f", {cr.unrouted} unrouted" if cr.unrouted else "")))
            self._post(BoardSnap(dataclasses.replace(cr.board),
                                 results=cr.results, grid=cr.grid))
            if select_best(results) is cr:
                self._post(BoardSnap(dataclasses.replace(cr.board),
                                     results=cr.results, grid=cr.grid,
                                     kind="overall_best"))
                try:
                    n = _quick_violations(cr.board, cr.grid, cr.results, rules)
                    self._post(SelfCheck(n))
                except Exception:
                    pass
            if feedback and k + 1 < cycles and not self._cancel.is_set():
                new_field = router.congestion_heatmap(cr.conns, cr.results,
                                                      cr.grid, frame)
                field = (new_field if field is None
                         else field.blended(new_field, _FEEDBACK_DECAY))

        if not results:
            self._post(Phase("cancelled"))
            return

        best = select_best(results)
        self._post(Phase(
            f"best of {len(results)} cycles: seed {best.seed} — routed "
            f"{best.routed}/{best.n_conns}, energy {best.energy:.1f}, "
            f"{best.vias} vias"))
        self._post(BoardSnap(dataclasses.replace(best.board),
                             results=best.results, grid=best.grid,
                             kind="overall_best"))

        self._post(Phase("writing placed + routed board"))
        pcb.stamp_comment(best.board,
            f"PyAutoRoute v{__version__} — placed + routed "
            f"{datetime.date.today().isoformat()}")
        new_nodes = _results_to_nodes(best.board, best.grid, best.results)
        new_nodes.extend(self._ground_plane_nodes(cfg, best.board, rules, out_path,
                                                  routed_nodes=new_nodes))
        # cycles always uses --place, which forces clear mode
        pcb.write_board(best.board, out_path, new_nodes=new_nodes,
                        strip_free_vias=True, strip_segments=True)
        if fill_nets or getattr(cfg, "ground_plane", False):
            pcb.try_refill_zones(out_path)
        routed_board = pcb.load_board(out_path)
        violations = geometry.clearance_violations(routed_board, rules)
        total = best.routed + best.unrouted
        self._post(BoardSnap(routed_board))
        self._post(Done(str(out_path), total, best.routed, best.unrouted,
                        best.length, best.vias, violations, routed_board))
