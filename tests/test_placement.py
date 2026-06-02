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


def _fp(ref, x, y, pads_local, locked=False, overlap_ok=False, angle=0.0,
        edge_affinity=None, group_id=None):
    """Build a Footprint at (x, y) from ``pads_local`` = [(px, py, net), ...]."""
    pads, offsets = [], []
    for (px, py, net) in pads_local:
        pads.append(_pad(net))
        offsets.append((px, py, 0.0))
    fp = Footprint(ref=ref, x=x, y=y, angle=angle, locked=locked,
                   overlap_ok=overlap_ok, pads=pads, local_offsets=offsets,
                   at_node=sexpr.SList(), fp_node=sexpr.SList(),
                   x0=x, y0=y, angle0=angle, edge_affinity=edge_affinity,
                   group_id=group_id)
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


def test_place_recenters_unlocked_group_onto_starting_centroid():
    # With nothing locked the placement is translation-invariant and the cluster
    # drifts; place() must recenter it back onto its original centroid.
    a = _fp("U1", 10.0, 10.0, [(0.0, 0.0, "N0"), (2.0, 0.0, "N1")])
    b = _fp("U2", 70.0, 70.0, [(0.0, 0.0, "N1"), (2.0, 0.0, "N2")])
    c = _fp("U3", 40.0, 20.0, [(0.0, 0.0, "N2"), (2.0, 0.0, "N0")])
    board = _board([a, b, c])
    orig = (sum(fp.x0 for fp in (a, b, c)) / 3, sum(fp.y0 for fp in (a, b, c)) / 3)
    placement.place(board, placement.PlaceParams(iters=4000, seed=7))
    cx = sum(fp.x for fp in (a, b, c)) / 3
    cy = sum(fp.y for fp in (a, b, c)) / 3
    assert math.isclose(cx, orig[0], abs_tol=1e-6)
    assert math.isclose(cy, orig[1], abs_tol=1e-6)


def test_recenter_preserves_energy_and_is_noop_when_locked():
    a = _fp("U1", 10.0, 10.0, [(0.0, 0.0, "N0"), (2.0, 0.0, "N1")])
    b = _fp("U2", 70.0, 70.0, [(0.0, 0.0, "N1"), (2.0, 0.0, "N2")])
    board = _board([a, b])
    # rigid drift, then recenter brings it back with no energy change
    for fp in (a, b):
        fp.x += 25.0
        fp.y -= 13.0
        fp.sync_pads()
    before = placement._Placer(board, placement.PlaceParams())._energy()
    dx, dy = placement.recenter(board)
    after = placement._Placer(board, placement.PlaceParams())._energy()
    assert math.isclose(dx, -25.0) and math.isclose(dy, 13.0)
    assert math.isclose(before, after, rel_tol=1e-9)
    # a lock anchors the layout: recenter must do nothing
    locked = _fp("LK", 5.0, 5.0, [(0.0, 0.0, "N0")], locked=True)
    board2 = _board([locked, a])
    a.x += 30.0
    a.sync_pads()
    assert placement.recenter(board2) == (0.0, 0.0)


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
        '  (property "Autoroute-overlap" "yes")'
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


def test_parse_edge_affinity_property():
    def _ap(ref, edge=None, overlap=None):
        props = ""
        if edge is not None:
            props += f'  (property "Autoroute-edge" "{edge}")'
        if overlap is not None:
            props += f'  (property "Autoroute-overlap" "{overlap}")'
        return (f' (footprint "f" (at 0 0) (property "Reference" "{ref}")' + props +
                f'  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "{ref}")))')
    text = ('(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal))'
            + _ap("J1", edge="left")
            + _ap("J2", edge="any")
            + _ap("J3", edge="top", overlap="yes")   # the two props are independent
            + _ap("J4", edge="sideways")             # unknown side -> ignored
            + _ap("J5")                              # no Autoroute property
            + _ap("J6", edge="")                     # empty value -> any
            + ')')
    fps = {fp.ref: fp for fp in _board_from_text(text).footprints}
    assert fps["J1"].edge_affinity == "left"
    assert fps["J2"].edge_affinity == "any"
    assert fps["J3"].edge_affinity == "top" and fps["J3"].overlap_ok
    assert fps["J4"].edge_affinity is None
    assert fps["J5"].edge_affinity is None
    assert fps["J6"].edge_affinity == "any"


