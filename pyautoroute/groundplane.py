"""Auto-add a ground plane after routing."""

from __future__ import annotations

from typing import TYPE_CHECKING
import fnmatch

if TYPE_CHECKING:
    from .pcb import Board
    from .rules import DesignRules
    from sexpr import SList


def build(board: Board, rules: DesignRules, *,
          net: str | None = None,
          layer: str = "B.Cu",
          margin: float | None = None,
          stitch_pitch: float | None = None) -> tuple[list[SList], list[str]]:
    """Build ground-plane zone node(s) and connecting vias.

    Args:
        board: the routed board.
        rules: design rules (clearance, via size).
        net: ground net name; if None, auto-detect ("GND", then glob "gnd"/"ground").
        layer: zone layer ("B.Cu", "F.Cu", or pass multiple times for each).
        margin: outline inset margin (mm); if None, uses board default clearance.
        stitch_pitch: optional pitch (mm) for stitching vias between layers; if None, no stitching.

    Returns:
        (list of zone/via nodes, list of warning strings). Empty list if skipped.
    """
    from . import pcb, geometry

    nodes: list[SList] = []
    warnings: list[str] = []

    # ── Guard: existing pour ──────────────────────────────────────────────────
    if pcb.zone_fill_nets(board):
        warnings.append("Board already has a copper pour; not adding ground plane")
        return ([], warnings)

    # ── GND auto-detect ───────────────────────────────────────────────────────
    gnd_net = net
    if gnd_net is None:
        pads_by_net = board.pads_by_net()
        # Exact "GND" (case-insensitive)
        gnd_candidates = [n for n in pads_by_net.keys() if n.upper() == "GND"]
        if gnd_candidates:
            gnd_net = gnd_candidates[0]
        else:
            # Glob "gnd" or "ground"
            gnd_candidates = [
                n for n in pads_by_net.keys()
                if fnmatch.fnmatch(n.lower(), "gnd*") or fnmatch.fnmatch(n.lower(), "*ground*")
            ]
            if len(gnd_candidates) == 1:
                gnd_net = gnd_candidates[0]
            elif gnd_candidates:
                warnings.append(
                    f"Ambiguous ground nets: {gnd_candidates}; use --ground-net to specify"
                )
                return ([], warnings)
            else:
                warnings.append("No ground net found; use --ground-net to specify")
                return ([], warnings)

    # ── Pour polygon (outline inset by margin) ────────────────────────────────
    if margin is None:
        margin = rules.default_class.clearance
    try:
        outline_poly = geometry.outline_to_polygon(board.outline)
    except Exception as e:
        warnings.append(f"Could not compute outline: {e}")
        return ([], warnings)

    inset_poly = outline_poly.buffer(-margin)
    if inset_poly.is_empty:
        warnings.append(f"Outline inset by {margin} mm is empty (margin too large?)")
        return ([], warnings)

    # Handle MultiPolygon: take the largest
    if hasattr(inset_poly, "geoms"):  # MultiPolygon
        inset_poly = max(inset_poly.geoms, key=lambda p: p.area)

    # Get exterior ring vertices (drop repeated closing point)
    pts = list(inset_poly.exterior.coords)[:-1]

    # ── Build zone node ───────────────────────────────────────────────────────
    clearance = rules.clearance_for(gnd_net)
    min_thickness = 0.25
    zone_node = pcb.make_zone_node(
        board, layer, gnd_net, pts,
        clearance=clearance,
        min_thickness=min_thickness
    )
    nodes.append(zone_node)

    # ── Connectivity vias (for SMD-only islands) ──────────────────────────────
    connectivity_vias = _add_connectivity_vias(
        board, rules, gnd_net, layer, inset_poly, clearance
    )
    nodes.extend(connectivity_vias)

    # ── Stitching vias (optional grid) ────────────────────────────────────────
    if stitch_pitch is not None and stitch_pitch > 0:
        stitch_vias = _add_stitching_vias(
            board, rules, gnd_net, layer, inset_poly, stitch_pitch, clearance
        )
        nodes.extend(stitch_vias)

    return (nodes, warnings)


