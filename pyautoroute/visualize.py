"""Matplotlib rendering of a (routed) board.

`draw_board` paints a board onto a caller-supplied Axes; it backs the live,
embedded GUI canvas (`pyautoroute.gui.canvas`). With ``results`` (+ ``grid``) it
draws an in-progress routing straight from the router's node paths, before any
segments have been written to the board, and with ``rats_nest`` it overlays the
unrouted airwires (`pyautoroute.gui.app`'s "Rats-nest" toggle).
"""

from __future__ import annotations

import math

from matplotlib.collections import LineCollection, PolyCollection

from . import geometry
from .pcb import Board

_LAYER_COLOR = {"F.Cu": "#cc3333", "B.Cu": "#3366cc"}

# Footprint courtyard / fab / silkscreen layers to render as outlines.
_FP_OUTLINE_LAYERS = {"F.CrtYd", "B.CrtYd", "F.Fab", "B.Fab", "F.SilkS", "B.SilkS"}
_FP_OUTLINE_COLOR = "#888888"

# Autoroute property marker colours.
_EDGE_COLOR = "#ff8800"     # orange — edge-affinity arrows / star
_OVERLAP_COLOR = "#aa00cc"  # purple — overlap-ok ring
_LOCK_COLOR = "#cc0000"     # red — locked footprint
_GROUP_COLOR = "#009999"    # teal — KiCad native group members

# Direction vectors for edge affinity (in board coordinates, Y-down).
# "top" = smaller Y → negative dy; "bottom" = larger Y → positive dy.
_EDGE_DIR = {"left": (-1.0, 0.0), "right": (1.0, 0.0),
             "top": (0.0, -1.0), "bottom": (0.0, 1.0)}
_ARROW_MM = 4.0   # arrow length in mm

# Silkscreen text layers (both old and new KiCad naming).
_SILK_LAYERS = {"F.SilkS", "B.SilkS", "F.Silkscreen", "B.Silkscreen"}
_SILK_TEXT_COLOR = {"front": "#aa8800", "back": "#007799"}

# Rats-nest airwires — thin dashed, neutral, subordinate to copper.
_RATSNEST_COLOR = "#9a9a9a"


def _draw_results(ax, grid, results) -> None:
    """Draw routed node paths (tracks + vias) from router results.

    Args:
        ax: the matplotlib Axes to draw on.
        grid: the routing grid (node -> coordinate conversion, layer names).
        results: per-connection `pyautoroute.router.RouteResult` or `None`.
    """
    segs_by_layer: dict[str, list] = {}
    via_xs: list[float] = []
    via_ys: list[float] = []
    for res in results:
        if res is None:
            continue
        for (l0, c0, r0), (l1, c1, r1) in zip(res.path, res.path[1:]):
            if l0 != l1:
                x, y = grid.node_xy(c0, r0)
                via_xs.append(x)
                via_ys.append(y)
            else:
                x0, y0 = grid.node_xy(c0, r0)
                x1, y1 = grid.node_xy(c1, r1)
                layer = grid.layers[l0]
                segs_by_layer.setdefault(layer, []).append([(x0, y0), (x1, y1)])
    for layer, segs in segs_by_layer.items():
        color = _LAYER_COLOR.get(layer, "#999")
        lc = LineCollection(segs, color=color, linewidth=1.4, alpha=0.85)
        ax.add_collection(lc)
    if via_xs:
        ax.plot(via_xs, via_ys, "o", mfc="none", mec="#11aa11", ms=5)