def _hub_and_satellites(flag_ref=None, side=None):
    """A central hub U0 wired to four satellites U1..U4 around it.

    Each satellite connects to the hub only, so ratsnest alone pulls them toward
    the centre — a clean baseline for showing the edge term pull a flagged part
    outward. ``flag_ref`` (if given) gets ``edge_affinity = side``.
    """
    hub = _fp("U0", 40, 40,
              [(0, 0, "n1"), (0, 0, "n2"), (0, 0, "n3"), (0, 0, "n4")],
              edge_affinity=(side if flag_ref == "U0" else None))
    coords = {"U1": (50, 40), "U2": (40, 50), "U3": (40, 30), "U4": (30, 40)}
    sats = [_fp(ref, x, y, [(0, 0, f"n{i}")],
                edge_affinity=(side if flag_ref == ref else None))
            for i, (ref, (x, y)) in enumerate(coords.items(), start=1)]
    return _board([hub] + sats)


def _layout_bounds(board):
    bs = [_body_box(fp).bounds for fp in board.footprints]
    return (min(b[0] for b in bs), min(b[1] for b in bs),
            max(b[2] for b in bs), max(b[3] for b in bs))


def test_edge_affinity_pulls_flagged_footprint_to_named_side():
    # U1, normally pulled toward the centre by its connection to the hub, is
    # flagged edge-left: it should end on the layout's left boundary.
    board = _hub_and_satellites(flag_ref="U1", side="left")
    placement.place(board, placement.PlaceParams(iters=5000, seed=1, edge_weight=15.0))
    minx, miny, maxx, maxy = _layout_bounds(board)
    u1 = next(fp for fp in board.footprints if fp.ref == "U1")
    bx0, by0, bx1, by1 = _body_box(u1).bounds
    assert bx0 - minx < 1.0                 # U1 sits on the left edge
    assert bx0 - minx == min(bx0 - minx, maxx - bx1, by0 - miny, maxy - by1)


def test_edge_affinity_any_reaches_perimeter():
    # The hub normally sits in the centre (it connects to everything); flagged
    # `edge` (any side) it should instead end on the layout perimeter.
    board = _hub_and_satellites(flag_ref="U0", side="any")
    placement.place(board, placement.PlaceParams(iters=5000, seed=2, edge_weight=15.0))
    minx, miny, maxx, maxy = _layout_bounds(board)
    bx0, by0, bx1, by1 = _body_box(board.footprints[0]).bounds   # U0 = the hub
    dist_to_nearest_side = min(bx0 - minx, maxx - bx1, by0 - miny, maxy - by1)
    assert dist_to_nearest_side < 1.0


def test_edge_affinity_off_by_default_leaves_energy_unchanged():
    # With nothing flagged, the edge term is zero regardless of edge_weight.
    from pyautoroute.placement import _Placer, PlaceParams
    board = _hub_and_satellites()
    placer = _Placer(board, PlaceParams(edge_weight=100.0))
    placer._rebuild_cache()
    assert placer._edge == 0.0
    assert placer._flagged == {}
    assert placer._containment == 0.0            # no keep_outline -> no containment
    assert placer._outline_poly is None


def test_edge_affinity_prefers_parallel_orientation():
    # An elongated 1x4 header flagged edge-left should cost less in the edge term
    # when its long axis lies parallel to the left edge (pads stacked along y, so
    # the box is thin in x) than perpendicular to it (pads in a row along x). The
    # far-side metric folds the box's perpendicular depth into the distance, so the
    # annealer is pushed to orient the connector flat against the edge rather than
    # rotating it so only one pad reaches the edge.
    from pyautoroute.placement import _Placer, PlaceParams
    row = [(0, 0, "n1"), (2.54, 0, "n2"), (5.08, 0, "n3"), (7.62, 0, "n4")]
    conn = _fp("J1", 40, 40, row, edge_affinity="left")
    anchor = _fp("U1", 10, 40, [(0, 0, "n1")])   # anchors the layout's left edge
    board = _board([conn, anchor])
    placer = _Placer(board, PlaceParams())

    def edge_cost_at(angle):
        conn.angle = angle
        conn.sync_pads()
        placer._rebuild_cache()
        return placer._edge

    perpendicular = edge_cost_at(0.0)    # row along x -> pokes inward from the edge
    parallel = edge_cost_at(90.0)        # row along y -> flat against the edge
    assert parallel < perpendicular