def _add_connectivity_vias(board: Board, rules: DesignRules, gnd_net: str, layer: str,
                           pour_poly, clearance: float) -> list[SList]:
    """Add vias to connect GND islands that don't reach the pour layer.

    For each connected GND component that doesn't have copper on the pour layer:
    place a via at a point inside the pour polygon to tie the component in.
    """
    from . import pcb
    from shapely.geometry import Point

    vias = []

    # Union-find over GND copper (pads + segments + vias)
    parent: dict = {}

    def _find(k):
        if k not in parent:
            parent[k] = k
        if parent[k] != k:
            parent[k] = _find(parent[k])
        return parent[k]

    def _union(a, b):
        parent[_find(a)] = _find(b)

    # Helper to snap a point to 0.01 mm grid
    SNAP_MM = 0.01
    def _snap(x, y):
        return (round(x / SNAP_MM), round(y / SNAP_MM))

    # Track which layers each component has copper on
    component_layers: dict[tuple, set] = {}

    # Register GND pads
    for pad in board.pads:
        if pad.net != gnd_net:
            continue
        snap_pos = _snap(pad.cx, pad.cy)
        # Through-hole pads reach all layers
        if "Via" in pad.pad_type or all(layer in pad.copper_layers for layer in ["F.Cu", "B.Cu"]):
            component_layers[snap_pos] = {"F.Cu", "B.Cu"}
        else:
            component_layers[snap_pos] = {
                pad.copper_layers[0] if pad.copper_layers else "F.Cu"
            }
        _union(("pad", id(pad)), snap_pos)

    # Register GND segments
    for seg in board.segments:
        if seg.net != gnd_net:
            continue
        p1 = _snap(seg.x1, seg.y1)
        p2 = _snap(seg.x2, seg.y2)
        component_layers.setdefault(p1, set()).add(seg.layer)
        component_layers.setdefault(p2, set()).add(seg.layer)
        _union(p1, p2)


    # Find components that don't reach the pour layer
    roots_needing_via: set = set()
    for snap_pos, layers in component_layers.items():
        if layer not in layers:
            roots_needing_via.add(_find(snap_pos))

    # For each component needing a via, find a point inside the pour polygon
    for root in roots_needing_via:
        # Find a pad or segment point in this component that's inside the pour
        via_point = None
        for pad in board.pads:
            if pad.net != gnd_net:
                continue
            if _find(_snap(pad.cx, pad.cy)) == root and Point(pad.cx, pad.cy).within(pour_poly):
                via_point = (pad.cx, pad.cy)
                break
        if via_point is None:
            for seg in board.segments:
                if seg.net != gnd_net:
                    continue
                mid_x = (seg.x1 + seg.x2) / 2
                mid_y = (seg.y1 + seg.y2) / 2
                if _find(_snap(mid_x, mid_y)) == root and Point(mid_x, mid_y).within(pour_poly):
                    via_point = (mid_x, mid_y)
                    break

        if via_point is None:
            # Try any position in the component
            for snap_pos in component_layers.keys():
                if _find(snap_pos) == root:
                    x = snap_pos[0] * 0.01
                    y = snap_pos[1] * 0.01
                    if Point(x, y).within(pour_poly):
                        via_point = (x, y)
                        break

        if via_point:
            via_size = rules.via_diameter_for(gnd_net) if rules else 0.5
            via_drill = rules.via_drill_for(gnd_net) if rules else 0.25
            via_node = pcb.make_via(
                board, via_point[0], via_point[1], via_size, via_drill,
                "F.Cu", "B.Cu", gnd_net
            )
            vias.append(via_node)

    return vias


def _add_stitching_vias(board: Board, rules: DesignRules, gnd_net: str, layer: str,
                        pour_poly, pitch: float, clearance: float) -> list[SList]:
    """Add a grid of stitching vias over the pour polygon."""
    from . import pcb
    from shapely.geometry import Point

    vias = []

    # Bounding box of the pour polygon
    minx, miny, maxx, maxy = pour_poly.bounds

    # Grid of vias at pitch intervals
    x = minx + (pitch / 2)
    while x < maxx:
        y = miny + (pitch / 2)
        while y < maxy:
            if Point(x, y).within(pour_poly):
                # Simple clearance check: not too close to any other-net pad
                too_close = False
                for pad in board.pads:
                    if pad.net != gnd_net:
                        dist = ((pad.cx - x) ** 2 + (pad.cy - y) ** 2) ** 0.5
                        if dist < clearance + 0.5:  # 0.5 mm via radius
                            too_close = True
                            break
                if not too_close:
                    via_size = rules.via_diameter_for(gnd_net) if rules else 0.5
                    via_drill = rules.via_drill_for(gnd_net) if rules else 0.25
                    via_node = pcb.make_via(
                        board, x, y, via_size, via_drill,
                        "F.Cu", "B.Cu", gnd_net
                    )
                    vias.append(via_node)
            y += pitch
        x += pitch

    return vias
