"""Connectivity analysis and routing statistics for an existing board.

`routing_stats` is the main entry point: given a loaded `Board` (and
optionally design rules), it returns a `RoutingStats` summary of the
board's current routing state — useful both for reporting the initial
state before auto-routing and for comparing with the result afterwards.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pcb import Board

# Endpoint-snapping grid (mm) for matching segment endpoints to pad centres.
_SNAP_MM = 0.01


def _snap(x: float, y: float) -> tuple[int, int]:
    return (round(x / _SNAP_MM), round(y / _SNAP_MM))


def _find(parent: dict, k):
    if k not in parent:
        parent[k] = k
    if parent[k] != k:
        parent[k] = _find(parent, parent[k])
    return parent[k]


def _union(parent: dict, a, b) -> None:
    parent[_find(parent, a)] = _find(parent, b)


@dataclass
class RoutingStats:
    """Routing statistics for a board at a given point in time."""
    total: int           # MST connections in the full netlist
    routed: int          # connections with an end-to-end path via segments
    unrouted: int        # connections without a path
    length: float        # total track length (mm)
    vias: int            # via count
    ideal_length: float = 0.0  # sum of est_length over connections (for directness ratio)
    violations: list = field(default_factory=list)  # clearance violations

    def summary(self) -> str:
        """One-line human-readable summary."""
        viol = (f", {len(self.violations)} DRC violation(s)"
                if self.violations else ", DRC clean")
        return (f"{self.routed}/{self.total} connections routed, "
                f"{self.length:.1f} mm track, {self.vias} vias{viol}")


def routing_stats(board: "Board", rules=None, exclude=None) -> RoutingStats:
    """Analyse the board's current routing state.

    Uses a union-find over segment endpoints (snapped to a 0.01 mm grid)
    to determine which MST connections are satisfied end-to-end.  Vias are
    handled implicitly: because both the F.Cu segment leading into a via
    and the B.Cu segment leaving it share the same net name and the same
    endpoint position, they are unioned together without any special via
    logic.

    Args:
        board: the board to analyse (segments and pads are read; not mutated).
        rules: optional design rules for DRC; if ``None``, violations list
            is empty.
        exclude: optional list of net names to exclude from statistics
            (e.g. copper-pour nets). Filtered from connections, length, and
            via counts. If ``None``, no exclusions are applied.

    Returns:
        A `RoutingStats` with the connection coverage, track metrics and
        (if rules are provided) clearance violations.
    """
    from . import netlist, geometry

    conns = netlist.build_connections(board, exclude=exclude)

    # ── union-find over pad and segment-endpoint nodes ──────────────────
    parent: dict = {}

    # Register every pad as a node keyed by object identity, and also union
    # it with its snapped board position (so segment endpoints nearby join it).
    pad_node: dict = {}   # id(pad) → node key
    for pad in board.pads:
        k = ("pad", id(pad))
        pad_node[id(pad)] = k
        _find(parent, k)
        _union(parent, k, ("pos", pad.net, _snap(pad.cx, pad.cy)))

    # Union each segment's two endpoints (same net, both positions).
    for seg in board.segments:
        p1 = ("pos", seg.net, _snap(seg.x1, seg.y1))
        p2 = ("pos", seg.net, _snap(seg.x2, seg.y2))
        _union(parent, p1, p2)

    # Count routed connections: both pads must reach the same component.
    n_routed = sum(
        1 for c in conns
        if _find(parent, pad_node[id(c.a)]) == _find(parent, pad_node[id(c.b)])
    )

    # Track length (filtered by exclude set if provided).
    excl_set = set(exclude or [])
    length = sum(
        math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in board.segments
        if s.net not in excl_set
    )

    # Via count (filtered by exclude set if provided).
    via_count = sum(1 for v in board.free_vias if v.net not in excl_set)

    # Ideal length (straight-line sum over all (non-excluded) connections).
    ideal_length = sum(c.est_length for c in conns)

    # DRC.
    violations: list = []
    if rules is not None:
        violations = geometry.clearance_violations(board, rules)

    return RoutingStats(
        total=len(conns),
        routed=n_routed,
        unrouted=len(conns) - n_routed,
        length=length,
        ideal_length=ideal_length,
        vias=via_count,
        violations=violations,
    )
