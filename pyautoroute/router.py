"""A* maze router plus an incremental routing state that supports rip-up.

The cost model encodes the optimisation priorities below wirelength: a diagonal
step costs its true length (so a 45 degree run beats a 90 degree staircase), a
direction change adds a bend penalty (90 degree bends cost more than 45), a layer
change adds a via penalty, and B.Cu adds a small per-step penalty so F.Cu wins
ties. The heuristic is octile distance to the nearest target (admissible, since
every extra cost term is non-negative).

``RoutingState`` layers the dynamic copper (routed tracks/vias) on top of the
grid's static occupancy (pads + board edge). It is keyed per *connection* so two
connections of the same net never block each other and any single connection can
be ripped up and rerouted exactly — the foundation for simulated annealing.
"""

from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass

import numpy as np
import shapely
from shapely.geometry import LineString, Point

from .grid import FREE, Grid
from .sexpr import SList

try:
    from pyautoroute._astar_c import astar as _astar_fast
    _USE_C_ASTAR = True
except ImportError:
    _astar_fast = None
    _USE_C_ASTAR = False

SQRT2 = math.sqrt(2.0)

# `RoutingState.cover_owner` sentinels (distinct from any net id, which are ≥0).
_COVER_EMPTY = -1     # node covered by no committed copper
_COVER_MIXED = -2     # node covered by 2+ different nets (blocks everyone)


# A pad-centre stub shorter than this (mm) is dropped as a zero-length segment:
# the path's terminal node already coincides with the pad anchor.
_STUB_EPS = 1e-3

# 8 compass directions, indexed 0..7 (used for bend-penalty bookkeeping)
_DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]


@dataclass
class RouteParams:
    bend45: float = 0.05          # mm added per 45-degree direction change
    bend90: float = 0.20
    bend135: float = 0.40
    bend180: float = 0.80
    via_cost: float = 2.0         # mm-equivalent penalty for a layer change
    back_layer_penalty: float = 0.02   # mm per step on a non-front layer
    max_expansions: int = 2_000_000
    # When set, bound each search to a box around the source/target nodes grown
    # by this many mm on every side, widening the box and retrying on failure
    # (falling back to the full grid). Trades a little path optimality for a much
    # smaller search on large boards; `None` searches the whole grid (optimal).
    search_margin: float | None = None

    def bend(self, turn_units: int) -> float:
        """Penalty for a direction change of `turn_units` 45-degree steps.

        Args:
            turn_units: number of 45-degree increments turned (0-4).

        Returns:
            The bend penalty in mm-equivalent (0 for going straight).
        """
        return (0.0, self.bend45, self.bend90, self.bend135, self.bend180)[turn_units]


def _turn_units(a: int, b: int) -> int:
    """Turn magnitude between two of the 8 compass directions, in 45-deg steps.

    Args:
        a: previous direction index (0-7), or ``< 0`` for "no previous step".
        b: next direction index (0-7).

    Returns:
        The smaller turn magnitude in 45-degree units (0-4); 0 if either index
        is negative.
    """
    if a < 0 or b < 0:
        return 0
    d = abs(a - b)
    return min(d, 8 - d)


@dataclass
class RouteResult:
    net: str
    path: list[tuple[int, int, int]]   # (layer_idx, col, row)
    length: float                      # routed wirelength (mm)
    vias: int
    # pad anchor (centre) coordinates of the two endpoints, if known. The writer
    # stubs the path's terminal nodes to these so each track ends on the pad
    # anchor — KiCad then keeps the track attached when the footprint is moved.
    src_xy: tuple[float, float] | None = None
    dst_xy: tuple[float, float] | None = None


@dataclass
class BoardRouting:
    results: list[RouteResult | None]  # indexed by connection index
    routed: int
    unrouted: int
    total_length: float
    total_vias: int


# --- congestion feedback ------------------------------------------------------
#
# A coarse, board-wide "where did routing struggle?" heatmap, fed back into the
# next cycle's placement (`--place-feedback`) to spread footprints out of the hot
# zones. It is intentionally low-resolution (a small multiple of the routing
# pitch) and lives in a *fixed* board frame so successive cycles — whose
# per-cycle routing grids differ as footprints move — accumulate onto one common
# raster. Routing itself is untouched: the field is read off the routed results.


