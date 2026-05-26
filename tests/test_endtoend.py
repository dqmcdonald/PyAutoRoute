"""End-to-end + generality tests: route a synthetic board and self-check.

These avoid kicad-cli (so they run anywhere) by routing through the real
pipeline and asserting zero clearance violations via the in-repo checker. They
also exercise the generality paths: a gr_line (not gr_poly) outline and
--exclude-net.
"""

from __future__ import annotations

from pyautoroute import geometry, netlist, router, rules, sexpr
from pyautoroute.grid import Grid
from pyautoroute.pcb import Board, OutlineShape, Pad


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
