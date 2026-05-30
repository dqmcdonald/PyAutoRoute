"""Tests for congestion-aware re-placement feedback (Phase 4):

* `router.CongestionField` / `congestion_frame` / `congestion_heatmap` — the
  coarse "where did routing struggle?" heatmap derived from routed results.
* the placement congestion term (`PlaceParams.congestion_field` / weight), which
  pushes footprints out of the hot cells.
* the CLI ``--place-feedback`` end-to-end loop.
"""

from __future__ import annotations

import pathlib

import numpy as np
import pytest

from pyautoroute import autoroute, netlist, pcb, pipeline, placement, router, sexpr
from pyautoroute.pcb import Board, Footprint, OutlineShape, Pad
from pyautoroute.placement import PlaceParams
from pyautoroute.router import CongestionField, RouteParams
from pyautoroute.rules import load_rules

_BOARD = (pathlib.Path(__file__).resolve().parents[1]
          / "TestProjects" / "Test3" / "Test3.kicad_pcb")
_PRO = _BOARD.with_suffix(".kicad_pro")


# --- synthetic board helpers (mirrors test_placement) ------------------------

def _pad(net):
    return Pad(net=net, pad_type="smd", shape="rect", cx=0.0, cy=0.0, w=2.0,
               h=2.0, angle=0.0, copper_layers=["F.Cu", "B.Cu"])


def _fp(ref, x, y, nets):
    pads = [_pad(n) for n in nets]
    offs = [(0.0, 0.0, 0.0) for _ in nets]
    fp = Footprint(ref=ref, x=x, y=y, angle=0.0, locked=False, overlap_ok=False,
                   pads=pads, local_offsets=offs, at_node=sexpr.SList(),
                   fp_node=sexpr.SList(), x0=x, y0=y, angle0=0.0)
    fp.sync_pads()
    return fp


def _board(footprints, size=80):
    pads = [p for fp in footprints for p in fp.pads]
    outline = [OutlineShape("poly", {"pts": [(0, 0), (size, 0),
                                             (size, size), (0, size)]})]
    return Board(tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"], pads=pads,
                 free_vias=[], segments=[], zones=[], outline=outline,
                 footprints=footprints)