@dataclass
class CongestionField:
    """A coarse heatmap of routing difficulty over a fixed board frame.

    The board area is rasterised into ``ny × nx`` cells of side ``cell`` mm with
    origin ``(minx, miny)``; ``values`` (normalised to ``[0, 1]``) is high where
    routing was hard — dense copper, vias, and the regions of connections left
    unrouted. Placement samples it at footprint centroids and is pushed toward
    cooler cells (`pyautoroute.placement`).

    Attributes:
        minx: frame origin x (mm).
        miny: frame origin y (mm).
        cell: cell side (mm).
        nx: cell columns.
        ny: cell rows.
        values: ``(ny, nx)`` float array in ``[0, 1]``; 0 outside is implied
            (`value_at` returns 0 beyond the frame).
    """
    minx: float
    miny: float
    cell: float
    nx: int
    ny: int
    values: np.ndarray

    def value_at(self, x: float, y: float) -> float:
        """Sample the field at board coordinate ``(x, y)``.

        Args:
            x: board x (mm).
            y: board y (mm).

        Returns:
            The cell value (``[0, 1]``), or ``0.0`` outside the frame — a
            footprint pushed off the field feels no congestion (the placement's
            compaction/rats-nest terms keep it from drifting away).
        """
        c = int((x - self.minx) / self.cell)
        r = int((y - self.miny) / self.cell)
        if 0 <= c < self.nx and 0 <= r < self.ny:
            return float(self.values[r, c])
        return 0.0

    def blended(self, other: "CongestionField", decay: float) -> "CongestionField":
        """Exponentially blend an earlier field with a new one on the same frame.

        ``decay`` weights the *accumulated* history: ``decay·self + (1-decay)·other``.
        Higher ``decay`` remembers longer (signal accumulates), lower reacts faster
        to the latest cycle. The blend is renormalised so the result stays in
        ``[0, 1]`` and one runaway cycle can't dominate.

        Args:
            other: the new field (must share this field's frame).
            decay: history weight in ``[0, 1]``.

        Returns:
            A new `CongestionField` on the same frame.
        """
        v = decay * self.values + (1.0 - decay) * other.values
        peak = float(v.max())
        if peak > 0:
            v = v / peak
        return CongestionField(self.minx, self.miny, self.cell, self.nx, self.ny, v)


def congestion_frame(board, pitch: float, *, cell_mult: int = 4,
                     margin: float = 5.0) -> CongestionField:
    """Build an empty `CongestionField` framing a board's pad extent.

    The frame is fixed once (from the freshly loaded board) and reused for every
    feedback cycle, so each cycle's `congestion_heatmap` rasters onto the same
    cells regardless of how that cycle's placement (and thus its routing grid)
    shifted.

    Args:
        board: the board whose pads define the extent.
        pitch: the routing-grid pitch (mm); the cell side is ``cell_mult × pitch``.
        cell_mult: coarse cell side as a multiple of ``pitch`` (default 4).
        margin: extra border (mm) added on every side so parts that spread
            outward still land on the field.

    Returns:
        A zero-valued `CongestionField`; ``nx``/``ny`` are at least 1.
    """
    xs = [p.cx for p in board.pads]
    ys = [p.cy for p in board.pads]
    if not xs:
        return CongestionField(0.0, 0.0, max(pitch * cell_mult, 1e-6), 1, 1,
                               np.zeros((1, 1)))
    cell = max(pitch * cell_mult, 1e-6)
    minx, miny = min(xs) - margin, min(ys) - margin
    maxx, maxy = max(xs) + margin, max(ys) + margin
    nx = max(1, int(math.ceil((maxx - minx) / cell)))
    ny = max(1, int(math.ceil((maxy - miny) / cell)))
    return CongestionField(minx, miny, cell, nx, ny, np.zeros((ny, nx)))


# relative cell weights for the heatmap (normalised away afterwards, so only
# their ratios matter): an unrouted connection is the strongest signal, a via
# next, an ordinary track node the baseline.
_W_TRACK = 1.0
_W_VIA = 3.0
_W_UNROUTED = 10.0


