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

from .grid import Grid
from .sexpr import SList

SQRT2 = math.sqrt(2.0)

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

def astar(state: RoutingState, net_id: int,
          sources: list[tuple[int, int, int]],
          targets: list[tuple[int, int, int]],
          params: RouteParams | None = None) -> list[tuple[int, int, int]] | None:
    """Find a min-cost node path from any source to any target.

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
    target_set = set(targets)
    tgt_cr = [(c, r) for (_, c, r) in targets]
    pitch = grid.pitch

    def h(col: int, row: int) -> float:
        best = math.inf
        for tc, tr in tgt_cr:
            dx, dy = abs(col - tc), abs(row - tr)
            lo, hi = (dx, dy) if dx < dy else (dy, dx)
            best = min(best, (hi - lo) + lo * SQRT2)
        return best * pitch

    counter = itertools.count()
    gscore: dict[tuple[int, int, int, int], float] = {}
    came: dict[tuple[int, int, int, int], tuple] = {}
    heap = []
    for (li, c, r) in sources:
        if not state.is_free(li, c, r, net_id):
            continue
        s = (li, c, r, -1)
        gscore[s] = 0.0
        came[s] = None
        heapq.heappush(heap, (h(c, r), next(counter), s))

    expansions = 0
    while heap:
        f, _, st = heapq.heappop(heap)
        li, c, r, pdir = st
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
            if not state.is_free(li, nc, nr, net_id):
                continue
            diagonal = dx != 0 and dy != 0
            if diagonal and not (state.is_free(li, c + dx, r, net_id)
                                 and state.is_free(li, c, r + dy, net_id)):
                continue
            step = pitch * (SQRT2 if diagonal else 1.0)
            cost = step + params.bend(_turn_units(pdir, di))
            if li != 0:
                cost += params.back_layer_penalty
            ng = g + cost
            ns = (li, nc, nr, di)
            if ng < gscore.get(ns, math.inf):
                gscore[ns] = ng
                came[ns] = st
                heapq.heappush(heap, (ng + h(nc, nr), next(counter), ns))

        for nli in range(grid.n_layers):
            if nli == li:
                continue
            if not state.can_via(c, r, net_id):
                continue
            ng = g + params.via_cost
            ns = (nli, c, r, -1)
            if ng < gscore.get(ns, math.inf):
                gscore[ns] = ng
                came[ns] = st
                heapq.heappush(heap, (ng + h(c, r), next(counter), ns))

    return None


def _reconstruct(came, st) -> list[tuple[int, int, int]]:
    """Rebuild the node path from the A* came-from map.

    Args:
        came: mapping of search state -> predecessor state.
        st: the goal search state to walk back from.

    Returns:
        The ``(layer, col, row)`` path from source to goal, with consecutive
        duplicates removed.
    """
    out = []
    while st is not None:
        li, c, r, _ = st
        out.append((li, c, r))
        st = came[st]
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
              on_progress=None) -> BoardRouting:
    """Route connections in the given order, committing each success to `state`.

    Args:
        state: the routing state to commit successful routes into.
        connections: the connections to route (indexed by `order`).
        order: indices into `connections` giving the routing order.
        params: cost-model parameters; defaults are used when `None`.
        on_progress: optional callback ``(done, total, routed, unrouted)`` after
            each connection.

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
