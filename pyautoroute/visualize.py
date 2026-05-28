"""Matplotlib rendering of a (routed) board.

`draw_board` paints a board onto a caller-supplied Axes so the same routine backs
both the ``--debug-plot`` PNG (`render`) and a live, embedded GUI canvas. With
``results`` (+ ``grid``) it draws an in-progress routing straight from the router's
node paths, before any segments have been written to the board.
"""

from __future__ import annotations

from . import geometry
from .pcb import Board

_LAYER_COLOR = {"F.Cu": "#cc3333", "B.Cu": "#3366cc"}


def _draw_results(ax, grid, results) -> None:
    """Draw routed node paths (tracks + vias) from router results.

    Args:
        ax: the matplotlib Axes to draw on.
        grid: the routing grid (node -> coordinate conversion, layer names).
        results: per-connection `pyautoroute.router.RouteResult` or `None`.
    """
    for res in results:
        if res is None:
            continue
        for (l0, c0, r0), (l1, c1, r1) in zip(res.path, res.path[1:]):
            if l0 != l1:
                x, y = grid.node_xy(c0, r0)
                ax.plot(x, y, "o", mfc="none", mec="#11aa11", ms=5)
            else:
                x0, y0 = grid.node_xy(c0, r0)
                x1, y1 = grid.node_xy(c1, r1)
                ax.plot([x0, x1], [y0, y1],
                        color=_LAYER_COLOR.get(grid.layers[l0], "#999"),
                        lw=1.4, alpha=0.85)


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
    for pad in board.pads:
        poly = geometry.pad_polygon(pad)
        for layer in pad.copper_layers:
            ax.fill(*poly.exterior.xy, color=_LAYER_COLOR.get(layer, "#999"), alpha=0.45)
    if results is not None and grid is not None:
        _draw_results(ax, grid, results)
    else:
        for seg in board.segments:
            ax.plot([seg.x1, seg.x2], [seg.y1, seg.y2],
                    color=_LAYER_COLOR.get(seg.layer, "#999"), lw=1.4, alpha=0.85)
    for via in board.free_vias:
        ax.plot(via.cx, via.cy, "o", mfc="none", mec="#11aa11", ms=5)
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