def congestion_heatmap(conns, results, grid: Grid, frame: CongestionField, *,
                       blur: float = 1.0) -> CongestionField:
    """Derive a `CongestionField` from one cycle's routed results.

    Accumulates, onto ``frame``'s cells, the copper density of every routed path
    (a unit per track node, more per via) plus a strong mark along the straight
    line between the endpoints of every *unrouted* connection — the two things a
    spread-out placement could relieve. The raw counts are smoothed with a light
    Gaussian blur (so the placement sees a gradient, not a step) and normalised to
    ``[0, 1]``. Routing state is only read, never changed.

    Args:
        conns: the connection list (parallel to ``results``); supplies the
            endpoints of unrouted connections.
        results: per-connection `RouteResult` (``None`` where unrouted).
        grid: the routing grid the ``results`` were produced on (maps path nodes
            to board coordinates); may differ frame from ``frame``.
        frame: the fixed `CongestionField` whose geometry (origin/cell/size) the
            heatmap is rasterised onto. Its values are ignored.
        blur: Gaussian-blur sigma in cells (0 disables); softens the field.

    Returns:
        A new `CongestionField` on ``frame``'s geometry, normalised to ``[0, 1]``.
    """
    from scipy.ndimage import gaussian_filter

    acc = np.zeros((frame.ny, frame.nx))

    def _bump(x: float, y: float, w: float) -> None:
        c = int((x - frame.minx) / frame.cell)
        r = int((y - frame.miny) / frame.cell)
        if 0 <= c < frame.nx and 0 <= r < frame.ny:
            acc[r, c] += w

    for res in results:
        if res is None:
            continue
        prev_layer = None
        for (layer, col, row) in res.path:
            x, y = grid.node_xy(col, row)
            _bump(x, y, _W_TRACK)
            if prev_layer is not None and layer != prev_layer:
                _bump(x, y, _W_VIA)            # a via at this node
            prev_layer = layer

    for res, conn in zip(results, conns):
        if res is not None:
            continue
        ax, ay = conn.a.cx, conn.a.cy
        bx, by = conn.b.cx, conn.b.cy
        steps = max(1, int(math.hypot(bx - ax, by - ay) / frame.cell))
        for s in range(steps + 1):
            t = s / steps
            _bump(ax + t * (bx - ax), ay + t * (by - ay), _W_UNROUTED)

    if blur > 0 and acc.any():
        acc = gaussian_filter(acc, sigma=blur)
    peak = float(acc.max())
    if peak > 0:
        acc = acc / peak
    return CongestionField(frame.minx, frame.miny, frame.cell,
                           frame.nx, frame.ny, acc)


# --- incremental occupancy ----------------------------------------------------

class RoutingState:
    """Dynamic routed-copper occupancy layered over a grid's static occupancy.

    Occupancy is recorded per connection index, with each connection's net, so
    rip-up is exact and same-net connections don't block one another.
    """

    def __init__(self, grid: Grid):
        """Layer dynamic routed-copper occupancy over a static grid.

        Args:
            grid: the static routing grid (pad/edge occupancy) to build on.
        """
        self.grid = grid
        self.cover: dict[tuple[int, int, int], set[int]] = {}   # node -> {conn_idx}
        self.conn_cover: dict[int, set[tuple[int, int, int]]] = {}
        self.conn_net: dict[int, int] = {}
        # Vectorised mirror of `cover` for the A* free-mask overlay: per node, the
        # net id of its committed copper, or `_COVER_EMPTY`/`_COVER_MIXED`. Built
        # incrementally on commit/ripup (touching only the affected connection's
        # nodes) so each search overlays "blocked by another net" with one numpy
        # op instead of a Python loop over every committed node. `cover` /
        # `conn_net` remain the source of truth; this is a derived index.
        self.cover_owner = np.full((grid.n_layers, grid.ny, grid.nx),
                                   _COVER_EMPTY, dtype=np.int32)
        # hfield (octile heuristic) cache, keyed by the connection's target set;
        # a connection's targets are fixed, so its field is identical every reroute.
        self._hfield_cache: dict[frozenset, np.ndarray] = {}

    def _refresh_owner(self, node: tuple[int, int, int]) -> None:
        """Recompute `cover_owner[node]` from the connections currently covering it."""
        occ = self.cover.get(node)
        li, c, r = node
        if not occ:
            self.cover_owner[li, r, c] = _COVER_EMPTY
            return
        it = iter(occ)
        net = self.conn_net[next(it)]
        for i in it:
            if self.conn_net[i] != net:
                self.cover_owner[li, r, c] = _COVER_MIXED
                return
        self.cover_owner[li, r, c] = net

    def is_free(self, layer_idx: int, col: int, row: int, net_id: int) -> bool:
        """Whether `net_id` may occupy a node, considering routed copper too.

        Args:
            layer_idx: copper layer index.
            col: node column.
            row: node row.
            net_id: the routing net's id.

        Returns:
            True if the static grid permits it and no *other* net's committed
            copper covers the node.
        """
        if not self.grid.is_free(layer_idx, col, row, net_id):
            return False
        occ = self.cover.get((layer_idx, col, row))
        if occ:
            for idx in occ:
                if self.conn_net[idx] != net_id:
                    return False
        return True

    def can_via(self, col: int, row: int, net_id: int) -> bool:
        """Whether a via for `net_id` fits at a node, over static + routed copper.

        Args:
            col: node column for the via centre.
            row: node row for the via centre.
            net_id: the routing net's id.

        Returns:
            True if every node in the via-clearance stencil is free for `net_id`
            on all copper layers.
        """
        for dc, dr in self.grid._via_stencil:
            c, r = col + dc, row + dr
            if not self.grid.in_bounds(c, r):
                return False
            for li in range(self.grid.n_layers):
                if not self.is_free(li, c, r, net_id):
                    return False
        return True

    def commit(self, conn_idx: int, result: RouteResult) -> None:
        """Record a routed connection's (inflated) copper as occupancy.

        Args:
            conn_idx: the connection's index, used as the rip-up key.
            result: the routed path to commit.
        """
        net_id = self.grid.net_id(result.net)
        nodes = self._covered_nodes(result)
        self.conn_net[conn_idx] = net_id
        self.conn_cover[conn_idx] = nodes
        for nd in nodes:
            self.cover.setdefault(nd, set()).add(conn_idx)
            self._refresh_owner(nd)

    def ripup(self, conn_idx: int) -> None:
        """Remove a connection's committed copper from the occupancy.

        Args:
            conn_idx: the connection index previously passed to `commit`.
        """
        for nd in self.conn_cover.pop(conn_idx, ()):
            occ = self.cover.get(nd)
            if occ is not None:
                occ.discard(conn_idx)
                if not occ:
                    del self.cover[nd]
            self._refresh_owner(nd)
        self.conn_net.pop(conn_idx, None)

    def _raster(self, geom, layer_idx: int, out: set):
        """Add every grid node whose centre lies inside `geom` to `out`.

        Args:
            geom: the shapely area to rasterise.
            layer_idx: the layer index tagged onto each added node.
            out: the ``(layer, col, row)`` node set extended in place.
        """
        m = self.grid._mesh_in_bbox(geom.bounds)
        if m is None:
            return
        cols, rows, xx, yy = m
        mask = shapely.contains_xy(geom, xx, yy)
        if not mask.any():
            return
        rr, cc = np.where(mask)
        for k in range(len(rr)):
            out.add((layer_idx, int(cols[cc[k]]), int(rows[rr[k]])))

    def _covered_nodes(self, result: RouteResult) -> set:
        """Grid nodes a routed path occupies, inflated by the clearance margin.

        Args:
            result: the routed path.

        Returns:
            The ``(layer, col, row)`` nodes the path's copper (tracks + via
            disks) claims for clearance bookkeeping.
        """
        grid = self.grid
        width = grid.rules.track_width_for(result.net)
        via_r = grid.rules.via_diameter_for(result.net) / 2.0
        # Treat committed copper like a pad obstacle: grow it by the full grid
        # margin (= routing-track half-width + clearance + discretisation safety).
        # The routing-track half-width term matters on multi-class boards where a
        # later wide track would otherwise encroach on this one's clearance.
        extra = grid.margin
        nodes: set = set(result.path)
        for (l0, c0, r0), (l1, c1, r1) in zip(result.path, result.path[1:]):
            if l0 != l1:
                x, y = grid.node_xy(c0, r0)
                disk = Point(x, y).buffer(via_r + extra)
                for li in range(grid.n_layers):
                    self._raster(disk, li, nodes)
            else:
                x0, y0 = grid.node_xy(c0, r0)
                x1, y1 = grid.node_xy(c1, r1)
                track = LineString([(x0, y0), (x1, y1)]).buffer(width / 2.0 + extra)
                self._raster(track, l0, nodes)
        return nodes


