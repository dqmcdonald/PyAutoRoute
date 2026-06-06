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
    assert len(board.free_vias) == 0
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


# --- move_values_to_silk / move_refs_to_fab tests ----------------------------

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


def test_move_values_to_silk_moves_fab_to_silk():
    board = _board_with_value("F.Fab")
    changed = pcb.move_values_to_silk(board)
    assert changed == 1
    assert _value_layer(board) == "F.SilkS"


def test_move_values_to_silk_back_footprint():
    board = _board_with_value("B.Fab", fp_layer="B.Cu")
    changed = pcb.move_values_to_silk(board)
    assert changed == 1
    assert _value_layer(board) == "B.SilkS"


def test_move_values_to_silk_already_silk_no_change():
    board = _board_with_value("F.SilkS")
    changed = pcb.move_values_to_silk(board)
    assert changed == 0


def test_move_values_to_silk_legacy_fp_text():
    board = _board_with_value("F.Fab", fmt="legacy")
    changed = pcb.move_values_to_silk(board)
    assert changed == 1
    assert _value_layer(board) == "F.SilkS"


@pytest.mark.skipif(not PCB.exists(), reason="Test1 board not present")
def test_move_values_to_silk_roundtrip_write(tmp_path):
    """After fixing, no Value text remains on a Fab layer."""
    board = pcb.load_board(PCB)
    pcb.move_values_to_silk(board)
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


def test_move_values_to_silk_written_to_file(tmp_path):
    """After fix, write_board serialises the new silk layer correctly."""
    board = _board_with_value("F.Fab")
    pcb.move_values_to_silk(board)
    out = tmp_path / "fixed.kicad_pcb"
    pcb.write_board(board, out, new_nodes=None, strip_free_vias=False)
    reloaded = pcb.load_board(out)
    assert _value_layer(reloaded) == "F.SilkS"


# --- move_refs_to_fab tests --------------------------------------------------

def _board_with_ref(layer: str, fp_layer: str = "F.Cu", fmt: str = "property") -> pcb.Board:
    """Build a minimal board with a single footprint whose Reference text is on *layer*."""
    if fmt == "property":
        txt = (
            f'(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal)'
            f'  (5 "F.SilkS" user "F.Silkscreen") (7 "B.SilkS" user "B.Silkscreen")'
            f'  (8 "F.Fab" user) (9 "B.Fab" user))'
            f' (footprint "Lib:R" (layer "{fp_layer}") (at 0 0)'
            f'  (property "Reference" "R1"'
            f'   (at 0 0) (layer "{layer}")'
            f'   (effects (font (size 1 1))))'
            f'  (pad "1" smd rect (at -1 0) (size 1 1)'
            f'   (layers "F.Cu" "F.Mask") (net "A"))))'
        )
    else:
        txt = (
            f'(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal)'
            f'  (5 "F.SilkS" user "F.Silkscreen") (7 "B.SilkS" user "B.Silkscreen")'
            f'  (8 "F.Fab" user) (9 "B.Fab" user))'
            f' (footprint "Lib:R" (layer "{fp_layer}") (at 0 0)'
            f'  (fp_text reference "R1"'
            f'   (at 0 0) (layer "{layer}")'
            f'   (effects (font (size 1 1))))'
            f'  (pad "1" smd rect (at -1 0) (size 1 1)'
            f'   (layers "F.Cu" "F.Mask") (net "A"))))'
        )
    return _board_from_text(txt)


def _ref_layer(board: pcb.Board) -> str | None:
    """Return the layer of the first Reference property in the board tree."""
    from pyautoroute.pcb import children, child, strings, atoms_after_head
    for fp_node in children(board.tree, "footprint"):
        for prop in children(fp_node, "property"):
            atoms = atoms_after_head(prop)
            if atoms and atoms[0].text == "Reference":
                ls = strings(child(prop, "layer"))
                return ls[0] if ls else None
        for txt in children(fp_node, "fp_text"):
            atoms = atoms_after_head(txt)
            if atoms and atoms[0].text == "reference":
                ls = strings(child(txt, "layer"))
                return ls[0] if ls else None
    return None


def test_move_refs_to_fab_moves_silk_to_fab():
    board = _board_with_ref("F.SilkS")
    changed = pcb.move_refs_to_fab(board)
    assert changed == 1
    assert _ref_layer(board) == "F.Fab"


def test_move_refs_to_fab_back_footprint():
    board = _board_with_ref("B.SilkS", fp_layer="B.Cu")
    changed = pcb.move_refs_to_fab(board)
    assert changed == 1
    assert _ref_layer(board) == "B.Fab"