def _hot_center_field(size=80, n=10):
    """A `CongestionField` over a ``size`` board, hot in the central cells."""
    vals = np.zeros((n, n))
    vals[n // 2 - 2:n // 2 + 2, n // 2 - 2:n // 2 + 2] = 1.0
    return CongestionField(0.0, 0.0, size / n, n, n, vals)


# --- CongestionField -----------------------------------------------------------

def test_value_at_inside_outside_and_clamp():
    f = _hot_center_field()
    assert f.value_at(40.0, 40.0) == 1.0          # hot centre
    assert f.value_at(4.0, 4.0) == 0.0            # cool corner
    assert f.value_at(-50.0, -50.0) == 0.0        # outside the frame -> 0
    assert f.value_at(1e6, 1e6) == 0.0


def test_blended_renormalises_to_unit_peak():
    a = _hot_center_field()
    b = CongestionField(a.minx, a.miny, a.cell, a.nx, a.ny, np.zeros((a.ny, a.nx)))
    b.values[0, 0] = 1.0                          # a different hot cell
    out = a.blended(b, 0.5)
    assert out.values.shape == a.values.shape
    assert out.values.max() == pytest.approx(1.0)  # renormalised
    # both sources contribute (history-weighted)
    assert out.value_at(40.0, 40.0) > 0.0 and out.values[0, 0] > 0.0


def test_congestion_frame_covers_pads_with_margin():
    fps = [_fp("U1", 20.0, 20.0, [""]), _fp("U2", 60.0, 60.0, [""])]
    board = _board(fps)
    frame = router.congestion_frame(board, pitch=2.0, cell_mult=4, margin=5.0)
    assert frame.cell == pytest.approx(8.0)
    # every pad lands inside the frame
    for p in board.pads:
        assert frame.value_at(p.cx, p.cy) == 0.0   # zero-valued, but in-bounds
        c = int((p.cx - frame.minx) / frame.cell)
        r = int((p.cy - frame.miny) / frame.cell)
        assert 0 <= c < frame.nx and 0 <= r < frame.ny


# --- congestion_heatmap --------------------------------------------------------

class _DummyGrid:
    """Stand-in grid: heatmap only calls node_xy for *routed* paths (none here)."""
    def node_xy(self, col, row):                  # pragma: no cover - unused
        return float(col), float(row)


def test_heatmap_marks_unrouted_region_and_normalises():
    # two footprints, one net between them, but routing produces nothing (all
    # unrouted) -> the segment between the pads must be the hot region.
    fps = [_fp("U1", 20.0, 40.0, ["N1"]), _fp("U2", 60.0, 40.0, ["N1"])]
    board = _board(fps)
    conns = netlist.build_connections(board)
    frame = router.congestion_frame(board, pitch=2.0)
    results = [None] * len(conns)                 # nothing routed
    field = router.congestion_heatmap(conns, results, _DummyGrid(), frame)
    assert field.values.max() == pytest.approx(1.0)   # normalised
    # the midpoint of the unrouted net is hot; a far corner is cold
    assert field.value_at(40.0, 40.0) > field.value_at(2.0, 78.0)


# --- placement term ------------------------------------------------------------

def test_congestion_term_pushes_footprints_out_of_hot_cells():
    fps = [_fp(f"U{i}", 38.0 + 2 * (i % 2), 38.0 + 2 * (i // 2), [""])
           for i in range(4)]                     # clustered in the hot centre
    board = _board(fps)
    field = _hot_center_field()
    before = sum(field.value_at(fp.x, fp.y) for fp in board.footprints)
    assert before > 0.0
    pp = PlaceParams(iters=2000, seed=1, congestion_field=field,
                     congestion_weight=50.0, compact_weight=0.0)
    placement.place(board, pp)
    after = sum(field.value_at(fp.x, fp.y) for fp in board.footprints)
    assert after < before                          # spread out of the hot zone


def test_congestion_term_is_deterministic_for_a_seed():
    field = _hot_center_field()

    def _run():
        fps = [_fp(f"U{i}", 38.0 + 2 * (i % 2), 38.0 + 2 * (i // 2), [""])
               for i in range(4)]
        board = _board(fps)
        pp = PlaceParams(iters=1000, seed=3, congestion_field=field,
                         congestion_weight=50.0, compact_weight=0.0)
        placement.place(board, pp)
        return [(round(fp.x, 6), round(fp.y, 6)) for fp in board.footprints]

    assert _run() == _run()


def test_no_field_leaves_placement_unchanged():
    # weight 0 / field None must be a no-op vs the same seed without the params.
    def _poses(**extra):
        fps = [_fp("U1", 30.0, 30.0, ["A"]), _fp("U2", 50.0, 30.0, ["A"])]
        board = _board(fps)
        placement.place(board, PlaceParams(iters=500, seed=2, **extra))
        return [(round(fp.x, 6), round(fp.y, 6)) for fp in board.footprints]

    assert _poses() == _poses(congestion_field=None, congestion_weight=0.0)


# --- end-to-end feedback (needs the Test3 board) ------------------------------

_needs_board = pytest.mark.skipif(not _BOARD.exists(),
                                  reason="Test3 board not present")


@_needs_board
def test_heatmap_from_a_real_routed_cycle_is_normalised():
    rules = load_rules(_PRO)
    pitch = autoroute.default_pitch(rules)
    pp = PlaceParams(iters=150)
    rp = RouteParams(via_cost=2.0)
    kw = dict(annealing=False, iters=None, time_budget=None,
              unrouted_weight=100.0, anneal_temps=(2.0, 0.1), via_weight=2.0)
    cr = pipeline.run_cycle(_BOARD, rules, pitch, pp, rp,
                            route_kw=kw, place_margin=2.0, seed=1)
    board = pcb.load_board(_BOARD)
    frame = router.congestion_frame(board, pitch)
    field = router.congestion_heatmap(cr.conns, cr.results, cr.grid, frame)
    assert field.values.shape == (frame.ny, frame.nx)
    assert 0.0 <= field.values.min() and field.values.max() == pytest.approx(1.0)


@_needs_board
def test_cli_place_feedback_runs_and_is_clean(tmp_path):
    out = tmp_path / "fb.kicad_pcb"
    rc = autoroute.main([str(_BOARD), "-o", str(out), "--place",
                         "--cycles", "3", "--place-feedback",
                         "--place-iters", "150", "--seed", "1", "--quiet"])
    assert rc == 0                                 # clean (no DRC violation)
    assert out.exists()


@_needs_board
def test_cli_place_feedback_ignored_without_cycles(tmp_path, capsys):
    # --place-feedback with cycles<=1 should warn and disable, not crash.
    out = tmp_path / "nofb.kicad_pcb"
    # rc may be 0 or 2 (DRC) depending on the placement; we only assert the guard
    # fired and the run did not crash.
    autoroute.main([str(_BOARD), "-o", str(out), "--place",
                    "--place-feedback", "--place-iters", "100", "--seed", "1"])
    assert "place-feedback needs --cycles" in capsys.readouterr().out
