"""End-to-end + generality tests: route a synthetic board and self-check.

These avoid kicad-cli (so they run anywhere) by routing through the real
pipeline and asserting zero clearance violations via the in-repo checker. They
also exercise the generality paths: a gr_line (not gr_poly) outline and
--exclude-net.
"""

from __future__ import annotations

import pathlib

import pytest

from pyautoroute import autoroute, geometry, netlist, pcb, router, rules, sexpr
from pyautoroute.grid import Grid
from pyautoroute.pcb import Board, OutlineShape, Pad

_TEST_BOARD = (pathlib.Path(__file__).resolve().parents[1]
               / "TestProjects" / "Test5" / "Test5.kicad_pcb")


def _pad(net, cx, cy):
    return Pad(net=net, pad_type="smd", shape="rect", cx=cx, cy=cy, w=1.2, h=1.2,
              angle=0.0, copper_layers=["F.Cu", "B.Cu"])


def _gr_line_outline(size):
    pts = [(0, 0), (size, 0), (size, size), (0, size)]
    return [OutlineShape("line", {"start": pts[i], "end": pts[(i + 1) % 4]})
            for i in range(4)]


def _board(pads, size=30):
    return Board(tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"], pads=pads,
                 free_vias=[], segments=[], zones=[], outline=_gr_line_outline(size))


def _route(board, exclude=None):
    r = rules.default_rules()
    conns = netlist.build_connections(board, exclude=exclude or [])
    state = router.RoutingState(Grid(board, r, pitch=0.5))
    params = router.RouteParams(max_expansions=300_000)
    result = router.route_all(state, conns, netlist.greedy_order(conns), params)
    # build a routed board to self-check
    nodes = []
    for res in result.results:
        if res is not None:
            nodes += router.path_to_nodes(board, state.grid, res)
    routed = Board(tree=sexpr.SList(), copper_layers=board.copper_layers,
                   pads=board.pads, free_vias=[], segments=_segments(nodes),
                   zones=[], outline=board.outline)
    return result, geometry.clearance_violations(routed, r)


def _segments(nodes):
    from pyautoroute.pcb import Segment, child, floats, strings
    segs = []
    for n in nodes:
        if sexpr.head_symbol(n) != "segment":
            continue
        s = floats(child(n, "start"))
        e = floats(child(n, "end"))
        segs.append(Segment(s[0], s[1], e[0], e[1],
                            floats(child(n, "width"))[0],
                            strings(child(n, "layer"))[0],
                            child(n, "net")[-1].text))
    return segs


def test_endtoend_gr_line_outline_routes_clean():
    pads = []
    for k, y in enumerate((6, 12, 18, 24)):
        pads += [_pad(f"N{k}", 5, y), _pad(f"N{k}", 25, y)]
    board = _board(pads)
    result, violations = _route(board)
    assert result.routed == len(netlist.build_connections(board))
    assert violations == []


def test_endtoend_exclude_net():
    pads = [_pad("GND", 5, 6), _pad("GND", 25, 6),
            _pad("SIG", 5, 18), _pad("SIG", 25, 18)]
    board = _board(pads)
    conns = netlist.build_connections(board, exclude=["GND"])
    assert {c.net for c in conns} == {"SIG"}
    result, violations = _route(board, exclude=["GND"])
    assert violations == []
    # excluded net produced no routed tracks
    assert all(r.net != "GND" for r in result.results if r is not None)


@pytest.mark.skipif(not _TEST_BOARD.exists(), reason="Test5 board not present")
def test_cli_writes_snapshots_and_log(tmp_path):
    # --snapshots / --log emit N snapshot boards + a verbose log that records the
    # annealing parameters (including --unrouted-weight / --anneal-temps)
    out = tmp_path / "out.kicad_pcb"
    args = autoroute.build_parser().parse_args(
        [str(_TEST_BOARD), "-o", str(out), "--iters", "30", "--snapshots", "3",
         "--unrouted-weight", "60", "--anneal-temps", "5", "0.1", "--log", "--quiet"])
    rc = autoroute.run(args)
    assert rc == 0

    snaps = sorted((tmp_path / "snapshots").glob("*.kicad_pcb"))
    assert len(snaps) == 3                    # one board per requested snapshot

    log = out.with_suffix(".log")             # bare --log -> <output>.log
    text = log.read_text()
    assert "snapshots      3" in text         # parameter dump
    assert "unrouted wt    60.0" in text      # exposed anneal params logged
    assert "anneal temps   5.0 -> 0.1" in text
    assert "snapshot 3/3" in text             # progress trace
    assert "self-check:    clean" in text     # final metrics