def test_move_refs_to_fab_already_fab_no_change():
    board = _board_with_ref("F.Fab")
    changed = pcb.move_refs_to_fab(board)
    assert changed == 0


def test_move_refs_to_fab_legacy_fp_text():
    board = _board_with_ref("F.SilkS", fmt="legacy")
    changed = pcb.move_refs_to_fab(board)
    assert changed == 1
    assert _ref_layer(board) == "F.Fab"


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


# --- footprint constraint editor tests ----------------------------------------


class TestFootprintConstraints:
    """Test footprint constraint helpers (Phase 1 of interactive GUI feature)."""

    def test_footprint_bbox_single_pad(self):
        text = (
            '(kicad_pcb (layers (0 "F.Cu") (2 "B.Cu"))'
            ' (footprint "x" (at 10 20)'
            '  (pad "1" smd roundrect (at 5 0) (size 2 4) '
            '   (layers "F.Cu") (net ""))))'
        )
        board = _board_from_text(text)
        fp = board.footprints[0]
        x0, y0, x1, y1 = pcb.footprint_bbox(fp)
        # Pad center (10+5, 20+0) = (15, 20), size 2x4
        # half-extent = 0.5 * sqrt(2^2 + 4^2) = 0.5 * sqrt(20) ≈ 2.236
        he = 0.5 * math.sqrt(4 + 16)
        assert math.isclose(x0, 15 - he, abs_tol=1e-6)
        assert math.isclose(x1, 15 + he, abs_tol=1e-6)
        assert math.isclose(y0, 20 - he, abs_tol=1e-6)
        assert math.isclose(y1, 20 + he, abs_tol=1e-6)

    def test_footprint_bbox_empty(self):
        text = (
            '(kicad_pcb (layers (0 "F.Cu") (2 "B.Cu"))'
            ' (footprint "x" (at 10 20)))'
        )
        board = _board_from_text(text)
        fp = board.footprints[0]
        bbox = pcb.footprint_bbox(fp)
        assert bbox == (0.0, 0.0, 0.0, 0.0)

    def test_footprint_at_direct_hit(self):
        text = (
            '(kicad_pcb (layers (0 "F.Cu") (2 "B.Cu"))'
            ' (footprint "x" (at 10 20)'
            '  (property "Reference" "U1")'
            '  (pad "1" smd roundrect (at 0 0) (size 4 4) '
            '   (layers "F.Cu") (net ""))))'
        )
        board = _board_from_text(text)
        # Center is at (10, 20), half-extent = 2*sqrt(2) ≈ 2.828
        fp = pcb.footprint_at(board, 10.0, 20.0)
        assert fp is not None
        assert fp.ref == "U1"

    def test_footprint_at_miss(self):
        text = (
            '(kicad_pcb (layers (0 "F.Cu") (2 "B.Cu"))'
            ' (footprint "x" (at 10 20)'
            '  (property "Reference" "U1")'
            '  (pad "1" smd roundrect (at 0 0) (size 2 2) '
            '   (layers "F.Cu") (net ""))))'
        )
        board = _board_from_text(text)
        # Click far away
        fp = pcb.footprint_at(board, 100.0, 100.0)
        assert fp is None

    def test_footprint_at_overlapping_smallest_wins(self):
        text = (
            '(kicad_pcb (layers (0 "F.Cu") (2 "B.Cu"))'
            ' (footprint "x" (at 10 20)'
            '  (property "Reference" "U1")'
            '  (pad "1" smd roundrect (at 0 0) (size 20 20) '
            '   (layers "F.Cu") (net "")))'
            ' (footprint "y" (at 11 21)'
            '  (property "Reference" "U2")'
            '  (pad "1" smd roundrect (at 0 0) (size 2 2) '
            '   (layers "F.Cu") (net ""))))'
        )
        board = _board_from_text(text)
        # Click at (11, 21), which is in both footprints' bboxes
        # U1 bbox ≈ [0, 10] x [10, 30], U2 bbox ≈ [10, 12] x [20, 22]
        # U2 is smaller so should win
        fp = pcb.footprint_at(board, 11.0, 21.0)
        assert fp is not None
        assert fp.ref == "U2"

    def test_set_footprint_edge_creates_property(self):
        text = (
            '(kicad_pcb (layers (0 "F.Cu") (2 "B.Cu"))'
            ' (footprint "U1" (at 10 20)'
            '  (property "Reference" "U1")))'
        )
        board = _board_from_text(text)
        fp = board.footprints[0]
        assert fp.edge_affinity is None
        pcb.set_footprint_edge(fp, "left")
        assert fp.edge_affinity == "left"
        # Verify it's in the tree
        assert pcb._footprint_edge_affinity(fp.fp_node) == "left"

    def test_set_footprint_edge_removes_with_none(self):
        text = (
            '(kicad_pcb (layers (0 "F.Cu") (2 "B.Cu"))'
            ' (footprint "U1" (at 10 20)'
            '  (property "Autoroute-edge" "left")))'
        )
        board = _board_from_text(text)
        fp = board.footprints[0]
        assert fp.edge_affinity == "left"
        pcb.set_footprint_edge(fp, None)
        assert fp.edge_affinity is None
        assert pcb._footprint_edge_affinity(fp.fp_node) is None

    def test_set_footprint_overlap(self):
        text = (
            '(kicad_pcb (layers (0 "F.Cu") (2 "B.Cu"))'
            ' (footprint "U1" (at 10 20)'
            '  (property "Reference" "U1")))'
        )
        board = _board_from_text(text)
        fp = board.footprints[0]
        assert fp.overlap_ok is False
        pcb.set_footprint_overlap(fp, True)
        assert fp.overlap_ok is True
        assert pcb._footprint_overlap_ok(fp.fp_node) is True
        pcb.set_footprint_overlap(fp, False)
        assert fp.overlap_ok is False
        assert pcb._footprint_overlap_ok(fp.fp_node) is False

    def test_set_footprint_locked(self):
        text = (
            '(kicad_pcb (layers (0 "F.Cu") (2 "B.Cu"))'
            ' (footprint "U1" (at 10 20)'
            '  (property "Reference" "U1")))'
        )
        board = _board_from_text(text)
        fp = board.footprints[0]
        assert fp.locked is False
        pcb.set_footprint_locked(fp, True)
        assert fp.locked is True
        assert pcb._footprint_locked(fp.fp_node) is True
        pcb.set_footprint_locked(fp, False)
        assert fp.locked is False
        assert pcb._footprint_locked(fp.fp_node) is False

    def test_set_footprint_decoupling(self):
        text = (
            '(kicad_pcb (layers (0 "F.Cu") (2 "B.Cu"))'
            ' (footprint "C1" (at 10 20)'
            '  (property "Reference" "C1")))'
        )
        board = _board_from_text(text)
        fp = board.footprints[0]
        assert fp.decouple_target is None
        pcb.set_footprint_decoupling(fp, "U3")
        assert fp.decouple_target == "U3"
        assert pcb._footprint_decouple(fp.fp_node) == "U3"
        pcb.set_footprint_decoupling(fp, "auto")
        assert fp.decouple_target == "auto"
        assert pcb._footprint_decouple(fp.fp_node) == "auto"
        pcb.set_footprint_decoupling(fp, None)
        assert fp.decouple_target is None
        assert pcb._footprint_decouple(fp.fp_node) is None

    def test_parse_decoupling_property(self):
        text = (
            '(kicad_pcb (layers (0 "F.Cu") (2 "B.Cu"))'
            ' (footprint "C1" (at 10 20)'
            '  (property "Reference" "C1")'
            '  (property "Autoroute-decouple" "U7")))'
        )
        board = _board_from_text(text)
        assert board.footprints[0].decouple_target == "U7"

    def test_constraint_round_trip_to_file(self):
        text = (
            '(kicad_pcb (layers (0 "F.Cu") (2 "B.Cu"))'
            ' (footprint "U1" (at 10 20)'
            '  (property "Reference" "U1"))'
            ' (footprint "U2" (at 30 40)'
            '  (property "Reference" "U2")))'
        )
        board = _board_from_text(text)
        u1, u2 = board.footprints
        pcb.set_footprint_edge(u1, "right")
        pcb.set_footprint_overlap(u1, True)
        pcb.set_footprint_locked(u1, True)
        pcb.set_footprint_edge(u2, "any")
        pcb.set_footprint_decoupling(u2, "U1")

        # Write and reload
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.kicad_pcb',
                                         delete=False) as f:
            tmp_path = f.name
        try:
            pcb.write_board(board, tmp_path)
            board2 = pcb.load_board(tmp_path)
            u1_r, u2_r = board2.footprints
            assert u1_r.edge_affinity == "right"
            assert u1_r.overlap_ok is True
            assert u1_r.locked is True
            assert u2_r.edge_affinity == "any"
            assert u2_r.overlap_ok is False
            assert u2_r.locked is False
            assert u2_r.decouple_target == "U1"
            assert u1_r.decouple_target is None
        finally:
            import os
            os.unlink(tmp_path)


