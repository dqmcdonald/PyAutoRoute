"""Two-layer routing grid with per-layer occupancy and pad access nodes.

A uniform grid of nodes is laid over the board's bounding box. Each node on each
copper layer is tagged with an *owner*:

* ``FREE`` (-1)    nothing there — any net may route through;
* ``BLOCKED`` (-2) board edge, no-net copper, or copper of two different nets —
  no net may route through;
* ``net id``       copper of exactly one net — only that net may route through
  (so a net can always reach its own pads).

Obstacles are inflated by ``track/2 + clearance`` before rasterising, so a track
centred on a FREE node automatically satisfies clearance. Diagonal moves are the
router's concern; the grid only answers "is this node usable by net N".
"""

from __future__ import annotations

import math

import numpy as np
import shapely

from . import geometry
from .pcb import Board, Pad
from .rules import DesignRules

FREE = -1
BLOCKED = -2

_SQRT2 = math.sqrt(2.0)


class Grid:
    def __init__(self, board: Board, rules: DesignRules, pitch: float):
        self.board = board
        self.rules = rules
        self.pitch = pitch
        self.layers = list(board.copper_layers)
        self.n_layers = len(self.layers)
        self._layer_idx = {name: i for i, name in enumerate(self.layers)}

        outline = geometry.outline_to_polygon(board.outline)
        self.outline = outline
        minx, miny, maxx, maxy = outline.bounds
        self.minx, self.miny = minx, miny
        self.nx = int(np.floor((maxx - minx) / pitch)) + 1
        self.ny = int(np.floor((maxy - miny) / pitch)) + 1

        # node coordinate lookup tables
        self._xs = minx + np.arange(self.nx) * pitch
        self._ys = miny + np.arange(self.ny) * pitch

        # net-name <-> id maps ("" = unconnected copper, always a blocker)
        self._net_id: dict[str, int] = {}
        self._id_net: dict[int, str] = {}

        # conservative inflation margins (exact for single-net-class boards).
        # `safety` accounts for grid discretisation: a track segment between two
        # free node-centres must stay clear of obstacles, so obstacles are grown
        # by an extra half-diagonal of the cell pitch.
        max_track = max(c.track_width for c in rules.classes.values())
        max_via = max(c.via_diameter for c in rules.classes.values())
        self._max_clear = max(rules.clearance_for(n) for n in self._known_nets(board))
        self.safety = pitch * _SQRT2 / 2.0
        self.margin = max_track / 2.0 + self._max_clear + self.safety
        self.via_margin = max_via / 2.0 + self._max_clear + self.safety
        self.edge_margin = rules.min_copper_edge_clearance + max_track / 2.0 + self.safety

        # precomputed disk stencil for via-clearance sampling
        self._via_stencil = self._disk_stencil(self.via_margin)

        self.owner = np.full((self.n_layers, self.ny, self.nx), FREE, dtype=np.int32)
        self._build_edge_mask()
        self._build_obstacle_owners()
        self._force_pad_interiors()

    def _disk_stencil(self, radius: float) -> list[tuple[int, int]]:
        rc = int(math.ceil(radius / self.pitch))
        out = []
        for dc in range(-rc, rc + 1):
            for dr in range(-rc, rc + 1):
                if math.hypot(dc, dr) * self.pitch <= radius:
                    out.append((dc, dr))
        return out

    # --- net id helpers ------------------------------------------------------

    def _known_nets(self, board: Board) -> list[str]:
        nets = {p.net for p in board.pads if p.net}
        return list(nets) or ["Default"]

    def net_id(self, net: str) -> int:
        if net not in self._net_id:
            i = len(self._net_id)
            self._net_id[net] = i
            self._id_net[i] = net
        return self._net_id[net]

    def layer_index(self, layer: str) -> int:
        return self._layer_idx[layer]

    # --- coordinate conversion ----------------------------------------------

    def node_xy(self, col: int, row: int) -> tuple[float, float]:
        return (self.minx + col * self.pitch, self.miny + row * self.pitch)

    def nearest_node(self, x: float, y: float) -> tuple[int, int]:
        col = int(round((x - self.minx) / self.pitch))
        row = int(round((y - self.miny) / self.pitch))
        col = min(max(col, 0), self.nx - 1)
        row = min(max(row, 0), self.ny - 1)
        return col, row

    def in_bounds(self, col: int, row: int) -> bool:
        return 0 <= col < self.nx and 0 <= row < self.ny

    # --- occupancy build -----------------------------------------------------

    def _mesh_in_bbox(self, bounds):
        """Grid node indices + coordinates whose bbox overlaps `bounds`."""
        bx0, by0, bx1, by1 = bounds
        c0 = max(int(np.floor((bx0 - self.minx) / self.pitch)), 0)
        c1 = min(int(np.ceil((bx1 - self.minx) / self.pitch)), self.nx - 1)
        r0 = max(int(np.floor((by0 - self.miny) / self.pitch)), 0)
        r1 = min(int(np.ceil((by1 - self.miny) / self.pitch)), self.ny - 1)
        if c1 < c0 or r1 < r0:
            return None
        cols = np.arange(c0, c1 + 1)
        rows = np.arange(r0, r1 + 1)
        xx, yy = np.meshgrid(self._xs[cols], self._ys[rows])
        return cols, rows, xx, yy

    def _build_edge_mask(self):
        # nodes outside the inset board polygon are blocked on every layer
        inset = self.outline.buffer(-self.edge_margin)
        xx, yy = np.meshgrid(self._xs, self._ys)
        if inset.is_empty:
            self.owner[:] = BLOCKED
            return
        inside = shapely.contains_xy(inset, xx, yy)  # (ny, nx)
        outside = ~inside
        for li in range(self.n_layers):
            self.owner[li][outside] = BLOCKED

    def _mark(self, layer_idx, rows, cols, mask, net_id):
        """Assign ownership for the masked nodes, resolving conflicts to BLOCKED."""
        sub = self.owner[layer_idx][np.ix_(rows, cols)]
        # FREE -> net_id; same net -> unchanged; anything else -> BLOCKED
        take = mask & (sub == FREE)
        sub[take] = net_id
        conflict = mask & (sub != FREE) & (sub != net_id) & (sub != BLOCKED)
        sub[conflict] = BLOCKED
        self.owner[layer_idx][np.ix_(rows, cols)] = sub

    def _build_obstacle_owners(self):
        for obs in geometry.board_obstacles(self.board):
            if obs.layer not in self._layer_idx:
                continue
            li = self._layer_idx[obs.layer]
            nid = self.net_id(obs.net) if obs.net else BLOCKED
            grown = geometry.inflate(obs.geom, self.margin)
            m = self._mesh_in_bbox(grown.bounds)
            if m is None:
                continue
            cols, rows, xx, yy = m
            mask = shapely.contains_xy(grown, xx, yy)
            if mask.any():
                self._mark(li, rows, cols, mask, nid)

    def _force_pad_interiors(self):
        """Nodes strictly inside a real pad belong to that pad's net, overriding
        any foreign clearance zone — a track may always enter its own pad (the
        pad copper is already there, so it adds no clearance violation). The
        inflated zone *outside* the pad still keeps other nets clear of it."""
        for pad in self.board.pads:
            if not pad.net:
                continue
            nid = self.net_id(pad.net)
            poly = geometry.pad_polygon(pad)
            m = self._mesh_in_bbox(poly.bounds)
            if m is None:
                continue
            cols, rows, xx, yy = m
            mask = shapely.contains_xy(poly, xx, yy)
            if not mask.any():
                continue
            for layer in pad.copper_layers:
                if layer not in self._layer_idx:
                    continue
                li = self._layer_idx[layer]
                sub = self.owner[li][np.ix_(rows, cols)]
                sub[mask] = nid
                self.owner[li][np.ix_(rows, cols)] = sub

    # --- queries -------------------------------------------------------------

    def is_free(self, layer_idx: int, col: int, row: int, net_id: int) -> bool:
        if not self.in_bounds(col, row):
            return False
        o = self.owner[layer_idx, row, col]
        return o == FREE or o == net_id

    def can_via(self, col: int, row: int, net_id: int) -> bool:
        """A via needs its whole pad+clearance disk free for this net on every
        copper layer (vias are larger than tracks, so they clear more area)."""
        for dc, dr in self._via_stencil:
            c, r = col + dc, row + dr
            if not self.in_bounds(c, r):
                return False
            for li in range(self.n_layers):
                o = self.owner[li, r, c]
                if o != FREE and o != net_id:
                    return False
        return True

    def pad_access_nodes(self, pad: Pad) -> list[tuple[int, int, int]]:
        """Grid nodes (layer_idx, col, row) lying inside the pad on its layers."""
        poly = geometry.pad_polygon(pad)
        m = self._mesh_in_bbox(poly.bounds)
        if m is None:
            return []
        cols, rows, xx, yy = m
        mask = shapely.contains_xy(poly, xx, yy)
        nodes: list[tuple[int, int, int]] = []
        if not mask.any():
            # tiny pad fell between nodes: snap to nearest node
            col, row = self.nearest_node(pad.cx, pad.cy)
            for layer in pad.copper_layers:
                if layer in self._layer_idx:
                    nodes.append((self._layer_idx[layer], col, row))
            return nodes
        rr, cc = np.where(mask)
        for layer in pad.copper_layers:
            if layer not in self._layer_idx:
                continue
            li = self._layer_idx[layer]
            for k in range(len(rr)):
                nodes.append((li, int(cols[cc[k]]), int(rows[rr[k]])))
        return nodes
