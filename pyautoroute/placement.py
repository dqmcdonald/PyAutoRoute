"""Experimental simulated-annealing footprint placement.

An opt-in pass (``--place``) that arranges the board's footprints *before*
routing, the placement analogue of `pyautoroute.anneal`: where the annealer moves
tracks, this moves footprint positions/rotations to minimise rats-nest length
while keeping bodies from overlapping and pulling the layout together.

Energy ``E = ratsnest + overlap_weight·overlap_area + compact_weight·bbox_area``:

- **ratsnest** — total MST length over the pad centroids (reuses
  `pyautoroute.netlist`); shrinks as connected pads are drawn together.
- **overlap_area** — pairwise intersection of footprint body boxes, found via a
  shapely `STRtree`. Each box is inflated by half of ``buffer`` per side, so a pair
  registers as overlapping until its *gap* exceeds ``buffer``; the optimiser then
  keeps footprints at least ``buffer`` apart, leaving room for routing clearance
  (this is the fix for placements packing so tightly that the routed board failed
  DRC). A pair where either footprint opted in via the ``Autoroute=overlap``
  property (`pcb.Footprint.overlap_ok`) contributes only its *pad-vs-pad* overlap
  (also buffer-inflated), not body overlap — the shield-over-board case.
- **bbox_area** — area of the bounding box of all footprints; compaction emerges
  from this term under cooling, with no separate phase.

Moves (over the *movable* footprints — locked ones are fixed obstacles): translate
by a temperature-scaled random step, rotate (``rotate_mode``: ``ortho`` = ±90°/180°,
``free`` = any angle, ``none`` = no rotation), or swap two footprints' origins.
Worse moves are accepted with Metropolis probability under a geometric
``t_start → t_end`` schedule; the best-seen placement is kept and left on the
board. Pad absolute coordinates are kept in sync on every move
(`pcb.Footprint.sync_pads`) so the energy geometry stays consistent; after the run
the caller applies `pcb.apply_placement` to finalise the pads and board outline
before routing.
"""

from __future__ import annotations

import math
import random
import time
from collections import deque
from dataclasses import dataclass, field

from shapely.geometry import box
from shapely.strtree import STRtree

from . import netlist
from .pcb import Board, Footprint, Pad

# Window (in iterations) over which the live acceptance ratio is measured, so the
# reported rate tracks how the cooling schedule bites (it falls towards zero as T
# cools) rather than being dominated by the hot start. Mirrors `anneal`.
_ACCEPT_WINDOW = 100


@dataclass
class PlaceParams:
    iters: int | None = None
    time_budget: float | None = None
    t_start: float = 8.0
    t_end: float = 0.05
    overlap_weight: float = 20.0      # mm-equivalent cost per mm² of body/pad overlap
    compact_weight: float = 0.02      # mm-equivalent cost per mm² of layout bbox
    step: float = 20.0                # max translate step (mm) at t_start
    buffer: float = 0.5               # keep-out gap (mm) enforced between footprints
    rotate_mode: str = "ortho"        # "ortho" (+/-90/180), "free" (any angle), "none"
    seed: int = 0
    exclude: list[str] = field(default_factory=list)


@dataclass
class PlaceResult:
    start_energy: float
    best_energy: float
    iterations: int
    accepted: int
    moved: int                        # footprints whose pose changed from the input
    final_ratsnest: float = 0.0       # energy breakdown at the best placement
    final_overlap: float = 0.0        # penalised footprint overlap area (mm²)
    final_bbox: float = 0.0           # layout bounding-box area (mm²)

    @property
    def accept_ratio(self) -> float:
        """Fraction of proposed moves accepted over the run (0 if none made)."""
        return self.accepted / self.iterations if self.iterations else 0.0


def _half_extent(pad: Pad) -> float:
    """Rotation-independent half-extent of a pad (half its bounding diagonal)."""
    return 0.5 * math.hypot(pad.w, pad.h)


