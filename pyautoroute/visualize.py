"""Optional matplotlib debug render of a (routed) board."""

from __future__ import annotations

from . import geometry
from .pcb import Board

_LAYER_COLOR = {"F.Cu": "#cc3333", "B.Cu": "#3366cc"}


def render(board: Board, out_path: str, title: str = "PyAutoRoute") -> None:
    """Write a matplotlib PNG of the board: outline, pads, tracks, and vias.

    Layers are colour-coded (F.Cu red, B.Cu blue) and the Y axis is inverted to
    match KiCad's Y-down board coordinates.

    Args:
        board: the (routed) board to render.
        out_path: destination path for the PNG.
        title: the plot title.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    outline = geometry.outline_to_polygon(board.outline)
    fig, ax = plt.subplots(figsize=(9, 10))
    ax.plot(*outline.exterior.xy, "k-", lw=1.5)
    for pad in board.pads:
        poly = geometry.pad_polygon(pad)
        for layer in pad.copper_layers:
            ax.fill(*poly.exterior.xy, color=_LAYER_COLOR.get(layer, "#999"), alpha=0.45)
    for seg in board.segments:
        ax.plot([seg.x1, seg.x2], [seg.y1, seg.y2],
                color=_LAYER_COLOR.get(seg.layer, "#999"), lw=1.4, alpha=0.85)
    for via in board.free_vias:
        ax.plot(via.cx, via.cy, "o", mfc="none", mec="#11aa11", ms=5)
    ax.set_aspect("equal")
    ax.invert_yaxis()      # KiCad Y points down
    ax.set_title(title)
    fig.savefig(out_path, dpi=85, bbox_inches="tight")
    plt.close(fig)