# --- A* single-connection router ----------------------------------------------

def build_free_mask(state: RoutingState, net_id: int) -> np.ndarray:
    """Boolean free mask for `net_id` over the whole grid, all layers.

    A node is free iff the static grid permits it (FREE or already owned by
    this net) and no *other* net's committed copper covers it — the same
    full-grid overlay `astar`'s `_precompute` builds internally, factored out
    so other searches (the diff-pair coupled A*) can also trade a per-
    expansion `RoutingState.is_free` call (dict lookup + set scan) for a
    direct array index.

    Args:
        state: the routing state (occupancy).
        net_id: the routing net's id.

    Returns:
        A ``(n_layers, ny, nx)`` boolean array.
    """
    owner = state.grid.owner
    free = (owner == FREE) | (owner == net_id)
    free &= (state.cover_owner == _COVER_EMPTY) | (state.cover_owner == net_id)
    return free


def astar(state: RoutingState, net_id: int,
          sources: list[tuple[int, int, int]],
          targets: list[tuple[int, int, int]],
          params: RouteParams | None = None) -> list[tuple[int, int, int]] | None:
    """Find a min-cost node path from any source to any target.

    Search internals are optimised for the Python inner loop without changing
    the cost model or results: search states are packed into a single integer
    key (``((layer*ny + row)*nx + col)*9 + dir+1``) instead of a tuple; a per-net
    boolean free mask is built once from the static occupancy so neighbour
    freeness is a direct array index rather than a `RoutingState.is_free` call;
    the octile heuristic is precomputed over the whole grid as a numpy field; and
    the via-target layer list is cached on the grid. These are transparent — the
    returned path is identical to the unoptimised search.

    When ``params.search_margin`` is set, the search is bounded to a box around
    the source/target nodes (grown by that margin in mm), masking everything
    outside it not-free. The box widens and the search retries on failure,
    falling back to the full grid, so a route is still found whenever one exists;
    the trade-off is that a bounded route may be slightly longer than the global
    optimum. With ``search_margin=None`` (the default) the whole grid is searched.

    Args:
        state: the live routing state (occupancy the search must respect).
        net_id: the routing net's id (nodes owned by other nets are blocked).
        sources: candidate start nodes ``(layer, col, row)`` (a pad's accesses).
        targets: candidate goal nodes ``(layer, col, row)``.
        params: cost-model parameters; defaults are used when `None`.

    Returns:
        The ``(layer, col, row)`` path from a source to a target, or `None` if
        none is reachable within the expansion budget.
    """
    params = params or RouteParams()
    grid = state.grid
    if not sources or not targets:
        return None
    nx, ny, n_layers, pitch = grid.nx, grid.ny, grid.n_layers, grid.pitch

    # --- flattened integer state key ----------------------------------------
    # `(layer, col, row, dir)` is packed into a single int; dict lookups on ints
    # are faster than on tuples and avoid a tuple allocation per neighbour. `dir`
    # ranges over -1..7, shifted to 0..8 by the +1 so all keys stay non-negative.
    #   key = ((layer * ny + row) * nx + col) * 9 + (dir + 1)
    _D = 9
    _LROW = nx * _D

    def encode(li: int, c: int, r: int, d: int) -> int:
        return (li * ny + r) * _LROW + c * _D + (d + 1)

    # --- per-net free mask + heuristic field --------------------------------
    # These two numpy precomputes dominate the per-call cost, so they are built
    # by `_precompute` for a given box and (when the search is bounded) only
    # within that box. Outside the box every node is not-free and the heuristic
    # is +inf, which the search never reaches — so a bounded search pays for the
    # box area rather than the whole grid.
    #   * free mask: a node is free for this net iff the static grid permits it
    #     (FREE or owned by this net) AND no *other* net's committed copper covers
    #     it; indexing the boolean array directly avoids a per-expansion call.
    #   * heuristic field: octile distance to the nearest target × pitch (layer-
    #     and direction-independent, so a single numpy broadcast per target).
    owner = grid.owner
    target_set = set((li, c, r) for (li, c, r) in targets)
    tgt_cr = [(c, r) for (_, c, r) in targets]

    def _precompute(cmin: int, cmax: int, rmin: int, rmax: int):
        cs_, rs_ = slice(cmin, cmax + 1), slice(rmin, rmax + 1)
        free = np.zeros((n_layers, ny, nx), dtype=bool)
        ow = owner[:, rs_, cs_]
        free[:, rs_, cs_] = (ow == FREE) | (ow == net_id)
        # Overlay dynamic routed copper: a node is blocked iff another net (or a
        # mix of nets) covers it. `cover_owner` is maintained incrementally, so
        # this is one vectorised numpy op instead of a Python loop over every
        # committed node — the dominant cost of a reroute during annealing.
        co = state.cover_owner[:, rs_, cs_]
        free[:, rs_, cs_] &= (co == _COVER_EMPTY) | (co == net_id)

        # Heuristic field. A connection's targets are fixed, so for the full-grid
        # (unbounded) search the field is identical every reroute — cache it.
        full = cmin == 0 and cmax == nx - 1 and rmin == 0 and rmax == ny - 1
        if full:
            key = frozenset(tgt_cr)
            cached = state._hfield_cache.get(key)
            if cached is not None:
                return free, cached
        hfield = np.full((ny, nx), np.inf)
        bcols = np.arange(cmin, cmax + 1)[None, :]
        brows = np.arange(rmin, rmax + 1)[:, None]
        box_h = np.full((rmax - rmin + 1, cmax - cmin + 1), np.inf)
        for tc, tr in tgt_cr:
            dx = np.abs(bcols - tc)
            dy = np.abs(brows - tr)
            lo = np.minimum(dx, dy)
            hi = np.maximum(dx, dy)
            box_h = np.minimum(box_h, (hi - lo) + lo * SQRT2)
        hfield[rs_, cs_] = box_h * pitch
        if full:
            state._hfield_cache[key] = hfield
        return free, hfield

    # --- via neighbourhood (constant per grid) ------------------------------
    # The set of layers a via can jump to from any node is just "all other
    # layers"; precompute it once on the grid so the via branch doesn't rebuild
    # the layer list on every expansion.
    via_layers = getattr(grid, "_via_layer_neighbours", None)
    if via_layers is None:
        via_layers = [tuple(j for j in range(n_layers) if j != i)
                      for i in range(n_layers)]
        grid._via_layer_neighbours = via_layers

    def _search(free, hfield) -> list[tuple[int, int, int]] | None:
        """Run one A* search over the given per-net `free` mask + heuristic field.

        These two arrays are all that vary between a bounded attempt and the
        full-grid fallback, so the whole search (both the Cython fast path and the
        pure-Python heap loop) takes them as parameters. Out-of-box nodes are
        not-free with a +inf heuristic, which bounds the frontier without any
        other change.
        """
        def is_free(li: int, c: int, r: int) -> bool:
            return 0 <= c < nx and 0 <= r < ny and bool(free[li, r, c])

        def h(c: int, r: int) -> float:
            return float(hfield[r, c])

        def can_via(c: int, r: int) -> bool:
            for dc, dr in grid._via_stencil:
                cc, rr = c + dc, r + dr
                if not (0 <= cc < nx and 0 <= rr < ny):
                    return False
                if not free[:, rr, cc].all():
                    return False
            return True

        # --- optional Cython fast path --------------------------------------
        # The native core consumes the exact same precomputed structures (free
        # mask, heuristic field, via stencil/layer lists) and integer state
        # packing, so it returns a bit-for-bit identical path. It is skipped
        # transparently when the extension is not built (`_USE_C_ASTAR` is False).
        if _USE_C_ASTAR:
            free_c = np.ascontiguousarray(free, dtype=np.uint8)
            hfield_c = np.ascontiguousarray(hfield, dtype=np.float64)
            return _astar_fast(
                free_c, hfield_c,
                list(sources), list(targets),
                [(int(dc), int(dr)) for dc, dr in grid._via_stencil],
                [tuple(v) for v in via_layers],
                float(pitch), float(params.via_cost),
                float(params.bend45), float(params.bend90),
                float(params.bend135), float(params.bend180),
                float(params.back_layer_penalty),
                int(params.max_expansions),
            )

        counter = itertools.count()
        gscore: dict[int, float] = {}
        # key -> (predecessor_key, (layer, col, row)); the node tuple is stored so
        # path reconstruction needs no decode of the packed integer key.
        came: dict[int, tuple | None] = {}
        heap = []
        for (li, c, r) in sources:
            if not is_free(li, c, r):
                continue
            s = encode(li, c, r, -1)
            gscore[s] = 0.0
            came[s] = (None, (li, c, r))
            heapq.heappush(heap, (h(c, r), next(counter), s, li, c, r, -1))

        expansions = 0
        while heap:
            f, _, st, li, c, r, pdir = heapq.heappop(heap)
            g = gscore[st]
            if f > g + h(c, r) + 1e-9:
                continue
            if (li, c, r) in target_set:
                return _reconstruct(came, st)

            expansions += 1
            if expansions > params.max_expansions:
                return None

            for di, (dx, dy) in enumerate(_DIRS):
                nc, nr = c + dx, r + dy
                if not is_free(li, nc, nr):
                    continue
                diagonal = dx != 0 and dy != 0
                if diagonal and not (is_free(li, c + dx, r)
                                     and is_free(li, c, r + dy)):
                    continue
                step = pitch * (SQRT2 if diagonal else 1.0)
                cost = step + params.bend(_turn_units(pdir, di))
                if li != 0:
                    cost += params.back_layer_penalty
                ng = g + cost
                ns = encode(li, nc, nr, di)
                if ng < gscore.get(ns, math.inf):
                    gscore[ns] = ng
                    came[ns] = (st, (li, nc, nr))
                    heapq.heappush(heap, (ng + h(nc, nr), next(counter),
                                          ns, li, nc, nr, di))

            if n_layers > 1 and can_via(c, r):
                ng = g + params.via_cost
                for nli in via_layers[li]:
                    ns = encode(nli, c, r, -1)
                    if ng < gscore.get(ns, math.inf):
                        gscore[ns] = ng
                        came[ns] = (st, (nli, c, r))
                        heapq.heappush(heap, (ng + h(c, r), next(counter),
                                              ns, nli, c, r, -1))

        return None

    # --- bounded search with widen-on-failure -------------------------------
    # Unbounded: precompute and search the whole grid (the historical behaviour).
    if params.search_margin is None:
        return _search(*_precompute(0, nx - 1, 0, ny - 1))

    # Bound the search to a box around the source/target nodes grown by the
    # margin. `_precompute` fills the free mask and heuristic field only inside
    # the box, so each bounded attempt pays for the box area, not the whole grid.
    # On failure widen the box and retry; once it covers the grid this is the
    # unbounded search, so a route is still found whenever one exists.
    cs = [c for (_, c, _) in sources] + [c for (_, c, _) in targets]
    rs = [r for (_, _, r) in sources] + [r for (_, _, r) in targets]
    cmin0, cmax0 = min(cs), max(cs)
    rmin0, rmax0 = min(rs), max(rs)
    pad = max(1, int(math.ceil(params.search_margin / pitch)))
    while True:
        cmin, cmax = max(0, cmin0 - pad), min(nx - 1, cmax0 + pad)
        rmin, rmax = max(0, rmin0 - pad), min(ny - 1, rmax0 + pad)
        full = cmin == 0 and cmax == nx - 1 and rmin == 0 and rmax == ny - 1
        path = _search(*_precompute(cmin, cmax, rmin, rmax))
        if path is not None or full:
            return path
        pad *= 2


