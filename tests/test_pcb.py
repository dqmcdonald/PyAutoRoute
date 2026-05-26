"""Tests for pyautoroute.pcb (board model + routed-board writer)."""

from __future__ import annotations

import math
import pathlib

import pytest

from pyautoroute import pcb

REPO = pathlib.Path(__file__).resolve().parent.parent
PCB = REPO / "TestProjects" / "Test1" / "Test1.kicad_pcb"


def test_rotate_matches_kicad_convention():
    # local (1, 0) rotated by +90 -> (0, -1) under KiCad RotatePoint
    x, y = pcb.rotate(1.0, 0.0, 90.0)
    assert math.isclose(x, 0.0, abs_tol=1e-9)
    assert math.isclose(y, -1.0, abs_tol=1e-9)
    # 180 negates
    x, y = pcb.rotate(2.0, 3.0, 180.0)
    assert math.isclose(x, -2.0, abs_tol=1e-9)
    assert math.isclose(y, -3.0, abs_tol=1e-9)


def test_pad_abs_position_with_footprint_rotation():
    # Footprint at (10, 20) rotated -90; pad local (-3.365, -8.89).
    # Position: x' = px*cos + py*sin ; y' = -px*sin + py*cos, fa=-90.
    # Orientation: KiCad stores the pad `at` angle ABSOLUTELY (it already
    # includes the footprint rotation), so pad.angle is the stored 270, not
    # fa + 270. (Verified against kicad-cli DRC connectivity on SW1.)
    text = (
        '(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal))'
        ' (footprint "x" (at 10 20 -90)'
        '  (property "Reference" "U1")'
        '  (pad "1" smd roundrect (at -3.365 -8.89 270) (size 1 1)'
        '   (layers "F.Cu" "F.Mask") (net "GND"))))'
    )
    board = _board_from_text(text)
    p = board.pads[0]
    assert math.isclose(p.cx, 10 + 8.89, abs_tol=1e-6)
    assert math.isclose(p.cy, 20 - 3.365, abs_tol=1e-6)
    assert math.isclose(p.angle, 270.0, abs_tol=1e-9)
    assert p.net == "GND"
    assert p.copper_layers == ["F.Cu"]


def test_th_pad_on_both_layers():
    text = (
        '(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal))'
        ' (footprint "x" (at 0 0 0)'
        '  (property "Reference" "J1")'
        '  (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1)'
        '   (layers "*.Cu" "*.Mask") (net "5V"))))'
    )
    board = _board_from_text(text)
    p = board.pads[0]
    assert p.copper_layers == ["F.Cu", "B.Cu"]
    assert p.drill == 1.0


def _board_from_text(text: str) -> pcb.Board:
    import tempfile
    f = tempfile.NamedTemporaryFile("w", suffix=".kicad_pcb", delete=False)
    f.write(text)
    f.close()
    return pcb.load_board(f.name)


def test_numbered_net_format():
    text = (
        '(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal))'
        ' (net 0 "")'
        ' (net 1 "GND")'
        ' (footprint "x" (at 0 0 0) (property "Reference" "R1")'
        '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1))))'
    )
    board = _board_from_text(text)
    assert not board.name_only_nets
    assert board.pads[0].net == "GND"


@pytest.mark.skipif(not PCB.exists(), reason="Test1 board not present")
def test_load_test1_board():
    board = pcb.load_board(PCB)
    assert board.copper_layers == ["F.Cu", "B.Cu"]
    assert board.front_layer == "F.Cu"
    assert board.name_only_nets
    assert len(board.pads) == 96
    assert len(board.free_vias) == 10
    assert len(board.segments) == 0
    # outline present
    assert any(s.kind == "poly" for s in board.outline)
    # GND has many pads
    by_net = board.pads_by_net()
    assert "GND" in by_net and len(by_net["GND"]) > 1


@pytest.mark.skipif(not PCB.exists(), reason="Test1 board not present")
def test_writer_noop_is_byte_identical(tmp_path):
    board = pcb.load_board(PCB)
    out = tmp_path / "out.kicad_pcb"
    pcb.write_board(board, out, new_nodes=None, strip_free_vias=False)
    assert out.read_text() == PCB.read_text()


@pytest.mark.skipif(not PCB.exists(), reason="Test1 board not present")
def test_writer_strips_free_vias(tmp_path):
    board = pcb.load_board(PCB)
    out = tmp_path / "stripped.kicad_pcb"
    pcb.write_board(board, out, new_nodes=None, strip_free_vias=True)
    reloaded = pcb.load_board(out)
    assert len(reloaded.free_vias) == 0


@pytest.mark.skipif(not PCB.exists(), reason="Test1 board not present")
def test_writer_appends_segment(tmp_path):
    board = pcb.load_board(PCB)
    seg = pcb.make_segment(board, 50.0, 50.0, 55.0, 50.0, 0.2, "F.Cu", "GND")
    out = tmp_path / "withseg.kicad_pcb"
    pcb.write_board(board, out, new_nodes=[seg], strip_free_vias=True)
    reloaded = pcb.load_board(out)
    assert len(reloaded.segments) == 1
    s = reloaded.segments[0]
    assert (s.x1, s.y1, s.x2, s.y2) == (50.0, 50.0, 55.0, 50.0)
    assert s.net == "GND" and s.layer == "F.Cu"
