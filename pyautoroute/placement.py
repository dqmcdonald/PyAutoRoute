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
  (also buffer-inflated), not body overlap — the shield-over-board case. Each
  footprint box also covers its visible silkscreen text, and standalone
  board-level silk text (``gr_text`` pin labels, title block) is added as fixed
  keep-out boxes, so footprints aren't placed on top of existing silkscreen.
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
from dataclasses import dataclass, field, replace

from shapely.geometry import box
from shapely.strtree import STRtree

from . import netlist
from .pcb import Board, Footprint, Pad

# Window (in iterations) over which the live acceptance ratio is measured, so the
# reported rate tracks how the cooling schedule bites (it falls towards zero as T
# cools) rather than being dominated by the hot start. Mirrors `anneal`.
_ACCEPT_WINDOW = 100

_SILK_LAYERS = {"F.SilkS", "B.SilkS", "F.Silkscreen", "B.Silkscreen"}


def _fp_silk_text_extents(fp: Footprint) -> list[tuple[float, float, float]]:
    """Return ``(local_x, local_y, half_diag)`` for each visible silkscreen text.

    The ``(local_x, local_y)`` are in the footprint's local coordinate frame
    (pre-rotation); ``half_diag`` is a rotation-invariant radius estimate based
    on the text content length and declared font height, so the extent can be
    applied without knowing the text's own angle.

    Args:
        fp: the footprint whose text nodes are scanned.

    Returns:
        One tuple per visible, non-hidden silkscreen text item (Reference or
        Value property, or fp_text) in the footprint.
    """
    from .pcb import children, child, strings, floats, atoms_after_head
    from .sexpr import SList

    extents: list[tuple[float, float, float]] = []

    def _hidden(node) -> bool:
        h = child(node, "hide")
        if h is None:
            return False
        vals = atoms_after_head(h)
        return bool(vals) and vals[0].text == "yes"

    def _layer(node) -> str:
        ls = strings(child(node, "layer"))
        return ls[0] if ls else ""

    def _font_h(node) -> float:
        eff = child(node, "effects")
        if eff is None:
            return 1.0
        fnt = child(eff, "font")
        if fnt is None:
            return 1.0
        sz = child(fnt, "size")
        vals = floats(sz) if sz is not None else []
        return vals[0] if vals else 1.0

    def _add(content: str, at_vals, fh: float) -> None:
        if len(at_vals) < 2 or not content:
            return
        w = len(content) * fh * 0.7
        h = fh * 1.3
        extents.append((at_vals[0], at_vals[1], 0.5 * math.hypot(w, h)))

    fp_node = fp.fp_node

    for prop in children(fp_node, "property"):
        if _hidden(prop) or _layer(prop) not in _SILK_LAYERS:
            continue
        atoms = atoms_after_head(prop)
        if len(atoms) < 2 or atoms[0].text not in ("Reference", "Value"):
            continue
        content = atoms[1].text
        _add(content, floats(child(prop, "at")), _font_h(prop))

    for txt in children(fp_node, "fp_text"):
        if _hidden(txt) or _layer(txt) not in _SILK_LAYERS:
            continue
        atoms = atoms_after_head(txt)
        if len(atoms) < 2:
            continue
        content = atoms[1].text.replace("${REFERENCE}", fp.ref)
        _add(content, floats(child(txt, "at")), _font_h(txt))

    return extents


