"""Tests for pyautoroute.geometry (pad/outline shapely geometry)."""

from __future__ import annotations

import math
import pathlib

import pytest

from pyautoroute import geometry, pcb
from pyautoroute.pcb import OutlineShape, Pad

REPO = pathlib.Path(__file__).resolve().parent.parent
PCB = REPO / "TestProjects" / "Test1" / "Test1.kicad_pcb"


def _pad(shape, w, h, cx=0, cy=0, angle=0, rratio=None, delta=None):
    return Pad(net="N", pad_type="smd", shape=shape, cx=cx, cy=cy, w=w, h=h,
               angle=angle, copper_layers=["F.Cu"], roundrect_rratio=rratio,
               rect_delta=delta)


def test_rect_pad_area_and_center():
    poly = geometry.pad_polygon(_pad("rect", 2.0, 1.0, cx=5, cy=7))
    assert math.isclose(poly.area, 2.0, rel_tol=1e-6)
    assert math.isclose(poly.centroid.x, 5.0, abs_tol=1e-6)
    assert math.isclose(poly.centroid.y, 7.0, abs_tol=1e-6)


def test_circle_pad_area():
    poly = geometry.pad_polygon(_pad("circle", 2.0, 2.0))
    assert math.isclose(poly.area, math.pi, rel_tol=1e-2)


def test_roundrect_area_between_rect_and_inscribed():
    poly = geometry.pad_polygon(_pad("roundrect", 2.0, 2.0, rratio=0.25))
    assert poly.area < 4.0           # rounded corners removed area
    assert poly.area > 3.0


def test_rotated_rect_bounds():
    # 2x1 rect rotated 90deg -> bounds become 1 wide, 2 tall
    poly = geometry.pad_polygon(_pad("rect", 2.0, 1.0, angle=90))
    minx, miny, maxx, maxy = poly.bounds
    assert math.isclose(maxx - minx, 1.0, abs_tol=1e-6)
    assert math.isclose(maxy - miny, 2.0, abs_tol=1e-6)


def test_outline_poly_area_and_bounds():
    shapes = [OutlineShape("poly", {"pts": [(0, 0), (10, 0), (10, 5), (0, 5)]})]
    poly = geometry.outline_to_polygon(shapes)
    assert math.isclose(poly.area, 50.0, rel_tol=1e-9)


def test_outline_from_lines_stitched():
    pts = [(0, 0), (4, 0), (4, 3), (0, 3)]
    lines = []
    for i in range(4):
        a, b = pts[i], pts[(i + 1) % 4]
        lines.append(OutlineShape("line", {"start": a, "end": b}))
    poly = geometry.outline_to_polygon(lines)
    assert math.isclose(poly.area, 12.0, rel_tol=1e-9)


def test_inflate_grows_area():
    poly = geometry.pad_polygon(_pad("rect", 2.0, 2.0))
    grown = geometry.inflate(poly, 0.5)
    assert grown.area > poly.area
    assert grown.contains(poly)


@pytest.mark.skipif(not PCB.exists(), reason="Test1 board not present")
def test_test1_pads_inside_outline():
    board = pcb.load_board(PCB)
    outline = geometry.outline_to_polygon(board.outline)
    grown = outline.buffer(0.5)   # tolerate pads near the edge
    inside = sum(1 for p in board.pads if grown.contains(geometry.pad_polygon(p).centroid))
    # the vast majority of pad centres must lie within the board area;
    # this catches a wrong rotation convention (which throws pads far away)
    assert inside >= int(0.95 * len(board.pads))


@pytest.mark.skipif(not PCB.exists(), reason="Test1 board not present")
def test_obstacle_index_layers():
    board = pcb.load_board(PCB)
    idx = geometry.ObstacleIndex(geometry.board_obstacles(board))
    assert set(idx.layers()) <= {"F.Cu", "B.Cu"}
    assert "F.Cu" in idx.layers()