# ── write_board strip_segments ─────────────────────────────────────────────────

def _board_with_segment(tmp_path):
    """Write a minimal board with one segment and return the path."""
    raw = (
        '(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal))'
        ' (segment (start 0 0) (end 10 0) (width 0.25) (layer "F.Cu")'
        '  (net "A") (uuid "aaa")))'
    )
    p = tmp_path / "seg.kicad_pcb"
    p.write_text(raw, encoding="utf-8")
    return p


def test_write_board_strips_segments(tmp_path):
    """strip_segments=True removes existing tracks from the output."""
    src = _board_with_segment(tmp_path)
    board = pcb.load_board(src)
    assert len(board.segments) == 1

    out = tmp_path / "out.kicad_pcb"
    pcb.write_board(board, out, strip_segments=True)

    reloaded = pcb.load_board(out)
    assert len(reloaded.segments) == 0


def test_write_board_preserves_segments_by_default(tmp_path):
    """strip_segments defaults to False — existing tracks are kept."""
    src = _board_with_segment(tmp_path)
    board = pcb.load_board(src)

    out = tmp_path / "out.kicad_pcb"
    pcb.write_board(board, out, strip_segments=False)

    reloaded = pcb.load_board(out)
    assert len(reloaded.segments) == 1


# --- stackup parsing ---------------------------------------------------------