def _board_silk_text_boxes(board: Board) -> list[tuple[float, float, float]]:
    """Return ``(cx, cy, half_diag)`` for each visible board-level silk text.

    Board-level ``gr_text`` items — connector pin labels, a title block, etc. —
    are not footprints, so the placer would otherwise ignore them and happily
    drop a footprint on top (the locked "Bus Indicator" / pin-label text on the
    Test1 board). Each tuple is the text's centre and a rotation-invariant
    half-diagonal radius of its extent, in board coordinates, mirroring the
    estimate `_fp_silk_text_extents` uses for footprint text, so footprints can
    be pushed clear of the text.

    Args:
        board: the board whose top-level ``gr_text`` nodes are scanned.

    Returns:
        One tuple per visible, non-hidden silkscreen ``gr_text`` on the board.
    """
    from .pcb import children, child, strings, floats, atoms_after_head

    out: list[tuple[float, float, float]] = []
    for txt in children(board.tree, "gr_text"):
        h = child(txt, "hide")
        if h is not None:
            vals = atoms_after_head(h)
            if vals and vals[0].text == "yes":
                continue
        ls = strings(child(txt, "layer"))
        if not ls or ls[0] not in _SILK_LAYERS:
            continue
        atoms = atoms_after_head(txt)
        if not atoms:
            continue
        content = atoms[0].text
        at = floats(child(txt, "at"))
        if not content or len(at) < 2:
            continue
        x, y, angle = at[0], at[1], (at[2] if len(at) >= 3 else 0.0)

        fh = 1.0
        eff = child(txt, "effects")
        fnt = child(eff, "font") if eff is not None else None
        sz = child(fnt, "size") if fnt is not None else None
        szv = floats(sz) if sz is not None else []
        if szv:
            fh = szv[0]

        # Estimate the unrotated text extent, then offset the box centre from
        # the `at` anchor per the justify (KiCad's `at` is an edge/corner when
        # justified, the centre otherwise); y grows downward.
        w = len(content) * fh * 0.7
        th = fh * 1.3
        just_node = child(eff, "justify") if eff is not None else None
        just = {a.text for a in atoms_after_head(just_node)} if just_node else set()
        dx = 0.5 * w if "left" in just else (-0.5 * w if "right" in just else 0.0)
        dy = -0.5 * th if "bottom" in just else (0.5 * th if "top" in just else 0.0)
        cos_a = math.cos(math.radians(angle))
        sin_a = math.sin(math.radians(angle))
        cx = x + dx * cos_a + dy * sin_a
        cy = y - dx * sin_a + dy * cos_a
        out.append((cx, cy, 0.5 * math.hypot(w, th)))
    return out


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
    # Early-termination (stall detection): if the windowed accept ratio stays
    # below `stall_ratio` for `stall_patience` consecutive full accept-windows,
    # the run stops early. Disabled when `stall_patience <= 0` or
    # `stall_ratio <= 0` (the default), so the full budget is honoured.
    stall_ratio: float = 0.02
    stall_patience: int = 0


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
        # Precompute silkscreen text local extents once; looked up by id(fp).
        self._text_extents: dict[int, list[tuple[float, float, float]]] = {
            id(fp): _fp_silk_text_extents(fp) for fp in self.boxed
        }
        # Fixed board-level silkscreen text (pin labels, title block): static
        # keep-out boxes footprints must avoid, so they aren't dropped on top.
        self._fixed_text = [
            box(cx - hr - self.half_buffer, cy - hr - self.half_buffer,
                cx + hr + self.half_buffer, cy + hr + self.half_buffer)
            for cx, cy, hr in _board_silk_text_boxes(board)
        ]
        self._fixed_tree = STRtree(self._fixed_text) if self._fixed_text else None

        # Incremental-energy cache (populated by `_rebuild_cache`).
        #
        # The ratsnest topology is fixed for the whole run: a move changes pad
        # *positions*, never which pads are connected, so the connection list and
        # the footprint->incident-connection index are built once, and a move
        # recomputes only the lengths of the connections touching the moved
        # footprints. Overlap is cached per box and updated for the moved boxes
        # against their neighbours; the bbox is recomputed from the cached box
        # bounds (O(N), cheap relative to the old O(P^2) MST rebuild).
        self._boxes: list = []            # per-footprint body box, parallel to self.boxed
        self._conns: list = []            # cached netlist connections (fixed topology)
        self._fp_conns: dict[int, list[int]] = {}   # boxed-index -> incident conn indices
        self._rats = 0.0
        self._overlap = 0.0
        self._bbox = 0.0
        self._idx_of_fp: dict[int, int] = {id(fp): i for i, fp in enumerate(self.boxed)}

    def _build_index(self) -> None:
        """Build the fixed connection list and footprint->connection incidence.

        Called once: the ratsnest topology does not change across moves, so the
        connection list (and which footprint each connection's pads belong to)
        is computed a single time.
        """
        self._conns = netlist.build_connections(self.board, self.p.exclude)
        pad_to_fp: dict[int, int] = {}
        for i, fp in enumerate(self.boxed):
            for pad in fp.pads:
                pad_to_fp[id(pad)] = i
        self._fp_conns = {i: [] for i in range(len(self.boxed))}
        for ci, c in enumerate(self._conns):
            fa = pad_to_fp.get(id(c.a))
            fb = pad_to_fp.get(id(c.b))
            for fi in {fa, fb}:
                if fi is not None:
                    self._fp_conns[fi].append(ci)

    def _rebuild_cache(self) -> None:
        """Full recompute of the energy cache (init / after a structural change)."""
        if not self._conns and self.boxed:
            self._build_index()
        self._boxes = [self._fp_box(fp) for fp in self.boxed]
        self._rats = sum(c.est_length for c in self._conns)
        self._overlap = self._overlap_area(self._boxes) + \
            self._fixed_text_overlap(self._boxes)
        self._bbox = self._bbox_area(self._boxes)

    @staticmethod
    def _bbox_area(boxes) -> float:
        """Area of the bounding box enclosing all `boxes` (0 if none)."""
        if not boxes:
            return 0.0
        minx = min(b.bounds[0] for b in boxes)
        miny = min(b.bounds[1] for b in boxes)
        maxx = max(b.bounds[2] for b in boxes)
        maxy = max(b.bounds[3] for b in boxes)
        return max(0.0, maxx - minx) * max(0.0, maxy - miny)

    def _cached_energy(self) -> float:
        """Energy from the current cache (see the module docstring)."""
        return (self._rats + self.p.overlap_weight * self._overlap
                + self.p.compact_weight * self._bbox)

    def _overlap_touching(self, idxs: set[int], boxes) -> float:
        """Overlap area of every pair/fixed-text touching a footprint in `idxs`.

        Each footprint-footprint pair with at least one endpoint in `idxs` is
        counted exactly once (pairs wholly inside `idxs` included once), plus the
        fixed board-silk-text overlap of the boxes in `idxs`.

        Args:
            idxs: boxed-indices of the moved footprints.
            boxes: the per-footprint body boxes to evaluate against.

        Returns:
            The total overlap (mm^2) attributable to the moved footprints.
        """
        tree = STRtree(boxes) if len(boxes) >= 2 else None
        total = 0.0
        seen: set[tuple[int, int]] = set()
        for i in idxs:
            bi = boxes[i]
            if tree is not None:
                for j in tree.query(bi):
                    j = int(j)
                    if j == i or not bi.intersects(boxes[j]):
                        continue
                    key = (i, j) if i < j else (j, i)
                    if key in seen:
                        continue
                    seen.add(key)
                    fi, fj = self.boxed[i], self.boxed[j]
                    if fi.overlap_ok or fj.overlap_ok:
                        total += self._pad_overlap(fi, fj)
                    else:
                        total += bi.intersection(boxes[j]).area
            if self._fixed_tree is not None and not self.boxed[i].overlap_ok:
                for j in self._fixed_tree.query(bi):
                    t = self._fixed_text[int(j)]
                    if bi.intersects(t):
                        total += bi.intersection(t).area
        return total

    def _fp_box(self, fp: Footprint):
        """Buffer-inflated axis-aligned body box of a footprint.

        Covers pads and any visible silkscreen text (Reference/Value) so the
        overlap penalty also pushes text labels apart.
        """
        hb = self.half_buffer
        xs0 = min(p.cx - _half_extent(p) for p in fp.pads) - hb
        ys0 = min(p.cy - _half_extent(p) for p in fp.pads) - hb
        xs1 = max(p.cx + _half_extent(p) for p in fp.pads) + hb
        ys1 = max(p.cy + _half_extent(p) for p in fp.pads) + hb
        # Extend to cover silkscreen text (local coords → board coords).
        txt_list = self._text_extents.get(id(fp))
        if txt_list:
            cos_a = math.cos(math.radians(fp.angle))
            sin_a = math.sin(math.radians(fp.angle))
            for lx, ly, hr in txt_list:
                bx = fp.x + lx * cos_a + ly * sin_a
                by = fp.y - lx * sin_a + ly * cos_a
                xs0 = min(xs0, bx - hr)
                ys0 = min(ys0, by - hr)
                xs1 = max(xs1, bx + hr)
                ys1 = max(ys1, by + hr)
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

    def _fixed_text_overlap(self, boxes) -> float:
        """Overlap (mm²) between footprint boxes and fixed board silk text.

        Args:
            boxes: per-footprint body boxes, parallel to ``self.boxed``.

        Returns:
            Total intersection area of each footprint box with the static
            board-level silkscreen text boxes. ``overlap_ok`` footprints (which
            are meant to sit over the board) are exempt.
        """
        if self._fixed_tree is None:
            return 0.0
        total = 0.0
        for i, bi in enumerate(boxes):
            if self.boxed[i].overlap_ok:
                continue
            for j in self._fixed_tree.query(bi):
                t = self._fixed_text[j]
                if bi.intersects(t):
                    total += bi.intersection(t).area
        return total

    def _energy_components(self) -> tuple[float, float, float]:
        """Return the ``(ratsnest, overlap_area, bbox_area)`` energy terms."""
        rats = sum(c.est_length
                   for c in netlist.build_connections(self.board, self.p.exclude))
        boxes = [self._fp_box(fp) for fp in self.boxed]
        overlap = self._overlap_area(boxes) + self._fixed_text_overlap(boxes)
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

    def run(self, on_progress=None, cancel=None) -> PlaceResult:
        """Run the annealing loop; leave the board at the best placement seen.

        Args:
            on_progress: optional callback ``(it, total, energy, best, temp,
                accept)`` invoked each iteration, where ``accept`` is the fraction
                of moves accepted over the last ``_ACCEPT_WINDOW`` iterations.
            cancel: optional `threading.Event`; when set, the loop stops early and
                the board is left at the best placement found so far (for a GUI
                Stop button).

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
            if cancel is not None and cancel.is_set():
                break
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
          on_progress=None, runs: int = 1, cancel=None) -> PlaceResult:
    """Place a board's footprints by simulated annealing; return the best seen.

    Mutates `board`'s footprint poses (and their pads) in place, leaving them at
    the best placement found. Locked footprints are held fixed; footprints flagged
    ``Autoroute=overlap`` may overlap others' bodies but not their pads. Call
    `pyautoroute.pcb.apply_placement` afterwards to finalise pad coordinates and
    regenerate the board outline before routing.

    With ``runs > 1`` the placement is repeated that many times — each restarted
    from the board's original poses with the seed stepped by the run index — and
    the lowest-energy placement is kept (best-of-N; SA is stochastic, so several
    short runs often beat one long run).

    Args:
        board: the board to place, mutated in place.
        params: the placement parameters; ``None`` uses defaults.
        on_progress: optional per-iteration progress callback (see `_Placer.run`).
        runs: number of independent placement runs; the best is kept.
        cancel: optional `threading.Event`; when set, stops early (between and
            within runs) and returns the best placement found so far.

    Returns:
        The `PlaceResult` with the best placement's energy and run statistics.
    """
    params = params or PlaceParams()
    if runs <= 1:
        return _Placer(board, params).run(on_progress, cancel)

    orig = [(fp, fp.x, fp.y, fp.angle) for fp in board.footprints]
    best: PlaceResult | None = None
    best_poses = None
    for k in range(runs):
        if cancel is not None and cancel.is_set():
            break
        for fp, x, y, a in orig:                 # restart from the original layout
            fp.x, fp.y, fp.angle = x, y, a
            fp.sync_pads()
        result = _Placer(board, replace(params, seed=params.seed + k)).run(
            on_progress, cancel)
        if best is None or result.best_energy < best.best_energy:
            best = result
            best_poses = [(fp, fp.x, fp.y, fp.angle) for fp in board.footprints]
    if best is None:                             # cancelled before any run finished
        return _Placer(board, params).run(on_progress, cancel)
    for fp, x, y, a in best_poses:               # leave the board at the best
        fp.x, fp.y, fp.angle = x, y, a
        fp.sync_pads()
    return best