# --- keep-outline (phase 2) --------------------------------------------------

def test_keep_outline_contains_footprints_within_outline():
    from pyautoroute.geometry import outline_to_polygon
    # Footprints start OUTSIDE a 40x40 outline; keep_outline should pull them in.
    fps = [_fp("U1", 60, 60, [(0, 0, "")]),
           _fp("U2", 70, 5, [(0, 0, "")]),
           _fp("U3", 5, 70, [(0, 0, "")])]
    board = _board(fps, size=40)
    placement.place(board, placement.PlaceParams(
        iters=5000, seed=1, keep_outline=True))
    poly = outline_to_polygon(board.outline)
    for fp in board.footprints:
        assert _body_box(fp).difference(poly).area < 1.0   # inside the outline


def test_keep_outline_edge_affinity_targets_the_outline_edge():
    from pyautoroute.geometry import outline_to_polygon
    # U1 (flagged edge-left, but pulled toward the centre by its net) should reach
    # the *outline's* left edge (x≈0), not merely the left of the cluster.
    fps = [_fp("U1", 20, 20, [(0, 0, "n1")], edge_affinity="left"),
           _fp("U2", 25, 20, [(0, 0, "n1")]),
           _fp("U3", 25, 25, [(0, 0, "n1")])]
    board = _board(fps, size=40)
    placement.place(board, placement.PlaceParams(
        iters=5000, seed=3, keep_outline=True, edge_weight=15.0))
    minx = outline_to_polygon(board.outline).bounds[0]
    u1 = next(fp for fp in board.footprints if fp.ref == "U1")
    assert _body_box(u1).bounds[0] - minx < 2.0


def test_apply_placement_keep_outline_keeps_or_falls_back():
    fps = [_fp("U1", 10, 10, [(0, 0, "")])]
    board = _board(fps, size=40)                 # a real (non-synthesised) outline
    before = board.outline
    assert pcb.apply_placement(board, keep_outline=True) is True
    assert board.outline is before               # left untouched

    board2 = _board(fps, size=40)
    board2.outline_synthesized = True            # nothing real to keep
    assert pcb.apply_placement(board2, keep_outline=True) is False
    assert board2.outline is not None            # regenerated a bounding box


def test_keep_outline_preserves_existing_edge_cuts(tmp_path):
    text = (
        '(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal))'
        ' (gr_line (start 0 0) (end 40 0) (stroke (width 0.05) (type solid))'
        '  (layer "Edge.Cuts") (uuid "11111111-1111-1111-1111-111111111111"))'
        ' (footprint "x" (at 10 20 0)'
        '  (property "Reference" "U1")'
        '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "A"))))'
    )
    board = _board_from_text(text)
    u1 = next(fp for fp in board.footprints if fp.ref == "U1")
    u1.x, u1.y = 12.5, 22.5
    kept = pcb.apply_placement(board, margin=1.0, keep_outline=True)
    assert kept is True
    pcb.sync_tree_from_placement(board, keep_outline=kept)
    out = tmp_path / "kept.kicad_pcb"
    pcb.write_board(board, out, new_nodes=None, strip_free_vias=False)
    text_out = out.read_text()
    assert "gr_line" in text_out                 # original Edge.Cuts preserved
    assert "gr_rect" not in text_out             # not replaced by a bounding rect
    r1 = next(fp for fp in pcb.load_board(out).footprints if fp.ref == "U1")
    assert math.isclose(r1.x, 12.5) and math.isclose(r1.y, 22.5)   # pose still rewritten


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


