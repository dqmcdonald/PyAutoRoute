"""Net grouping and rats-nest decomposition.

Each multi-pad net is reduced to a set of two-pin connections via a minimum
spanning tree over the pad centroids, so routing every connection joins all of
the net's pads with the least total rats-nest length. Nets matching an
``--exclude-net`` pattern are dropped (their pads still act as obstacles via the
grid, but no connections are generated for them).
"""

from __future__ import annotations

import fnmatch
import math
from dataclasses import dataclass

import numpy as np
from scipy.sparse.csgraph import minimum_spanning_tree
from scipy.spatial.distance import pdist, squareform

from .pcb import Board, Pad


@dataclass
class Connection:
    net: str
    a: Pad
    b: Pad

    @property
    def est_length(self) -> float:
        """Straight-line distance between the two pad centres (mm)."""
        return math.hypot(self.a.cx - self.b.cx, self.a.cy - self.b.cy)


def is_excluded(net: str, patterns: list[str]) -> bool:
    """Return whether a net matches any exclude pattern.

    Args:
        net: the net name to test.
        patterns: case-sensitive glob patterns (e.g. ``["GND", "/PWR*"]``).

    Returns:
        True if `net` matches at least one pattern.
    """
    return any(fnmatch.fnmatchcase(net, p) for p in patterns)


def _mst_connections(net: str, pads: list[Pad]) -> list[Connection]:
    """Decompose a multi-pad net into two-pin connections via a spanning tree.

    Args:
        net: the net name.
        pads: the pads on this net.

    Returns:
        The MST edges over the pad centroids as `Connection`s (empty for a
        single pad), so routing them all joins every pad with minimal rats-nest
        length.
    """
    n = len(pads)
    if n < 2:
        return []
    if n == 2:
        return [Connection(net, pads[0], pads[1])]
    coords = np.array([(p.cx, p.cy) for p in pads])
    dist = squareform(pdist(coords))
    mst = minimum_spanning_tree(dist).tocoo()
    return [Connection(net, pads[int(i)], pads[int(j)])
            for i, j in zip(mst.row, mst.col)]


_SNAP = 0.001  # 1 µm grid for connectivity snapping


class _UnionFind:
    """Simple path-compressed union-find over arbitrary hashable keys."""

    def __init__(self):
        self._parent: dict = {}

    def find(self, k):
        if k not in self._parent:
            self._parent[k] = k
        if self._parent[k] != k:
            self._parent[k] = self.find(self._parent[k])
        return self._parent[k]

    def union(self, a, b):
        self._parent[self.find(a)] = self.find(b)


def _snap(x: float, y: float) -> tuple[int, int]:
    return (round(x / _SNAP), round(y / _SNAP))


def pre_routed_connections(
    board: Board,
    connections: list[Connection],
) -> tuple[list[Connection], list[Connection]]:
    """Split *connections* into those already satisfied by existing copper and the rest.

    Builds a layer-aware union-find over all existing segments, free vias, and
    multi-layer (THT) pads, then classifies each MST connection as pre-routed
    (both endpoints are already in the same connected component) or unrouted.

    Args:
        board: the board, whose ``segments``, ``free_vias``, and ``pads`` supply
            the existing copper graph.
        connections: the full MST connection list from `build_connections`.

    Returns:
        ``(pre_routed, unrouted)`` — two lists that partition *connections*.
        Pre-routed connections should be counted as completed; unrouted ones
        should be passed to the router.
    """
    uf = _UnionFind()

    # Segments: join endpoints on the same layer
    for seg in board.segments:
        k1 = (seg.layer, *_snap(seg.x1, seg.y1))
        k2 = (seg.layer, *_snap(seg.x2, seg.y2))
        uf.union(k1, k2)

    # Free vias: join their position across both layers they span
    for via in board.free_vias:
        if len(via.layers) >= 2:
            k1 = (via.layers[0], *_snap(via.cx, via.cy))
            k2 = (via.layers[1], *_snap(via.cx, via.cy))
            uf.union(k1, k2)

    # THT pads: present on multiple copper layers — join all
    for pad in board.pads:
        if len(pad.copper_layers) >= 2:
            keys = [(lyr, *_snap(pad.cx, pad.cy)) for lyr in pad.copper_layers]
            for i in range(1, len(keys)):
                uf.union(keys[0], keys[i])

    def _pad_key(pad):
        layer = pad.copper_layers[0] if pad.copper_layers else "F.Cu"
        return (layer, *_snap(pad.cx, pad.cy))

    pre_routed: list[Connection] = []
    unrouted: list[Connection] = []
    for conn in connections:
        if uf.find(_pad_key(conn.a)) == uf.find(_pad_key(conn.b)):
            pre_routed.append(conn)
        else:
            unrouted.append(conn)

    return pre_routed, unrouted


def build_connections(board: Board, exclude: list[str] | None = None) -> list[Connection]:
    """Build the two-pin connection list (rats-nest) for a board.

    Args:
        board: the board whose pads-by-net drive the netlist.
        exclude: glob patterns for nets to skip routing (their pads still act as
            obstacles); `None` excludes nothing.

    Returns:
        The MST connections for every non-excluded multi-pad net.
    """
    exclude = exclude or []
    conns: list[Connection] = []
    for net, pads in board.pads_by_net().items():
        if is_excluded(net, exclude):
            continue
        conns.extend(_mst_connections(net, pads))
    return conns


def greedy_order(connections: list[Connection]) -> list[int]:
    """Compute the initial routing order: shortest connections first.

    Args:
        connections: the connections to order.

    Returns:
        Indices into `connections`, sorted by ascending estimated length.
    """
    return sorted(range(len(connections)),
                  key=lambda i: connections[i].est_length)