def _reconstruct(came, st) -> list[tuple[int, int, int]]:
    """Rebuild the node path from the A* came-from map.

    Args:
        came: mapping of integer search-state key -> ``(predecessor_key,
            (layer, col, row))`` (or `None` at a source).
        st: the goal search-state key to walk back from.

    Returns:
        The ``(layer, col, row)`` path from source to goal, with consecutive
        duplicates removed.
    """
    out = []
    while st is not None:
        prev, node = came[st]
        out.append(node)
        st = prev
    out.reverse()
    dedup = [out[0]]
    for n in out[1:]:
        if n != dedup[-1]:
            dedup.append(n)
    return dedup


def route_connection(state: RoutingState, net: str,
                     sources: list[tuple[int, int, int]],
                     targets: list[tuple[int, int, int]],
                     params: RouteParams | None = None,
                     src_xy: tuple[float, float] | None = None,
                     dst_xy: tuple[float, float] | None = None) -> RouteResult | None:
    """Route one connection and package the path with its metrics.

    Args:
        state: the live routing state (occupancy).
        net: the connection's net name.
        sources: candidate start nodes (one pad's access nodes).
        targets: candidate goal nodes (the other pad's access nodes).
        params: cost-model parameters; defaults are used when `None`.
        src_xy: the source pad's anchor (centre) coordinates, if known; recorded
            on the result so the writer stubs the track to the pad anchor.
        dst_xy: the target pad's anchor (centre) coordinates, if known.

    Returns:
        The `RouteResult` (path + length + vias), or `None` if unroutable.
    """
    net_id = state.grid.net_id(net)
    path = astar(state, net_id, sources, targets, params)
    if path is None:
        return None
    length, vias = _path_metrics(state.grid, path)
    length += _stub_length(state.grid, path, src_xy, dst_xy)
    return RouteResult(net=net, path=path, length=length, vias=vias,
                       src_xy=src_xy, dst_xy=dst_xy)


