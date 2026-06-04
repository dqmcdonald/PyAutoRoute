"""Experimental simulated-annealing footprint placement.

An opt-in pass (``--place``) that arranges the board's footprints *before*
routing, the placement analogue of `pyautoroute.anneal`: where the annealer moves
tracks, this moves footprint positions/rotations to minimise rats-nest length
while keeping bodies from overlapping and pulling the layout together.

Energy ``E = ratsnest + overlap_weight·overlap_area + compact_weight·bbox_area
+ edge_weight·edge_distance + containment_weight·area_outside_outline
+ congestion_weight·Σ field(centroid) + spread_weight·Σ_cell count²``:

- **ratsnest** — total MST length over the pad centroids (reuses
  `pyautoroute.netlist`); shrinks as connected pads are drawn together.
- **overlap_area** — pairwise intersection of footprint body boxes, found via a
  shapely `STRtree`. Each box is inflated by half of ``buffer`` per side, so a pair
  registers as overlapping until its *gap* exceeds ``buffer``; the optimiser then
  keeps footprints at least ``buffer`` apart, leaving room for routing clearance
  (this is the fix for placements packing so tightly that the routed board failed
  DRC). A pair where either footprint opted in via the ``Autoroute-overlap``
  property (`pcb.Footprint.overlap_ok`) contributes only its *pad-vs-pad* overlap
  (also buffer-inflated), not body overlap — the shield-over-board case. Each
  footprint box also covers its visible silkscreen text, and standalone
  board-level silk text (``gr_text`` pin labels, title block) is added as fixed
  keep-out boxes, so footprints aren't placed on top of existing silkscreen.
- **bbox_area** — area of the bounding box of all footprints; compaction emerges
  from this term under cooling, with no separate phase.
- **edge_distance** — only for footprints that opt in via the ``Autoroute-edge``
  property (``any`` for the nearest side, or ``left`` / ``right`` / ``top`` /
  ``bottom`` for a named one; `pcb.Footprint.edge_affinity`). Each flagged
  footprint is penalised by the distance from its target side of the current
  layout bounding box to the *far* side of its box — i.e. the gap to the edge
  plus the box's depth perpendicular to it — so the term both pulls connectors
  and the like onto the board boundary **and** orients them to lie flat against
  it (long axis parallel) rather than rotating to reach the edge with a single
  pad. Measured against the layout bbox (not absolute coordinates), so it stays
  translation-invariant like the other terms. Zero when nothing is flagged, so
  the default behaviour is unchanged.
- **area_outside_outline** — only under ``keep_outline`` (the ``--keep-outline``
  mode), and only when the board has a closed Edge.Cuts. Penalises the area each
  footprint box protrudes outside that fixed outline, containing the placement
  within the *existing* board shape rather than regenerating a bounding box; edge
  affinity then targets the outline rather than the layout bbox. Zero otherwise.
  Note ``keep_outline`` makes the energy depend on absolute position (the outline
  is fixed), so `recenter` is skipped in that mode.
- **Σ field(centroid)** — only under congestion feedback (``--place-feedback``),
  when a `pyautoroute.router.CongestionField` from a previous cycle's routed
  result is supplied. Each footprint's body-box centre is sampled in the field
  (high where routing was congested — dense copper, vias, unrouted nets) and the
  values summed, so minimising the term spreads parts *out of* the hot cells the
  router struggled with. The field is in absolute board coordinates (it anchors
  the layout), so like ``keep_outline`` it makes the energy position-dependent and
  `recenter` is skipped. Zero when no field is supplied, so default behaviour is
  unchanged.
- **Σ_cell count²** — only when ``spread_weight > 0``. The board outline (or
  layout bounding box) is divided into a ``spread_cells × spread_cells`` grid and
  the sum of squared footprint counts per cell is minimised. By Cauchy-Schwarz
  this is minimised when every cell has the same occupancy, so the term pushes
  the placement towards *uniform density*, correcting the cluster-in-one-corner
  failure mode that arises with ``--keep-outline`` and locked corner parts (which
  pin the bounding-box term to a constant, making ``compact_weight`` inert). A
  value around ``3.0`` works well for most boards; ``0.0`` (the default) leaves
  the behaviour unchanged.

Moves (over the *movable* footprints — locked ones are fixed obstacles): translate
by a temperature-scaled random step, rotate (``rotate_mode``: ``ortho`` = ±90°/180°,
``free`` = any angle, ``none`` = no rotation), or swap two footprints' origins.
``swap_prob`` (default 0.2) sets the probability of attempting a swap move each
iteration; set higher for boards with many interchangeable ICs where position swaps
explore the design space much faster than translates.
Worse moves are accepted with Metropolis probability under a geometric
``t_start → t_end`` schedule; the best-seen placement is kept and left on the
board. Pad absolute coordinates are kept in sync on every move
(`pcb.Footprint.sync_pads`) so the energy geometry stays consistent; after the run
the caller applies `pcb.apply_placement` to finalise the pads and board outline
before routing.

The energy depends only on the footprints' *relative* poses, so it is
translation-invariant and an unlocked cluster random-walks (drifts) during the
run. `place` therefore calls `recenter` before returning, shifting the placement
rigidly back onto its starting centroid without changing any energy term.
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
from .pcb import Board, Footprint, Pad, rotate

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


def _board_silk_text_boxes(board: Board):
    """Return a Shapely polygon for each visible board-level silk text.

    Board-level ``gr_text`` items — connector pin labels, a title block, etc. —
    are not footprints, so the placer would otherwise ignore them and happily
    drop a footprint on top (the locked "Bus Indicator" / pin-label text on the
    Test1 board). Each polygon is a tight rotated rectangle that covers the text
    extent, in board coordinates.

    Args:
        board: the board whose top-level ``gr_text`` nodes are scanned.

    Returns:
        One Shapely polygon per visible, non-hidden silkscreen ``gr_text``.
    """
    from .pcb import children, child, strings, floats, atoms_after_head
    from shapely.affinity import rotate as sh_rotate

    out = []
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

        # Build a tight axis-aligned rectangle at angle=0 centred at (cx,cy),
        # then rotate it into place. This avoids the ~30× area overestimate
        # that the old half-diagonal (circumscribed-circle) approximation
        # produces for wide, flat text strings.
        rect = box(cx - w / 2, cy - th / 2, cx + w / 2, cy + th / 2)
        if angle:
            rect = sh_rotate(rect, -angle, origin=(cx, cy))
        out.append(rect)
    return out


@dataclass
class PlaceParams:
    iters: int | None = None
    time_budget: float | None = None
    t_start: float = 8.0
    t_end: float = 0.05
    overlap_weight: float = 20.0      # mm-equivalent cost per mm² of body/pad overlap
    compact_weight: float = 0.02      # mm-equivalent cost per mm² of layout bbox
    edge_weight: float = 2.0          # mm-cost per mm an edge-flagged footprint sits from its target edge
    keep_outline: bool = False        # contain footprints within the board's existing Edge.Cuts
    containment_weight: float = 50.0  # cost per mm² a footprint protrudes outside the kept outline
    # Congestion feedback (--place-feedback): when `congestion_field` is set (a
    # `router.CongestionField` from a previous cycle's routed result), each
    # footprint is penalised by `congestion_weight · field(centroid)`, pushing
    # parts out of the cells where routing struggled. Absolute-coordinate, so the
    # field anchors the layout — `recenter` is skipped while it is active. None /
    # weight 0 (the default) leaves placement unchanged.
    congestion_field: object = None
    congestion_weight: float = 0.0    # mm-cost per unit field value at a footprint centroid
    spread_weight: float = 0.0        # penalise uneven cell occupancy (sum count²); spreads the layout
    spread_cells: int = 8             # approximate cells along the longer board axis
    step: float = 20.0                # max translate step (mm) at t_start
    buffer: float = 0.5               # keep-out gap (mm) enforced between footprints
    rotate_mode: str = "ortho"        # "ortho" (+/-90/180), "free" (any angle), "none"
    swap_prob: float = 0.2            # probability of attempting a swap move (0–1)
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
    final_edge: float = 0.0           # total edge-affinity distance (mm); 0 if none flagged

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
        # keep-out polygons footprints must avoid, so they aren't dropped on top.
        _text_polys = _board_silk_text_boxes(board)
        self._fixed_text = (
            [p.buffer(self.half_buffer) for p in _text_polys]
            if _text_polys else []
        )
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
        self._bounds: list = []           # per-box (minx,miny,maxx,maxy), parallel to _boxes
        self._conns: list = []            # cached netlist connections (fixed topology)
        self._conn_len: list = []         # cached per-connection length, parallel to _conns
        self._fp_conns: dict[int, list[int]] = {}   # boxed-index -> incident conn indices
        self._rats = 0.0
        self._overlap = 0.0
        self._bbox = 0.0
        self._edge = 0.0
        self._containment = 0.0
        self._congestion = 0.0
        self._idx_of_fp: dict[int, int] = {id(fp): i for i, fp in enumerate(self.boxed)}
        # Footprints that opted into edge placement: boxed-index -> side
        # ("any"|"left"|"right"|"top"|"bottom"). Fixed for the run.
        self._flagged: dict[int, str] = {
            i: fp.edge_affinity for i, fp in enumerate(self.boxed) if fp.edge_affinity
        }
        # Native KiCad groups: footprints sharing a group_id move as a rigid body.
        # Build group_id -> [boxed-indices] only for movable members. Groups where
        # any member is locked are excluded entirely (conservative: don't partially
        # constrain a group). Single-member groups are also pruned (degenerate).
        locked_ids = {fp.group_id for fp in self.boxed if fp.locked and fp.group_id}
        _raw: dict[str, list[int]] = {}
        for i, fp in enumerate(self.boxed):
            if fp.group_id and not fp.locked:
                _raw.setdefault(fp.group_id, []).append(i)
        self._groups: dict[str, list[int]] = {
            gid: idxs for gid, idxs in _raw.items()
            if len(idxs) >= 2 and gid not in locked_ids
        }
        grouped_fp_ids = {id(self.boxed[i]) for idxs in self._groups.values() for i in idxs}
        # Move units: each unit is a list[Footprint]. Groups are multi-fp lists;
        # ungrouped movable footprints are single-fp lists. _move() samples uniformly
        # from this list, so group size doesn't bias selection frequency.
        self._move_units: list[list[Footprint]] = (
            [[fp] for fp in self.movable if id(fp) not in grouped_fp_ids]
            + [[self.boxed[i] for i in idxs] for idxs in self._groups.values()]
        )
        # --keep-outline: contain footprints within the board's existing Edge.Cuts
        # (rather than regenerating a bounding box). The polygon and its bounds are
        # fixed for the run; edge affinity then targets the outline, not the layout
        # bbox. Falls back to no containment if the board has no closed outline.
        self._outline_poly = None
        self._outline_bounds = None
        if params.keep_outline and board.outline and not board.outline_synthesized:
            try:
                from . import geometry
                self._outline_poly = geometry.outline_to_polygon(board.outline)
                self._outline_bounds = self._outline_poly.bounds
            except Exception:                 # no closed outline -> no containment
                self._outline_poly = None
                self._outline_bounds = None

        # Density-spread grid.  When spread_weight > 0 the board area is divided
        # into a grid and the sum of squared per-cell footprint counts is penalised,
        # which drives the placement towards uniform density (Cauchy-Schwarz: the
        # sum is minimised when all counts are equal).  Uses the outline bounds when
        # available, otherwise falls back to the layout bounding box on first
        # _rebuild_cache.  The grid is rectilinear with aspect ratio matched to the
        # board so cells are approximately square.
        self._cell_size_x: float = 0.0
        self._cell_size_y: float = 0.0
        self._cell_nx: int = 0
        self._cell_ny: int = 0
        self._fp_cell: list = []        # per-boxed-index current (ci, cj)
        self._cell_counts: dict = {}    # (ci, cj) -> count
        self._spread: float = 0.0
        if params.spread_weight > 0:
            ref_bounds = (self._outline_bounds if self._outline_bounds is not None
                          else None)   # filled on first _rebuild_cache if None
            if ref_bounds is not None:
                self._init_spread_grid(ref_bounds)

    def _init_spread_grid(self, bounds: tuple) -> None:
        """Set up the density grid from *bounds* = ``(ox0, oy0, ox1, oy1)``."""
        ox0, oy0, ox1, oy1 = bounds
        bw = max(ox1 - ox0, 1e-6)
        bh = max(oy1 - oy0, 1e-6)
        nc = max(2, self.p.spread_cells)
        if bw >= bh:
            self._cell_nx = nc
            self._cell_ny = max(2, round(nc * bh / bw))
        else:
            self._cell_ny = nc
            self._cell_nx = max(2, round(nc * bw / bh))
        self._cell_size_x = bw / self._cell_nx
        self._cell_size_y = bh / self._cell_ny

    def _fp_to_cell(self, fp) -> tuple:
        """Return the ``(ci, cj)`` grid cell for footprint *fp*'s position."""
        ox0, oy0 = self._outline_bounds[0], self._outline_bounds[1]
        ci = min(self._cell_nx - 1, max(0, int((fp.x - ox0) / self._cell_size_x)))
        cj = min(self._cell_ny - 1, max(0, int((fp.y - oy0) / self._cell_size_y)))
        return (ci, cj)

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
        self._bounds = [b.bounds for b in self._boxes]
        self._conn_len = [c.est_length for c in self._conns]
        self._rats = sum(self._conn_len)
        self._overlap = self._overlap_area(self._boxes) + \
            self._fixed_text_overlap(self._boxes)
        self._bbox = self._bbox_from_bounds()
        self._edge = self._edge_sum_from_bounds(self._bounds)
        self._containment = self._containment_sum(self._boxes)
        self._congestion = self._congestion_sum(self._bounds)
        # Spread term: (re)build cell assignments from current footprint positions.
        # If the grid wasn't set up in __init__ (no outline at that point), try now
        # using the layout bounding box as the reference.
        self._spread = 0.0
        self._fp_cell = [(0, 0)] * len(self.boxed)
        self._cell_counts = {}
        if self.p.spread_weight > 0:
            if self._cell_size_x == 0.0 and self._bounds:
                # No outline → derive grid from current layout bounds
                minx = min(b[0] for b in self._bounds)
                miny = min(b[1] for b in self._bounds)
                maxx = max(b[2] for b in self._bounds)
                maxy = max(b[3] for b in self._bounds)
                self._outline_bounds = (minx, miny, maxx, maxy)
                self._init_spread_grid(self._outline_bounds)
            if self._cell_size_x > 0:
                for i, fp in enumerate(self.boxed):
                    cell = self._fp_to_cell(fp)
                    self._fp_cell[i] = cell
                    self._cell_counts[cell] = self._cell_counts.get(cell, 0) + 1
                self._spread = sum(c * c for c in self._cell_counts.values())

    def _congestion_sum(self, bounds) -> float:
        """Total congestion-field value sampled at the footprint centroids.

        Zero (and skipped) unless ``--place-feedback`` supplied a
        `router.CongestionField`. Each footprint's body-box centre is sampled and
        the values summed; minimising this term pushes parts out of the cells where
        a previous cycle's routing was congested. Like the bbox and edge terms it
        depends on the absolute layout, so it is an O(N) recompute per move.

        Args:
            bounds: per-footprint ``(minx, miny, maxx, maxy)`` tuples (e.g.
                ``self._bounds``).

        Returns:
            The summed field value (dimensionless), or ``0.0`` when no field.
        """
        field = self.p.congestion_field
        if field is None or not bounds:
            return 0.0
        total = 0.0
        for bx0, by0, bx1, by1 in bounds:
            total += field.value_at((bx0 + bx1) * 0.5, (by0 + by1) * 0.5)
        return total

    def _containment_sum(self, boxes) -> float:
        """Total footprint area protruding outside the kept board outline.

        Zero (and skipped) unless ``--keep-outline`` built a containment polygon
        (`self._outline_poly`). Penalising the area each box sticks out keeps the
        placement inside an existing Edge.Cuts instead of regenerating a bounding
        box. Each box is the buffer-inflated body box, so a small margin to the
        edge is kept automatically.

        Args:
            boxes: the per-footprint body boxes (e.g. ``self._boxes``).

        Returns:
            The summed protruding area (mm²).
        """
        poly = self._outline_poly
        if poly is None:
            return 0.0
        return sum(b.difference(poly).area for b in boxes)

    def _containment_touching(self, idxs, boxes) -> float:
        """Containment-area contribution of just the boxes in ``idxs`` (0 if off)."""
        if self._outline_poly is None:
            return 0.0
        return sum(boxes[i].difference(self._outline_poly).area for i in idxs)

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

    def _bbox_from_bounds(self) -> float:
        """Area of the layout bbox from the cached per-box ``(minx,miny,maxx,maxy)``.

        Reads the plain-tuple bounds cached in ``self._bounds`` rather than calling
        shapely ``.bounds`` on every box each step, which dominated the placement
        SA hot loop.
        """
        bnds = self._bounds
        if not bnds:
            return 0.0
        minx = min(b[0] for b in bnds)
        miny = min(b[1] for b in bnds)
        maxx = max(b[2] for b in bnds)
        maxy = max(b[3] for b in bnds)
        return max(0.0, maxx - minx) * max(0.0, maxy - miny)

    def _edge_sum_from_bounds(self, bounds) -> float:
        """Total edge-affinity distance for the flagged footprints.

        For each flagged footprint, the distance from the reference's target side
        to the box's *far* side: ``left`` = how far the box's right edge is from
        the reference's left edge, etc.; ``any`` = the smallest such distance over
        the four sides. Using the far side means the distance is *gap to the edge
        + the box's depth perpendicular to it*, so minimising it both pulls the
        part onto the boundary **and** orients it to lie flat against the edge
        (long axis parallel) rather than poking inward — otherwise a connector
        could rotate so only one pad reached the edge. The reference is the kept
        board outline's bounding box under ``--keep-outline`` (so edge parts snap
        to the real edge), otherwise the *layout* bounding box (the extent of all
        ``bounds``). ``0`` when nothing is flagged.

        Args:
            bounds: per-footprint ``(minx, miny, maxx, maxy)`` tuples, indexed
                like ``self.boxed`` (e.g. ``self._bounds``).

        Returns:
            The summed distance (mm).
        """
        if not self._flagged or not bounds:
            return 0.0
        if self._outline_bounds is not None:
            minx, miny, maxx, maxy = self._outline_bounds
        else:
            minx = min(b[0] for b in bounds)
            miny = min(b[1] for b in bounds)
            maxx = max(b[2] for b in bounds)
            maxy = max(b[3] for b in bounds)
        total = 0.0
        for fi, side in self._flagged.items():
            bx0, by0, bx1, by1 = bounds[fi]
            if side == "left":
                d = bx1 - minx
            elif side == "right":
                d = maxx - bx0
            elif side == "top":
                d = by1 - miny
            elif side == "bottom":
                d = maxy - by0
            else:                                     # "any" — nearest side
                d = min(bx1 - minx, maxx - bx0, by1 - miny, maxy - by0)
            total += d
        return total

    def _cached_energy(self) -> float:
        """Energy from the current cache (see the module docstring)."""
        return (self._rats + self.p.overlap_weight * self._overlap
                + self.p.compact_weight * self._bbox
                + self.p.edge_weight * self._edge
                + self.p.containment_weight * self._containment
                + self.p.congestion_weight * self._congestion
                + self.p.spread_weight * self._spread)

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

    def _move_delta(self, idxs: set[int]) -> None:
        """Update the energy cache for a move that touched footprints ``idxs``.

        Recomputes only the parts of the energy the move can change: the lengths
        of the ratsnest connections incident on the moved footprints, the overlap
        contributions of the moved boxes (against their neighbours and the fixed
        board silk text), and the layout bounding box. The connection *topology*
        and the per-footprint silk extents are fixed for the run, so they are
        never rebuilt here.

        The cached ``self._boxes`` for ``idxs`` must still hold the *pre-move*
        boxes when this is called; they are refreshed in place as part of the
        update. The footprints themselves must already be at their post-move
        poses (pads synced).

        Args:
            idxs: boxed-indices of the footprints whose pose changed.
        """
        # Ratsnest: only connections incident on a moved footprint change length.
        seen_conns: set[int] = set()
        for fi in idxs:
            for ci in self._fp_conns.get(fi, ()):
                seen_conns.add(ci)
        for ci in seen_conns:
            self._rats -= self._conn_len[ci]
        # Overlap + containment: remove the moved boxes' old contribution, refresh
        # the boxes, then add their new contribution. Both are per-box (a box vs
        # its neighbours / the fixed outline), so only the moved boxes change.
        self._overlap -= self._overlap_touching(idxs, self._boxes)
        self._containment -= self._containment_touching(idxs, self._boxes)
        for fi in idxs:
            self._boxes[fi] = self._fp_box(self.boxed[fi])
            self._bounds[fi] = self._boxes[fi].bounds
        self._overlap += self._overlap_touching(idxs, self._boxes)
        self._containment += self._containment_touching(idxs, self._boxes)
        for ci in seen_conns:
            nl = self._conns[ci].est_length
            self._conn_len[ci] = nl
            self._rats += nl
        # bbox + edge + congestion: cheap O(N) recompute from the cached
        # plain-tuple bounds (the bbox/edge depend on the global layout extent and
        # the congestion sample on each centroid, both of which any move can shift).
        self._bbox = self._bbox_from_bounds()
        self._edge = self._edge_sum_from_bounds(self._bounds)
        self._congestion = self._congestion_sum(self._bounds)
        # Spread: update cell occupancy for the moved footprints and recompute.
        if self.p.spread_weight > 0 and self._cell_size_x > 0:
            for fi in idxs:
                old_cell = self._fp_cell[fi]
                n = self._cell_counts.get(old_cell, 0) - 1
                if n <= 0:
                    self._cell_counts.pop(old_cell, None)
                else:
                    self._cell_counts[old_cell] = n
                new_cell = self._fp_to_cell(self.boxed[fi])
                self._cell_counts[new_cell] = self._cell_counts.get(new_cell, 0) + 1
                self._fp_cell[fi] = new_cell
            self._spread = sum(c * c for c in self._cell_counts.values())

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
        boxes = [self._fp_box(fp) for fp in self.boxed]
        edge = self._edge_sum_from_bounds([b.bounds for b in boxes])
        containment = self._containment_sum(boxes)
        congestion = self._congestion_sum([b.bounds for b in boxes])
        return (rats + self.p.overlap_weight * overlap
                + self.p.compact_weight * bbox_area + self.p.edge_weight * edge
                + self.p.containment_weight * containment
                + self.p.congestion_weight * congestion)

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
        """Apply one random move and return its undo snapshot and touched indices.

        Moves operate on "units" — either a single footprint or a KiCad group
        (all members move together as a rigid body). Groups are sampled from
        ``self._move_units`` with the same probability as individual footprints.

        Args:
            temp_frac: ``T / t_start`` in ``[t_end/t_start, 1]``; scales the
                translate step so moves shrink as the schedule cools.

        Returns:
            ``(snapshot, idxs)`` — the `_snapshot` of the footprints touched (for
            `_restore`) and the set of their boxed-indices (for `_move_delta`).
        """
        r = self.rng.random()
        if len(self._move_units) >= 2 and r < self.p.swap_prob:
            # Swap: exchange the centroids of two units while preserving internal
            # relative offsets within each group.
            ua, ub = self.rng.sample(self._move_units, 2)
            snap = self._snapshot(ua + ub)
            cax = sum(fp.x for fp in ua) / len(ua)
            cay = sum(fp.y for fp in ua) / len(ua)
            cbx = sum(fp.x for fp in ub) / len(ub)
            cby = sum(fp.y for fp in ub) / len(ub)
            dx, dy = cbx - cax, cby - cay
            for fp in ua:
                fp.x += dx; fp.y += dy; fp.sync_pads()
            for fp in ub:
                fp.x -= dx; fp.y -= dy; fp.sync_pads()
            return snap, {self._idx_of_fp[id(fp)] for fp in ua + ub}

        unit = self.rng.choice(self._move_units)
        snap = self._snapshot(unit)
        if self.p.rotate_mode != "none" and r < 0.5:         # rotate
            if self.p.rotate_mode == "free":
                delta = self.rng.uniform(0.0, 360.0)
            else:                                            # "ortho"
                delta = self.rng.choice((90.0, -90.0, 180.0))
            if len(unit) == 1:
                unit[0].angle = (unit[0].angle + delta) % 360.0
                unit[0].sync_pads()
            else:
                # Rotate every member's origin around the group centroid.
                cx = sum(fp.x for fp in unit) / len(unit)
                cy = sum(fp.y for fp in unit) / len(unit)
                for fp in unit:
                    rx, ry = rotate(fp.x - cx, fp.y - cy, delta)
                    fp.x = cx + rx
                    fp.y = cy + ry
                    fp.angle = (fp.angle + delta) % 360.0
                    fp.sync_pads()
        else:                                                # translate
            s = self.p.step * temp_frac
            dx = self.rng.uniform(-s, s)
            dy = self.rng.uniform(-s, s)
            for fp in unit:
                fp.x += dx; fp.y += dy; fp.sync_pads()
        return snap, {self._idx_of_fp[id(fp)] for fp in unit}

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
        if not self._move_units:
            self._rebuild_cache()
            E = self._cached_energy()
            return PlaceResult(E, E, 0, 0, 0,
                               self._rats, self._overlap, self._bbox, self._edge)

        self._rebuild_cache()
        E = self._cached_energy()
        start_E = best_E = E
        best = self._snapshot(self.movable)
        accepted = 0
        recent = deque(maxlen=_ACCEPT_WINDOW)   # 1/0 per recent move, for the live ratio

        # Stall detection: count consecutive completed accept-windows whose
        # acceptance ratio stayed below `stall_ratio`; break after `stall_patience`
        # of them. Disabled when either knob is non-positive.
        stall_on = self.p.stall_patience > 0 and self.p.stall_ratio > 0.0
        stall_count = 0
        window_seen = 0

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

            snap, idxs = self._move(T / self.p.t_start)
            # Snapshot the cache entries the move can disturb, for a cheap revert:
            # the scalar totals, the moved boxes, and the lengths of the
            # connections incident on the moved footprints.
            touched_conns = {ci for i in idxs for ci in self._fp_conns.get(i, ())}
            _sp_active = self.p.spread_weight > 0 and self._cell_size_x > 0
            cache_save = (self._rats, self._overlap, self._bbox,
                          self._edge, self._containment,
                          {i: (self._boxes[i], self._bounds[i]) for i in idxs},
                          {ci: self._conn_len[ci] for ci in touched_conns},
                          self._spread,
                          {i: self._fp_cell[i] for i in idxs} if _sp_active else {},
                          self._cell_counts.copy() if _sp_active else {})
            self._move_delta(idxs)
            E_new = self._cached_energy()
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
                (self._rats, self._overlap, self._bbox,
                 self._edge, self._containment,
                 saved_boxes, saved_lens,
                 self._spread, saved_fp_cells, saved_cc) = cache_save
                for i, (b, bnd) in saved_boxes.items():
                    self._boxes[i] = b
                    self._bounds[i] = bnd
                for ci, ln in saved_lens.items():
                    self._conn_len[ci] = ln
                if _sp_active:
                    for i, cell in saved_fp_cells.items():
                        self._fp_cell[i] = cell
                    self._cell_counts = saved_cc
            recent.append(1 if accept else 0)

            it += 1
            window_seen += 1
            if on_progress is not None:
                on_progress(it, total, E, best_E, T, sum(recent) / len(recent))

            if stall_on and window_seen >= _ACCEPT_WINDOW:
                if sum(recent) / len(recent) < self.p.stall_ratio:
                    stall_count += 1
                    if stall_count >= self.p.stall_patience:
                        break
                else:
                    stall_count = 0
                window_seen = 0

        self._restore(best)
        moved = sum(1 for fp in self.board.footprints if fp.moved)
        # Recompute the cache at the best placement so the reported breakdown is
        # exactly consistent with `best_E` (both use the fixed ratsnest topology),
        # then re-derive best_E from it so they reconcile to the last bit.
        self._rebuild_cache()
        best_E = self._cached_energy()
        return PlaceResult(start_E, best_E, it, accepted, moved,
                           self._rats, self._overlap, self._bbox, self._edge)


