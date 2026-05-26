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
        return (0.0, self.bend45, self.bend90, self.bend135, self.bend180)[turn_units]


def _turn_units(a: int, b: int) -> int:
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
        self.grid = grid
        self.cover: dict[tuple[int, int, int], set[int]] = {}   # node -> {conn_idx}
        self.conn_cover: dict[int, set[tuple[int, int, int]]] = {}
        self.conn_net: dict[int, int] = {}

    def is_free(self, layer_idx: int, col: int, row: int, net_id: int) -> bool:
        if not self.grid.is_free(layer_idx, col, row, net_id):
            return False
        occ = self.cover.get((layer_idx, col, row))
        if occ:
            for idx in occ:
                if self.conn_net[idx] != net_id:
                    return False
        return True

    def can_via(self, col: int, row: int, net_id: int) -> bool:
        for dc, dr in self.grid._via_stencil:
            c, r = col + dc, row + dr
            if not self.grid.in_bounds(c, r):
                return False
            for li in range(self.grid.n_layers):
                if not self.is_free(li, c, r, net_id):
                    return False
        return True

    def commit(self, conn_idx: int, result: RouteResult) -> None:
        net_id = self.grid.net_id(result.net)
        nodes = self._covered_nodes(result)
        self.conn_net[conn_idx] = net_id
        self.conn_cover[conn_idx] = nodes
        for nd in nodes:
            self.cover.setdefault(nd, set()).add(conn_idx)

    def ripup(self, conn_idx: int) -> None:
        for nd in self.conn_cover.pop(conn_idx, ()):
            occ = self.cover.get(nd)
            if occ is not None:
                occ.discard(conn_idx)
                if not occ:
                    del self.cover[nd]
        self.conn_net.pop(conn_idx, None)

    def _raster(self, geom, layer_idx: int, out: set):
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
    """Find a min-cost node path from any source to any target, or None."""
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
                     params: RouteParams | None = None) -> RouteResult | None:
    net_id = state.grid.net_id(net)
    path = astar(state, net_id, sources, targets, params)
    if path is None:
        return None
    length, vias = _path_metrics(state.grid, path)
    return RouteResult(net=net, path=path, length=length, vias=vias)


def _path_metrics(grid: Grid, path) -> tuple[float, int]:
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
    """Route connections in the given order, committing each success to `state`."""
    results: list[RouteResult | None] = [None] * len(connections)
    routed = unrouted = 0
    total_len = 0.0
    total_vias = 0
    for k, idx in enumerate(order):
        conn = connections[idx]
        srcs = state.grid.pad_access_nodes(conn.a)
        tgts = state.grid.pad_access_nodes(conn.b)
        res = route_connection(state, conn.net, srcs, tgts, params)
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
    """Convert a node path into KiCad (segment ...) / (via ...) s-expr nodes."""
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
    return nodes


def _sign(v: int) -> int:
    return (v > 0) - (v < 0)


def _seg(board, grid: Grid, net: str, a, b, width: float) -> SList:
    from .pcb import make_segment
    x1, y1 = grid.node_xy(a[1], a[2])
    x2, y2 = grid.node_xy(b[1], b[2])
    return make_segment(board, x1, y1, x2, y2, width, grid.layers[a[0]], net)