@pytest.mark.skipif(not _TEST_BOARD.exists(), reason="Test5 board not present")
def test_cli_snapshots_ignored_without_annealing(tmp_path):
    out = tmp_path / "out.kicad_pcb"
    args = autoroute.build_parser().parse_args(
        [str(_TEST_BOARD), "-o", str(out), "--snapshots", "3", "--quiet"])
    assert autoroute.run(args) == 0
    assert not (tmp_path / "snapshots").exists()    # no annealing -> no snapshots


def test_cli_anneal_param_flags_parse():
    # defaults come from AnnealParams; overrides parse to the right namespace
    from pyautoroute import anneal
    p = autoroute.build_parser()
    a = p.parse_args(["b.kicad_pcb"])
    assert a.unrouted_weight == anneal.AnnealParams.unrouted_weight
    assert tuple(a.anneal_temps) == (anneal.AnnealParams.t_start,
                                     anneal.AnnealParams.t_end)
    a2 = p.parse_args(["b.kicad_pcb", "--unrouted-weight", "50",
                       "--anneal-temps", "6", "0.1"])
    assert a2.unrouted_weight == 50.0
    assert tuple(a2.anneal_temps) == (6.0, 0.1)


def test_cli_rejects_invalid_anneal_params():
    # validation happens in main(): START must exceed END > 0, weight >= 0
    with pytest.raises(SystemExit):
        autoroute.main(["b.kicad_pcb", "--anneal-temps", "0.1", "6"])
    with pytest.raises(SystemExit):
        autoroute.main(["b.kicad_pcb", "--unrouted-weight", "-1"])


def test_default_output_names():
    p = pathlib.Path("/x/Board.kicad_pcb")
    assert autoroute.default_output(p).name == "Board_routed.kicad_pcb"
    assert autoroute.default_output(p, place=True).name == "Board_placed_routed.kicad_pcb"
    assert autoroute.default_output(p, place_only=True).name == "Board_placed.kicad_pcb"


@pytest.mark.skipif(not _TEST_BOARD.exists(), reason="Test5 board not present")
def test_cli_place_only_writes_placed_board(tmp_path):
    import shutil
    src = tmp_path / "Test5.kicad_pcb"
    shutil.copy(_TEST_BOARD, src)
    pro = _TEST_BOARD.with_suffix(".kicad_pro")
    if pro.exists():
        shutil.copy(pro, tmp_path / "Test5.kicad_pro")
    orig = pcb.load_board(src)
    orig_pos = {fp.ref: (round(fp.x, 3), round(fp.y, 3)) for fp in orig.footprints}

    args = autoroute.build_parser().parse_args(
        [str(src), "--place-only", "--place-iters", "1500", "--quiet"])
    assert autoroute.run(args) == 0                      # clean self-check

    out = tmp_path / "Test5_placed.kicad_pcb"            # _placed naming
    assert out.exists()
    placed = pcb.load_board(out)
    placed_pos = {fp.ref: (round(fp.x, 3), round(fp.y, 3)) for fp in placed.footprints}
    assert placed_pos != orig_pos                        # placement moved parts
    assert len(placed.segments) == len(orig.segments)    # nothing was routed


def test_cli_place_only_rejects_routing_flags():
    with pytest.raises(SystemExit):
        autoroute.main(["b.kicad_pcb", "--place-only", "--iters", "10"])