class _Placer:
    def __init__(self, board: Board, params: PlaceParams):
        """Set up the placer over a board's footprints.

        Args:
            board: the board whose footprint poses are optimised in place.
            params: the placement parameters (budget, schedule, weights, step).
        """
        self.board = board
        self.p = params
        self.rng = random.Random(params.seed)
        self.boxed = [fp for fp in board.footprints if fp.pads]
        self.movable = [fp for fp in self.boxed if not fp.locked]
        # Each body/pad box is grown by half the buffer per side, so two boxes
        # register as overlapping whenever their *gap* is below the full buffer —
        # the optimiser then pushes footprints at least `buffer` apart, leaving
        # room for the routing clearance and avoiding the too-tight placements
        # that previously failed DRC.
        self.half_buffer = max(0.0, params.buffer) / 2.0

    def _fp_box(self, fp: Footprint):
        """Buffer-inflated axis-aligned body box of a footprint, from its pads."""
        hb = self.half_buffer
        xs0 = min(p.cx - _half_extent(p) for p in fp.pads) - hb
        ys0 = min(p.cy - _half_extent(p) for p in fp.pads) - hb
        xs1 = max(p.cx + _half_extent(p) for p in fp.pads) + hb
        ys1 = max(p.cy + _half_extent(p) for p in fp.pads) + hb
        return box(xs0, ys0, xs1, ys1)

    def _pad_box(self, pad: Pad):
        """Buffer-inflated axis-aligned box around a single pad at its centre."""
        he = _half_extent(pad) + self.half_buffer
        return box(pad.cx - he, pad.cy - he, pad.cx + he, pad.cy + he)

    def _pad_overlap(self, fa: Footprint, fb: Footprint) -> float:
        """Total intersection area between two footprints' pad boxes (mm²)."""
        boxes_b = [self._pad_box(p) for p in fb.pads]
        total = 0.0
        for a in (self._pad_box(p) for p in fa.pads):
            for b in boxes_b:
                if a.intersects(b):
                    total += a.intersection(b).area
        return total

    def _overlap_area(self, boxes) -> float:
        """Penalised overlap across all footprint pairs (mm²).

        Args:
            boxes: per-footprint body boxes, parallel to ``self.boxed``.

        Returns:
            Body-box overlap for ordinary pairs, plus pad-only overlap for pairs
            involving an ``overlap_ok`` footprint.
        """
        if len(boxes) < 2:
            return 0.0
        tree = STRtree(boxes)
        total = 0.0
        for i, bi in enumerate(boxes):
            for j in tree.query(bi):
                if j <= i or not bi.intersects(boxes[j]):
                    continue
                fi, fj = self.boxed[i], self.boxed[j]
                if fi.overlap_ok or fj.overlap_ok:
                    total += self._pad_overlap(fi, fj)
                else:
                    total += bi.intersection(boxes[j]).area
        return total

    def _energy_components(self) -> tuple[float, float, float]:
        """Return the ``(ratsnest, overlap_area, bbox_area)`` energy terms."""
        rats = sum(c.est_length
                   for c in netlist.build_connections(self.board, self.p.exclude))
        boxes = [self._fp_box(fp) for fp in self.boxed]
        overlap = self._overlap_area(boxes)
        if boxes:
            minx = min(b.bounds[0] for b in boxes)
            miny = min(b.bounds[1] for b in boxes)
            maxx = max(b.bounds[2] for b in boxes)
            maxy = max(b.bounds[3] for b in boxes)
            bbox_area = max(0.0, maxx - minx) * max(0.0, maxy - miny)
        else:
            bbox_area = 0.0
        return rats, overlap, bbox_area

    def _energy(self) -> float:
        """Current placement energy (see the module docstring)."""
        rats, overlap, bbox_area = self._energy_components()
        return rats + self.p.overlap_weight * overlap + self.p.compact_weight * bbox_area

    @staticmethod
    def _snapshot(fps):
        """Capture ``(fp, x, y, angle)`` poses for undo/best-tracking."""
        return [(fp, fp.x, fp.y, fp.angle) for fp in fps]

    @staticmethod
    def _restore(snap):
        """Restore poses captured by `_snapshot` and re-sync their pads."""
        for fp, x, y, angle in snap:
            fp.x, fp.y, fp.angle = x, y, angle
            fp.sync_pads()

    def _move(self, temp_frac: float):
        """Apply one random move and return its undo snapshot.

        Args:
            temp_frac: ``T / t_start`` in ``[t_end/t_start, 1]``; scales the
                translate step so moves shrink as the schedule cools.

        Returns:
            The `_snapshot` of the footprints touched, for `_restore`.
        """
        r = self.rng.random()
        if len(self.movable) >= 2 and r < 0.2:
            a, b = self.rng.sample(self.movable, 2)          # swap origins
            snap = self._snapshot([a, b])
            a.x, b.x = b.x, a.x
            a.y, b.y = b.y, a.y
            a.sync_pads()
            b.sync_pads()
            return snap
        fp = self.rng.choice(self.movable)
        snap = self._snapshot([fp])
        if self.p.rotate_mode != "none" and r < 0.5:         # rotate
            if self.p.rotate_mode == "free":
                fp.angle = self.rng.uniform(0.0, 360.0)
            else:                                            # "ortho"
                fp.angle = (fp.angle + self.rng.choice((90.0, -90.0, 180.0))) % 360.0
        else:                                                # translate
            s = self.p.step * temp_frac
            fp.x += self.rng.uniform(-s, s)
            fp.y += self.rng.uniform(-s, s)
        fp.sync_pads()
        return snap

    def run(self, on_progress=None) -> PlaceResult:
        """Run the annealing loop; leave the board at the best placement seen.

        Args:
            on_progress: optional callback ``(it, total, energy, best, temp,
                accept)`` invoked each iteration, where ``accept`` is the fraction
                of moves accepted over the last ``_ACCEPT_WINDOW`` iterations.

        Returns:
            The `PlaceResult` with start/best energy, run statistics, and the
            energy breakdown at the best placement.
        """
        for fp in self.boxed:
            fp.sync_pads()
        if not self.movable:
            E = self._energy()
            rats, overlap, bbox = self._energy_components()
            return PlaceResult(E, E, 0, 0, 0, rats, overlap, bbox)

        E = self._energy()
        start_E = best_E = E
        best = self._snapshot(self.movable)
        accepted = 0
        recent = deque(maxlen=_ACCEPT_WINDOW)   # 1/0 per recent move, for the live ratio

        total = self.p.iters if self.p.iters else 1_000_000
        t0 = time.time()
        ratio = self.p.t_end / self.p.t_start
        it = 0
        while True:
            if self.p.iters is not None and it >= self.p.iters:
                break
            if self.p.time_budget is not None and time.time() - t0 >= self.p.time_budget:
                break
            if self.p.iters is None and self.p.time_budget is None and it >= 2000:
                break

            frac = (it / total) if self.p.time_budget is None else min(
                1.0, (time.time() - t0) / self.p.time_budget)
            T = self.p.t_start * (ratio ** frac)

            snap = self._move(T / self.p.t_start)
            E_new = self._energy()
            dE = E_new - E
            accept = dE <= 0 or self.rng.random() < math.exp(-dE / max(T, 1e-9))
            if accept:
                E = E_new
                accepted += 1
                if E < best_E:
                    best_E = E
                    best = self._snapshot(self.movable)
            else:
                self._restore(snap)
            recent.append(1 if accept else 0)

            it += 1
            if on_progress is not None:
                on_progress(it, total, E, best_E, T, sum(recent) / len(recent))

        self._restore(best)
        moved = sum(1 for fp in self.board.footprints if fp.moved)
        rats, overlap, bbox = self._energy_components()
        return PlaceResult(start_E, best_E, it, accepted, moved, rats, overlap, bbox)


def place(board: Board, params: PlaceParams | None = None,
          on_progress=None) -> PlaceResult:
    """Place a board's footprints by simulated annealing; return the best seen.

    Mutates `board`'s footprint poses (and their pads) in place, leaving them at
    the best placement found. Locked footprints are held fixed; footprints flagged
    ``Autoroute=overlap`` may overlap others' bodies but not their pads. Call
    `pyautoroute.pcb.apply_placement` afterwards to finalise pad coordinates and
    regenerate the board outline before routing.

    Args:
        board: the board to place, mutated in place.
        params: the placement parameters; ``None`` uses defaults.
        on_progress: optional per-iteration progress callback (see `_Placer.run`).

    Returns:
        The `PlaceResult` with the best placement's energy and run statistics.
    """
    return _Placer(board, params or PlaceParams()).run(on_progress)
