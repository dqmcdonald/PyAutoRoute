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


# --- fix_value_layers tests --------------------------------------------------

def _board_with_value(layer: str, fp_layer: str = "F.Cu", fmt: str = "property") -> pcb.Board:
    """Build a minimal board with a single footprint whose Value text is on *layer*."""
    if fmt == "property":
        txt = (
            f'(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal)'
            f'  (5 "F.SilkS" user "F.Silkscreen") (7 "B.SilkS" user "B.Silkscreen"))'
            f' (footprint "Lib:C" (layer "{fp_layer}") (at 0 0)'
            f'  (property "Value" "10nF"'
            f'   (at 0 0) (layer "{layer}")'
            f'   (effects (font (size 1 1))))'
            f'  (pad "1" smd rect (at -1 0) (size 1 1)'
            f'   (layers "F.Cu" "F.Mask") (net "A"))))'
        )
    else:
        # Legacy fp_text value format
        txt = (
            f'(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal)'
            f'  (5 "F.SilkS" user "F.Silkscreen") (7 "B.SilkS" user "B.Silkscreen"))'
            f' (footprint "Lib:C" (layer "{fp_layer}") (at 0 0)'
            f'  (fp_text value "10nF"'
            f'   (at 0 0) (layer "{layer}")'
            f'   (effects (font (size 1 1))))'
            f'  (pad "1" smd rect (at -1 0) (size 1 1)'
            f'   (layers "F.Cu" "F.Mask") (net "A"))))'
        )
    return _board_from_text(txt)


def _value_layer(board: pcb.Board) -> str | None:
    """Return the layer of the first Value property in the board tree."""
    from pyautoroute.pcb import children, child, strings, atoms_after_head
    for fp_node in children(board.tree, "footprint"):
        for prop in children(fp_node, "property"):
            atoms = atoms_after_head(prop)
            if atoms and atoms[0].text == "Value":
                ls = strings(child(prop, "layer"))
                return ls[0] if ls else None
        for txt in children(fp_node, "fp_text"):
            atoms = atoms_after_head(txt)
            if atoms and atoms[0].text == "value":
                ls = strings(child(txt, "layer"))
                return ls[0] if ls else None
    return None


def test_fix_value_layers_moves_fab_to_silk():
    board = _board_with_value("F.Fab")
    changed = pcb.fix_value_layers(board)
    assert changed == 1
    assert _value_layer(board) == "F.SilkS"


def test_fix_value_layers_back_footprint():
    board = _board_with_value("B.Fab", fp_layer="B.Cu")
    changed = pcb.fix_value_layers(board)
    assert changed == 1
    assert _value_layer(board) == "B.SilkS"


def test_fix_value_layers_already_silk_no_change():
    board = _board_with_value("F.SilkS")
    changed = pcb.fix_value_layers(board)
    assert changed == 0


def test_fix_value_layers_legacy_fp_text():
    board = _board_with_value("F.Fab", fmt="legacy")
    changed = pcb.fix_value_layers(board)
    assert changed == 1
    assert _value_layer(board) == "F.SilkS"


@pytest.mark.skipif(not PCB.exists(), reason="Test1 board not present")
def test_fix_value_layers_roundtrip_write(tmp_path):
    """After fixing, no Value text remains on a Fab layer."""
    board = pcb.load_board(PCB)
    pcb.fix_value_layers(board)
    out = tmp_path / "fixed.kicad_pcb"
    pcb.write_board(board, out, new_nodes=None, strip_free_vias=False)
    reloaded = pcb.load_board(out)
    from pyautoroute.pcb import children, child, strings, atoms_after_head
    fab_layers = {"F.Fab", "B.Fab"}
    for fp_node in children(reloaded.tree, "footprint"):
        for prop in children(fp_node, "property"):
            atoms = atoms_after_head(prop)
            if atoms and atoms[0].text == "Value":
                layer = strings(child(prop, "layer"))
                assert not layer or layer[0] not in fab_layers, \
                    f"Value still on {layer} after fix"