def test_cli_placement_control_flags_parse():
    from pyautoroute import placement
    p = autoroute.build_parser()
    a = p.parse_args(["b.kicad_pcb"])
    assert tuple(a.place_temps) == (placement.PlaceParams.t_start,
                                    placement.PlaceParams.t_end)
    assert a.place_step == placement.PlaceParams.step
    assert a.place_rotate == placement.PlaceParams.rotate_mode
    a2 = p.parse_args(["b.kicad_pcb", "--place-temps", "6", "0.1",
                       "--place-step", "8", "--place-rotate", "none"])
    assert tuple(a2.place_temps) == (6.0, 0.1)
    assert a2.place_step == 8.0 and a2.place_rotate == "none"


def test_cli_rejects_invalid_placement_controls():
    with pytest.raises(SystemExit):
        autoroute.main(["b.kicad_pcb", "--place-temps", "0.1", "6"])
    with pytest.raises(SystemExit):
        autoroute.main(["b.kicad_pcb", "--place-step", "0"])


def test_cli_runs_flags_parse_and_validate():
    p = autoroute.build_parser()
    a = p.parse_args(["b.kicad_pcb"])
    assert a.runs == 1 and a.place_runs == 1
    a2 = p.parse_args(["b.kicad_pcb", "--runs", "4", "--place-runs", "3"])
    assert a2.runs == 4 and a2.place_runs == 3
    with pytest.raises(SystemExit):
        autoroute.main(["b.kicad_pcb", "--runs", "0"])
    with pytest.raises(SystemExit):
        autoroute.main(["b.kicad_pcb", "--place-runs", "0"])


@pytest.mark.skipif(not _TEST_BOARD.exists(), reason="Test5 board not present")
def test_cli_multi_run_routes_clean(tmp_path):
    out = tmp_path / "out.kicad_pcb"
    args = autoroute.build_parser().parse_args(
        [str(_TEST_BOARD), "-o", str(out), "--iters", "40", "--runs", "3",
         "--log", "--quiet"])
    assert autoroute.run(args) == 0                 # best-of-3, clean self-check
    text = out.with_suffix(".log").read_text()
    assert "runs           3" in text
    assert "best of 3 runs" in text


def test_cli_jobs_flag_parses_and_validates():
    p = autoroute.build_parser()
    assert p.parse_args(["b.kicad_pcb"]).jobs == 1                 # default
    assert p.parse_args(["b.kicad_pcb", "-j", "2"]).jobs == 2
    assert p.parse_args(["b.kicad_pcb", "--jobs", "0"]).jobs == 0  # 0 == all CPUs
    with pytest.raises(SystemExit):
        autoroute.main(["b.kicad_pcb", "--jobs", "-1"])


@pytest.mark.skipif(not _TEST_BOARD.exists(), reason="Test5 board not present")
def test_cli_parallel_runs_routes_clean(tmp_path):
    # best-of-N across worker processes: valid, DRC-clean result, best line logged
    out = tmp_path / "out.kicad_pcb"
    args = autoroute.build_parser().parse_args(
        [str(_TEST_BOARD), "-o", str(out), "--iters", "40", "--runs", "2",
         "--jobs", "2", "--log", "--quiet"])
    assert autoroute.run(args) == 0
    assert out.exists()
    text = out.with_suffix(".log").read_text()
    assert "best of 2 runs" in text
    assert "across 2 workers" in text


def test_coarse_grid_note():
    # no warning at the derived pitch or up to 2x it; warn beyond that, and the
    # message suggests the derived pitch as the remedy
    assert autoroute.coarse_grid_note(0.3, 0.3) is None
    assert autoroute.coarse_grid_note(0.6, 0.3) is None      # exactly 2x: boundary
    note = autoroute.coarse_grid_note(0.8, 0.3)              # 2.7x: too coarse
    assert note is not None
    assert "coarse" in note and "--grid 0.3" in note
    assert autoroute.coarse_grid_note(0.8, 0.0) is None      # degenerate rules


@pytest.mark.skipif(not _TEST_BOARD.exists(), reason="Test5 board not present")
def test_cli_warns_on_coarse_grid(tmp_path):
    # an explicit coarse --grid surfaces the warning in the log
    out = tmp_path / "out.kicad_pcb"
    args = autoroute.build_parser().parse_args(
        [str(_TEST_BOARD), "-o", str(out), "--grid", "2", "--log", "--quiet"])
    autoroute.run(args)
    assert "warning:" in out.with_suffix(".log").read_text()
