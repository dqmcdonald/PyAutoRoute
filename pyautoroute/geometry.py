"""Shapely geometry for pads, the board outline, and routing obstacles.

All shapes are in KiCad board coordinates (millimetres, Y pointing down). Pad
rotation uses the same convention as :func:`pyautoroute.pcb.rotate`, so a pad's
polygon lines up with the absolute centre computed during board parsing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from shapely import affinity
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

from .pcb import Board, OutlineShape, Pad, Segment, Via

_ARC_SEGMENTS = 24


# --- pad / track / via shapes ------------------------------------------------

def _base_pad_shape(pad: Pad) -> Polygon:
    """Build the pad's copper outline centred at the origin and unrotated.

    Handles circle / oval / roundrect / trapezoid; rect and custom (and any
    unknown shape) fall back to the bounding box.

    Args:
        pad: the pad whose shape/size fields define the polygon.

    Returns:
        The origin-centred, unrotated pad polygon.
    """
    w, h = pad.w, pad.h
    shape = pad.shape
    if shape == "circle":
        return Point(0, 0).buffer(max(w, h) / 2.0)
    if shape == "oval":
        if abs(w - h) < 1e-9:
            return Point(0, 0).buffer(w / 2.0)
        if w >= h:
            r, half = h / 2.0, (w - h) / 2.0
            return LineString([(-half, 0), (half, 0)]).buffer(r)
        r, half = w / 2.0, (h - w) / 2.0
        return LineString([(0, -half), (0, half)]).buffer(r)
    if shape == "roundrect":
        r = (pad.roundrect_rratio or 0.0) * min(w, h)
        if r <= 1e-9:
            return box(-w / 2, -h / 2, w / 2, h / 2)
        inner = box(-w / 2 + r, -h / 2 + r, w / 2 - r, h / 2 - r)
        return inner.buffer(r, join_style="round")
    if shape == "trapezoid" and pad.rect_delta is not None:
        dx, dy = w / 2.0, h / 2.0
        ddx, ddy = pad.rect_delta[0] / 2.0, pad.rect_delta[1] / 2.0
        return Polygon([
            (-dx - ddy, dy + ddx),
            (dx + ddy, dy - ddx),
            (dx - ddy, -dy + ddx),
            (-dx + ddy, -dy - ddx),
        ])
    # rect, custom (bounding-box fallback), or anything unknown
    return box(-w / 2, -h / 2, w / 2, h / 2)


def pad_polygon(pad: Pad) -> Polygon:
    """Build a pad's absolute copper polygon (rotated then translated).

    Args:
        pad: the pad, whose `angle`/`cx`/`cy` place the base shape on the board.

    Returns:
        The pad polygon in board coordinates.
    """
    base = _base_pad_shape(pad)
    # KiCad rotate(x,y,a) == CCW rotation by -a, so undo with shapely's CCW
    rotated = affinity.rotate(base, -pad.angle, origin=(0, 0))
    return affinity.translate(rotated, pad.cx, pad.cy)


def segment_polygon(seg: Segment) -> Polygon:
    """Build the copper polygon of a track segment (a width-thick capsule).

    Args:
        seg: the segment, whose endpoints and `width` define the capsule.

    Returns:
        The segment's copper area as a round-capped buffer.
    """
    line = LineString([(seg.x1, seg.y1), (seg.x2, seg.y2)])
    return line.buffer(seg.width / 2.0, cap_style="round")


def via_polygon(via: Via) -> Polygon:
    """Build a via's copper polygon (a disk of its annular-ring diameter).

    Args:
        via: the via, whose centre and `size` define the disk.

    Returns:
        The via's circular copper area.
    """
    return Point(via.cx, via.cy).buffer(via.size / 2.0)


def inflate(geom, dist: float):
    """Grow a geometry outward by `dist` with rounded corners.

    Args:
        geom: the shapely geometry to grow.
        dist: the offset distance (mm); ``<= 0`` returns `geom` unchanged.

    Returns:
        The buffered geometry (or `geom` itself when `dist <= 0`).
    """
    if dist <= 0:
        return geom
    return geom.buffer(dist, join_style="round")


# --- board outline -----------------------------------------------------------

def _arc_points(start, mid, end) -> list[tuple[float, float]]:
    """Sample a circular arc defined by three points into a polyline.

    Args:
        start: the arc start point ``(x, y)``.
        mid: a point on the arc between start and end (fixes the sweep).
        end: the arc end point ``(x, y)``.

    Returns:
        ``_ARC_SEGMENTS + 1`` points along the arc; falls back to
        ``[start, end]`` if the three points are collinear.
    """
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    # circumcentre of the three points
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [start, end]
    ux = ((x1 ** 2 + y1 ** 2) * (y2 - y3) + (x2 ** 2 + y2 ** 2) * (y3 - y1)
          + (x3 ** 2 + y3 ** 2) * (y1 - y2)) / d
    uy = ((x1 ** 2 + y1 ** 2) * (x3 - x2) + (x2 ** 2 + y2 ** 2) * (x1 - x3)
          + (x3 ** 2 + y3 ** 2) * (x2 - x1)) / d
    cx, cy = ux, uy
    a0 = math.atan2(y1 - cy, x1 - cx)
    a1 = math.atan2(y3 - cy, x3 - cx)
    am = math.atan2(y2 - cy, x2 - cx)
    r = math.hypot(x1 - cx, y1 - cy)

    # choose sweep direction that passes through the mid point
    def norm(a):
        while a < 0:
            a += 2 * math.pi
        while a >= 2 * math.pi:
            a -= 2 * math.pi
        return a

    a0n, a1n, amn = norm(a0), norm(a1), norm(am)
    ccw_span = norm(a1n - a0n)
    mid_ccw = norm(amn - a0n)
    ccw = mid_ccw <= ccw_span
    span = ccw_span if ccw else -(2 * math.pi - ccw_span)

    pts = []
    for i in range(_ARC_SEGMENTS + 1):
        a = a0 + span * (i / _ARC_SEGMENTS)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def outline_to_polygon(shapes: list[OutlineShape]) -> Polygon:
    """Assemble Edge.Cuts shapes into a single board-area polygon.

    Closed shapes (poly/rect/circle) are taken directly; loose edges
    (line/arc) are noded and polygonized so outlines with overlapping or
    collinear-redundant segments still close. The largest resulting region is
    returned.

    Args:
        shapes: the Edge.Cuts shapes from `pyautoroute.pcb.Board.outline`.

    Returns:
        The board-area polygon.

    Raises:
        ValueError: if no closed outline can be formed.
    """
    closed: list[Polygon] = []
    edges: list[LineString] = []
    for s in shapes:
        if s.kind == "poly":
            pts = s.data["pts"]
            if len(pts) >= 3:
                closed.append(Polygon(pts))
        elif s.kind == "rect":
            (x1, y1), (x2, y2) = s.data["start"], s.data["end"]
            closed.append(box(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
        elif s.kind == "circle":
            (cx, cy), (ex, ey) = s.data["center"], s.data["end"]
            closed.append(Point(cx, cy).buffer(math.hypot(ex - cx, ey - cy)))
        elif s.kind == "line":
            edges.append(LineString([s.data["start"], s.data["end"]]))
        elif s.kind == "arc":
            edges.append(LineString(_arc_points(
                s.data["start"], s.data["mid"], s.data["end"])))

    if edges:
        # Node the edges first: KiCad outlines may contain overlapping or
        # collinear-redundant segments (e.g. two edges sharing part of a span
        # instead of meeting at a single vertex). polygonize needs a cleanly
        # noded graph, so union the linework to split overlaps before tracing.
        closed.extend(polygonize(unary_union(edges)))
    if not closed:
        raise ValueError("no closed board outline found on Edge.Cuts")
    merged = unary_union(closed)
    if merged.geom_type == "MultiPolygon":
        return max(merged.geoms, key=lambda g: g.area)
    return merged


# --- obstacle index ----------------------------------------------------------

@dataclass
class Obstacle:
    geom: Polygon
    net: str
    layer: str


@dataclass
class Drill:
    """A drilled hole on the board (a plated or non-plated through-hole).

    Drills are net-agnostic, all-layer keep-outs: a barrel passes through every
    copper layer regardless of any annular ring, and ``min_hole_to_hole`` spacing
    applies between *any* two holes (even on the same net), unlike copper
    clearance. They are therefore tracked separately from `Obstacle`.
    """
    geom: Point          # barrel centre
    radius: float        # drill_diameter / 2 (mm)
    plated: bool         # True for thru_hole, False for np_thru_hole
    ref: str             # owning footprint reference designator (for messages)


def board_drills(board: Board) -> list[Drill]:
    """Collect every drilled hole on the board.

    Through-hole pads (`thru_hole` / `np_thru_hole`) with a non-`None`
    `pyautoroute.pcb.Pad.drill` become a `Drill`. Routed vias are *not* included:
    their spacing is governed by the grid's via-clearance model, not
    ``min_hole_to_hole``.

    Args:
        board: the board whose pads are scanned.

    Returns:
        One `Drill` per drilled through-hole pad.
    """
    drills: list[Drill] = []
    for pad in board.pads:
        if pad.pad_type not in ("thru_hole", "np_thru_hole") or not pad.drill:
            continue
        drills.append(Drill(
            geom=Point(pad.cx, pad.cy),
            radius=pad.drill / 2.0,
            plated=(pad.pad_type == "thru_hole"),
            ref=pad.fp_ref,
        ))
    return drills


def board_obstacles(board: Board) -> list[Obstacle]:
    """Collect raw (un-inflated) copper obstacles tagged with net and layer.

    Drilled through-hole pads also contribute an **all-layer barrel keep-out**:
    on any copper layer the pad lacks an annular ring (e.g. a non-plated mounting
    hole with no copper), a disk of the drill radius is added so the router never
    drives copper across the hole. On layers where the pad already has copper the
    ring already covers the barrel, so no duplicate is emitted.

    Args:
        board: the board whose pads, segments, free vias, and zones become
            obstacles.

    Returns:
        One `Obstacle` per copper shape per layer it occupies.
    """
    obs: list[Obstacle] = []
    for pad in board.pads:
        poly = pad_polygon(pad)
        for layer in pad.copper_layers:
            obs.append(Obstacle(poly, pad.net, layer))
        # A drilled hole reserves its barrel on every copper layer. Layers the
        # pad already coppers are covered by the ring above; add a bare barrel
        # disk for the rest (the common case being a layerless NPTH hole).
        if pad.pad_type in ("thru_hole", "np_thru_hole") and pad.drill:
            barrel = Point(pad.cx, pad.cy).buffer(pad.drill / 2.0)
            for layer in board.copper_layers:
                if layer not in pad.copper_layers:
                    obs.append(Obstacle(barrel, pad.net, layer))
    for seg in board.segments:
        obs.append(Obstacle(segment_polygon(seg), seg.net, seg.layer))
    for via in board.free_vias:
        poly = via_polygon(via)
        for layer in board.copper_layers:
            obs.append(Obstacle(poly, via.net, layer))
    for zone in board.zones:
        if zone.get("fill_enabled"):
            continue  # copper pours are managed by KiCad; not routing obstacles
        pts = zone.get("polygon") or []
        if len(pts) >= 3:
            zpoly = Polygon(pts)
            for layer in (zone.get("layers") or []):
                if layer in board.copper_layers:
                    obs.append(Obstacle(zpoly, zone.get("net", ""), layer))
    return obs


def clearance_violations(board: Board, rules) -> list[tuple[str, str, str, float]]:
    """Run the in-repo DRC self-check for inter-net clearance.

    This is the fast self-check that runs without kicad-cli: it re-derives copper
    from the (routed) board and checks pairwise spacing per layer via an STRtree.

    Args:
        board: the (routed) board to check.
        rules: the `pyautoroute.rules.DesignRules` giving per-net clearances.

    Returns:
        One ``(layer, net_a, net_b, gap)`` tuple per different-net pair closer
        than the required clearance; an empty list means the board is clean.
    """
    obs = board_obstacles(board)
    by_layer: dict[str, list[Obstacle]] = {}
    for o in obs:
        by_layer.setdefault(o.layer, []).append(o)

    violations = []
    for layer, items in by_layer.items():
        tree = STRtree([o.geom for o in items])
        for i, o in enumerate(items):
            need = max(rules.clearance_for(o.net), 0.0)
            # query neighbours within the largest plausible clearance
            probe = o.geom.buffer(need + 0.01)
            for j in tree.query(probe):
                if j <= i:
                    continue
                other = items[j]
                if other.net == o.net and o.net:
                    continue
                req = rules.pair_clearance(o.net, other.net)
                gap = o.geom.distance(other.geom)
                if gap < req - 1e-6:
                    violations.append((layer, o.net, other.net, gap))
    return violations


def drill_violations(board: Board, rules) -> list[tuple[str, str, float]]:
    """Run the in-repo hole-to-hole spacing self-check.

    Checks every pair of drilled holes against the board's flat
    ``min_hole_to_hole`` rule. Holes are net-agnostic: two holes on the *same*
    net still must respect the spacing, so (unlike `clearance_violations`) there
    is no same-net exemption and a single all-layer STRtree pass suffices.

    Args:
        board: the board to check.
        rules: the `pyautoroute.rules.DesignRules` giving ``min_hole_to_hole``.

    Returns:
        One ``(ref_a, ref_b, gap)`` tuple per hole pair whose edge-to-edge gap is
        below ``min_hole_to_hole``; an empty list means the drills are clean.
    """
    drills = board_drills(board)
    if len(drills) < 2:
        return []
    need = max(rules.min_hole_to_hole, 0.0)
    max_r = max(d.radius for d in drills)
    tree = STRtree([d.geom for d in drills])
    violations = []
    for i, d in enumerate(drills):
        # neighbours whose centre could be within (need + r_d + r_other)
        probe = d.geom.buffer(d.radius + need + max_r + 0.01)
        for j in tree.query(probe):
            if j <= i:
                continue
            other = drills[j]
            gap = d.geom.distance(other.geom) - d.radius - other.radius
            if gap < need - 1e-6:
                violations.append((d.ref, other.ref, gap))
    return violations


class ObstacleIndex:
    """Per-layer STRtree of obstacle polygons for fast spatial queries."""

    def __init__(self, obstacles: list[Obstacle]):
        """Bucket obstacles by layer and build a per-layer STRtree.

        Args:
            obstacles: the obstacles to index (e.g. from `board_obstacles`).
        """
        self._by_layer: dict[str, list[Obstacle]] = {}
        for o in obstacles:
            self._by_layer.setdefault(o.layer, []).append(o)
        self._trees: dict[str, STRtree] = {}
        for layer, obs in self._by_layer.items():
            self._trees[layer] = STRtree([o.geom for o in obs])

    def layers(self) -> list[str]:
        """Return the layer names that have at least one indexed obstacle."""
        return list(self._by_layer.keys())

    def query(self, layer: str, geom) -> list[Obstacle]:
        """Find obstacles on a layer whose bounding box intersects `geom`.

        Args:
            layer: the copper layer to query.
            geom: the shapely geometry to test against.

        Returns:
            Candidate obstacles on `layer` (bbox-level overlap; refine with an
            exact predicate if needed), or ``[]`` if the layer is unknown.
        """
        tree = self._trees.get(layer)
        if tree is None:
            return []
        obs = self._by_layer[layer]
        return [obs[i] for i in tree.query(geom)]