def recenter(board: Board) -> tuple[float, float]:
    """Translate the placed footprints back onto their original centroid.

    Placement energy (ratsnest + overlap + bbox-area) is *translation-invariant*:
    none of its terms depends on where the cluster sits, only on the footprints'
    relative poses. With nothing locked, the whole group therefore random-walks
    during annealing and drifts away from the board origin — the more iterations
    run, the further it migrates. This shifts every movable footprint by a single
    rigid offset so the movable footprints' centroid returns to where it started,
    leaving every energy term (and so the placement result) exactly unchanged.

    A rigid translation only restores the original position when the layout is
    free to move as a whole, so this is a no-op when any footprint is locked:
    locked footprints are fixed obstacles that anchor the layout in absolute
    coordinates, so there is no drift to correct and shifting the movable group
    would only break its alignment with the anchors.

    Args:
        board: the board whose movable footprints are recentred in place (pads
            re-synced).

    Returns:
        The ``(dx, dy)`` offset applied (``(0.0, 0.0)`` when nothing moved, e.g.
        no movable footprints or any footprint locked).
    """
    movable = [fp for fp in board.footprints if fp.pads and not fp.locked]
    if not movable or any(fp.locked for fp in board.footprints if fp.pads):
        return 0.0, 0.0
    n = len(movable)
    cur_cx = sum(fp.x for fp in movable) / n
    cur_cy = sum(fp.y for fp in movable) / n
    orig_cx = sum(fp.x0 for fp in movable) / n
    orig_cy = sum(fp.y0 for fp in movable) / n
    dx, dy = orig_cx - cur_cx, orig_cy - cur_cy
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return 0.0, 0.0
    for fp in movable:
        fp.x += dx
        fp.y += dy
        fp.sync_pads()
    return dx, dy


def place(board: Board, params: PlaceParams | None = None,
          on_progress=None, runs: int = 1, cancel=None) -> PlaceResult:
    """Place a board's footprints by simulated annealing; return the best seen.

    Mutates `board`'s footprint poses (and their pads) in place, leaving them at
    the best placement found. Locked footprints are held fixed; footprints flagged
    ``Autoroute-overlap`` may overlap others' bodies but not their pads; those
    flagged ``Autoroute-edge=<side>`` are pulled to the board boundary and oriented
    flat against it (`edge_weight`). Call
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
    # keep_outline ties the placement to a fixed outline (absolute coordinates),
    # so the layout must not be re-centred afterwards — that would shift parts
    # back out of the outline they were contained within. A congestion field is
    # likewise absolute (it anchors parts to/away from board cells), so recentring
    # would invalidate it too.
    do_recenter = not params.keep_outline and params.congestion_field is None
    if runs <= 1:
        result = _Placer(board, params).run(on_progress, cancel)
        if do_recenter:
            recenter(board)           # undo translation-invariant drift
        return result

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
    if do_recenter:
        recenter(board)                          # undo translation-invariant drift
    return best
