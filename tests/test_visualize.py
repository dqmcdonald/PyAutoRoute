"""Tests for pyautoroute.visualize (board rendering)."""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
from matplotlib.figure import Figure   # noqa: E402

from pyautoroute import rules, sexpr, visualize   # noqa: E402
from pyautoroute.grid import Grid                  # noqa: E402
from pyautoroute.pcb import Board, OutlineShape, Pad, Segment   # noqa: E402
from pyautoroute.router import RoutingState, route_connection   # noqa: E402


def _ax():
    return Figure().subplots()


def _pad(net, cx, cy):
    return Pad(net=net, pad_type="smd", shape="rect", cx=cx, cy=cy, w=1.0, h=1.0,
              angle=0.0, copper_layers=["F.Cu"])


def _board(pads, segments=()):
    outline = [OutlineShape("poly", {"pts": [(0, 0), (20, 0), (20, 20), (0, 20)]})]
    return Board(tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"], pads=pads,
                 free_vias=[], segments=list(segments), zones=[], outline=outline)


def test_draw_board_draws_pads_and_segments():
    board = _board([_pad("A", 5, 5), _pad("A", 15, 5)],
                   segments=[Segment(5, 5, 15, 5, 0.2, "F.Cu", "A")])
    ax = _ax()
    visualize.draw_board(ax, board, title="t")
    assert len(ax.patches) == 2                 # one fill per pad
    assert len(ax.lines) >= 1                   # board outline
    assert len(ax.collections) >= 1             # segment LineCollection
    assert ax.get_title() == "t"
    assert ax.yaxis_inverted()                  # KiCad Y-down


def test_draw_board_clears_between_calls():
    board = _board([_pad("A", 5, 5)])
    ax = _ax()
    visualize.draw_board(ax, board)
    visualize.draw_board(ax, board)             # redraw must not accumulate
    assert len(ax.patches) == 1


def test_draw_board_renders_in_progress_results():
    a, b = _pad("A", 4, 10), _pad("A", 16, 10)
    board = _board([a, b])                       # no committed segments yet
    grid = Grid(board, rules.default_rules(), pitch=0.5)
    state = RoutingState(grid)
    res = route_connection(state, "A", grid.pad_access_nodes(a),
                           grid.pad_access_nodes(b))
    ax = _ax()
    visualize.draw_board(ax, board, results=[res], grid=grid)
    assert len(ax.lines) >= 1                    # board outline
    assert len(ax.collections) >= 1             # routed path LineCollection


def test_render_writes_png(tmp_path):
    board = _board([_pad("A", 5, 5), _pad("A", 15, 15)])
    out = tmp_path / "b.png"
    visualize.render(board, str(out))
    assert out.exists() and out.stat().st_size > 0
