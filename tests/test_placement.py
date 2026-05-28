"""Tests for pyautoroute.placement (footprint auto-placement) and the
placement-related parsing/writer support in pyautoroute.pcb."""

from __future__ import annotations

import math
import tempfile

from shapely.geometry import box

from pyautoroute import netlist, pcb, placement, sexpr
from pyautoroute.pcb import Board, Footprint, OutlineShape, Pad


def _pad(net, w=2.0, h=2.0):
    return Pad(net=net, pad_type="smd", shape="rect", cx=0.0, cy=0.0, w=w, h=h,
               angle=0.0, copper_layers=["F.Cu", "B.Cu"])


def _fp(ref, x, y, pads_local, locked=False, overlap_ok=False, angle=0.0):
    """Build a Footprint at (x, y) from ``pads_local`` = [(px, py, net), ...]."""
    pads, offsets = [], []
    for (px, py, net) in pads_local:
        pads.append(_pad(net))
        offsets.append((px, py, 0.0))
    fp = Footprint(ref=ref, x=x, y=y, angle=angle, locked=locked,
                   overlap_ok=overlap_ok, pads=pads, local_offsets=offsets,
                   at_node=sexpr.SList(), fp_node=sexpr.SList(),
                   x0=x, y0=y, angle0=angle)
    fp.sync_pads()
    return fp


def _board(footprints, size=80):
    pads = [p for fp in footprints for p in fp.pads]
    outline = [OutlineShape("poly", {"pts": [(0, 0), (size, 0), (size, size), (0, size)]})]
    return Board(tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"], pads=pads,
                 free_vias=[], segments=[], zones=[], outline=outline,
                 footprints=footprints)


def _body_box(fp):
    he = lambda p: 0.5 * math.hypot(p.w, p.h)
    return box(min(p.cx - he(p) for p in fp.pads), min(p.cy - he(p) for p in fp.pads),
               max(p.cx + he(p) for p in fp.pads), max(p.cy + he(p) for p in fp.pads))


def _max_body_overlap(footprints):
    worst = 0.0
    boxes = [(_body_box(fp), fp) for fp in footprints]
    for i, (bi, fi) in enumerate(boxes):
        for bj, fj in boxes[i + 1:]:
            if fi.overlap_ok or fj.overlap_ok:
                continue
            if bi.intersects(bj):
                worst = max(worst, bi.intersection(bj).area)
    return worst


# --- sync_pads / model -------------------------------------------------------

def test_sync_pads_applies_translation_and_rotation():
    fp = _fp("U1", 10.0, 20.0, [(3.0, 0.0, "A")])
    assert math.isclose(fp.pads[0].cx, 13.0) and math.isclose(fp.pads[0].cy, 20.0)
    fp.x, fp.y, fp.angle = 0.0, 0.0, 90.0
    fp.sync_pads()
    # local (3,0) rotated +90 -> (0,-3); pad angle picks up the footprint rotation
    assert math.isclose(fp.pads[0].cx, 0.0, abs_tol=1e-9)
    assert math.isclose(fp.pads[0].cy, -3.0, abs_tol=1e-9)
    assert math.isclose(fp.pads[0].angle, 90.0)


# --- placement run -----------------------------------------------------------

def test_place_never_worsens_energy_and_respects_locks():
    fixed = _fp("LOCK", 40.0, 40.0, [(0.0, 0.0, "N0"), (2.0, 0.0, "N1")], locked=True)
    a = _fp("U1", 10.0, 10.0, [(0.0, 0.0, "N0"), (2.0, 0.0, "N2")])
    b = _fp("U2", 70.0, 70.0, [(0.0, 0.0, "N1"), (2.0, 0.0, "N2")])
    board = _board([fixed, a, b])
    res = placement.place(board, placement.PlaceParams(iters=1500, seed=1))
    assert res.best_energy <= res.start_energy + 1e-6
    # locked footprint never moves
    assert not fixed.moved
    assert (fixed.x, fixed.y) == (40.0, 40.0)


def test_place_separates_overlapping_bodies():
    a = _fp("U1", 20.0, 20.0, [(-2.0, 0.0, "N0"), (2.0, 0.0, "N1")])
    b = _fp("U2", 21.0, 20.5, [(-2.0, 0.0, "N0"), (2.0, 0.0, "N1")])  # overlapping a
    board = _board([a, b])
    assert _max_body_overlap([a, b]) > 0.0
    placement.place(board, placement.PlaceParams(iters=4000, seed=2,
                                                 overlap_weight=200.0))
    assert _max_body_overlap([a, b]) < 1e-6


