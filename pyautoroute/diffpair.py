"""Coupled A* router for differential pairs.

``route_diff_pair`` routes both traces of a diff pair simultaneously by running
a standard A* over the **+ trace position** while requiring the companion −
trace (at a fixed grid-node offset) to also be free at every expansion step.
Because the offset is constant throughout the route, the − path is derived in
O(n) from the + path after the search — the state space stays identical in size
to a single-net A* search.

The offset is computed from the source pad positions so the traces start
parallel; it is rounded to the nearest grid pitch that satisfies the requested
``dp_gap``.
"""

from __future__ import annotations

import heapq
import itertools
import math

from .grid import BLOCKED, FREE, Grid
from .netlist import DiffPairConnection
from .router import (
    SQRT2, RouteParams, RouteResult, RoutingState,
    _DIRS, _path_metrics, _stub_length, _turn_units,
)


def bake_routing_state(state: RoutingState, grid: Grid) -> None:
    """Transfer a RoutingState's committed copper into the grid's static owner array.

    After diff pairs are routed and committed into a temporary RoutingState, this
    function merges those routes into the grid so that all subsequent RoutingState
    instances built on the same grid see the diff pair copper as static obstacles —
    without any changes to ``run_routing`` or the annealing loop.

    Only FREE nodes are claimed (pad interiors and board-edge blocks are
    preserved); conflicts between two different nets collapse to BLOCKED.

    Args:
        state: a RoutingState with committed diff pair routes.
        grid: the grid whose ``owner`` array is updated in place.
    """
    for (li, c, r), conn_set in state.cover.items():
        if not conn_set:
            continue
        net_ids = {state.conn_net[idx] for idx in conn_set}
        nid = next(iter(net_ids)) if len(net_ids) == 1 else BLOCKED
        current = grid.owner[li, r, c]
        if current == FREE:
            grid.owner[li, r, c] = nid
        elif current != nid and current != BLOCKED:
            grid.owner[li, r, c] = BLOCKED


def _offset_from_pads(
    state: RoutingState,
    dp_conn: DiffPairConnection,
) -> tuple[int, int]:
    """Compute the fixed grid-node offset (+ → −) from the source pad positions.

    Rounds the pad-centre delta to the nearest grid pitch. Falls back to the
    destination pad delta if both agree; averages them if they disagree.

    Args:
        state: the routing state (grid for coordinate conversion).
        dp_conn: the diff pair connection whose pad positions are used.

    Returns:
        ``(dc_off, dr_off)`` — the integer grid-column / grid-row offset from
        the + trace to the − trace.
    """
    grid = state.grid

    def _delta(px: float, py: float, nx_: float, ny_: float) -> tuple[int, int]:
        cp, rp = grid.nearest_node(px, py)
        cn, rn = grid.nearest_node(nx_, ny_)
        return cn - cp, rn - rp

    dc_src, dr_src = _delta(
        dp_conn.src_p.cx, dp_conn.src_p.cy,
        dp_conn.src_n.cx, dp_conn.src_n.cy,
    )
    dc_dst, dr_dst = _delta(
        dp_conn.dst_p.cx, dp_conn.dst_p.cy,
        dp_conn.dst_n.cx, dp_conn.dst_n.cy,
    )

    # Round average: if src and dst agree, this is exact; if not, it's
    # a reasonable compromise for the bulk of the route.
    dc_off = round((dc_src + dc_dst) / 2)
    dr_off = round((dr_src + dr_dst) / 2)
    return dc_off, dr_off


def _coupled_astar(
    state: RoutingState,
    net_id_p: int,
    net_id_n: int,
    dc_off: int,
    dr_off: int,
    sources: list[tuple[int, int, int]],
    targets: list[tuple[int, int, int]],
    params: RouteParams,
) -> list[tuple[int, int, int]] | None:
    """A* over the + trace, checking the − trace companion at every expansion.

    The state is ``(layer, col_p, row_p, dir)`` — identical in structure and
    size to the single-net A* in ``router.py``.  At each expansion both
    ``(layer, col_p, row_p)`` (+ trace) and ``(layer, col_p + dc_off,
    row_p + dr_off)`` (− trace) must be free for their respective nets.

    Args:
        state: the routing state (occupancy).
        net_id_p: integer net id for the + trace.
        net_id_n: integer net id for the − trace.
        dc_off: column offset from + to − trace (fixed throughout).
        dr_off: row offset from + to − trace (fixed throughout).
        sources: candidate start nodes (layer, col, row) for the + trace.
        targets: candidate goal nodes (layer, col, row) for the + trace.
        params: cost-model parameters.

    Returns:
        The ``(layer, col, row)`` path for the + trace, or ``None`` if no route
        was found within the expansion budget.
    """
    grid = state.grid
    nx, ny, n_layers, pitch = grid.nx, grid.ny, grid.n_layers, grid.pitch

    # State key: same packing as single-net astar.
    _D = 9
    _LROW = nx * _D

    def encode(li: int, c: int, r: int, d: int) -> int:
        return (li * ny + r) * _LROW + c * _D + (d + 1)

    target_set = frozenset((li, c, r) for li, c, r in targets)
    tgt_cr = [(c, r) for _, c, r in targets]

    def is_free_pair(li: int, c: int, r: int) -> bool:
        cn, rn = c + dc_off, r + dr_off
        if not grid.in_bounds(c, r) or not grid.in_bounds(cn, rn):
            return False
        return (state.is_free(li, c, r, net_id_p)
                and state.is_free(li, cn, rn, net_id_n))

    def can_via_pair(c: int, r: int) -> bool:
        return (state.can_via(c, r, net_id_p)
                and state.can_via(c + dc_off, r + dr_off, net_id_n))

    def h(c: int, r: int) -> float:
        best = math.inf
        for tc, tr in tgt_cr:
            dx = abs(c - tc)
            dy = abs(r - tr)
            lo = min(dx, dy)
            d = (max(dx, dy) - lo + lo * SQRT2) * pitch
            if d < best:
                best = d
        return best

    via_layers = getattr(grid, "_via_layer_neighbours", None)
    if via_layers is None:
        via_layers = [tuple(j for j in range(n_layers) if j != i)
                      for i in range(n_layers)]
        grid._via_layer_neighbours = via_layers

    counter = itertools.count()
    gscore: dict[int, float] = {}
    came: dict[int, tuple | None] = {}
    heap: list = []

    for li, c, r in sources:
        if not is_free_pair(li, c, r):
            continue
        s = encode(li, c, r, -1)
        gscore[s] = 0.0
        came[s] = (None, (li, c, r))
        heapq.heappush(heap, (h(c, r), next(counter), s, li, c, r, -1))

    expansions = 0
    while heap:
        f, _, st, li, c, r, pdir = heapq.heappop(heap)
        g = gscore.get(st, math.inf)
        if f > g + h(c, r) + 1e-9:
            continue
        if (li, c, r) in target_set:
            return _reconstruct(came, st)

        expansions += 1
        if expansions > params.max_expansions:
            return None

        for di, (dx, dy) in enumerate(_DIRS):
            nc, nr = c + dx, r + dy
            if not is_free_pair(li, nc, nr):
                continue
            diagonal = dx != 0 and dy != 0
            if diagonal and not (is_free_pair(li, c + dx, r)
                                 and is_free_pair(li, c, r + dy)):
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

        if n_layers > 1 and can_via_pair(c, r):
            ng = g + params.via_cost
            for nli in via_layers[li]:
                ns = encode(nli, c, r, -1)
                if ng < gscore.get(ns, math.inf):
                    gscore[ns] = ng
                    came[ns] = (st, (nli, c, r))
                    heapq.heappush(heap, (ng + h(c, r), next(counter),
                                          ns, nli, c, r, -1))

    return None