def _stub_length(grid: Grid, path, src_xy, dst_xy) -> float:
    """Extra wirelength of the pad-anchor stubs at the path's two ends.

    Args:
        grid: the grid (node -> coordinate conversion).
        path: the routed ``(layer, col, row)`` node path.
        src_xy: the source pad anchor, or `None`.
        dst_xy: the target pad anchor, or `None`.

    Returns:
        The summed length (mm) of the (non-degenerate) endpoint stubs.
    """
    extra = 0.0
    for xy, node in ((src_xy, path[0]), (dst_xy, path[-1])):
        if xy is None:
            continue
        nx, ny = grid.node_xy(node[1], node[2])
        d = math.hypot(nx - xy[0], ny - xy[1])
        if d >= _STUB_EPS:
            extra += d
    return extra


def _path_metrics(grid: Grid, path) -> tuple[float, int]:
    """Measure a node path's wirelength and via count.

    Args:
        grid: the grid (for the pitch / coordinate scale).
        path: the ``(layer, col, row)`` node path.

    Returns:
        ``(length_mm, n_vias)`` — layer changes count as vias, in-layer steps
        add their geometric length.
    """
    length = 0.0
    vias = 0
    for (l0, c0, r0), (l1, c1, r1) in zip(path, path[1:]):
        if l0 != l1:
            vias += 1
        else:
            length += math.hypot(c1 - c0, r1 - r0) * grid.pitch
    return length, vias


