"""Headless test of the GUI worker thread.

The worker imports no tkinter (it only posts event objects onto a queue), so it
can be driven without a display: feed it a config object, wait for the thread,
and drain the queue. This guards the GUI's place→route path now that it runs
through the shared `pyautoroute.pipeline` (`run_placement` / `run_routing`).

`gui.controls.RunConfig` lives in a tkinter-importing module, so the worker's
config is duck-typed; here a `SimpleNamespace` with the same attributes stands in.
"""

from __future__ import annotations

import pathlib
import queue
import shutil
import threading
import types

import pytest

from pyautoroute.gui import worker
from pyautoroute.gui.events import BoardSnap, Done, Error, Phase, Progress
from pyautoroute.placement import PlaceParams

_SRC = pathlib.Path(__file__).resolve().parents[1] / "TestProjects" / "Test3"

pytestmark = pytest.mark.skipif(
    not (_SRC / "Test3.kicad_pcb").exists(), reason="Test3 board not present")

_DEFAULTS = dict(
    input=None, pro=None, output=None, place=False, place_only=False, grid=None,
    iters=None, time_budget=None, runs=1, exclude_net=[], via_weight=2.0,
    unrouted_weight=100.0, anneal_temps=(2.0, 0.1), seed=1,
    place_iters=None, place_time=None, place_margin=2.0, place_buffer=None,
    place_overlap_weight=PlaceParams.overlap_weight,
    place_compact_weight=PlaceParams.compact_weight,
    place_edge_weight=PlaceParams.edge_weight,
    place_temps=(PlaceParams.t_start, PlaceParams.t_end),
    place_step=PlaceParams.step, place_rotate=PlaceParams.rotate_mode,
    place_runs=1, cycles=1, place_feedback=False, congestion_weight=5.0,
    snapshots=0, silk_labels=False, keep_outline=False,
    ground_plane=False, ground_net=None, ground_plane_layer="B.Cu",
    ground_plane_margin=None, stitch_vias=None)


def _copy_board(tmp_path):
    """Copy the board (+ project) into tmp so output is written there, not in-tree."""
    for suf in (".kicad_pcb", ".kicad_pro"):
        src = _SRC / f"Test3{suf}"
        if src.exists():
            shutil.copy(src, tmp_path / f"Test3{suf}")
    return tmp_path / "Test3.kicad_pcb"


def _cfg(board, **over):
    d = dict(_DEFAULTS, input=str(board))
    d.update(over)
    return types.SimpleNamespace(**d)


def _drive(cfg, timeout=180):
    q: queue.Queue = queue.Queue()
    cancel = threading.Event()
    w = worker.Worker(q, cancel)
    w.start(cfg)
    assert w.join(timeout), "worker thread did not finish in time"
    events = []
    while not q.empty():
        events.append(q.get())
    return events


def _no_error(events):
    err = next((e for e in events if isinstance(e, Error)), None)
    assert err is None, getattr(err, "tb", "")


def test_worker_route_only_clean(tmp_path):
    events = _drive(_cfg(_copy_board(tmp_path)))
    _no_error(events)
    done = [e for e in events if isinstance(e, Done)]
    assert len(done) == 1
    assert done[0].routed > 0 and done[0].violations == []     # routed & DRC-clean
    # live events flowed through the shared-pipeline hooks
    assert any(isinstance(e, Phase) for e in events)
    assert any(isinstance(e, Progress) for e in events)
    assert any(isinstance(e, BoardSnap) for e in events)


def test_worker_place_and_route_clean(tmp_path):
    events = _drive(_cfg(_copy_board(tmp_path), place=True))
    _no_error(events)
    done = [e for e in events if isinstance(e, Done)]
    assert len(done) == 1
    assert done[0].routed > 0 and done[0].violations == []
    # a placement phase ran and posted at least one board snapshot
    assert any(isinstance(e, Phase) and "placing" in e.name for e in events)
    assert any(isinstance(e, BoardSnap) for e in events)


def test_worker_cycles_place_and_route_clean(tmp_path):
    # best-of-cycles through the GUI worker: 3 place+route cycles, keep the best.
    events = _drive(_cfg(_copy_board(tmp_path), place=True, cycles=3,
                         place_iters=120))
    _no_error(events)
    done = [e for e in events if isinstance(e, Done)]
    assert len(done) == 1
    assert done[0].routed > 0 and done[0].violations == []
    # per-cycle progress was tagged, and a winner was selected
    assert any(isinstance(e, Phase) and e.name.startswith("cycle 1/3")
               for e in events)
    assert any(isinstance(e, Phase) and e.name.startswith("cycle 3/3")
               for e in events)
    assert any(isinstance(e, Phase) and "best of" in e.name for e in events)


def test_worker_cycles_with_feedback_clean(tmp_path):
    # congestion feedback path: cycles run sequentially, accumulating the field.
    events = _drive(_cfg(_copy_board(tmp_path), place=True, cycles=2,
                         place_feedback=True, congestion_weight=5.0,
                         place_iters=120))
    _no_error(events)
    done = [e for e in events if isinstance(e, Done)]
    assert len(done) == 1
    assert done[0].routed > 0 and done[0].violations == []
    assert any(isinstance(e, Phase) and "best of" in e.name for e in events)


def test_worker_ground_plane_clean(tmp_path):
    # ground plane generation after routing
    board = _copy_board(tmp_path)
    events = _drive(_cfg(board, ground_plane=True, ground_plane_layer="B.Cu"))
    _no_error(events)
    done = [e for e in events if isinstance(e, Done)]
    assert len(done) == 1
    assert done[0].routed > 0
    # the output board should have a zone node (ground plane)
    # we reload it to verify the zone was written
    from pyautoroute import pcb
    routed_board = pcb.load_board(board.with_stem(board.stem + "_routed"))
    assert any(z['net'] == "GND" for z in routed_board.zones)
