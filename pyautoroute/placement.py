"""Simulated-annealing footprint placement.

An opt-in pass (``--place``) that arranges the board's footprints *before*
routing, the placement analogue of `pyautoroute.anneal`: where the annealer moves
tracks, this moves footprint positions/rotations to minimise rats-nest length
while keeping bodies from overlapping and pulling the layout together.

Energy ``E = ratsnest + overlap_weight·overlap_area + compact_weight·bbox_area
+ edge_weight·edge_distance + containment_weight·area_outside_outline
+ congestion_weight·Σ field(centroid) + spread_weight·Σ_cell count²
+ decouple_weight·Σ dist(cap, IC)``:

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
- **Σ dist(cap, IC)** — for each footprint marked as a *decoupling cap* via the
  ``Autoroute-decouple`` property (`pcb.Footprint.decouple_target`: an IC refdes,
  or ``auto`` to resolve the IC by net search,
  `pyautoroute.netlist.resolve_decoupling_ic`). The cap is softly pulled toward
  its IC's centroid, so it settles next to it instead of drifting — a flexible
  alternative to a rigid KiCad group. The overlap/buffer term keeps the cap from
  landing *on* the IC, so the cap seats at the buffer gap. Translation-invariant
  (depends only on the cap↔IC separation). Zero when no cap is marked or
  ``decouple_weight = 0``.

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


def _board_silk_text_boxes(board: Board, skip_uuids: frozenset[str] | None = None):
    """Return a Shapely polygon for each visible board-level silk text.

    Board-level ``gr_text`` items — connector pin labels, a title block, etc. —
    are not footprints, so the placer would otherwise ignore them and happily
    drop a footprint on top (the locked "Bus Indicator" / pin-label text on the
    Test1 board). Each polygon is a tight rotated rectangle that covers the text
    extent, in board coordinates.

    Items whose UUID appears in *skip_uuids* are omitted — they are grouped with
    a footprint and handled as part of that footprint's bounding box instead.

    Args:
        board: the board whose top-level ``gr_text`` nodes are scanned.
        skip_uuids: set of gr_text UUIDs to exclude (grouped text that moves
            with a footprint rather than acting as a fixed obstacle).

    Returns:
        One Shapely polygon per visible, non-hidden silkscreen ``gr_text``.
    """
    from .pcb import children, child, strings, floats, atoms_after_head
    from shapely.affinity import rotate as sh_rotate

    out = []
    for txt in children(board.tree, "gr_text"):
        # Skip text that is grouped with a footprint — it is included in that
        # footprint's bounding box and travels with it (not a fixed obstacle).
        if skip_uuids:
            uuid_node = child(txt, "uuid")
            if uuid_node:
                uvals = atoms_after_head(uuid_node)
                if uvals and uvals[0].text in skip_uuids:
                    continue
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
    scatter_start: bool = False         # scatter unlocked footprints randomly before annealing
    # Post-anneal polish (steepest descent): after the SA loop settles on its
    # best placement, optionally refine it by gradient descent — relaxes close
    # contacts and slides parts into their local energy minimum. Translations
    # only; monotone (only strictly-improving steps are taken), so it can never
    # worsen the annealed result. Disabled by default (``polish=False``).
    polish: bool = False
    polish_iters: int = 20            # max descent sweeps over all movable units
    polish_time: float | None = None  # optional wall-clock cap (s) for the polish
    polish_eps: float = 0.05          # finite-difference step (mm) for the gradient
    polish_step: float = 0.5          # initial line-search distance (mm)
    polish_min_step: float = 0.01     # smallest line-search distance before giving up
    polish_tol: float = 1e-3          # stop when a sweep improves E by less than this (relative)
    # Decoupling-cap attraction: footprints marked with the ``Autoroute-decouple``
    # property (`pcb.Footprint.decouple_target`) are softly pulled toward their
    # associated IC, so a decoupling cap settles next to it instead of drifting.
    # mm-cost per mm of cap→IC centroid distance; 0 disables the term.
    decouple_weight: float = 5.0


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
    polish_sweeps: int = 0            # descent sweeps run by the post-anneal polish (0 if off)
    polish_improvement: float = 0.0   # energy reduction from the polish (>= 0)
    warnings: list[str] = field(default_factory=list)  # e.g. unresolved decouple targets

    @property
    def accept_ratio(self) -> float:
        """Fraction of proposed moves accepted over the run (0 if none made)."""
        return self.accepted / self.iterations if self.iterations else 0.0


def _half_extent(pad: Pad) -> float:
    """Rotation-independent half-extent of a pad (half its bounding diagonal)."""
    return 0.5 * math.hypot(pad.w, pad.h)


def _fp_centroid(fp: Footprint) -> tuple[float, float]:
    """Centroid of a footprint's pad centres (its origin if it has no pads)."""
    if not fp.pads:
        return (fp.x, fp.y)
    n = len(fp.pads)
    return (sum(p.cx for p in fp.pads) / n, sum(p.cy for p in fp.pads) / n)


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
        # Extend _text_extents with grouped gr_text items (top-level text that
        # shares a KiCad group with a footprint).  Their extents are expressed
        # in the footprint's local pre-rotation frame so _fp_box can use them
        # at the footprint's current angle.  Exclude their UUIDs from the fixed-
        # obstacle list since they move with their footprint.
        from . import pcb as _pcb
        _grouped_gr = _pcb.gr_text_group_fps(board)
        _grouped_uuids = frozenset(_grouped_gr.keys())
        for _tuuid, (_tnode, _fps) in _grouped_gr.items():
            _at = _pcb.floats(_pcb.child(_tnode, "at"))
            if len(_at) < 2:
                continue
            _tx, _ty = _at[0], _at[1]
            _atoms = _pcb.atoms_after_head(_tnode)
            _content = _atoms[0].text if _atoms else ""
            _eff = _pcb.child(_tnode, "effects")
            _fnt = _pcb.child(_eff, "font") if _eff is not None else None
            _sz = _pcb.child(_fnt, "size") if _fnt is not None else None
            _szv = _pcb.floats(_sz) if _sz is not None else []
            _fh = _szv[0] if _szv else 1.0
            _hr = 0.5 * math.hypot(len(_content) * _fh * 0.7, _fh * 1.3)
            # Add the text extent to every footprint in the group (each
            # expressed in that footprint's local frame).  The local coord is
            # constant as the group rotates rigidly (verified by the transform
            # invariant: rotate(board_rel, -angle) is preserved under rigid motion).
            for _fp in _fps:
                _brel_x = _tx - _fp.x0
                _brel_y = _ty - _fp.y0
                _lx, _ly = rotate(_brel_x, _brel_y, -_fp.angle0)
                self._text_extents.setdefault(id(_fp), []).append((_lx, _ly, _hr))
        # Fixed board-level silkscreen text (pin labels, title block): static
        # keep-out polygons footprints must avoid.  Grouped text is excluded
        # because it moves with its footprint (handled via _text_extents above).
        _text_polys = _board_silk_text_boxes(board, skip_uuids=_grouped_uuids)
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
        # Decoupling-cap attraction: each footprint marked `decouple_target` is
        # softly pulled toward its associated IC. Resolve the target to a boxed
        # index here (an "auto" target is resolved by net search); collect any
        # warnings for the caller. Pairs are (cap-index, ic-index); the per-pair
        # distance is cached and updated incrementally like the ratsnest term.
        self._decouple_pairs: list[tuple[int, int]] = []
        self._decouple_warnings: list[str] = []
        if self.p.decouple_weight > 0:
            self._build_decouple_pairs()
        self._fp_decouple: dict[int, list[int]] = {}
        for pi, (a, b) in enumerate(self._decouple_pairs):
            self._fp_decouple.setdefault(a, []).append(pi)
            self._fp_decouple.setdefault(b, []).append(pi)
        self._decouple_len: list[float] = []   # per-pair cap→IC distance
        self._decouple = 0.0                    # cached Σ distance
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

    def _build_decouple_pairs(self) -> None:
        """Resolve ``decouple_target`` marks into (cap-index, IC-index) pairs.

        A concrete refdes target is looked up directly; ``"auto"`` is resolved by
        net search (`netlist.resolve_decoupling_ic`). Unresolvable or self-target
        marks are skipped with a warning recorded in ``self._decouple_warnings``.
        """
        ref_to_idx: dict[str, int] = {}
        for i, fp in enumerate(self.boxed):
            ref_to_idx.setdefault(fp.ref, i)        # first wins; dup handled by resolver
        for ci, fp in enumerate(self.boxed):
            tgt = fp.decouple_target
            if not tgt:
                continue
            if tgt == "auto":
                ref, _cands, warn = netlist.resolve_decoupling_ic(self.board, fp)
                if warn:
                    self._decouple_warnings.append(warn)
                tgt = ref
            if not tgt:
                continue
            ii = ref_to_idx.get(tgt)
            if ii is None:
                self._decouple_warnings.append(
                    f"{fp.ref}: decouple target {tgt!r} not found on the board")
            elif ii == ci:
                self._decouple_warnings.append(
                    f"{fp.ref}: decouple target {tgt!r} is the cap itself")
            else:
                self._decouple_pairs.append((ci, ii))

    def _pair_distance(self, a: int, b: int) -> float:
        """Centroid distance between two boxed footprints (for a decouple pair)."""
        ax, ay = _fp_centroid(self.boxed[a])
        bx, by = _fp_centroid(self.boxed[b])
        return math.hypot(ax - bx, ay - by)

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
        # Decoupling attraction: per-pair cap→IC centroid distance.
        self._decouple_len = [self._pair_distance(a, b)
                              for (a, b) in self._decouple_pairs]
        self._decouple = sum(self._decouple_len)
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
        cfield = self.p.congestion_field
        if cfield is None or not bounds:
            return 0.0
        total = 0.0
        for bx0, by0, bx1, by1 in bounds:
            total += cfield.value_at((bx0 + bx1) * 0.5, (by0 + by1) * 0.5)
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
                + self.p.spread_weight * self._spread
                + self.p.decouple_weight * self._decouple)

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

    def _save_cache(self, idxs: set[int]):
        """Snapshot the energy-cache entries a move over ``idxs`` can disturb.

        Captures the scalar energy totals, the moved boxes/bounds, the lengths of
        the connections incident on the moved footprints, and (when active) the
        spread cell bookkeeping. Pass the returned token to `_load_cache` to
        revert the cache to exactly this state (paired with restoring the poses
        via `_restore`). Shared by the SA loop's reject path and the polish
        stage's probe/revert.

        Args:
            idxs: boxed-indices of the footprints the move touches.

        Returns:
            An opaque snapshot token for `_load_cache`.
        """
        touched = {ci for i in idxs for ci in self._fp_conns.get(i, ())}
        touched_dec = {pi for i in idxs for pi in self._fp_decouple.get(i, ())}
        sp = self.p.spread_weight > 0 and self._cell_size_x > 0
        return (self._rats, self._overlap, self._bbox, self._edge,
                self._containment, self._congestion,
                {i: (self._boxes[i], self._bounds[i]) for i in idxs},
                {ci: self._conn_len[ci] for ci in touched},
                self._spread,
                {i: self._fp_cell[i] for i in idxs} if sp else None,
                self._cell_counts.copy() if sp else None,
                self._decouple,
                {pi: self._decouple_len[pi] for pi in touched_dec})

    def _load_cache(self, saved) -> None:
        """Revert the energy cache to a `_save_cache` snapshot."""
        (self._rats, self._overlap, self._bbox, self._edge,
         self._containment, self._congestion,
         boxes, lens, self._spread, fp_cells, cc,
         self._decouple, dec_lens) = saved
        for i, (b, bnd) in boxes.items():
            self._boxes[i] = b
            self._bounds[i] = bnd
        for ci, ln in lens.items():
            self._conn_len[ci] = ln
        if fp_cells is not None:
            for i, cell in fp_cells.items():
                self._fp_cell[i] = cell
            self._cell_counts = cc
        for pi, ln in dec_lens.items():
            self._decouple_len[pi] = ln

    def _energy_after_translate(self, unit, idxs: set[int],
                                dx: float, dy: float) -> float:
        """Energy if ``unit`` were shifted by ``(dx, dy)``; poses + cache unchanged.

        Applies the shift, updates the cache incrementally, reads the resulting
        energy, then reverts both the footprint poses and the cache. Used by the
        polish stage's finite-difference gradient and line search.

        Args:
            unit: the move unit (list of footprints) to probe.
            idxs: the unit's boxed-indices.
            dx: x shift (mm).
            dy: y shift (mm).

        Returns:
            The energy the shift would produce.
        """
        snap = self._snapshot(unit)
        saved = self._save_cache(idxs)
        for fp in unit:
            fp.x += dx
            fp.y += dy
            fp.sync_pads()
        self._move_delta(idxs)
        e = self._cached_energy()
        self._restore(snap)
        self._load_cache(saved)
        return e

    def _commit_translate(self, unit, idxs: set[int],
                          dx: float, dy: float) -> float:
        """Shift ``unit`` by ``(dx, dy)`` permanently; return the new cached energy."""
        for fp in unit:
            fp.x += dx
            fp.y += dy
            fp.sync_pads()
        self._move_delta(idxs)
        return self._cached_energy()

    def _polish(self, on_progress=None, cancel=None) -> tuple[int, float]:
        """Steepest-descent refinement after annealing.

        For each movable unit (single footprint or KiCad group), estimate the 2-D
        energy gradient of its translation by central finite differences, then
        step along the normalised descent direction with a backtracking line
        search, committing only strictly-improving steps. Sweep over all units,
        repeating until a sweep barely helps, the sweep budget is spent, the
        optional time budget elapses, or ``cancel`` is set.

        Every committed step strictly lowers `_cached_energy`, so the polish can
        never worsen the annealed placement. Angles are held fixed (translations
        only), and locked footprints — absent from ``_move_units`` — are untouched.

        Args:
            on_progress: optional callback ``(sweep, max_sweeps, energy)`` per sweep.
            cancel: optional `threading.Event`; stops between/within sweeps.

        Returns:
            ``(sweeps_done, improvement)`` — sweeps run and the total energy
            reduction (``>= 0``).
        """
        if not self.p.polish or not self._move_units or self.p.polish_iters <= 0:
            return 0, 0.0
        eps = self.p.polish_eps
        t0 = time.time()
        units = [(u, {self._idx_of_fp[id(fp)] for fp in u})
                 for u in self._move_units]
        E0 = E = self._cached_energy()
        sweeps = 0
        for _ in range(self.p.polish_iters):
            if cancel is not None and cancel.is_set():
                break
            if (self.p.polish_time is not None
                    and time.time() - t0 >= self.p.polish_time):
                break
            sweep_start = E
            for unit, idxs in units:
                gx = (self._energy_after_translate(unit, idxs, eps, 0.0)
                      - self._energy_after_translate(unit, idxs, -eps, 0.0)) / (2.0 * eps)
                gy = (self._energy_after_translate(unit, idxs, 0.0, eps)
                      - self._energy_after_translate(unit, idxs, 0.0, -eps)) / (2.0 * eps)
                g = math.hypot(gx, gy)
                if g < 1e-12:
                    continue
                dx, dy = -gx / g, -gy / g          # unit descent direction (mm)
                step = self.p.polish_step
                while step >= self.p.polish_min_step:
                    if self._energy_after_translate(
                            unit, idxs, dx * step, dy * step) < E - 1e-12:
                        E = self._commit_translate(unit, idxs, dx * step, dy * step)
                        break
                    step *= 0.5
            sweeps += 1
            if on_progress is not None:
                on_progress(sweeps, self.p.polish_iters, E)
            if sweep_start - E <= self.p.polish_tol * max(1.0, abs(E)):
                break
        return sweeps, E0 - E

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
        # Decoupling: recompute the distance of each pair incident on a moved
        # footprint (either the cap or its IC may have moved).
        seen_dec: set[int] = set()
        for fi in idxs:
            for pi in self._fp_decouple.get(fi, ()):
                seen_dec.add(pi)
        for pi in seen_dec:
            self._decouple -= self._decouple_len[pi]
            a, b = self._decouple_pairs[pi]
            nl = self._pair_distance(a, b)
            self._decouple_len[pi] = nl
            self._decouple += nl

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

    def run(self, on_progress=None, cancel=None,
            on_polish_progress=None) -> PlaceResult:
        """Run the annealing loop; leave the board at the best placement seen.

        Args:
            on_progress: optional callback ``(it, total, energy, best, temp,
                accept)`` invoked each iteration, where ``accept`` is the fraction
                of moves accepted over the last ``_ACCEPT_WINDOW`` iterations.
            cancel: optional `threading.Event`; when set, the loop stops early and
                the board is left at the best placement found so far (for a GUI
                Stop button).
            on_polish_progress: optional callback ``(sweep, max_sweeps, energy)``
                invoked after each polish descent sweep; ``None`` suppresses it.

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
                               self._rats, self._overlap, self._bbox, self._edge,
                               warnings=list(self._decouple_warnings))

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
            # Snapshot the cache entries the move can disturb, for a cheap revert
            # on a rejected move (see `_save_cache`/`_load_cache`).
            saved = self._save_cache(idxs)
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
                self._load_cache(saved)
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
        # Recompute the cache at the best placement so the reported breakdown is
        # exactly consistent with `best_E` (both use the fixed ratsnest topology),
        # then re-derive best_E from it so they reconcile to the last bit.
        self._rebuild_cache()
        best_E = self._cached_energy()
        # Optional post-anneal polish: monotone gradient descent that only ever
        # lowers the energy, so best_E (and the breakdown) can only improve.
        polish_sweeps, polish_improvement = self._polish(
            on_progress=on_polish_progress, cancel=cancel)
        if polish_sweeps:
            best_E = self._cached_energy()
        moved = sum(1 for fp in self.board.footprints if fp.moved)
        return PlaceResult(start_E, best_E, it, accepted, moved,
                           self._rats, self._overlap, self._bbox, self._edge,
                           polish_sweeps=polish_sweeps,
                           polish_improvement=polish_improvement,
                           warnings=list(self._decouple_warnings))


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


def scatter_footprints(board: Board, seed: int) -> None:
    """Randomly scatter unlocked footprints across the board area.

    Gives each ``--cycles`` run or ``--place-runs`` pass a genuinely different
    starting layout so the placement annealer explores different basins of
    attraction rather than always refining the as-designed configuration. KiCad
    groups with 2+ footprints are treated as rigid units: all footprint members
    move together. Top-level ``gr_text`` items that belong to the same group are
    handled later by `pcb.sync_tree_from_placement`, which applies the full
    original-to-final transformation using ``fp.x0/y0``. Positions are drawn
    uniformly within the board outline's bounding box (or the current layout
    bounding box when no real outline exists). Rotations are sampled from
    {0, 90, 180, 270}°. Locked footprints are untouched.

    Args:
        board: the board to scatter (mutated in place).
        seed: RNG seed for reproducibility.
    """
    import random

    movable = [fp for fp in board.footprints if not fp.locked]
    if not movable:
        return

    try:
        from . import geometry
        if board.outline and not board.outline_synthesized:
            poly = geometry.outline_to_polygon(board.outline)
            ox0, oy0, ox1, oy1 = poly.bounds
        else:
            raise ValueError("no real outline")
    except Exception:
        xs = [fp.x for fp in movable]
        ys = [fp.y for fp in movable]
        span_x = max(xs) - min(xs) or 10.0
        span_y = max(ys) - min(ys) or 10.0
        ox0 = min(xs) - span_x * 0.1
        oy0 = min(ys) - span_y * 0.1
        ox1 = max(xs) + span_x * 0.1
        oy1 = max(ys) + span_y * 0.1

    # Build rigid groups: exclude groups with locked members; require 2+ footprints.
    # Single-footprint groups (with or without associated text) don't need special
    # handling here — each such footprint scatters independently and the text
    # follows via sync_tree_from_placement.
    locked_ids = {fp.group_id for fp in board.footprints if fp.locked and fp.group_id}
    groups_dict: dict[str, list[Footprint]] = {}
    for fp in movable:
        if fp.group_id and fp.group_id not in locked_ids:
            groups_dict.setdefault(fp.group_id, []).append(fp)

    groups = [fps for fps in groups_dict.values() if len(fps) >= 2]
    grouped_ids = {id(fp) for group in groups for fp in group}
    ungrouped = [fp for fp in movable if id(fp) not in grouped_ids]

    rng = random.Random(seed)
    ortho = [0.0, 90.0, 180.0, 270.0]

    # Scatter ungrouped footprints independently.
    for fp in ungrouped:
        fp.x = rng.uniform(ox0, ox1)
        fp.y = rng.uniform(oy0, oy1)
        fp.angle = rng.choice(ortho)
        fp.sync_pads()

    # Scatter multi-footprint groups as rigid units.
    for group in groups:
        anchor_x = rng.uniform(ox0, ox1)
        anchor_y = rng.uniform(oy0, oy1)
        angle_delta = rng.choice(ortho) - group[0].angle

        cx = sum(fp.x for fp in group) / len(group)
        cy = sum(fp.y for fp in group) / len(group)

        for fp in group:
            rel_x = fp.x - cx
            rel_y = fp.y - cy
            if angle_delta != 0.0:
                rotated_x, rotated_y = rotate(rel_x, rel_y, angle_delta)
            else:
                rotated_x, rotated_y = rel_x, rel_y
            fp.x = anchor_x + rotated_x
            fp.y = anchor_y + rotated_y
            fp.angle = (fp.angle + angle_delta) % 360.0
            fp.sync_pads()


def place(board: Board, params: PlaceParams | None = None,
          on_progress=None, runs: int = 1, cancel=None,
          on_polish_progress=None) -> PlaceResult:
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
        on_polish_progress: optional callback ``(sweep, max_sweeps, energy)``
            invoked after each polish descent sweep (see `_Placer.run`).

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
        result = _Placer(board, params).run(on_progress, cancel,
                                            on_polish_progress=on_polish_progress)
        if do_recenter:
            recenter(board)           # undo translation-invariant drift
        return result

    orig = [(fp, fp.x, fp.y, fp.angle) for fp in board.footprints]
    best: PlaceResult | None = None
    best_poses = None
    for k in range(runs):
        if cancel is not None and cancel.is_set():
            break
        if params.scatter_start:
            scatter_footprints(board, params.seed + k)
        else:
            for fp, x, y, a in orig:             # restart from the original layout
                fp.x, fp.y, fp.angle = x, y, a
                fp.sync_pads()
        result = _Placer(board, replace(params, seed=params.seed + k)).run(
            on_progress, cancel, on_polish_progress=on_polish_progress)
        if best is None or result.best_energy < best.best_energy:
            best = result
            best_poses = [(fp, fp.x, fp.y, fp.angle) for fp in board.footprints]
    if best is None:                             # cancelled before any run finished
        return _Placer(board, params).run(on_progress, cancel,
                                          on_polish_progress=on_polish_progress)
    for fp, x, y, a in best_poses:               # leave the board at the best
        fp.x, fp.y, fp.angle = x, y, a
        fp.sync_pads()
    if do_recenter:
        recenter(board)                          # undo translation-invariant drift
    return best


def energy_heatmap(board, params=None):
    """Compute per-footprint and per-connection energy data for heat-map rendering.

    Builds the placer's connection index and computes the current energy
    breakdown without running the annealer. Fast enough to call on-demand
    after placement or on a live snapshot.

    Args:
        board: the placed board to analyse.
        params: placement parameters (weights); ``None`` uses defaults.

    Returns:
        A ``(fp_heat, conn_heat)`` pair:

        - ``fp_heat``: ``{ref: (minx, miny, maxx, maxy, norm)}`` — per-footprint
          bounding box and a 0–1 normalized combined energy (ratsnest + overlap).
        - ``conn_heat``: ``[(x1, y1, x2, y2, norm)]`` — per MST connection with
          0–1 normalized length (0 = shortest, 1 = longest on this board).
    """
    params = params or PlaceParams()
    p = _Placer(board, params)
    p._rebuild_cache()

    # Per-footprint ratsnest contribution (sum of incident connection lengths).
    fp_rats = {i: sum(p._conn_len[ci] for ci in p._fp_conns.get(i, []))
               for i in range(len(p.boxed))}

    # Per-footprint overlap cost (weighted).
    fp_overlap = {i: p._overlap_touching({i}, p._boxes) * params.overlap_weight
                  for i in range(len(p.boxed))}

    costs = {i: fp_rats[i] + fp_overlap[i] for i in range(len(p.boxed))}
    max_cost = max(costs.values(), default=0.0) or 1.0

    fp_heat = {}
    for i, fp in enumerate(p.boxed):
        if i < len(p._boxes):
            minx, miny, maxx, maxy = p._boxes[i].bounds
            fp_heat[fp.ref] = (minx, miny, maxx, maxy, costs[i] / max_cost)

    max_len = max(p._conn_len, default=0.0) or 1.0
    conn_heat = [
        (c.a.cx, c.a.cy, c.b.cx, c.b.cy, p._conn_len[ci] / max_len)
        for ci, c in enumerate(p._conns)
    ]

    return fp_heat, conn_heat