# --- routing a list of connections --------------------------------------------

def route_all(state: RoutingState, connections, order: list[int],
              params: RouteParams | None = None,
              on_progress=None, on_partial=None) -> BoardRouting:
    """Route connections in the given order, committing each success to `state`.

    Args:
        state: the routing state to commit successful routes into.
        connections: the connections to route (indexed by `order`).
        order: indices into `connections` giving the routing order.
        params: cost-model parameters; defaults are used when `None`.
        on_progress: optional callback ``(done, total, routed, unrouted)`` after
            each connection.
        on_partial: optional callback ``(conn_idx, result_or_none)`` called after
            each connection, for callers that need the partial results list
            as it builds up (e.g. live board canvas updates).

    Returns:
        A `BoardRouting` with per-connection results and aggregate metrics.
    """
    results: list[RouteResult | None] = [None] * len(connections)
    routed = unrouted = 0
    total_len = 0.0
    total_vias = 0
    for k, idx in enumerate(order):
        conn = connections[idx]
        srcs = state.grid.pad_access_nodes(conn.a)
        tgts = state.grid.pad_access_nodes(conn.b)
        res = route_connection(state, conn.net, srcs, tgts, params,
                               src_xy=(conn.a.cx, conn.a.cy),
                               dst_xy=(conn.b.cx, conn.b.cy))
        results[idx] = res
        if res is not None:
            state.commit(idx, res)
            routed += 1
            total_len += res.length
            total_vias += res.vias
        else:
            unrouted += 1
        if on_partial is not None:
            on_partial(idx, res)
        if on_progress is not None:
            on_progress(k + 1, len(order), routed, unrouted)
    return BoardRouting(results, routed, unrouted, total_len, total_vias)


