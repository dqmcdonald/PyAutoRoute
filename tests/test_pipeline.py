"""Tests for the shared place→route→score pipeline (`pipeline.py`) and the
CLI ``--cycles`` best-of-cycles loop."""

from __future__ import annotations

import pathlib

import pytest

from pyautoroute import autoroute, pipeline
from pyautoroute.placement import PlaceParams
from pyautoroute.router import RouteParams
from pyautoroute.rules import load_rules

_BOARD = (pathlib.Path(__file__).resolve().parents[1]
          / "TestProjects" / "Test3" / "Test3.kicad_pcb")
_PRO = _BOARD.with_suffix(".kicad_pro")

pytestmark = pytest.mark.skipif(not _BOARD.exists(), reason="Test3 board not present")


def _cycle_inputs():
    """A small, fast (greedy, no-anneal) cycle setup for the Test3 board."""
    rules = load_rules(_PRO)
    pitch = autoroute.default_pitch(rules)
    place_params = PlaceParams(iters=200)
    route_params = RouteParams(via_cost=2.0)
    route_kw = dict(annealing=False, iters=None, time_budget=None,
                    unrouted_weight=100.0, anneal_temps=(2.0, 0.1), via_weight=2.0)
    return rules, pitch, place_params, route_params, route_kw


def test_run_cycle_basic_shape():
    rules, pitch, pp, rp, kw = _cycle_inputs()
    cr = pipeline.run_cycle(_BOARD, rules, pitch, pp, rp,
                            route_kw=kw, place_margin=2.0, seed=1)
    assert cr.routed + cr.unrouted == cr.n_conns      # every connection accounted for
    assert cr.n_conns > 0
    assert cr.score == (cr.unrouted, cr.energy)
    # the board comes back placement-synced and ready to write
    assert cr.board is not None and cr.grid is not None
    assert len(cr.results) == cr.n_conns


def test_run_cycle_is_deterministic_for_a_seed():
    rules, pitch, pp, rp, kw = _cycle_inputs()
    a = pipeline.run_cycle(_BOARD, rules, pitch, pp, rp,
                           route_kw=kw, place_margin=2.0, seed=7)
    b = pipeline.run_cycle(_BOARD, rules, pitch, pp, rp,
                           route_kw=kw, place_margin=2.0, seed=7)
    assert a.score == b.score                          # same seed -> same outcome
    assert a.board is not b.board                       # but a fresh board each time


def _mk(unrouted, energy, seed=0):
    return pipeline.CycleResult(
        seed=seed, board=None, grid=None, n_conns=10, results=[],
        routed=10 - unrouted, unrouted=unrouted, length=0.0, vias=0,
        energy=energy, summary=None)


def test_select_best_prefers_fewest_unrouted_then_energy():
    a = _mk(unrouted=2, energy=10.0)     # routes fewer nets, but lowest energy
    b = _mk(unrouted=1, energy=500.0)
    c = _mk(unrouted=1, energy=400.0)
    # completing connections dominates: a (2 unrouted) loses despite low energy;
    # between b and c (both 1 unrouted) the lower energy wins.
    assert pipeline.select_best([a, b, c]) is c
    # all-routed: pure energy tiebreak
    assert pipeline.select_best([_mk(0, 9.0), _mk(0, 8.0)]).energy == 8.0
    assert pipeline.select_best([]) is None


def test_cli_cycles_runs_and_writes(tmp_path):
    out = tmp_path / "out.kicad_pcb"
    args = autoroute.build_parser().parse_args(
        [str(_BOARD), "--place", "--cycles", "2",
         "--seed", "1", "--quiet", "--output", str(out)])
    assert autoroute.run(args) == 0                    # clean self-check
    assert out.exists()


def test_cli_cycles_parallel_writes(tmp_path):
    # The parallel cycle path (workers run pipeline.run_cycle, suppressed
    # progress) routes a board through the ProcessPoolExecutor and self-checks.
    out = tmp_path / "out.kicad_pcb"
    args = autoroute.build_parser().parse_args(
        [str(_BOARD), "--place", "--cycles", "2", "--jobs", "2",
         "--seed", "4", "--quiet", "--output", str(out)])
    assert autoroute.run(args) == 0
    assert out.exists()