def test_fix_value_layers_written_to_file(tmp_path):
    """After fix, write_board serialises the new silk layer correctly."""
    board = _board_with_value("F.Fab")
    pcb.fix_value_layers(board)
    out = tmp_path / "fixed.kicad_pcb"
    pcb.write_board(board, out, new_nodes=None, strip_free_vias=False)
    reloaded = pcb.load_board(out)
    assert _value_layer(reloaded) == "F.SilkS"


# --- _rotate_text_nodes tests ------------------------------------------------

def test_rotate_text_nodes_updates_fp_text_and_property():
    """After a 90° footprint rotation, fp_text and property (at) angles update."""
    txt = (
        '(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal)'
        '  (5 "F.SilkS" user "F.Silkscreen"))'
        ' (footprint "Lib:R" (layer "F.Cu") (at 0 0 0)'
        '  (fp_text user "${REFERENCE}" (at 1 0 0) (layer "F.SilkS")'
        '   (effects (font (size 1 1))))'
        '  (property "Value" "10K" (at 0 1 0) (layer "F.SilkS")'
        '   (effects (font (size 1 1))))'
        '  (pad "1" smd rect (at -1 0) (size 1 1)'
        '   (layers "F.Cu" "F.Mask") (net "A"))))'
    )
    board = _board_from_text(txt)
    fp = board.footprints[0]
    fp.angle = 90.0
    from pyautoroute.pcb import sync_tree_from_placement, children, child, floats
    # Make footprint appear moved so sync writes it
    fp.x0, fp.y0, fp.angle0 = fp.x, fp.y, 0.0
    sync_tree_from_placement(board)

    # Check angles were updated in the tree
    fp_node = fp.fp_node
    for txt_node in children(fp_node, "fp_text"):
        at_vals = floats(child(txt_node, "at"))
        assert len(at_vals) >= 3
        assert abs(at_vals[2] - 90.0) < 1e-6, f"fp_text angle {at_vals[2]} != 90"
    for prop_node in children(fp_node, "property"):
        at_vals = floats(child(prop_node, "at"))
        assert len(at_vals) >= 3
        assert abs(at_vals[2] - 90.0) < 1e-6, f"property angle {at_vals[2]} != 90"


# --- fill zone tests ---------------------------------------------------------

_BOARD_WITH_FILL_ZONE = (
    '(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal))'
    ' (net "GND") (net "VCC")'
    ' (zone (net "GND") (layer "B.Cu")'
    '  (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))'
    '  (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10))))'
    ' (zone (net "VCC") (layer "F.Cu")'
    '  (fill no)'
    '  (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10)))))'
)

_BOARD_NO_FILL_ZONE = (
    '(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal))'
    ' (net "GND")'
    ' (zone (net "GND") (layer "B.Cu")'
    '  (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10)))))'
)


def test_zone_fill_enabled_parsed():
    board = _board_from_text(_BOARD_WITH_FILL_ZONE)
    assert len(board.zones) == 2
    gnd_zone = next(z for z in board.zones if z["net"] == "GND")
    vcc_zone = next(z for z in board.zones if z["net"] == "VCC")
    assert gnd_zone["fill_enabled"] is True
    assert vcc_zone["fill_enabled"] is False


def test_zone_fill_nets_returns_only_filled():
    board = _board_from_text(_BOARD_WITH_FILL_ZONE)
    assert pcb.zone_fill_nets(board) == {"GND"}


def test_zone_fill_nets_empty_when_no_fill():
    board = _board_from_text(_BOARD_NO_FILL_ZONE)
    assert pcb.zone_fill_nets(board) == set()


def test_fill_zone_not_in_obstacles():
    from pyautoroute.geometry import board_obstacles
    board = _board_from_text(_BOARD_WITH_FILL_ZONE)
    obs = board_obstacles(board)
    # GND zone has fill_enabled → should not appear as an obstacle
    gnd_obs = [o for o in obs if o.net == "GND"]
    assert gnd_obs == [], "filled zone should not be an obstacle"
    # VCC zone has fill_enabled=False → should appear as an obstacle (layer is in copper_layers)
    vcc_obs = [o for o in obs if o.net == "VCC"]
    assert len(vcc_obs) == 1, "unfilled zone should be an obstacle"
