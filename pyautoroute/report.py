"""Connectivity analysis and routing statistics for an existing board.

`routing_stats` is the main entry point: given a loaded `Board` (and
optionally design rules), it returns a `RoutingStats` summary of the
board's current routing state — useful both for reporting the initial
state before auto-routing and for comparing with the result afterwards.

`diff_pair_stats` summarises the differential pairs routed during a run,
including estimated differential impedance from the board's stackup.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pcb import Board, Stackup
    from .netlist import DiffPairConnection
    from .router import RouteResult

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

    # ── proximity union: track endpoints that land inside a pad's copper ─
    # Hand-routed boards can end a track anywhere within the pad copper,
    # not necessarily at the pad centre. Build a per-net list of endpoint
    # positions and union any endpoint within the pad's half-extent.
    net_endpoints: dict[str, list[tuple[float, float, tuple]]] = {}
    for seg in board.segments:
        for x, y in ((seg.x1, seg.y1), (seg.x2, seg.y2)):
            net_endpoints.setdefault(seg.net, []).append((x, y, _snap(x, y)))

    for pad in board.pads:
        radius = math.hypot(pad.w, pad.h) / 2  # conservative pad half-extent
        pad_k = ("pad", id(pad))
        for ex, ey, esnap in net_endpoints.get(pad.net, []):
            if math.hypot(ex - pad.cx, ey - pad.cy) <= radius:
                _union(parent, pad_k, ("pos", pad.net, esnap))

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


# ---------------------------------------------------------------------------
# Differential pair reporting
# ---------------------------------------------------------------------------

@dataclass
class DiffPairStats:
    """Per-pair summary produced after differential pair routing."""
    net_p: str
    net_n: str
    length_p: float          # routed length of + trace (mm)
    length_n: float          # routed length of − trace (mm)
    skew: float              # |length_p − length_n| (mm)
    vias: int                # via count (both traces combined)
    layer: str               # layer(s) used, e.g. "F.Cu" or "F.Cu / B.Cu"
    track_width: float       # mm (from design rules)
    gap: float               # inner-edge spacing used (mm)
    zdiff_ohm: float | None  # estimated differential impedance (Ω), or None


def _zdiff(w: float, gap: float, h: float, er: float, t: float) -> float:
    """Estimate differential microstrip impedance (IPC-2141A approximation).

    Valid for outer-layer (microstrip) traces on a two-layer board.  Inner
    layers (stripline) require a different formula and are not supported here.

    Args:
        w: trace width (mm).
        gap: inner-edge spacing between the two traces (mm).
        h: dielectric thickness (mm).
        er: relative permittivity of the substrate.
        t: copper thickness (mm).

    Returns:
        Estimated differential impedance in ohms.
    """
    z0 = (87.0 / math.sqrt(er + 1.41)) * math.log(5.98 * h / (0.8 * w + t))
    zdiff = 2.0 * z0 * (1.0 - 0.347 * math.exp(-2.9 * gap / h))
    return zdiff


def diff_pair_stats(
    dp_results: list[tuple["DiffPairConnection", "RouteResult", "RouteResult"]],
    rules,
    stackup: "Stackup",
) -> list[DiffPairStats]:
    """Build per-pair statistics from routed diff pair results.

    Args:
        dp_results: list of ``(dp_conn, result_p, result_n)`` triples as
            produced by the diff pair pre-routing pass in ``autoroute.py``.
        rules: design rules (for track width lookup).
        stackup: the board's substrate parameters (for impedance estimation).

    Returns:
        One `DiffPairStats` per pair, in the order of *dp_results*.
    """
    out: list[DiffPairStats] = []
    for dp_conn, rp, rn in dp_results:
        # Determine layers used
        # path stores layer indices; convert via count to a label
        multilayer = rp.vias > 0
        if multilayer:
            layer_str = "F.Cu / B.Cu"
        else:
            # Use the layer index from the first path node; 0 = front layer
            layer_str = "F.Cu" if rp.path[0][0] == 0 else "B.Cu"

        w = rules.track_width_for(dp_conn.net_p)
        gap = rules.dp_gap_for(dp_conn.net_p, dp_conn.net_n)

        # Impedance only for single-layer (outer-layer microstrip) routes
        zdiff: float | None = None
        if not multilayer and rp.path[0][0] == 0:
            try:
                zdiff = _zdiff(w, gap,
                               stackup.dielectric_h, stackup.epsilon_r,
                               stackup.copper_thickness)
            except (ValueError, ZeroDivisionError):
                zdiff = None

        out.append(DiffPairStats(
            net_p=dp_conn.net_p,
            net_n=dp_conn.net_n,
            length_p=rp.length,
            length_n=rn.length,
            skew=abs(rp.length - rn.length),
            vias=rp.vias + rn.vias,
            layer=layer_str,
            track_width=w,
            gap=gap,
            zdiff_ohm=zdiff,
        ))
    return out


def format_diff_pair_table(stats: list[DiffPairStats],
                           stackup_assumed: bool = False) -> str:
    """Format a diff pair stats list as a human-readable table string.

    Args:
        stats: the per-pair stats from `diff_pair_stats`.
        stackup_assumed: if True, appends a footnote that FR4 defaults were used.

    Returns:
        A multi-line string ready to print or write to a log.
    """
    if not stats:
        return ""
    hdr = (f"  {'Pair':<28} {'Length+':>8} {'Length−':>8} "
           f"{'Skew':>7} {'Vias':>4}  {'Layer':<9} {'W':>5} {'Gap':>5}  ~Zdiff")
    sep = "  " + "-" * (len(hdr) - 2)
    lines = ["Differential pairs:", hdr, sep]
    zdiff_missing = False
    for s in stats:
        pair = f"{s.net_p}/{s.net_n}"
        zdiff_str = f"{s.zdiff_ohm:.0f} Ω" if s.zdiff_ohm is not None else "—"
        if s.zdiff_ohm is None:
            zdiff_missing = True
        lines.append(
            f"  {pair:<28} {s.length_p:>7.1f}mm {s.length_n:>7.1f}mm "
            f"{s.skew:>6.3f}mm {s.vias:>4}  {s.layer:<9} "
            f"{s.track_width:>4.2f}  {s.gap:>4.2f}  {zdiff_str}"
        )
    if stackup_assumed:
        lines.append("  † ~Zdiff: microstrip estimate, FR4 defaults (Er=4.5, h=1.6mm, t=0.035mm)")
    elif zdiff_missing:
        lines.append("  — Zdiff not estimated for multi-layer routes (stripline formula not implemented)")
    return "\n".join(lines)