def _fp_outline_segments(board: Board) -> list:
    """Extract footprint courtyard/fab/silkscreen line segments for rendering.

    Returns a list of [(x0,y0),(x1,y1)] pairs in board coordinates.
    """
    from .sexpr import SList
    segs = []
    for fp in board.footprints:
        fx, fy, fa = fp.x, fp.y, fp.angle
        cos_a = math.cos(math.radians(fa))
        sin_a = math.sin(math.radians(fa))

        def to_board(lx, ly):
            # KiCad rotate: same convention as pcb.rotate()
            rx = lx * cos_a + ly * sin_a
            ry = -lx * sin_a + ly * cos_a
            return fx + rx, fy + ry

        for item in fp.fp_node:
            if not isinstance(item, SList) or not item:
                continue
            tag = item[0].raw if hasattr(item[0], "raw") else None
            if tag not in ("fp_line", "fp_rect"):
                continue
            # Check layer
            layer = None
            start = end = None
            for sub in item[1:]:
                if not isinstance(sub, SList) or not sub:
                    continue
                sub_tag = sub[0].raw if hasattr(sub[0], "raw") else None
                if sub_tag == "layer":
                    # (layer "F.SilkS") — value may be Atom with text
                    vals = [s for s in sub[1:] if hasattr(s, "text")]
                    if vals:
                        layer = vals[0].text
                elif sub_tag == "start":
                    vals = [s for s in sub[1:] if hasattr(s, "as_float")]
                    if len(vals) >= 2:
                        start = (vals[0].as_float(), vals[1].as_float())
                elif sub_tag == "end":
                    vals = [s for s in sub[1:] if hasattr(s, "as_float")]
                    if len(vals) >= 2:
                        end = (vals[0].as_float(), vals[1].as_float())
            if layer not in _FP_OUTLINE_LAYERS or start is None or end is None:
                continue
            if tag == "fp_line":
                segs.append([to_board(*start), to_board(*end)])
            else:  # fp_rect — draw 4 edges
                x0, y0 = start
                x1, y1 = end
                corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
                for a, b in zip(corners, corners[1:]):
                    segs.append([to_board(*a), to_board(*b)])
    return segs


def _is_hidden(node) -> bool:
    """Return True if the node has a ``(hide yes)`` child."""
    from .pcb import child, atoms_after_head
    h = child(node, "hide")
    if h is None:
        return False
    vals = atoms_after_head(h)
    return bool(vals) and vals[0].text == "yes"


def _silk_text_items(board: Board):
    """Yield ``(x, y, text, angle_deg, is_back)`` for visible silkscreen text.

    Covers:
    - Top-level ``(gr_text ...)`` on a silkscreen layer.
    - ``(property "Reference" ...)`` and ``(property "Value" ...)`` within
      footprints, on a silkscreen layer and not hidden.
    - ``(fp_text ...)`` within footprints on a silkscreen layer (legacy format,
      resolves ``${REFERENCE}`` to the footprint reference).
    """
    from .pcb import children, child, strings, floats, atoms_after_head
    from .sexpr import SList, head_symbol

    def _layer(node) -> str:
        ls = strings(child(node, "layer"))
        return ls[0] if ls else ""

    # 1. Top-level gr_text
    for node in board.tree:
        if not isinstance(node, SList) or not node:
            continue
        if head_symbol(node) != "gr_text":
            continue
        if _is_hidden(node):
            continue
        lay = _layer(node)
        if lay not in _SILK_LAYERS:
            continue
        content_atoms = atoms_after_head(node)
        if not content_atoms:
            continue
        content = content_atoms[0].text
        if not content:
            continue
        at_vals = floats(child(node, "at"))
        if len(at_vals) < 2:
            continue
        x, y = at_vals[0], at_vals[1]
        angle = at_vals[2] if len(at_vals) >= 3 else 0.0
        yield x, y, content, angle, lay.startswith("B.")

    # 2. Footprint-scoped text
    for fp in board.footprints:
        fx, fy, fa = fp.x, fp.y, fp.angle
        cos_a = math.cos(math.radians(fa))
        sin_a = math.sin(math.radians(fa))

        def to_board(lx: float, ly: float) -> tuple[float, float]:
            return (fx + lx * cos_a + ly * sin_a,
                    fy - lx * sin_a + ly * cos_a)

        fp_node = fp.fp_node

        # property "Reference" / "Value" nodes
        for prop in children(fp_node, "property"):
            if _is_hidden(prop):
                continue
            lay = _layer(prop)
            if lay not in _SILK_LAYERS:
                continue
            prop_atoms = atoms_after_head(prop)
            if len(prop_atoms) < 2:
                continue
            if prop_atoms[0].text not in ("Reference", "Value"):
                continue
            content = prop_atoms[1].text
            if not content:
                continue
            at_vals = floats(child(prop, "at"))
            if len(at_vals) < 2:
                continue
            bx, by = to_board(at_vals[0], at_vals[1])
            angle = at_vals[2] if len(at_vals) >= 3 else 0.0
            yield bx, by, content, angle, lay.startswith("B.")

        # fp_text nodes (legacy format)
        for txt in children(fp_node, "fp_text"):
            if _is_hidden(txt):
                continue
            lay = _layer(txt)
            if lay not in _SILK_LAYERS:
                continue
            txt_atoms = atoms_after_head(txt)
            if len(txt_atoms) < 2:
                continue
            content = txt_atoms[1].text.replace("${REFERENCE}", fp.ref)
            if not content:
                continue
            at_vals = floats(child(txt, "at"))
            if len(at_vals) < 2:
                continue
            bx, by = to_board(at_vals[0], at_vals[1])
            angle = at_vals[2] if len(at_vals) >= 3 else 0.0
            yield bx, by, content, angle, lay.startswith("B.")


