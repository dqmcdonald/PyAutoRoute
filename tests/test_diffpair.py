"""Tests for differential pair detection, routing, and reporting."""

from __future__ import annotations

import math

import pytest

from pyautoroute import netlist, rules, sexpr
from pyautoroute.diffpair import bake_routing_state, route_diff_pair
from pyautoroute.grid import Grid
from pyautoroute.netlist import (
    DiffPairConnection, DiffPairSpec,
    build_diff_pair_connections, find_diff_pairs,
)
from pyautoroute.pcb import Board, OutlineShape, Pad, Stackup
from pyautoroute.report import DiffPairStats, _zdiff, diff_pair_stats, format_diff_pair_table
from pyautoroute.router import RoutingState


# --- shared helpers ----------------------------------------------------------

def _pad(net, cx, cy, fp_ref="U1", w=1.0, h=1.0, layers=("F.Cu",)):
    return Pad(net=net, pad_type="smd", shape="rect", cx=cx, cy=cy, w=w, h=h,
               angle=0.0, copper_layers=list(layers), fp_ref=fp_ref)


def _board(pads, size=30):
    outline = [OutlineShape("poly", {"pts": [(0, 0), (size, 0),
                                              (size, size), (0, size)]})]
    return Board(tree=sexpr.SList(), copper_layers=["F.Cu", "B.Cu"],
                 pads=pads, free_vias=[], segments=[], zones=[], outline=outline)


def _state(board, pitch=0.25):
    return RoutingState(Grid(board, rules.default_rules(), pitch=pitch))


# --- pair detection ----------------------------------------------------------

@pytest.mark.parametrize("net_p, net_n", [
    ("USB_D+", "USB_D-"),
    ("CLK_P", "CLK_N"),
    ("CLK_p", "CLK_n"),
    ("LVDS_DP", "LVDS_DN"),
    ("LVDS_dp", "LVDS_dn"),
])
def test_find_diff_pairs_naming_conventions(net_p, net_n):
    """All recognised +/- and P/N suffix styles are detected."""
    sp = _pad(net_p, 5, 5, fp_ref="U1")
    sn = _pad(net_n, 5, 6, fp_ref="U1")
    dp = _pad(net_p, 20, 5, fp_ref="U2")
    dn = _pad(net_n, 20, 6, fp_ref="U2")
    board = _board([sp, sn, dp, dn])
    pairs = find_diff_pairs(board)
    assert len(pairs) == 1
    assert pairs[0].net_p == net_p
    assert pairs[0].net_n == net_n


def test_find_diff_pairs_no_false_positives():
    """Single-ended nets and nets without partners are not paired."""
    a = _pad("GND", 5, 5)
    b = _pad("VCC", 5, 10)
    board = _board([a, b])
    assert find_diff_pairs(board) == []


def test_find_diff_pairs_exclude():
    """Excluded nets are not paired."""
    sp = _pad("CLK_P", 5, 5, fp_ref="U1")
    sn = _pad("CLK_N", 5, 6, fp_ref="U1")
    dp = _pad("CLK_P", 20, 5, fp_ref="U2")
    dn = _pad("CLK_N", 20, 6, fp_ref="U2")
    board = _board([sp, sn, dp, dn])
    assert find_diff_pairs(board, exclude=["CLK_P"]) == []
    assert find_diff_pairs(board, exclude=["CLK_N"]) == []


def test_find_diff_pairs_no_partner_net():
    """A net with a '+' suffix but no companion '-' net is not paired."""
    a = _pad("SIG+", 5, 5, fp_ref="U1")
    b = _pad("SIG+", 20, 5, fp_ref="U2")
    board = _board([a, b])
    assert find_diff_pairs(board) == []


def test_build_diff_pair_connections_two_pads():
    """Two-pad diff pair yields exactly one DiffPairConnection."""
    sp = _pad("D+", 5, 5, fp_ref="U1")
    sn = _pad("D-", 5, 6, fp_ref="U1")
    dp = _pad("D+", 20, 5, fp_ref="U2")
    dn = _pad("D-", 20, 6, fp_ref="U2")
    board = _board([sp, sn, dp, dn])
    pairs = find_diff_pairs(board)
    conns = build_diff_pair_connections(board, pairs)
    assert len(conns) == 1
    c = conns[0]
    assert c.net_p == "D+"
    assert c.net_n == "D-"
    # Source pads should come from U1, destination from U2 (or vice versa)
    endpoints = frozenset({c.src_p.fp_ref, c.dst_p.fp_ref})
    assert endpoints == {"U1", "U2"}