# --- node path -> KiCad nodes -------------------------------------------------

def path_to_nodes(board, grid: Grid, result: RouteResult) -> list[SList]:
    """Convert a routed node path into KiCad ``(segment ...)`` / ``(via ...)`` nodes.

    Collinear runs are merged into single segments and layer changes become vias.

    Args:
        board: the board (net-reference style for the new nodes).
        grid: the grid (node -> coordinate conversion, layer names).
        result: the routed path to serialise.

    Returns:
        The s-expression nodes to append to the routed board.
    """
    from .pcb import make_via

    path = result.path
    nodes: list[SList] = []
    if len(path) < 2:
        return nodes

    rules = grid.rules
    width = rules.track_width_for(result.net)
    via_d = rules.via_diameter_for(result.net)
    via_drill = rules.via_drill_for(result.net)

    run_start = path[0]
    prev = path[0]
    prev_dir = None
    for cur in path[1:]:
        l0, c0, r0 = prev
        l1, c1, r1 = cur
        if l0 != l1:
            if run_start[1:] != prev[1:]:
                nodes.append(_seg(board, grid, result.net, run_start, prev, width))
            x, y = grid.node_xy(c0, r0)
            nodes.append(make_via(board, x, y, via_d, via_drill,
                                  grid.layers[l0], grid.layers[l1], result.net))
            run_start = cur
            prev_dir = None
        else:
            d = (_sign(c1 - c0), _sign(r1 - r0))
            if prev_dir is not None and d != prev_dir:
                nodes.append(_seg(board, grid, result.net, run_start, prev, width))
                run_start = prev
            prev_dir = d
        prev = cur

    if run_start[1:] != prev[1:] and run_start[0] == prev[0]:
        nodes.append(_seg(board, grid, result.net, run_start, prev, width))

    # Stub each end to the pad anchor so the track terminates on the pad centre.
    # Each stub runs from a terminal grid node (inside the pad) to the pad's
    # centre (also inside the pad), so it stays within the pad's own copper and
    # adds no clearance violation.
    for xy, node in ((result.src_xy, path[0]), (result.dst_xy, path[-1])):
        stub = _centre_stub(board, grid, result.net, xy, node, width)
        if stub is not None:
            nodes.append(stub)
    return nodes


def _centre_stub(board, grid: Grid, net: str, xy, node, width: float) -> SList | None:
    """Build a stub segment from a pad anchor to a terminal grid node.

    Args:
        board: the board (net-reference style).
        grid: the grid (node -> coordinate conversion).
        net: the segment's net name.
        xy: the pad anchor ``(x, y)`` in mm, or `None` (no stub).
        node: the path's terminal ``(layer, col, row)`` node, on the pad.
        width: the track width (mm).

    Returns:
        A ``(segment ...)`` node on the node's layer, or `None` when no anchor is
        known or the node already sits on the anchor (a zero-length stub).
    """
    from .pcb import make_segment
    if xy is None:
        return None
    nx, ny = grid.node_xy(node[1], node[2])
    if math.hypot(nx - xy[0], ny - xy[1]) < _STUB_EPS:
        return None
    return make_segment(board, xy[0], xy[1], nx, ny, width, grid.layers[node[0]], net)


def _sign(v: int) -> int:
    """Return the sign of `v` as -1, 0, or 1.

    Args:
        v: the value.
    """
    return (v > 0) - (v < 0)


def _seg(board, grid: Grid, net: str, a, b, width: float) -> SList:
    """Build a ``(segment ...)`` node spanning two grid nodes on one layer.

    Args:
        board: the board (net-reference style).
        grid: the grid (node -> coordinate conversion).
        net: the segment's net name.
        a: the start ``(layer, col, row)`` node.
        b: the end ``(layer, col, row)`` node.
        width: the track width (mm).

    Returns:
        The ``(segment ...)`` node.
    """
    from .pcb import make_segment
    x1, y1 = grid.node_xy(a[1], a[2])
    x2, y2 = grid.node_xy(b[1], b[2])
    return make_segment(board, x1, y1, x2, y2, width, grid.layers[a[0]], net)
