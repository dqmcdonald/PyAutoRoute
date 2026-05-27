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
        """Build the routing grid: node lattice, inflation margins, occupancy.

        Lays a uniform node lattice over the board's bounding box, computes the
        clearance inflation margins, then marks each node's owner from the board
        edge, copper obstacles, and pad interiors.

        Args:
            board: the parsed board (outline, pads, copper) to grid.
            rules: design rules giving clearances, track widths, via geometry.
            pitch: grid spacing in mm (finer = better coverage, slower).
        """
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

        # Inflation margins (exact for single-net-class boards). `safety`
        # accounts for grid discretisation: a point on a track segment between
        # two free node-centres can be up to a half-diagonal of the cell pitch
        # from the nearer node. The required keep-out `clear` and that offset are
        # perpendicular in the worst case (obstacle abeam the segment midpoint),
        # so they combine in quadrature, not linearly: a node kept hypot(clear,
        # safety) from an obstacle guarantees the whole segment stays `clear`
        # away. Summing them instead (clear + safety) over-inflates and can wall
        # off dense through-hole clusters that are in fact routable.
        max_track = max(c.track_width for c in rules.classes.values())
        max_via = max(c.via_diameter for c in rules.classes.values())
        self._max_clear = max(rules.clearance_for(n) for n in self._known_nets(board))
        self.safety = pitch * _SQRT2 / 2.0
        self.margin = math.hypot(max_track / 2.0 + self._max_clear, self.safety)
        self.via_margin = math.hypot(max_via / 2.0 + self._max_clear, self.safety)
        self.edge_margin = math.hypot(
            rules.min_copper_edge_clearance + max_track / 2.0, self.safety)

        # precomputed disk stencil for via-clearance sampling
        self._via_stencil = self._disk_stencil(self.via_margin)

        self.owner = np.full((self.n_layers, self.ny, self.nx), FREE, dtype=np.int32)
        self._build_edge_mask()
        self._build_obstacle_owners()
        self._force_pad_interiors()

    def _disk_stencil(self, radius: float) -> list[tuple[int, int]]:
        """Precompute the ``(dcol, drow)`` offsets within a disk of `radius`.

        Args:
            radius: the disk radius in mm (e.g. the via clearance radius).

        Returns:
            Node-index offsets whose centres lie within `radius` of the origin.
        """
        rc = int(math.ceil(radius / self.pitch))
        out = []
        for dc in range(-rc, rc + 1):
            for dr in range(-rc, rc + 1):
                if math.hypot(dc, dr) * self.pitch <= radius:
                    out.append((dc, dr))
        return out

    # --- net id helpers ------------------------------------------------------

    def _known_nets(self, board: Board) -> list[str]:
        """Return the distinct net names that have pads.

        Args:
            board: the board to scan.

        Returns:
            The net names with at least one pad, or ``["Default"]`` if none.
        """
        nets = {p.net for p in board.pads if p.net}
        return list(nets) or ["Default"]

    def net_id(self, net: str) -> int:
        """Map a net name to a small integer id, assigning one on first use.

        Args:
            net: the net name.

        Returns:
            The stable integer id used in the occupancy grid for this net.
        """
        if net not in self._net_id:
            i = len(self._net_id)
            self._net_id[net] = i
            self._id_net[i] = net
        return self._net_id[net]

    def layer_index(self, layer: str) -> int:
        """Return the grid layer index for a copper-layer name.

        Args:
            layer: a copper-layer name such as ``"F.Cu"``.

        Returns:
            The 0-based layer index (0 is the front layer).
        """
        return self._layer_idx[layer]

    # --- coordinate conversion ----------------------------------------------

    def node_xy(self, col: int, row: int) -> tuple[float, float]:
        """Convert a grid node index to board coordinates.

        Args:
            col: the node column.
            row: the node row.

        Returns:
            The node's ``(x, y)`` in board mm.
        """
        return (self.minx + col * self.pitch, self.miny + row * self.pitch)

    def nearest_node(self, x: float, y: float) -> tuple[int, int]:
        """Find the grid node nearest a board coordinate (clamped in-bounds).

        Args:
            x: board x in mm.
            y: board y in mm.

        Returns:
            The clamped ``(col, row)`` of the closest node.
        """
        col = int(round((x - self.minx) / self.pitch))
        row = int(round((y - self.miny) / self.pitch))
        col = min(max(col, 0), self.nx - 1)
        row = min(max(row, 0), self.ny - 1)
        return col, row

    def in_bounds(self, col: int, row: int) -> bool:
        """Return whether ``(col, row)`` lies within the grid.

        Args:
            col: the node column.
            row: the node row.
        """
        return 0 <= col < self.nx and 0 <= row < self.ny

    # --- occupancy build -----------------------------------------------------

    def _mesh_in_bbox(self, bounds):
        """Return the grid node indices + coordinates overlapping a bbox.

        Args:
            bounds: a ``(minx, miny, maxx, maxy)`` bounding box in board mm.

        Returns:
            ``(cols, rows, xx, yy)`` index/coordinate meshgrids for the covered
            region, or `None` if the box falls outside the grid.
        """
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
        """Assign ownership for the masked nodes, resolving conflicts to BLOCKED.

        Args:
            layer_idx: the copper layer index to mark.
            rows: the row indices of the sub-grid being marked.
            cols: the column indices of the sub-grid being marked.
            mask: boolean array (over ``rows`` x ``cols``) of nodes to claim.
            net_id: the net id to assign; FREE nodes take it, same-net nodes are
                unchanged, and a different existing owner becomes BLOCKED.
        """
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
        """Return whether a net may route through a node.

        Args:
            layer_idx: the copper layer index.
            col: the node column.
            row: the node row.
            net_id: the routing net's id.

        Returns:
            True if the node is in bounds and either FREE or already owned by
            `net_id` (a net may always use its own copper).
        """
        if not self.in_bounds(col, row):
            return False
        o = self.owner[layer_idx, row, col]
        return o == FREE or o == net_id

    def can_via(self, col: int, row: int, net_id: int) -> bool:
        """Return whether a via for `net_id` may be placed at a node.

        A via needs its whole pad+clearance disk free for this net on every
        copper layer (vias are larger than tracks, so they clear more area).

        Args:
            col: the node column for the via centre.
            row: the node row for the via centre.
            net_id: the routing net's id.

        Returns:
            True if every node in the via-clearance stencil is free for
            `net_id` on all layers.
        """
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
        """Return the grid nodes a router can use to enter a pad.

        Args:
            pad: the pad to find access nodes for.

        Returns:
            ``(layer_idx, col, row)`` for each node inside the pad polygon on
            each of the pad's copper layers; if the pad falls between nodes, the
            single nearest node on each layer.
        """
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