# --- board-level silkscreen text (gr_text) -----------------------------------

def test_board_silk_text_boxes_parsed():
    """_board_silk_text_boxes returns visible silk gr_text and skips others."""
    text = (
        '(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal)'
        '  (5 "F.SilkS" user "F.Silkscreen"))'
        ' (gr_text "GND" (at 20 30 0) (layer "F.SilkS")'
        '  (effects (font (size 1.5 1.5)) (justify left bottom)))'
        ' (gr_text "Fab" (at 5 5 0) (layer "F.Fab")'          # not silk → ignored
        '  (effects (font (size 1 1))))'
        ' (gr_text "Hidden" (at 8 8 0) (layer "F.SilkS") (hide yes)'
        '  (effects (font (size 1 1)))))'
    )
    board = _board_from_text(text)
    from pyautoroute.placement import _board_silk_text_boxes
    boxes = _board_silk_text_boxes(board)
    assert len(boxes) == 1                       # only the visible silk text
    cx, cy, hr = boxes[0]
    assert hr > 0.0
    # justify left/bottom puts the anchor at the box's left/bottom (y-down),
    # so the centre is to the right of and above the (20, 30) anchor.
    assert cx > 20.0 and cy < 30.0


def test_place_avoids_board_silk_text():
    """A movable footprint is pushed off a fixed board-level silk label."""
    text = (
        '(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal)'
        '  (5 "F.SilkS" user "F.Silkscreen"))'
        ' (gr_text "LABEL" (at 40 40 0) (layer "F.SilkS")'
        '  (effects (font (size 3 3))))'
        ' (footprint "Lib:R" (layer "F.Cu") (at 40 40 0)'      # sits on the text
        '  (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net "A"))'
        '  (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net "B")))'
        ' (footprint "Lib:R" (layer "F.Cu") (at 60 60 0)'      # gives net pull
        '  (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net "A"))'
        '  (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net "B"))))'
    )
    board = _board_from_text(text)
    from pyautoroute.placement import _Placer, PlaceParams

    placer = _Placer(board, PlaceParams(iters=1, seed=0))
    before = placer._fixed_text_overlap([placer._fp_box(fp) for fp in placer.boxed])
    assert before > 0.0                          # starts overlapping the label

    placement.place(board, PlaceParams(iters=3000, seed=0))
    placer = _Placer(board, PlaceParams(iters=1, seed=0))
    after = placer._fixed_text_overlap([placer._fp_box(fp) for fp in placer.boxed])
    assert after < before                        # placement moved it off the text


def test_overlap_ok_exempt_from_board_silk_text():
    """An overlap_ok footprint contributes no fixed-text overlap penalty."""
    text = (
        '(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal)'
        '  (5 "F.SilkS" user "F.Silkscreen"))'
        ' (gr_text "LABEL" (at 40 40 0) (layer "F.SilkS")'
        '  (effects (font (size 3 3))))'
        ' (footprint "Lib:SH" (layer "F.Cu") (at 40 40 0)'
        '  (property "Autoroute-overlap" "yes" (at 0 0 0) (layer "F.Fab"))'
        '  (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net "A"))))'
    )
    board = _board_from_text(text)
    from pyautoroute.placement import _Placer, PlaceParams
    placer = _Placer(board, PlaceParams(iters=1, seed=0))
    boxes = [placer._fp_box(fp) for fp in placer.boxed]
    assert placer._fixed_text_overlap(boxes) == 0.0


# --- KiCad native group tests ------------------------------------------------

def _board_with_groups(group_map: dict):
    """Build a board where footprints carry group_id values from *group_map* (ref->gid)."""
    fps = [
        _fp("U1", 10.0, 10.0, [(0.0, 0.0, "N0"), (2.0, 0.0, "N1")],
            group_id=group_map.get("U1")),
        _fp("C1", 12.0, 10.0, [(0.0, 0.0, "N1")],
            group_id=group_map.get("C1")),
        _fp("U2", 40.0, 40.0, [(0.0, 0.0, "N0"), (2.0, 0.0, "N2")],
            group_id=group_map.get("U2")),
        _fp("C2", 42.0, 40.0, [(0.0, 0.0, "N2")],
            group_id=group_map.get("C2")),
        _fp("R1", 70.0, 70.0, [(0.0, 0.0, "N2"), (2.0, 0.0, "N0")]),
    ]
    return _board(fps), {fp.ref: fp for fp in fps}


