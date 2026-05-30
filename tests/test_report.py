"""Tests for pyautoroute.report (routing statistics)."""

from __future__ import annotations

import math

from pyautoroute import report, sexpr
from pyautoroute.pcb import Board, Pad, Segment, Via


def _pad(net, cx, cy):
    """Create a test pad."""
    return Pad(net=net, pad_type="smd", shape="rect", cx=cx, cy=cy, w=1, h=1,
              angle=0.0, copper_layers=["F.Cu"])


def _board(pads, segments=None, free_vias=None):
    """Create a test board."""
    return Board(
        tree=sexpr.SList(),
        copper_layers=["F.Cu", "B.Cu"],
        pads=pads,
        free_vias=free_vias or [],
        segments=segments or [],
        zones=[],
        outline=[]
    )


def test_routing_stats_basic_no_routing():
    """Two-pad net, no segments connecting them."""
    board = _board([_pad("A", 0, 0), _pad("A", 5, 0)])
    stats = report.routing_stats(board)
    assert stats.total == 1
    assert stats.routed == 0
    assert stats.unrouted == 1
    assert stats.length == 0.0
    assert stats.vias == 0
    assert stats.ideal_length == 5.0


def test_routing_stats_with_segments():
    """Two-pad net connected by a segment."""
    pads = [_pad("A", 0, 0), _pad("A", 5, 0)]
    segs = [Segment(x1=0, y1=0, x2=5, y2=0, width=0.25, layer="F.Cu", net="A")]
    board = _board(pads, segments=segs)
    stats = report.routing_stats(board)
    assert stats.total == 1
    assert stats.routed == 1
    assert stats.unrouted == 0
    assert stats.length == 5.0
    assert stats.vias == 0
    assert stats.ideal_length == 5.0


def test_routing_stats_with_vias():
    """Via on a net is counted."""
    pads = [_pad("A", 0, 0), _pad("A", 5, 0)]
    vias = [Via(cx=2.5, cy=0, size=0.5, drill=0.25, layers=("F.Cu", "B.Cu"), net="A")]
    board = _board(pads, free_vias=vias)
    stats = report.routing_stats(board)
    assert stats.vias == 1


def test_exclude_parameter_filters_connections():
    """Exclude GND net removes its connections from total."""
    pads = [
        _pad("GND", 0, 0), _pad("GND", 5, 0),
        _pad("DATA", 10, 0), _pad("DATA", 15, 0),
    ]
    board = _board(pads)

    # Without exclude: 2 nets = 2 connections
    stats_all = report.routing_stats(board)
    assert stats_all.total == 2

    # With exclude: only DATA = 1 connection
    stats_excl = report.routing_stats(board, exclude=["GND"])
    assert stats_excl.total == 1
    assert stats_excl.ideal_length == 5.0  # DATA connection only


def test_exclude_filters_segments_by_net():
    """Exclude parameter removes segments of that net from length count."""
    pads = [
        _pad("GND", 0, 0), _pad("GND", 5, 0),
        _pad("DATA", 10, 0), _pad("DATA", 15, 0),
    ]
    segs = [
        Segment(x1=0, y1=0, x2=5, y2=0, width=0.25, layer="F.Cu", net="GND"),
        Segment(x1=10, y1=0, x2=15, y2=0, width=0.25, layer="F.Cu", net="DATA"),
    ]
    board = _board(pads, segments=segs)

    # All segments included
    stats_all = report.routing_stats(board)
    assert stats_all.length == 10.0

    # GND excluded: only DATA segment
    stats_excl = report.routing_stats(board, exclude=["GND"])
    assert stats_excl.length == 5.0


def test_exclude_filters_vias_by_net():
    """Exclude parameter removes vias of that net from count."""
    pads = [_pad("A", 0, 0), _pad("A", 5, 0)]
    vias = [
        Via(cx=1, cy=0, size=0.5, drill=0.25, layers=("F.Cu", "B.Cu"), net="GND"),
        Via(cx=2, cy=0, size=0.5, drill=0.25, layers=("F.Cu", "B.Cu"), net="A"),
    ]
    board = _board(pads, free_vias=vias)

    # All vias included
    stats_all = report.routing_stats(board)
    assert stats_all.vias == 2

    # GND excluded: only A via
    stats_excl = report.routing_stats(board, exclude=["GND"])
    assert stats_excl.vias == 1


def test_exclude_none_is_same_as_no_exclude():
    """Exclude=None is the same as not passing exclude (backward compatibility)."""
    pads = [_pad("A", 0, 0), _pad("A", 5, 0)]
    segs = [Segment(x1=0, y1=0, x2=5, y2=0, width=0.25, layer="F.Cu", net="A")]
    vias = [Via(cx=2.5, cy=0, size=0.5, drill=0.25, layers=("F.Cu", "B.Cu"), net="A")]
    board = _board(pads, segments=segs, free_vias=vias)

    stats_no_param = report.routing_stats(board)
    stats_none = report.routing_stats(board, exclude=None)

    assert stats_no_param.total == stats_none.total
    assert stats_no_param.length == stats_none.length
    assert stats_no_param.vias == stats_none.vias


def test_ideal_length_is_sum_of_est_lengths():
    """ideal_length equals sum of straight-line distances."""
    # Two separate nets: 3-4-5 triangle and simple 5mm
    pads = [
        _pad("NET1", 0, 0), _pad("NET1", 3, 4),  # hypotenuse = 5
        _pad("NET2", 10, 0), _pad("NET2", 15, 0),  # simple = 5
    ]
    board = _board(pads)
    stats = report.routing_stats(board)

    assert stats.ideal_length == 10.0  # 5 + 5


def test_ideal_length_excludes_filtered_nets():
    """ideal_length only includes non-excluded connections."""
    pads = [
        _pad("GND", 0, 0), _pad("GND", 5, 0),
        _pad("DATA", 10, 0), _pad("DATA", 15, 0),
    ]
    board = _board(pads)

    stats_all = report.routing_stats(board)
    assert stats_all.ideal_length == 10.0

    stats_excl = report.routing_stats(board, exclude=["GND"])
    assert stats_excl.ideal_length == 5.0


def test_ideal_length_default_zero():
    """ideal_length defaults to 0.0 in dataclass."""
    # Single-pad net (no connections, no ideal length)
    board = _board([_pad("A", 0, 0)])
    stats = report.routing_stats(board)
    # Since no connections exist, ideal_length will be 0 (sum of empty list)
    assert stats.ideal_length == 0.0
