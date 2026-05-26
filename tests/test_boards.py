"""Generality tests across the bundled KiCad projects in TestProjects/.

These are parametrized over every real board found on disk (Test1..Test4 and any
others added later), so the parser, outline assembly, round-trip and writer are
exercised against a range of KiCad outputs: different outline primitives
(gr_poly / gr_rect / gr_line) and scales (a handful to ~140 pads). Boards are
discovered dynamically and the whole module is skipped if none are present.

The routing self-check only runs on small boards (so the suite stays fast); it
routes through the real pipeline, writes + reloads the result, and asserts zero
clearance violations via the in-repo checker.
"""

from __future__ import annotations

import pathlib

import pytest

from pyautoroute import geometry, netlist, pcb, router
from pyautoroute.grid import Grid
from pyautoroute.rules import load_rules

REPO = pathlib.Path(__file__).resolve().parent.parent
PROJECTS = REPO / "TestProjects"

# small enough to route inside a unit test
_ROUTE_MAX_PADS = 30


def _discover_boards():
    if not PROJECTS.exists():
        return []
    out = []
    for pcb_path in sorted(PROJECTS.glob("*/*.kicad_pcb")):
        if "_routed" in pcb_path.name:
            continue
        out.append(pcb_path)
    return out


BOARDS = _discover_boards()
BOARD_IDS = [p.parent.name for p in BOARDS]

pytestmark = pytest.mark.skipif(not BOARDS, reason="no TestProjects boards present")


def _rules_for(pcb_path: pathlib.Path):
    return load_rules(pcb_path.with_suffix(".kicad_pro"))


@pytest.fixture(params=BOARDS, ids=BOARD_IDS)
def board_path(request):
    return request.param


def test_board_parses(board_path):
    board = pcb.load_board(board_path)
    assert len(board.copper_layers) == 2          # this router targets 2 layers
    assert board.front_layer == board.copper_layers[0]
    assert board.pads, "board has no pads"
    # every pad resolved to at least one copper layer + a net string field
    assert all(p.copper_layers for p in board.pads)


def test_board_sexpr_roundtrip_byte_identical(board_path):
    from pyautoroute import sexpr
    text = board_path.read_text()
    assert sexpr.dump_file(sexpr.loads(text)) == text


def test_board_writer_noop_is_byte_identical(board_path, tmp_path):
    board = pcb.load_board(board_path)
    out = tmp_path / "noop.kicad_pcb"
    pcb.write_board(board, out, new_nodes=None, strip_free_vias=False)
    assert out.read_text() == board_path.read_text()


def test_board_outline_and_pads_inside(board_path):
    board = pcb.load_board(board_path)
    outline = geometry.outline_to_polygon(board.outline)
    assert outline.area > 0
    grown = outline.buffer(0.5)        # tolerate pads near the edge
    inside = sum(1 for p in board.pads
                 if grown.contains(geometry.pad_polygon(p).centroid))
    # a wrong rotation/geometry convention would throw pads outside the board
    assert inside >= int(0.95 * len(board.pads))


def test_board_routes_clean(board_path, tmp_path, request):
    board = pcb.load_board(board_path)
    small = len(board.pads) <= _ROUTE_MAX_PADS
    if not small and not request.config.getoption("--slow"):
        pytest.skip(f"{board_path.parent.name} is large; run with --slow to route it")

    rules = _rules_for(board_path)
    conns = netlist.build_connections(board)
    grid = Grid(board, rules, pitch=0.3)
    state = router.RoutingState(grid)
    params = router.RouteParams(max_expansions=400_000)
    result = router.route_all(state, conns, netlist.greedy_order(conns), params)

    nodes = []
    for res in result.results:
        if res is not None:
            nodes += router.path_to_nodes(board, grid, res)
    out = tmp_path / "routed.kicad_pcb"
    pcb.write_board(board, out, new_nodes=nodes, strip_free_vias=True)

    # reload our own output and assert it is clearance-clean (true at any scale)
    routed = pcb.load_board(out)
    violations = geometry.clearance_violations(routed, rules)
    assert violations == [], f"{len(violations)} clearance violations: {violations[:3]}"
    if small:
        assert result.routed == len(conns)   # small boards route completely
    else:
        assert result.routed >= 1            # large boards: routed something, clean