def _draw_autoroute_markers(ax, board: Board) -> None:
    """Draw markers for footprints that carry Autoroute KiCad properties.

    - Directional edge affinity → orange arrow pointing toward the target edge.
    - Edge-any affinity → orange star at the footprint centre.
    - Overlap-ok → open purple circle at the footprint centre.
    - Locked → red square outline at the footprint centre.
    """
    any_xs: list[float] = []
    any_ys: list[float] = []
    ol_xs: list[float] = []
    ol_ys: list[float] = []
    lock_xs: list[float] = []
    lock_ys: list[float] = []
    has_arrows = False
    # Group members: group_id -> list of (x, y) for drawing connecting lines.
    group_pts: dict[str, list[tuple[float, float]]] = {}

    for fp in board.footprints:
        if fp.edge_affinity:
            if fp.edge_affinity == "any":
                any_xs.append(fp.x)
                any_ys.append(fp.y)
            else:
                has_arrows = True
                dx, dy = _EDGE_DIR.get(fp.edge_affinity, (0.0, 0.0))
                ax.annotate(
                    "", xy=(fp.x + dx * _ARROW_MM, fp.y + dy * _ARROW_MM),
                    xytext=(fp.x, fp.y),
                    arrowprops=dict(arrowstyle="-|>", color=_EDGE_COLOR,
                                    lw=1.5, mutation_scale=12, alpha=0.6),
                    zorder=6,
                )
        if fp.overlap_ok:
            ol_xs.append(fp.x)
            ol_ys.append(fp.y)
        if fp.locked:
            lock_xs.append(fp.x)
            lock_ys.append(fp.y)
        if fp.group_id:
            group_pts.setdefault(fp.group_id, []).append((fp.x, fp.y))

    if any_xs:
        ax.plot(any_xs, any_ys, "*", color=_EDGE_COLOR,
                ms=10, zorder=6, alpha=0.55, linestyle="none")
    if ol_xs:
        ax.plot(ol_xs, ol_ys, "o", mfc="none", mec=_OVERLAP_COLOR,
                ms=12, mew=1.5, zorder=6, alpha=0.55, linestyle="none")
    if lock_xs:
        ax.plot(lock_xs, lock_ys, "s", mfc="none", mec=_LOCK_COLOR,
                ms=10, mew=1.5, zorder=6, alpha=0.55, linestyle="none")
    if group_pts:
        # Draw a diamond marker at each grouped footprint and a thin dashed line
        # connecting consecutive members so the grouping is easy to spot.
        gxs, gys = [], []
        segs = []
        for pts in group_pts.values():
            for x, y in pts:
                gxs.append(x); gys.append(y)
            segs.extend(zip(pts, pts[1:]))  # sequential segments fp0→fp1→fp2…
        ax.plot(gxs, gys, "D", mfc="none", mec=_GROUP_COLOR,
                ms=9, mew=1.5, zorder=6, alpha=0.55, linestyle="none")
        if segs:
            lc = LineCollection(
                [[(ax_, ay_), (bx_, by_)] for (ax_, ay_), (bx_, by_) in segs],
                color=_GROUP_COLOR, linewidth=1.0, linestyle="dashed",
                alpha=0.5, zorder=5,
            )
            ax.add_collection(lc)

    # Draw legend if any markers are present
    if any_xs or has_arrows or ol_xs or lock_xs or group_pts:
        import matplotlib.lines as mlines
        handles = []
        labels = []
        if has_arrows or any_xs:
            handles.append(mlines.Line2D([], [], color=_EDGE_COLOR, marker="*",
                                         linestyle="none", markersize=10, label="Edge"))
            labels.append("Edge")
        if ol_xs:
            handles.append(mlines.Line2D([], [], markerfacecolor="none",
                                         markeredgecolor=_OVERLAP_COLOR, marker="o",
                                         linestyle="none", markersize=10, label="Overlap"))
            labels.append("Overlap")
        if lock_xs:
            handles.append(mlines.Line2D([], [], markerfacecolor="none",
                                         markeredgecolor=_LOCK_COLOR, marker="s",
                                         linestyle="none", markersize=9, label="Locked"))
            labels.append("Locked")
        if group_pts:
            handles.append(mlines.Line2D([], [], markerfacecolor="none",
                                         markeredgecolor=_GROUP_COLOR, marker="D",
                                         linestyle="none", markersize=9, label="Group"))
            labels.append("Group")
        if handles:
            ax.legend(handles=handles, loc="upper right", fontsize=7,
                      framealpha=0.7, title="Constraints", title_fontsize=7)


