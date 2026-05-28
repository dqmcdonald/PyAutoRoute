"""Tests for the parameter-sweep tool (pyautoroute.tune) and the --auto probe."""

from __future__ import annotations

import pathlib

import pytest

from pyautoroute import autoroute, rules, sexpr, tune
from pyautoroute.pcb import Board, OutlineShape, Pad

_TEST_BOARD = (pathlib.Path(__file__).resolve().parents[1]
               / "TestProjects" / "Test5" / "Test5.kicad_pcb")


def _pad(net, cx, cy):
    return Pad(net=net, pad_type="smd", shape="rect", cx=cx, cy=cy, w=1.0, h=1.0,
              angle=0.0, copper_layers=["F.Cu", "B.Cu"])


def _board():
    pads = [_pad("A", 4, 10), _pad("A", 16, 10),
            _pad("B", 4, 14), _pad("B", 16, 14)]
    outline = [OutlineShape("poly", {"pts": [(0, 0), (20, 0), (20, 20), (0, 20)]})]
    return Board(tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"], pads=pads,
                 free_vias=[], segments=[], zones=[], outline=outline)


def test_score_orders_by_completion_then_length_then_vias():
    base = tune.TuneMetrics(routed=10, unrouted=0, length=100.0, vias=2, runtime=1.0)
    worse_unrouted = tune.TuneMetrics(9, 1, 100.0, 2, 1.0)
    longer = tune.TuneMetrics(10, 0, 120.0, 2, 1.0)
    more_vias = tune.TuneMetrics(10, 0, 100.0, 5, 1.0)
    assert tune.score(worse_unrouted) > tune.score(base)   # an unrouted net dominates
    assert tune.score(longer) > tune.score(base)
    assert tune.score(more_vias) > tune.score(base)
    # runtime only matters when weighted
    slow = tune.TuneMetrics(10, 0, 100.0, 2, 50.0)
    assert tune.score(slow) == tune.score(base)
    assert tune.score(slow, time_weight=1.0) > tune.score(base, time_weight=1.0)


def test_evaluate_routes_synthetic_board():
    board = _board()
    r = rules.default_rules()
    m = tune.evaluate(board, r, tune.Config(grid_mult=1.0), seed=0)
    assert m.routed == 2 and m.unrouted == 0       # both nets route
    assert m.length > 0


def test_sweep_returns_sorted_scores_and_best():
    board = _board()
    r = rules.default_rules()
    configs = tune.default_grid()                  # greedy (no anneal budget)
    scored = tune.sweep(board, r, configs, seeds=(0,))
    assert len(scored) == len(configs)
    # sorted best-first
    assert all(scored[i].median_score <= scored[i + 1].median_score
               for i in range(len(scored) - 1))
    assert tune.best_config(scored) is scored[0].config


@pytest.mark.skipif(not _TEST_BOARD.exists(), reason="Test5 board not present")
def test_tune_cli_runs(capsys):
    assert tune.main([str(_TEST_BOARD), "--time", "0", "--seeds", "1"]) == 0
    out = capsys.readouterr().out
    assert "best:" in out


@pytest.mark.skipif(not _TEST_BOARD.exists(), reason="Test5 board not present")
def test_cli_auto_routes_clean(tmp_path):
    # non-TTY (pytest) -> --auto applies the chosen settings without prompting
    out = tmp_path / "out.kicad_pcb"
    args = autoroute.build_parser().parse_args(
        [str(_TEST_BOARD), "-o", str(out), "--auto", "--auto-probe-time", "0",
         "--log", "--quiet"])
    assert autoroute.run(args) == 0
    assert "auto: best probe grid=" in out.with_suffix(".log").read_text()