def _min_body_gap(footprints):
    """Smallest gap (mm) between any two non-overlap_ok footprint body boxes;
    negative if they overlap."""
    worst = math.inf
    boxes = [(_body_box(fp), fp) for fp in footprints]
    for i, (bi, fi) in enumerate(boxes):
        for bj, fj in boxes[i + 1:]:
            if fi.overlap_ok or fj.overlap_ok:
                continue
            worst = min(worst, bi.distance(bj) if not bi.intersects(bj)
                        else -bi.intersection(bj).area)
    return worst


def test_place_buffer_keeps_footprints_apart():
    # two parts that start overlapping; with a buffer they must end up separated
    # by at least (close to) the buffer, not merely touching.
    a = _fp("U1", 20.0, 20.0, [(-2.0, 0.0, "N0"), (2.0, 0.0, "N1")])
    b = _fp("U2", 21.0, 20.5, [(-2.0, 0.0, "N0"), (2.0, 0.0, "N1")])
    board = _board([a, b])
    buffer = 1.5
    placement.place(board, placement.PlaceParams(
        iters=6000, seed=4, overlap_weight=200.0, buffer=buffer))
    # body boxes (un-inflated) are kept apart by ~buffer (allow a little slack
    # from the discrete annealing steps)
    assert _min_body_gap([a, b]) >= buffer - 0.3


def test_overlap_ok_allows_body_overlap_but_not_pad_overlap():
    # A shield (overlap_ok) over a board footprint, sharing no nets so the only
    # forces are overlap + compactness.
    base = _fp("BRD", 30.0, 30.0,
               [(-8.0, 0.0, "A"), (8.0, 0.0, "B")], locked=True)
    shield = _fp("SHIELD", 30.0, 30.0,
                 [(-8.0, 6.0, "C"), (8.0, 6.0, "D")], overlap_ok=True)
    board = _board([base, shield])
    placement.place(board, placement.PlaceParams(iters=3000, seed=3,
                                                 overlap_weight=200.0))
    # bodies may overlap (compactness pulls them together) ...
    body = _body_box(base).intersection(_body_box(shield)).area
    # ... but the pads must be kept apart
    he = lambda p: 0.5 * math.hypot(p.w, p.h)
    pad_overlap = 0.0
    for pa in base.pads:
        for pb in shield.pads:
            ba = box(pa.cx - he(pa), pa.cy - he(pa), pa.cx + he(pa), pa.cy + he(pa))
            bb = box(pb.cx - he(pb), pb.cy - he(pb), pb.cx + he(pb), pb.cy + he(pb))
            if ba.intersects(bb):
                pad_overlap += ba.intersection(bb).area
    assert pad_overlap < 1e-6
    assert body >= 0.0  # allowed; no assertion that it must be zero


def test_apply_placement_outline_encloses_pads_with_margin():
    a = _fp("U1", 10.0, 10.0, [(0.0, 0.0, "N0")])
    b = _fp("U2", 50.0, 40.0, [(0.0, 0.0, "N0")])
    board = _board([a, b])
    pcb.apply_placement(board, margin=3.0)
    rect = next(s for s in board.outline if s.kind == "rect")
    (x0, y0), (x1, y1) = rect.data["start"], rect.data["end"]
    for p in board.pads:
        assert x0 <= p.cx <= x1 and y0 <= p.cy <= y1
    # margin keeps the edge clear of the nearest pad by at least `margin`
    assert min(p.cx for p in board.pads) - x0 >= 3.0 - 1e-6


def test_place_rotate_none_keeps_angles():
    a = _fp("U1", 20.0, 20.0, [(-2.0, 0.0, "N0"), (2.0, 0.0, "N1")])
    b = _fp("U2", 40.0, 40.0, [(-2.0, 0.0, "N0"), (2.0, 0.0, "N1")])
    board = _board([a, b])
    placement.place(board, placement.PlaceParams(iters=2000, seed=5,
                                                 rotate_mode="none"))
    assert a.angle == 0.0 and b.angle == 0.0


def test_place_reports_acceptance_and_breakdown():
    a = _fp("U1", 20.0, 20.0, [(-2.0, 0.0, "N0"), (2.0, 0.0, "N1")])
    b = _fp("U2", 40.0, 40.0, [(-2.0, 0.0, "N0"), (2.0, 0.0, "N1")])
    board = _board([a, b])
    seen = []
    res = placement.place(board, placement.PlaceParams(iters=500, seed=6),
                          on_progress=lambda *a: seen.append(a))
    assert 0.0 <= res.accept_ratio <= 1.0
    assert res.iterations == 500
    # progress callback now carries the live acceptance fraction as a 6th arg
    assert seen and len(seen[-1]) == 6 and 0.0 <= seen[-1][5] <= 1.0
    # energy breakdown is populated and roughly reconstitutes the best energy
    p = placement.PlaceParams()
    recon = (res.final_ratsnest + p.overlap_weight * res.final_overlap
             + p.compact_weight * res.final_bbox)
    assert math.isclose(recon, res.best_energy, rel_tol=1e-6)