def _reconstruct(
    came: dict, st: int,
) -> list[tuple[int, int, int]]:
    """Rebuild the node path from the came-from map (same logic as router.py)."""
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


def route_diff_pair(
    state: RoutingState,
    dp_conn: DiffPairConnection,
    dp_gap: float,
    params: RouteParams | None = None,
) -> tuple[RouteResult, RouteResult] | None:
    """Route both traces of a differential pair simultaneously.

    Uses a coupled A* where the + trace is the primary search variable and the
    − trace always sits at a fixed grid-node offset.  Both traces must be free
    at every expansion step, so the result is automatically DRC-clean.

    Length matching is exact by construction: both paths have the same number
    of A* steps in every direction, so their geometric lengths are identical.

    Args:
        state: the routing state to query for occupancy (not committed here;
            the caller commits both results after a successful route).
        dp_conn: the diff pair connection specifying the paired pads and nets.
        dp_gap: the inner-edge spacing to enforce between the two traces (mm);
            used only to validate that the computed offset satisfies it.
        params: cost-model parameters; defaults are used when ``None``.

    Returns:
        ``(result_p, result_n)`` — one `RouteResult` per trace, or ``None`` if
        no route was found (pair is unroutable with the current offset).
    """
    params = params or RouteParams()
    grid = state.grid

    dc_off, dr_off = _offset_from_pads(state, dp_conn)
    if dc_off == 0 and dr_off == 0:
        return None   # pads snap to the same grid node — can't form a pair

    net_id_p = grid.net_id(dp_conn.net_p)
    net_id_n = grid.net_id(dp_conn.net_n)

    # Source and target access nodes for the + trace; companion nodes are implied
    # by the fixed offset and must also be in-bounds (filtered below).
    srcs_raw = grid.pad_access_nodes(dp_conn.src_p)
    tgts_raw = grid.pad_access_nodes(dp_conn.dst_p)

    sources = [(li, c, r) for li, c, r in srcs_raw
               if grid.in_bounds(c + dc_off, r + dr_off)]
    targets = [(li, c, r) for li, c, r in tgts_raw
               if grid.in_bounds(c + dc_off, r + dr_off)]

    if not sources or not targets:
        return None

    path_p = _coupled_astar(state, net_id_p, net_id_n,
                             dc_off, dr_off, sources, targets, params)
    if path_p is None:
        return None

    # Derive the − path: shift every node by the fixed offset.
    path_n = [(li, c + dc_off, r + dr_off) for li, c, r in path_p]

    src_p_xy = (dp_conn.src_p.cx, dp_conn.src_p.cy)
    dst_p_xy = (dp_conn.dst_p.cx, dp_conn.dst_p.cy)
    src_n_xy = (dp_conn.src_n.cx, dp_conn.src_n.cy)
    dst_n_xy = (dp_conn.dst_n.cx, dp_conn.dst_n.cy)

    len_p, vias_p = _path_metrics(grid, path_p)
    len_p += _stub_length(grid, path_p, src_p_xy, dst_p_xy)

    len_n, vias_n = _path_metrics(grid, path_n)
    len_n += _stub_length(grid, path_n, src_n_xy, dst_n_xy)

    result_p = RouteResult(net=dp_conn.net_p, path=path_p,
                           length=len_p, vias=vias_p,
                           src_xy=src_p_xy, dst_xy=dst_p_xy)
    result_n = RouteResult(net=dp_conn.net_n, path=path_n,
                           length=len_n, vias=vias_n,
                           src_xy=src_n_xy, dst_xy=dst_n_xy)
    return result_p, result_n