TEST4_PCB = REPO / "TestProjects" / "Test4" / "Test4_routed.kicad_pcb"


@pytest.mark.skipif(not TEST4_PCB.exists(), reason="Test4 board not present")
def test_stackup_parsed_from_test4():
    """Test4_routed.kicad_pcb has a stackup block; values must be parsed correctly."""
    board = pcb.load_board(TEST4_PCB)
    su = board.stackup
    assert su.epsilon_r == pytest.approx(4.5, rel=1e-3)
    assert su.copper_thickness == pytest.approx(0.035, rel=1e-3)
    # dielectric thickness from the file is 1.51 mm
    assert su.dielectric_h == pytest.approx(1.51, rel=1e-2)


def test_stackup_defaults_when_no_stackup():
    """A board without a stackup block gets FR4 defaults."""
    from pyautoroute.pcb import Stackup
    defaults = Stackup()
    assert defaults.epsilon_r == pytest.approx(4.5)
    assert defaults.copper_thickness == pytest.approx(0.035)
    assert defaults.dielectric_h == pytest.approx(1.6)
    # The Test1 board has no stackup → should fall back to defaults
    board = pcb.load_board(PCB)
    assert board.stackup.epsilon_r == pytest.approx(4.5)


# --- group gr_text sync tests -------------------------------------------------

_GROUP_TEXT_BOARD = (
    '(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal)'
    '  (5 "F.SilkS" user "F.Silkscreen"))'
    ' (footprint "Lib:A" (layer "F.Cu") (at 10 20)'
    '  (uuid "aaaa0001-0000-0000-0000-000000000000")'
    '  (property "Reference" "U1")'
    '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "N")))'
    ' (gr_text "Label"'
    '  (at 15 20 0)'
    '  (layer "F.SilkS")'
    '  (uuid "tttt0001-0000-0000-0000-000000000000")'
    '  (effects (font (size 1 1))))'
    ' (group ""'
    '  (uuid "gggg0001-0000-0000-0000-000000000000")'
    '  (members "aaaa0001-0000-0000-0000-000000000000"'
    '           "tttt0001-0000-0000-0000-000000000000")))'
)


def test_sync_tree_moves_grouped_gr_text_with_footprint():
    """gr_text in the same group as a moved footprint follows the footprint translation."""
    board = _board_from_text(_GROUP_TEXT_BOARD)
    fp = board.footprints[0]
    # Translate footprint by (+30, +10); text was 5 mm to the right, 0 mm up.
    fp.x = 40.0
    fp.y = 30.0
    # angle unchanged
    from pyautoroute.pcb import sync_tree_from_placement, child, floats
    sync_tree_from_placement(board)

    # Find the gr_text node in the tree.
    from pyautoroute import sexpr as sx
    text_node = next(
        n for n in board.tree
        if isinstance(n, sx.SList) and sx.head_symbol(n) == "gr_text"
    )
    at_vals = floats(child(text_node, "at"))
    # Original text was at (15, 20); footprint moved from (10, 20) to (40, 30),
    # so text should move by (+30, +10) to (45, 30).
    assert math.isclose(at_vals[0], 45.0, abs_tol=1e-6), f"x={at_vals[0]}"
    assert math.isclose(at_vals[1], 30.0, abs_tol=1e-6), f"y={at_vals[1]}"