def test_place_best_of_n_never_worse_than_first_run():
    def board():
        a = _fp("U1", 20.0, 20.0, [(-2.0, 0.0, "N0"), (2.0, 0.0, "N1")])
        b = _fp("U2", 21.0, 20.5, [(-2.0, 0.0, "N0"), (2.0, 0.0, "N1")])
        c = _fp("U3", 50.0, 50.0, [(-2.0, 0.0, "N1"), (2.0, 0.0, "N0")])
        return _board([a, b, c]), [a, b, c]

    b1, _ = board()
    first = placement.place(b1, placement.PlaceParams(iters=800, seed=0), runs=1)
    b3, _ = board()
    best = placement.place(b3, placement.PlaceParams(iters=800, seed=0), runs=3)
    # best-of-3 (seeds 0,1,2) includes run 0, so it can never be worse
    assert best.best_energy <= first.best_energy + 1e-6


def test_place_runs_deterministic():
    def run():
        a = _fp("U1", 20.0, 20.0, [(-2.0, 0.0, "N0")])
        b = _fp("U2", 40.0, 40.0, [(-2.0, 0.0, "N0")])
        board = _board([a, b])
        res = placement.place(board, placement.PlaceParams(iters=400, seed=0), runs=3)
        return res.best_energy, round(sum(p.cx + p.cy for p in board.pads), 6)
    assert run() == run()


def test_place_custom_temps_and_step_run():
    a = _fp("U1", 20.0, 20.0, [(-2.0, 0.0, "N0")])
    b = _fp("U2", 40.0, 40.0, [(-2.0, 0.0, "N0")])
    board = _board([a, b])
    res = placement.place(board, placement.PlaceParams(
        iters=300, seed=7, t_start=2.0, t_end=0.1, step=5.0))
    assert res.best_energy <= res.start_energy + 1e-6


def test_place_no_movable_footprints_is_noop():
    fixed = _fp("LOCK", 10.0, 10.0, [(0.0, 0.0, "N0")], locked=True)
    board = _board([fixed])
    res = placement.place(board, placement.PlaceParams(iters=100))
    assert res.moved == 0 and res.iterations == 0


# --- pcb parsing of lock / overlap property ----------------------------------

def _board_from_text(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".kicad_pcb", delete=False)
    f.write(text)
    f.close()
    return pcb.load_board(f.name)


def test_parse_lock_and_overlap_property():
    text = (
        '(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal))'
        ' (footprint "x" (at 10 20 90) (locked yes)'
        '  (property "Reference" "U1")'
        '  (property "Autoroute" "overlap")'
        '  (pad "1" smd rect (at 1 0 90) (size 1 1) (layers "F.Cu") (net "A")))'
        ' (footprint "y" locked (at 5 5)'
        '  (property "Reference" "U2")'
        '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "B")))'
        ' (footprint "z" (at 0 0)'
        '  (property "Reference" "U3")'
        '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "C"))))'
    )
    board = _board_from_text(text)
    fps = {fp.ref: fp for fp in board.footprints}
    assert fps["U1"].locked and fps["U1"].overlap_ok
    assert math.isclose(fps["U1"].angle, 90.0)
    # local offset stored relative to footprint rotation (pad at-angle - fp angle)
    assert math.isclose(fps["U1"].local_offsets[0][2], 0.0, abs_tol=1e-9)
    assert fps["U2"].locked and not fps["U2"].overlap_ok       # bare `locked` atom
    assert not fps["U3"].locked and not fps["U3"].overlap_ok


# --- pcb tree rewrite (round-trip) -------------------------------------------