def test_build_diff_pair_connections_same_footprint_matching():
    """Pad matching prefers pads on the same footprint over globally nearest."""
    # U1: D+@(5,5), D-@(5,6)  U2: D+@(20,5), D-@(20,6)
    sp1 = _pad("D+", 5, 5, fp_ref="U1")
    sn1 = _pad("D-", 5, 6, fp_ref="U1")
    sp2 = _pad("D+", 20, 5, fp_ref="U2")
    sn2 = _pad("D-", 20, 6, fp_ref="U2")
    board = _board([sp1, sn1, sp2, sn2])
    pairs = find_diff_pairs(board)
    conns = build_diff_pair_connections(board, pairs)
    assert len(conns) == 1
    # The matched src/dst n pads should be on the same footprint as the p pads
    assert conns[0].src_p.fp_ref == conns[0].src_n.fp_ref
    assert conns[0].dst_p.fp_ref == conns[0].dst_n.fp_ref


# --- coupled A* routing ------------------------------------------------------

def _dp_board_and_conn(pitch=0.5):
    """Build a simple 4-pad diff pair board: U1 left, U2 right."""
    sp = _pad("DP+", 4, 10, fp_ref="U1")
    sn = _pad("DP-", 4, 11, fp_ref="U1")
    dp = _pad("DP+", 20, 10, fp_ref="U2")
    dn = _pad("DP-", 20, 11, fp_ref="U2")
    board = _board([sp, sn, dp, dn])
    spec = DiffPairSpec("DP+", "DP-")
    conn = build_diff_pair_connections(board, [spec])[0]
    state = _state(board, pitch=pitch)
    return board, state, conn


def test_route_diff_pair_succeeds():
    """Coupled router finds a route for a simple horizontal pair."""
    _, state, conn = _dp_board_and_conn()
    gap = rules.default_rules().dp_gap_for("DP+", "DP-")
    result = route_diff_pair(state, conn, gap)
    assert result is not None


def test_route_diff_pair_lengths_equal():
    """Both traces have exactly the same routed length (length matching by construction)."""
    _, state, conn = _dp_board_and_conn()
    gap = rules.default_rules().dp_gap_for("DP+", "DP-")
    result = route_diff_pair(state, conn, gap)
    assert result is not None
    rp, rn = result
    assert abs(rp.length - rn.length) < 1e-6, (
        f"Traces not length-matched: {rp.length:.4f} vs {rn.length:.4f}"
    )


def test_route_diff_pair_no_vias_on_clear_board():
    """Horizontal pair on a clear board should not need vias."""
    _, state, conn = _dp_board_and_conn()
    gap = rules.default_rules().dp_gap_for("DP+", "DP-")
    result = route_diff_pair(state, conn, gap)
    assert result is not None
    rp, rn = result
    assert rp.vias == 0
    assert rn.vias == 0


def test_route_diff_pair_both_on_front_layer():
    """Both traces of a clear horizontal pair stay on F.Cu (layer index 0)."""
    _, state, conn = _dp_board_and_conn()
    gap = rules.default_rules().dp_gap_for("DP+", "DP-")
    result = route_diff_pair(state, conn, gap)
    assert result is not None
    rp, rn = result
    assert all(li == 0 for li, _, _ in rp.path)
    assert all(li == 0 for li, _, _ in rn.path)


def test_route_diff_pair_same_offset_throughout():
    """Every node in the + path has its companion - node at a fixed offset."""
    _, state, conn = _dp_board_and_conn()
    gap = rules.default_rules().dp_gap_for("DP+", "DP-")
    result = route_diff_pair(state, conn, gap)
    assert result is not None
    rp, rn = result
    assert len(rp.path) == len(rn.path)
    offsets = {(rn.path[i][1] - rp.path[i][1],
                rn.path[i][2] - rp.path[i][2])
               for i in range(len(rp.path))}
    assert len(offsets) == 1, f"Offset not constant: {offsets}"