def test_sync_tree_rotates_grouped_gr_text_with_footprint():
    """gr_text in the same group as a rotated footprint is rotated about the group centroid."""
    board = _board_from_text(_GROUP_TEXT_BOARD)
    fp = board.footprints[0]
    # Rotate footprint by 90°, keep it at the same position.
    fp.angle = 90.0
    from pyautoroute.pcb import sync_tree_from_placement, child, floats
    sync_tree_from_placement(board)

    from pyautoroute import sexpr as sx
    text_node = next(
        n for n in board.tree
        if isinstance(n, sx.SList) and sx.head_symbol(n) == "gr_text"
    )
    at_vals = floats(child(text_node, "at"))
    # Centroid = footprint at (10, 20). Text was at (15, 20), rel offset (5, 0).
    # After 90° KiCad rotation: rotate(5, 0, 90) = (0, -5).
    # New text pos = (10+0, 20-5) = (10, 15).
    assert math.isclose(at_vals[0], 10.0, abs_tol=1e-6), f"x={at_vals[0]}"
    assert math.isclose(at_vals[1], 15.0, abs_tol=1e-6), f"y={at_vals[1]}"
    # Text angle should be updated by 90°.
    assert len(at_vals) >= 3
    assert math.isclose(at_vals[2] % 360.0, 90.0, abs_tol=1e-6), f"angle={at_vals[2]}"


def test_sync_tree_does_not_move_ungrouped_gr_text():
    """gr_text not in any group is untouched by sync_tree_from_placement."""
    # Same board but the text UUID is NOT in the group's member list.
    txt = (
        '(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal)'
        '  (5 "F.SilkS" user "F.Silkscreen"))'
        ' (footprint "Lib:A" (layer "F.Cu") (at 10 20)'
        '  (uuid "aaaa0001-0000-0000-0000-000000000000")'
        '  (property "Reference" "U1")'
        '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "N")))'
        ' (gr_text "Label"'
        '  (at 15 20 0)'
        '  (layer "F.SilkS")'
        '  (uuid "tttt0001-0000-0000-0000-000000000000")'
        '  (effects (font (size 1 1))))'
        ' (group ""'
        '  (uuid "gggg0001-0000-0000-0000-000000000000")'
        '  (members "aaaa0001-0000-0000-0000-000000000000")))'
    )
    board = _board_from_text(txt)
    fp = board.footprints[0]
    fp.x = 40.0
    fp.y = 30.0
    from pyautoroute.pcb import sync_tree_from_placement, child, floats
    sync_tree_from_placement(board)

    from pyautoroute import sexpr as sx
    text_node = next(
        n for n in board.tree
        if isinstance(n, sx.SList) and sx.head_symbol(n) == "gr_text"
    )
    at_vals = floats(child(text_node, "at"))
    # Text not in group — position must be unchanged.
    assert math.isclose(at_vals[0], 15.0, abs_tol=1e-6)
    assert math.isclose(at_vals[1], 20.0, abs_tol=1e-6)


def test_gr_text_group_fps_identifies_grouped_text():
    """gr_text_group_fps returns grouped text UUID mapped to its footprint list."""
    from pyautoroute.pcb import gr_text_group_fps
    board = _board_from_text(_GROUP_TEXT_BOARD)
    result = gr_text_group_fps(board)
    assert "tttt0001-0000-0000-0000-000000000000" in result
    _, fps = result["tttt0001-0000-0000-0000-000000000000"]
    assert len(fps) == 1
    assert fps[0].ref == "U1"


def test_gr_text_group_fps_excludes_ungrouped_text():
    """gr_text_group_fps returns empty when text is not in any group."""
    from pyautoroute.pcb import gr_text_group_fps
    txt = (
        '(kicad_pcb (layers (0 "F.Cu" signal))'
        ' (footprint "Lib:A" (layer "F.Cu") (at 10 20)'
        '  (uuid "aaaa0001-0000-0000-0000-000000000000")'
        '  (property "Reference" "U1")'
        '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "N")))'
        ' (gr_text "Label" (at 15 20) (layer "F.SilkS")'
        '  (uuid "tttt0001-0000-0000-0000-000000000000")'
        '  (effects (font (size 1 1)))))'
    )
    board = _board_from_text(txt)
    assert gr_text_group_fps(board) == {}