def test_sync_tree_propagates_rotation_into_pad_angles(tmp_path):
    # A footprint with a rectangular (3x1) pad, rotated 90 deg. KiCad stores pad
    # angles absolutely, so the written board must give the pad an absolute angle
    # of 90 — otherwise the rectangle reloads in its old orientation and fails DRC.
    text = (
        '(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal))'
        ' (footprint "x" (at 20 20 0)'
        '  (property "Reference" "U1")'
        '  (pad "1" smd rect (at 2 0 0) (size 3 1) (layers "F.Cu") (net "A"))))'
    )
    board = _board_from_text(text)
    u1 = next(fp for fp in board.footprints if fp.ref == "U1")
    u1.angle = 90.0
    u1.sync_pads()
    pcb.apply_placement(board, margin=1.0)
    pcb.sync_tree_from_placement(board)
    out = tmp_path / "rot.kicad_pcb"
    pcb.write_board(board, out, new_nodes=None, strip_free_vias=False)

    reloaded = pcb.load_board(out)
    pad = next(fp for fp in reloaded.footprints if fp.ref == "U1").pads[0]
    assert math.isclose(pad.angle % 360.0, 90.0)
    # the reloaded pad polygon is the 3x1 rect turned on its side: ~1 wide, ~3 tall
    from pyautoroute.geometry import pad_polygon
    x0, y0, x1, y1 = pad_polygon(pad).bounds
    assert math.isclose(x1 - x0, 1.0, abs_tol=1e-6)
    assert math.isclose(y1 - y0, 3.0, abs_tol=1e-6)


def test_sync_tree_rewrites_at_and_regenerates_edge_cuts(tmp_path):
    text = (
        '(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal))'
        ' (gr_line (start 0 0) (end 40 0) (stroke (width 0.05) (type solid))'
        '  (layer "Edge.Cuts") (uuid "11111111-1111-1111-1111-111111111111"))'
        ' (footprint "x" (at 10 20 0)'
        '  (property "Reference" "U1")'
        '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "A")))'
        ' (footprint "y" (at 30 30 0)'
        '  (property "Reference" "U2")'
        '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "A"))))'
    )
    board = _board_from_text(text)
    u1 = next(fp for fp in board.footprints if fp.ref == "U1")
    u1.x, u1.y = 12.5, 22.5
    pcb.apply_placement(board, margin=1.0)
    pcb.sync_tree_from_placement(board)
    out = tmp_path / "moved.kicad_pcb"
    pcb.write_board(board, out, new_nodes=None, strip_free_vias=False)

    reloaded = pcb.load_board(out)
    r1 = next(fp for fp in reloaded.footprints if fp.ref == "U1")
    assert math.isclose(r1.x, 12.5) and math.isclose(r1.y, 22.5)
    assert math.isclose(r1.pads[0].cx, 12.5) and math.isclose(r1.pads[0].cy, 22.5)
    # the old Edge.Cuts gr_line is gone, replaced by a single gr_rect
    text_out = out.read_text()
    assert "gr_line" not in text_out
    assert "gr_rect" in text_out
    rects = [s for s in reloaded.outline if s.kind == "rect"]
    assert len(rects) == 1


# --- silkscreen text extent in body box --------------------------------------

def test_fp_silk_text_extents_parsed_footprint():
    """_fp_silk_text_extents returns one entry per visible silk label."""
    text = (
        '(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal)'
        '  (5 "F.SilkS" user "F.Silkscreen"))'
        ' (footprint "Lib:R" (layer "F.Cu") (at 0 0 0)'
        '  (property "Reference" "R1" (at 0 -2 0) (layer "F.SilkS")'
        '   (effects (font (size 1.5 1.5))))'
        '  (property "Value" "10K" (at 0 2 0) (layer "F.SilkS")'
        '   (effects (font (size 1.5 1.5))))'
        '  (property "Datasheet" "~" (at 0 4 0) (layer "F.Fab")'
        '   (effects (font (size 1 1))))'
        '  (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net "A"))))'
    )
    board = _board_from_text(text)
    fp = board.footprints[0]
    from pyautoroute.placement import _fp_silk_text_extents
    extents = _fp_silk_text_extents(fp)
    # Reference + Value are on F.SilkS; Datasheet is on F.Fab → ignored
    assert len(extents) == 2
    # Each extent is (local_x, local_y, half_diag) with half_diag > 0
    for lx, ly, hr in extents:
        assert hr > 0.0


def test_fp_box_grows_to_include_silk_text():
    """_fp_box must be larger when silkscreen text extends beyond the pad area."""
    text = (
        '(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal)'
        '  (5 "F.SilkS" user "F.Silkscreen"))'
        ' (footprint "Lib:R" (layer "F.Cu") (at 10 10 0)'
        '  (property "Reference" "R1" (at 0 -6 0) (layer "F.SilkS")'
        '   (effects (font (size 1 1))))'
        '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "A"))))'
    )
    board = _board_from_text(text)
    fp = board.footprints[0]

    from pyautoroute.placement import _Placer, PlaceParams
    placer = _Placer(board, PlaceParams(iters=1, seed=0))

    b = placer._fp_box(fp)
    # The Reference label is at local (0, -6) — which is board (10, 4) after
    # translation; its half_diag > 0, so the box must reach below y=4.
    assert b.bounds[1] < 4.0   # miny well below the text centre