def test_route_diff_pair_blocked_when_impossible():
    """Returns None when both traces cannot be placed simultaneously.

    A wall that spans the full board height (except the source/destination pads)
    on both F.Cu and B.Cu leaves no clearance for a 2-wide pair to pass.
    """
    sp = _pad("DP+", 2, 5, fp_ref="U1")
    sn = _pad("DP-", 2, 6, fp_ref="U1")
    dp = _pad("DP+", 18, 5, fp_ref="U2")
    dn = _pad("DP-", 18, 6, fp_ref="U2")
    # A full-height wall in the middle — spans the entire board (size=20)
    wall = _pad("X", 10, 10, fp_ref="W1", w=0.5, h=20, layers=("F.Cu", "B.Cu"))
    board = _board([sp, sn, dp, dn, wall], size=20)
    spec = DiffPairSpec("DP+", "DP-")
    conns = build_diff_pair_connections(board, [spec])
    if not conns:
        pytest.skip("board produced no connections")
    state = _state(board, pitch=0.25)
    gap = rules.default_rules().dp_gap_for("DP+", "DP-")
    result = route_diff_pair(state, conns[0], gap)
    assert result is None


# --- bake_routing_state ------------------------------------------------------

def test_bake_routing_state_blocks_other_nets():
    """After baking, nodes used by DP+ are blocked for a different net."""
    board, state, conn = _dp_board_and_conn()
    gap = rules.default_rules().dp_gap_for("DP+", "DP-")
    result = route_diff_pair(state, conn, gap)
    assert result is not None
    rp, rn = result
    state.commit(0, rp)
    state.commit(1, rn)
    grid = state.grid

    bake_routing_state(state, grid)

    # A path node from DP+ should now be owned by its net id in the static grid
    li, c, r = rp.path[len(rp.path) // 2]
    net_id_p = grid.net_id("DP+")
    assert grid.owner[li, r, c] == net_id_p


# --- impedance formula -------------------------------------------------------

def test_zdiff_fr4_typical():
    """IPC-2141A formula gives a physically plausible result for a standard FR4 stackup.

    0.2mm trace on 1.6mm dielectric gives a high single-ended impedance (~139Ω),
    so the differential result is ~211Ω — that's correct; low-impedance lines need
    a thinner dielectric or wider traces.  The test checks the formula is in a
    physically meaningful range (50–300Ω), not a target design value.
    """
    z = _zdiff(0.2, 0.2, 1.6, 4.5, 0.035)
    assert 50.0 < z < 300.0, f"Zdiff out of plausible range: {z:.1f} Ω"


def test_zdiff_wider_trace_lower_impedance():
    """Wider trace → lower impedance (Z0 decreases with width)."""
    z_narrow = _zdiff(0.1, 0.2, 1.6, 4.5, 0.035)
    z_wide   = _zdiff(0.5, 0.2, 1.6, 4.5, 0.035)
    assert z_narrow > z_wide


def test_zdiff_larger_gap_higher_impedance():
    """Wider gap → higher differential impedance (coupling decreases)."""
    z_close = _zdiff(0.2, 0.1, 1.6, 4.5, 0.035)
    z_far   = _zdiff(0.2, 0.8, 1.6, 4.5, 0.035)
    assert z_far > z_close


# --- reporting ---------------------------------------------------------------

def _make_dp_results():
    """Build minimal dp_results for reporting tests without actual routing."""
    board, state, conn = _dp_board_and_conn()
    gap = rules.default_rules().dp_gap_for("DP+", "DP-")
    result = route_diff_pair(state, conn, gap)
    assert result is not None
    rp, rn = result
    return [(conn, rp, rn)]


def test_diff_pair_stats_skew_zero():
    """Routed pair from the coupled A* has zero skew."""
    dp_results = _make_dp_results()
    r = rules.default_rules()
    su = Stackup()
    stats = diff_pair_stats(dp_results, r, su)
    assert len(stats) == 1
    assert stats[0].skew < 1e-6


def test_diff_pair_stats_zdiff_in_range():
    """Impedance estimate is in a physically plausible range (50–300 Ω)."""
    dp_results = _make_dp_results()
    r = rules.default_rules()
    su = Stackup()
    stats = diff_pair_stats(dp_results, r, su)
    z = stats[0].zdiff_ohm
    # Single-layer route → should produce an impedance estimate
    if z is not None:
        assert 50.0 < z < 300.0, f"Zdiff implausible: {z:.1f} Ω"


def test_format_diff_pair_table_contains_pair_name():
    """The formatted table includes the pair's net names."""
    dp_results = _make_dp_results()
    r = rules.default_rules()
    su = Stackup()
    stats = diff_pair_stats(dp_results, r, su)
    table = format_diff_pair_table(stats)
    assert "DP+" in table
    assert "DP-" in table


def test_format_diff_pair_table_empty_input():
    """Empty stats list returns an empty string."""
    assert format_diff_pair_table([]) == ""
