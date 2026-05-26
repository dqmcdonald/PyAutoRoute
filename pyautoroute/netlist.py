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
        return math.hypot(self.a.cx - self.b.cx, self.a.cy - self.b.cy)


def is_excluded(net: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(net, p) for p in patterns)


def _mst_connections(net: str, pads: list[Pad]) -> list[Connection]:
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


def build_connections(board: Board, exclude: list[str] | None = None) -> list[Connection]:
    exclude = exclude or []
    conns: list[Connection] = []
    for net, pads in board.pads_by_net().items():
        if is_excluded(net, exclude):
            continue
        conns.extend(_mst_connections(net, pads))
    return conns


def greedy_order(connections: list[Connection]) -> list[int]:
    """Initial routing order: shortest connections first."""
    return sorted(range(len(connections)),
                  key=lambda i: connections[i].est_length)
