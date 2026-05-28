"""Matplotlib rendering of a (routed) board.

`draw_board` paints a board onto a caller-supplied Axes so the same routine backs
both the ``--debug-plot`` PNG (`render`) and a live, embedded GUI canvas. With
``results`` (+ ``grid``) it draws an in-progress routing straight from the router's
node paths, before any segments have been written to the board.
"""

from __future__ import annotations

import math

from matplotlib.collections import LineCollection

from . import geometry
from .pcb import Board

_LAYER_COLOR = {"F.Cu": "#cc3333", "B.Cu": "#3366cc"}

# Footprint courtyard / fab / silkscreen layers to render as outlines.
_FP_OUTLINE_LAYERS = {"F.CrtYd", "B.CrtYd", "F.Fab", "B.Fab", "F.SilkS", "B.SilkS"}
_FP_OUTLINE_COLOR = "#888888"


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


def draw_board(ax, board: Board, *, results=None, grid=None,
               title: str | None = None) -> None:
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

    for pad in board.pads:
        poly = geometry.pad_polygon(pad)
        for layer in pad.copper_layers:
            ax.fill(*poly.exterior.xy, color=_LAYER_COLOR.get(layer, "#999"), alpha=0.45)

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

    ax.set_aspect("equal")
    ax.invert_yaxis()      # KiCad Y points down
    if title is not None:
        ax.set_title(title)


def render(board: Board, out_path: str, title: str = "PyAutoRoute") -> None:
    """Write a matplotlib PNG of the board: outline, pads, tracks, and vias.

    Args:
        board: the (routed) board to render.
        out_path: destination path for the PNG.
        title: the plot title.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 10))
    draw_board(ax, board, title=title)
    fig.savefig(out_path, dpi=85, bbox_inches="tight")
    plt.close(fig)
