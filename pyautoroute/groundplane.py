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
          stitch_pitch: float | None = None,
          routed_nodes: list | None = None) -> tuple[list[SList], list[str]]:
    """Build ground-plane zone node(s) and connecting vias.

    Args:
        board: the routed board.
        rules: design rules (clearance, via size).
        net: ground net name; if None, auto-detect ("GND", then glob "gnd"/"ground").
        layer: zone layer ("B.Cu", "F.Cu", or pass multiple times for each).
        margin: outline inset margin (mm); if None, uses board default clearance.
        stitch_pitch: optional pitch (mm) for stitching vias between layers; if None, no stitching.
        routed_nodes: freshly-generated routing SList nodes (segments + vias) not yet
            applied to *board*; included in the pour-layer obstacle check so that
            connectivity vias aren't placed where new routing already exists.

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
        board, rules, gnd_net, layer, inset_poly, clearance,
        routed_nodes=routed_nodes or []
    )
    nodes.extend(connectivity_vias)

    # ── Stitching vias (optional grid) ────────────────────────────────────────
    if stitch_pitch is not None and stitch_pitch > 0:
        stitch_vias = _add_stitching_vias(
            board, rules, gnd_net, layer, inset_poly, stitch_pitch, clearance
        )
        nodes.extend(stitch_vias)

    return (nodes, warnings)


def _node_head(node) -> str:
    """Return the head symbol of an SList node (e.g. 'segment', 'via')."""
    from . import sexpr as sx
    if node and isinstance(node[0], sx.Atom):
        return node[0].raw  # raw gives the unquoted symbol name
    return ""


def _child(node, key: str):
    """Return the first child SList of *node* whose head symbol matches *key*."""
    from . import sexpr as sx
    for child in node:
        if isinstance(child, sx.SList) and _node_head(child) == key:
            return child
    return None


def _atom_text(node, idx: int) -> str:
    """Return atom at *idx* in *node* as a plain string (quotes stripped)."""
    from . import sexpr as sx
    if node is None or idx >= len(node):
        return ""
    tok = node[idx]
    return tok.text if isinstance(tok, sx.Atom) else ""


def _float(node, idx: int) -> float:
    """Return atom at *idx* in *node* as a float."""
    from . import sexpr as sx
    if node is None or idx >= len(node):
        return 0.0
    tok = node[idx]
    if isinstance(tok, sx.Atom):
        try:
            return float(tok.text)
        except ValueError:
            return 0.0
    return 0.0


def _obstacles_from_nodes(nodes: list, layer: str, gnd_net: str):
    """Extract Obstacle objects for *layer* from freshly-built routing SList nodes.

    Parses ``(segment ...)`` and ``(via ...)`` nodes — the same format produced by
    ``pcb.make_segment`` / ``pcb.make_via`` — and returns `geometry.Obstacle` objects
    for copper on *layer* whose net differs from *gnd_net*.
    """
    from . import geometry, sexpr as sx
    from shapely.geometry import LineString, Point

    obs = []
    for node in nodes:
        if not isinstance(node, sx.SList) or not node:
            continue
        head = _node_head(node)

        if head == "segment":
            # (segment (start x1 y1) (end x2 y2) (width w) (layer "L") (net "N") ...)
            start_n = _child(node, "start")
            end_n   = _child(node, "end")
            width_n = _child(node, "width")
            layer_n = _child(node, "layer")
            net_n   = _child(node, "net")
            if not all([start_n, end_n, width_n, layer_n]):
                continue
            seg_layer = _atom_text(layer_n, 1)
            if seg_layer != layer:
                continue
            net = _atom_text(net_n, 1) if net_n else ""
            if net == gnd_net:
                continue
            x1, y1 = _float(start_n, 1), _float(start_n, 2)
            x2, y2 = _float(end_n, 1),   _float(end_n, 2)
            w       = _float(width_n, 1)
            line = LineString([(x1, y1), (x2, y2)]).buffer(w / 2)
            obs.append(geometry.Obstacle(line, net, seg_layer))

        elif head == "via":
            # (via (at x y) (size d) (layers "F.Cu" "B.Cu") (net "N") ...)
            at_n     = _child(node, "at")
            size_n   = _child(node, "size")
            layers_n = _child(node, "layers")
            net_n    = _child(node, "net")
            if not all([at_n, size_n, layers_n]):
                continue
            via_layers = [_atom_text(layers_n, i) for i in range(1, len(layers_n))]
            if layer not in via_layers:
                continue
            net = _atom_text(net_n, 1) if net_n else ""
            if net == gnd_net:
                continue
            cx, cy = _float(at_n, 1), _float(at_n, 2)
            d = _float(size_n, 1)
            obs.append(geometry.Obstacle(Point(cx, cy).buffer(d / 2), net, layer))

    return obs


def _add_connectivity_vias(board: Board, rules: DesignRules, gnd_net: str, layer: str,
                           pour_poly, clearance: float,
                           routed_nodes: list | None = None) -> list[SList]:
    """Add vias to connect GND islands that don't reach the pour layer.

    For each connected GND component that doesn't have copper on the pour layer:
    place a via at a point inside the pour polygon to tie the component in.
    """
    from . import pcb, geometry
    from shapely.geometry import Point
    from shapely.strtree import STRtree

    vias = []

    # Build an obstacle index for the pour layer so we can reject via positions
    # that would create a short with other-net copper already routed there.
    # Include both copper already in the board AND freshly-routed nodes that
    # haven't been applied to the board object yet.
    via_size = rules.via_diameter_for(gnd_net) if rules else 0.6
    via_drill = rules.via_drill_for(gnd_net) if rules else 0.3
    via_radius = via_size / 2.0

    pour_layer_obstacles = [
        o for o in geometry.board_obstacles(board)
        if o.layer == layer and o.net != gnd_net and o.net
    ]
    if routed_nodes:
        pour_layer_obstacles.extend(
            _obstacles_from_nodes(routed_nodes, layer, gnd_net)
        )
    if pour_layer_obstacles:
        _obs_tree = STRtree([o.geom for o in pour_layer_obstacles])
    else:
        _obs_tree = None

    def _via_clear(x: float, y: float) -> bool:
        """Return True if a via centred at (x, y) has no clearance conflict on the pour layer."""
        if _obs_tree is None:
            return True
        ring = Point(x, y).buffer(via_radius + clearance)
        for idx in _obs_tree.query(ring):
            obs = pour_layer_obstacles[idx]
            if Point(x, y).buffer(via_radius).distance(obs.geom) < clearance - 1e-6:
                return False
        return True

    # Union-find over GND copper (pads + segments)
    parent: dict = {}

    def _find(k):
        if k not in parent:
            parent[k] = k
        if parent[k] != k:
            parent[k] = _find(parent[k])
        return parent[k]

    def _union(a, b):
        parent[_find(a)] = _find(b)

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
        if "Via" in pad.pad_type or all(lyr in pad.copper_layers for lyr in ["F.Cu", "B.Cu"]):
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

    # Aggregate layers per component root, then find components lacking the pour layer.
    # Checking per-position would incorrectly flag a component as needing a via whenever
    # any segment endpoint in it lacks the pour layer, even if a THT pad in the same
    # component already provides full-layer coverage.
    root_layers: dict = {}
    for snap_pos, layers in component_layers.items():
        root = _find(snap_pos)
        root_layers.setdefault(root, set()).update(layers)

    roots_needing_via: set = {
        root for root, layers in root_layers.items()
        if layer not in layers
    }

    # For each component needing a via, find a conflict-free point inside the pour polygon.
    # Try candidate positions in order: GND pad centres, GND segment midpoints, any snap pos.
    # Skip any position where the via annular ring on the pour layer would overlap other-net
    # copper (which the router may have placed there).
    def _candidate_positions(root):
        for pad in board.pads:
            if pad.net != gnd_net:
                continue
            if _find(_snap(pad.cx, pad.cy)) == root and Point(pad.cx, pad.cy).within(pour_poly):
                yield (pad.cx, pad.cy)
        for seg in board.segments:
            if seg.net != gnd_net:
                continue
            mid_x = (seg.x1 + seg.x2) / 2
            mid_y = (seg.y1 + seg.y2) / 2
            if _find(_snap(mid_x, mid_y)) == root and Point(mid_x, mid_y).within(pour_poly):
                yield (mid_x, mid_y)
        for snap_pos in component_layers.keys():
            if _find(snap_pos) == root:
                x = snap_pos[0] * SNAP_MM
                y = snap_pos[1] * SNAP_MM
                if Point(x, y).within(pour_poly):
                    yield (x, y)

    for root in roots_needing_via:
        via_point = None
        for (x, y) in _candidate_positions(root):
            if _via_clear(x, y):
                via_point = (x, y)
                break

        if via_point:
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