def draw_board(ax, board: Board, *, results=None, grid=None,
               rats_nest=None, title: str | None = None) -> None:
    """Paint a board (outline, pads, tracks, vias) onto a matplotlib Axes.

    Layers are colour-coded (F.Cu red, B.Cu blue) and the Y axis is inverted to
    match KiCad's Y-down board coordinates. The Axes is cleared first, so this can
    be called repeatedly to refresh a live view.

    Args:
        ax: the matplotlib Axes to draw on.
        board: the board to render.
        results: optional in-progress router results to draw instead of the
            board's committed segments (requires `grid`); used for live rendering
            during routing, before the tracks are written to the board.
        grid: the routing grid backing `results` (node -> coordinate conversion).
        rats_nest: optional airwire segments ``[(x1, y1, x2, y2), …]`` to overlay
            as thin dashed lines (the unrouted connections); ``None``/empty draws
            nothing. Drawn beneath the copper so tracks stay legible.
        title: optional Axes title.
    """
    ax.clear()
    outline = geometry.outline_to_polygon(board.outline)
    ax.plot(*outline.exterior.xy, "k-", lw=1.5)

    # Footprint outlines (courtyard / fab / silkscreen)
    fp_segs = _fp_outline_segments(board)
    if fp_segs:
        lc = LineCollection(fp_segs, color=_FP_OUTLINE_COLOR, linewidth=0.6,
                            alpha=0.7)
        ax.add_collection(lc)

    pads_by_layer: dict[str, list] = {}
    for pad in board.pads:
        poly = geometry.pad_polygon(pad)
        coords = list(poly.exterior.coords)
        for layer in pad.copper_layers:
            pads_by_layer.setdefault(layer, []).append(coords)
    for layer, polys in pads_by_layer.items():
        pc = PolyCollection(polys, facecolor=_LAYER_COLOR.get(layer, "#999"),
                            alpha=0.45, edgecolor="none")
        ax.add_collection(pc)

    if rats_nest:
        air = [[(x1, y1), (x2, y2)] for (x1, y1, x2, y2) in rats_nest]
        ax.add_collection(LineCollection(
            air, colors=_RATSNEST_COLOR, linewidths=0.6,
            linestyles="dashed", alpha=0.6, zorder=0.5))

    if results is not None and grid is not None:
        _draw_results(ax, grid, results)
    else:
        # Use LineCollection for performance (avoids one matplotlib call per segment)
        segs_by_layer: dict[str, list] = {}
        for seg in board.segments:
            segs_by_layer.setdefault(seg.layer, []).append(
                [(seg.x1, seg.y1), (seg.x2, seg.y2)])
        for layer, segs in segs_by_layer.items():
            color = _LAYER_COLOR.get(layer, "#999")
            lc = LineCollection(segs, color=color, linewidth=1.4, alpha=0.85)
            ax.add_collection(lc)

    via_xs = [v.cx for v in board.free_vias]
    via_ys = [v.cy for v in board.free_vias]
    if via_xs:
        ax.plot(via_xs, via_ys, "o", mfc="none", mec="#11aa11", ms=5)

    # Silkscreen text (gr_text + footprint property/fp_text on SilkS layers)
    for tx, ty, content, angle, is_back in _silk_text_items(board):
        color = _SILK_TEXT_COLOR["back" if is_back else "front"]
        ax.text(tx, ty, content, fontsize=6, color=color,
                ha="left", va="baseline", rotation=angle,
                rotation_mode="anchor", clip_on=True)

    if board.footprints:
        _draw_autoroute_markers(ax, board)

    ax.set_aspect("equal")
    ax.invert_yaxis()      # KiCad Y points down
    if title is not None:
        ax.set_title(title)