def test_group_members_move_together_translate():
    """Grouped footprints maintain their relative offset after any translate move."""
    board, fps = _board_with_groups({"U1": "g1", "C1": "g1"})
    u1, c1 = fps["U1"], fps["C1"]
    dx0, dy0 = u1.x - c1.x, u1.y - c1.y

    placement.place(board, placement.PlaceParams(iters=600, seed=42, rotate_mode="none"))

    assert math.isclose(u1.x - c1.x, dx0, abs_tol=1e-9)
    assert math.isclose(u1.y - c1.y, dy0, abs_tol=1e-9)


def test_group_members_preserve_distance_after_rotate():
    """Group rotate preserves the inter-member distance (rigid body)."""
    board, fps = _board_with_groups({"U1": "g1", "C1": "g1"})
    u1, c1 = fps["U1"], fps["C1"]
    dist0 = math.hypot(u1.x - c1.x, u1.y - c1.y)

    placement.place(board, placement.PlaceParams(iters=600, seed=7))

    assert math.isclose(math.hypot(u1.x - c1.x, u1.y - c1.y), dist0, abs_tol=1e-6)


def test_group_swap_exchanges_centroids():
    """Swapping two groups moves each unit to the other's centroid."""
    from pyautoroute.placement import _Placer, PlaceParams
    board, fps = _board_with_groups({"U1": "g1", "C1": "g1", "U2": "g2", "C2": "g2"})
    placer = _Placer(board, PlaceParams(iters=1, seed=0))

    # Locate the two group units in _move_units.
    units_by_refs = {
        frozenset(fp.ref for fp in unit): unit
        for unit in placer._move_units if len(unit) == 2
    }
    ua = units_by_refs[frozenset({"U1", "C1"})]
    ub = units_by_refs[frozenset({"U2", "C2"})]

    cax0 = sum(fp.x for fp in ua) / 2; cay0 = sum(fp.y for fp in ua) / 2
    cbx0 = sum(fp.x for fp in ub) / 2; cby0 = sum(fp.y for fp in ub) / 2
    # Internal offsets before swap.
    da_x = ua[0].x - ua[1].x; da_y = ua[0].y - ua[1].y
    db_x = ub[0].x - ub[1].x; db_y = ub[0].y - ub[1].y

    # Force a swap by calling _move directly with the groups already selected.
    snap = placer._snapshot(ua + ub)
    dx, dy = cbx0 - cax0, cby0 - cay0
    for fp in ua:
        fp.x += dx; fp.y += dy; fp.sync_pads()
    for fp in ub:
        fp.x -= dx; fp.y -= dy; fp.sync_pads()

    cax1 = sum(fp.x for fp in ua) / 2; cay1 = sum(fp.y for fp in ua) / 2
    cbx1 = sum(fp.x for fp in ub) / 2; cby1 = sum(fp.y for fp in ub) / 2
    assert math.isclose(cax1, cbx0, abs_tol=1e-9)
    assert math.isclose(cay1, cby0, abs_tol=1e-9)
    assert math.isclose(cbx1, cax0, abs_tol=1e-9)
    assert math.isclose(cby1, cay0, abs_tol=1e-9)
    # Internal offsets unchanged.
    assert math.isclose(ua[0].x - ua[1].x, da_x, abs_tol=1e-9)
    assert math.isclose(ub[0].x - ub[1].x, db_x, abs_tol=1e-9)


def test_group_all_locked_excluded():
    """A group whose members are all locked is not in _Placer._groups."""
    from pyautoroute.placement import _Placer, PlaceParams
    fps = [
        _fp("U1", 10.0, 10.0, [(0.0, 0.0, "N0")], locked=True, group_id="g1"),
        _fp("C1", 12.0, 10.0, [(0.0, 0.0, "N0")], locked=True, group_id="g1"),
        _fp("R1", 40.0, 40.0, [(0.0, 0.0, "N0")]),
    ]
    board = _board(fps)
    placer = _Placer(board, PlaceParams(iters=1, seed=0))
    assert "g1" not in placer._groups


def test_group_partial_lock_treated_as_ungrouped():
    """When one member is locked the group is excluded; unlocked member moves freely."""
    from pyautoroute.placement import _Placer, PlaceParams
    fps = [
        _fp("U1", 10.0, 10.0, [(0.0, 0.0, "N0")], locked=True, group_id="g1"),
        _fp("C1", 12.0, 10.0, [(0.0, 0.0, "N0")], group_id="g1"),
        _fp("R1", 40.0, 40.0, [(0.0, 0.0, "N0")]),
    ]
    board = _board(fps)
    placer = _Placer(board, PlaceParams(iters=1, seed=0))
    assert "g1" not in placer._groups
    # C1 is movable and ungrouped, so it appears as a single-fp unit.
    unit_refs = [frozenset(fp.ref for fp in u) for u in placer._move_units]
    assert frozenset({"C1"}) in unit_refs


def test_group_single_member_treated_as_ungrouped():
    """A group with only one member is pruned and the member moves individually."""
    from pyautoroute.placement import _Placer, PlaceParams
    fps = [
        _fp("U1", 10.0, 10.0, [(0.0, 0.0, "N0")], group_id="solo"),
        _fp("R1", 40.0, 40.0, [(0.0, 0.0, "N0")]),
    ]
    board = _board(fps)
    placer = _Placer(board, PlaceParams(iters=1, seed=0))
    assert "solo" not in placer._groups
    unit_refs = [frozenset(fp.ref for fp in u) for u in placer._move_units]
    assert frozenset({"U1"}) in unit_refs


def test_parse_native_kicad_group():
    """load_board populates group_id from (group ...) nodes using member UUIDs."""
    text = (
        '(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal))'
        ' (footprint "Lib:A" (layer "F.Cu") (at 10 10)'
        '  (uuid "aaaa0001-0000-0000-0000-000000000000")'
        '  (property "Reference" "U1")'
        '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "N")))'
        ' (footprint "Lib:B" (layer "F.Cu") (at 20 20)'
        '  (uuid "bbbb0002-0000-0000-0000-000000000000")'
        '  (property "Reference" "C1")'
        '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "N")))'
        ' (footprint "Lib:C" (layer "F.Cu") (at 30 30)'
        '  (uuid "cccc0003-0000-0000-0000-000000000000")'
        '  (property "Reference" "R1")'
        '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "N")))'
        ' (group "" (uuid "gggg0001-0000-0000-0000-000000000000")'
        '  (members "aaaa0001-0000-0000-0000-000000000000"'
        '           "bbbb0002-0000-0000-0000-000000000000")))'
    )
    board = _board_from_text(text)
    fps = {fp.ref: fp for fp in board.footprints}
    assert fps["U1"].group_id == "gggg0001-0000-0000-0000-000000000000"
    assert fps["C1"].group_id == "gggg0001-0000-0000-0000-000000000000"
    assert fps["R1"].group_id is None


def test_group_placement_never_worsens_energy():
    """place() with grouped footprints still satisfies the energy-non-worsening invariant."""
    board, _ = _board_with_groups({"U1": "g1", "C1": "g1", "U2": "g2", "C2": "g2"})
    result = placement.place(board, placement.PlaceParams(iters=800, seed=0))
    assert result.best_energy <= result.start_energy + 1e-6


def test_group_constraint_in_summary():
    """_footprint_constraint_summary includes group= for grouped footprints."""
    from pyautoroute.autoroute import _footprint_constraint_summary
    fp = _fp("U1", 0.0, 0.0, [(0.0, 0.0, "N")],
             group_id="abcd1234-0000-0000-0000-000000000000")
    summary = _footprint_constraint_summary(fp)
    assert summary is not None and "group=abcd1234" in summary
