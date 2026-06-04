"""Net grouping and rats-nest decomposition.

Each multi-pad net is reduced to a set of two-pin connections via a minimum
spanning tree over the pad centroids, so routing every connection joins all of
the net's pads with the least total rats-nest length. Nets matching an
``--exclude-net`` pattern are dropped (their pads still act as obstacles via the
grid, but no connections are generated for them).

Differential-pair support: ``find_diff_pairs`` detects +/- or P/N net pairs by
name convention; ``build_diff_pair_connections`` produces ``DiffPairConnection``
objects whose two traces are always routed together by the coupled A* in
``diffpair.py``.
"""

from __future__ import annotations

import fnmatch
import math
import re
from dataclasses import dataclass

import numpy as np
from scipy.sparse.csgraph import minimum_spanning_tree
from scipy.spatial.distance import pdist, squareform

from .pcb import Board, Pad


@dataclass
class Connection:
    net: str
    a: Pad
    b: Pad

    @property
    def est_length(self) -> float:
        """Straight-line distance between the two pad centres (mm)."""
        return math.hypot(self.a.cx - self.b.cx, self.a.cy - self.b.cy)


# ---------------------------------------------------------------------------
# Differential pair support
# ---------------------------------------------------------------------------

# Ordered by specificity: longer suffixes checked first to avoid false matches
# (e.g. "_P"/"_N" before bare "P"/"N").
_DP_SUFFIX_PAIRS: list[tuple[str, str]] = [
    ("+", "-"),
    ("_P", "_N"),
    ("_p", "_n"),
    ("P", "N"),
    ("p", "n"),
]


@dataclass
class DiffPairSpec:
    """Names of a detected differential pair."""
    net_p: str   # positive net (e.g. "USB_D+")
    net_n: str   # negative net (e.g. "USB_D-")


@dataclass
class DiffPairConnection:
    """A two-pin routing job for both traces of a differential pair.

    ``src_p``/``src_n`` are the source pads (from the same component) and
    ``dst_p``/``dst_n`` are the destination pads.  The coupled A* routes both
    traces simultaneously, guaranteeing equal length and constant spacing.
    """
    net_p: str
    net_n: str
    src_p: Pad
    src_n: Pad
    dst_p: Pad
    dst_n: Pad

    @property
    def est_length(self) -> float:
        """Average straight-line length of the two rats-nest segments (mm)."""
        lp = math.hypot(self.src_p.cx - self.dst_p.cx, self.src_p.cy - self.dst_p.cy)
        ln = math.hypot(self.src_n.cx - self.dst_n.cx, self.src_n.cy - self.dst_n.cy)
        return (lp + ln) / 2


def find_diff_pairs(board: Board, exclude: list[str] | None = None) -> list[DiffPairSpec]:
    """Detect differential pairs in the board's netlist by naming convention.

    Checks each net name for the positive suffixes in ``+``, ``_P``, ``P``
    (case variants included); if the companion net with the negative suffix
    also exists, both are paired.  Each pair is returned exactly once.

    Args:
        board: the board whose net names are inspected.
        exclude: glob patterns for nets to skip (same as ``build_connections``).

    Returns:
        One `DiffPairSpec` per detected pair, in sorted net-name order.
    """
    exclude = exclude or []
    nets = set(board.pads_by_net().keys())
    seen: set[frozenset[str]] = set()
    pairs: list[DiffPairSpec] = []

    for net in sorted(nets):
        if is_excluded(net, exclude):
            continue
        for pos_sfx, neg_sfx in _DP_SUFFIX_PAIRS:
            if net.endswith(pos_sfx):
                stem = net[: -len(pos_sfx)]
                if not stem:
                    continue
                companion = stem + neg_sfx
                if companion in nets and not is_excluded(companion, exclude):
                    key = frozenset((net, companion))
                    if key not in seen:
                        seen.add(key)
                        pairs.append(DiffPairSpec(net_p=net, net_n=companion))
                break   # stop checking suffixes once one matches

    return pairs


def _match_dp_pads(pads_p: list[Pad], pads_n: list[Pad]) -> list[tuple[Pad, Pad]]:
    """Greedily match + pads to − pads, preferring pads on the same footprint.

    Args:
        pads_p: pads belonging to the positive net.
        pads_n: pads belonging to the negative net.

    Returns:
        A list of ``(pad_p, pad_n)`` tuples, one per matched pair, ordered by
        the positive-net pad's position in *pads_p*.
    """
    matched: list[tuple[Pad, Pad]] = []
    remaining_n = list(pads_n)

    for pp in pads_p:
        if not remaining_n:
            break
        # Same footprint first; fall back to nearest globally
        same_fp = [pn for pn in remaining_n if pn.fp_ref == pp.fp_ref]
        pool = same_fp if same_fp else remaining_n
        pn = min(pool, key=lambda p, _pp=pp: math.hypot(p.cx - _pp.cx, p.cy - _pp.cy))
        matched.append((pp, pn))
        remaining_n.remove(pn)

    return matched


def build_diff_pair_connections(
    board: Board,
    pairs: list[DiffPairSpec],
) -> list[DiffPairConnection]:
    """Build the coupled two-pin connection list for all diff pairs.

    For a pair whose nets each have exactly two pads, one `DiffPairConnection`
    is produced.  For more pads, the matched pad-pairs are reduced via an MST
    over their midpoint positions, producing N-1 connections for N pairs.

    Args:
        board: the board whose pads supply the netlist.
        pairs: the diff pairs to process (from `find_diff_pairs`).

    Returns:
        All `DiffPairConnection` objects ready to pass to the coupled router.
    """
    pads_by_net = board.pads_by_net()
    conns: list[DiffPairConnection] = []

    for spec in pairs:
        pads_p = pads_by_net.get(spec.net_p, [])
        pads_n = pads_by_net.get(spec.net_n, [])
        if len(pads_p) < 2 or len(pads_n) < 2:
            continue

        matched = _match_dp_pads(pads_p, pads_n)
        if len(matched) < 2:
            continue

        if len(matched) == 2:
            (sp, sn), (dp, dn) = matched
            conns.append(DiffPairConnection(spec.net_p, spec.net_n, sp, sn, dp, dn))
        else:
            # MST over midpoints of each matched pad-pair
            mids = np.array([((pp.cx + pn.cx) / 2, (pp.cy + pn.cy) / 2)
                             for pp, pn in matched])
            dist = squareform(pdist(mids))
            mst = minimum_spanning_tree(dist).tocoo()
            for i, j in zip(mst.row, mst.col):
                sp, sn = matched[int(i)]
                dp, dn = matched[int(j)]
                conns.append(DiffPairConnection(spec.net_p, spec.net_n, sp, sn, dp, dn))

    return conns


def is_excluded(net: str, patterns: list[str]) -> bool:
    """Return whether a net matches any exclude pattern.

    Args:
        net: the net name to test.
        patterns: case-sensitive glob patterns (e.g. ``["GND", "/PWR*"]``).

    Returns:
        True if `net` matches at least one pattern.
    """
    return any(fnmatch.fnmatchcase(net, p) for p in patterns)


def _mst_connections(net: str, pads: list[Pad]) -> list[Connection]:
    """Decompose a multi-pad net into two-pin connections via a spanning tree.

    Args:
        net: the net name.
        pads: the pads on this net.

    Returns:
        The MST edges over the pad centroids as `Connection`s (empty for a
        single pad), so routing them all joins every pad with minimal rats-nest
        length.
    """
    n = len(pads)
    if n < 2:
        return []
    if n == 2:
        return [Connection(net, pads[0], pads[1])]
    coords = np.array([(p.cx, p.cy) for p in pads])
    dist = squareform(pdist(coords))
    mst = minimum_spanning_tree(dist).tocoo()
    return [Connection(net, pads[int(i)], pads[int(j)])
            for i, j in zip(mst.row, mst.col)]


_SNAP = 0.001  # 1 µm grid for connectivity snapping


class _UnionFind:
    """Simple path-compressed union-find over arbitrary hashable keys."""

    def __init__(self):
        self._parent: dict = {}

    def find(self, k):
        if k not in self._parent:
            self._parent[k] = k
        if self._parent[k] != k:
            self._parent[k] = self.find(self._parent[k])
        return self._parent[k]

    def union(self, a, b):
        self._parent[self.find(a)] = self.find(b)


def _snap(x: float, y: float) -> tuple[int, int]:
    return (round(x / _SNAP), round(y / _SNAP))


def pre_routed_connections(
    board: Board,
    connections: list[Connection],
) -> tuple[list[Connection], list[Connection]]:
    """Split *connections* into those already satisfied by existing copper and the rest.

    Builds a layer-aware union-find over all existing segments, free vias, and
    multi-layer (THT) pads, then classifies each MST connection as pre-routed
    (both endpoints are already in the same connected component) or unrouted.

    Args:
        board: the board, whose ``segments``, ``free_vias``, and ``pads`` supply
            the existing copper graph.
        connections: the full MST connection list from `build_connections`.

    Returns:
        ``(pre_routed, unrouted)`` — two lists that partition *connections*.
        Pre-routed connections should be counted as completed; unrouted ones
        should be passed to the router.
    """
    uf = _UnionFind()

    # Segments: join endpoints on the same layer
    for seg in board.segments:
        k1 = (seg.layer, *_snap(seg.x1, seg.y1))
        k2 = (seg.layer, *_snap(seg.x2, seg.y2))
        uf.union(k1, k2)

    # Free vias: join their position across both layers they span
    for via in board.free_vias:
        if len(via.layers) >= 2:
            k1 = (via.layers[0], *_snap(via.cx, via.cy))
            k2 = (via.layers[1], *_snap(via.cx, via.cy))
            uf.union(k1, k2)

    # THT pads: present on multiple copper layers — join all
    for pad in board.pads:
        if len(pad.copper_layers) >= 2:
            keys = [(lyr, *_snap(pad.cx, pad.cy)) for lyr in pad.copper_layers]
            for i in range(1, len(keys)):
                uf.union(keys[0], keys[i])

    def _pad_key(pad):
        layer = pad.copper_layers[0] if pad.copper_layers else "F.Cu"
        return (layer, *_snap(pad.cx, pad.cy))

    pre_routed: list[Connection] = []
    unrouted: list[Connection] = []
    for conn in connections:
        if uf.find(_pad_key(conn.a)) == uf.find(_pad_key(conn.b)):
            pre_routed.append(conn)
        else:
            unrouted.append(conn)

    return pre_routed, unrouted


def build_connections(board: Board, exclude: list[str] | None = None) -> list[Connection]:
    """Build the two-pin connection list (rats-nest) for a board.

    Args:
        board: the board whose pads-by-net drive the netlist.
        exclude: glob patterns for nets to skip routing (their pads still act as
            obstacles); `None` excludes nothing.

    Returns:
        The MST connections for every non-excluded multi-pad net.
    """
    exclude = exclude or []
    conns: list[Connection] = []
    for net, pads in board.pads_by_net().items():
        if is_excluded(net, exclude):
            continue
        conns.extend(_mst_connections(net, pads))
    return conns


def greedy_order(connections: list[Connection], mode: str = "short",
                 seed: int | None = None) -> list[int]:
    """Compute the initial greedy routing order.

    Args:
        connections: the connections to order.
        mode: one of ``"short"`` (shortest first, default), ``"long"``
            (longest first — routes hard long connections while the board is
            clear), or ``"shuffle"`` (random — varies the starting state
            across runs so the annealer explores different configurations).
        seed: random seed for ``"shuffle"``; ``None`` uses the system source.

    Returns:
        Indices into `connections` in the requested order.
    """
    if mode == "long":
        return sorted(range(len(connections)),
                      key=lambda i: connections[i].est_length, reverse=True)
    if mode == "shuffle":
        import random as _random
        idx = list(range(len(connections)))
        _random.Random(seed).shuffle(idx)
        return idx
    return sorted(range(len(connections)),
                  key=lambda i: connections[i].est_length)


# ---------------------------------------------------------------------------
# Decoupling-capacitor → IC resolution
# ---------------------------------------------------------------------------

# Net-name classification. A decoupling cap bridges a power rail and ground;
# both are high-fanout, so naming is what tells them apart (with a fallback for
# unrecognised power names when the other net is clearly ground).
_GND_RE = re.compile(r"^(gnd|ground|agnd|dgnd|pgnd|gnda|gndd|vss|vssa|0v)$|^gnd",
                     re.IGNORECASE)
_PWR_RE = re.compile(r"^(vcc|vdd|vdda|vcca|vee|vbat|vin)"   # common rail names
                     r"|^[+-]\d"                            # +3V3, +5V, -12V
                     r"|^v\d",                              # V5, V33
                     re.IGNORECASE)

# A footprint is "IC-like" if its refdes looks like one (U.. / IC..) or it has
# at least this many pads (excludes 2-/3-pin passives and discretes).
_IC_PAD_THRESHOLD = 4


def _net_kind(net: str) -> str:
    """Classify a net name as ``"ground"``, ``"power"``, or ``"signal"``."""
    n = (net or "").strip()
    if not n:
        return "signal"
    if _GND_RE.match(n):
        return "ground"
    if _PWR_RE.match(n):
        return "power"
    return "signal"


def _fp_centroid(fp) -> tuple[float, float]:
    """Centroid of a footprint's pad centres (its origin if it has no pads)."""
    if not fp.pads:
        return (fp.x, fp.y)
    n = len(fp.pads)
    return (sum(p.cx for p in fp.pads) / n, sum(p.cy for p in fp.pads) / n)


def _is_ic_like(fp) -> bool:
    """Whether a footprint looks like an IC (by refdes or pad count)."""
    r = fp.ref.strip().upper()
    if r.startswith("IC"):
        return True
    if r[:1] == "U" and (len(r) == 1 or r[1].isdigit()):
        return True
    return len(fp.pads) >= _IC_PAD_THRESHOLD


def resolve_decoupling_ic(board: Board, cap):
    """Find the IC a decoupling cap serves by searching its nets.

    A decoupling cap bridges a **power** net and **ground**, both of which fan
    out to many footprints — so net membership alone is ambiguous. This narrows
    to footprints on the cap's *power* net that look like ICs, then picks the
    **nearest** to the cap (by pad-centroid distance), which matches how a
    decoupling cap is placed next to its IC's power pin. A warning is returned
    whenever the result is doubtful (no IC, a near-tie between two ICs, an
    unrecognised power name, or a non-unique refdes) or the part does not look
    like a decoupling cap at all (≠ 2 pad-nets, or it does not bridge power and
    ground).

    Args:
        board: the board to search.
        cap: the candidate decoupling-cap `pcb.Footprint`.

    Returns:
        ``(ic_ref, candidates, warning)``:
          - ``ic_ref``: the chosen IC's reference designator, or ``None`` if none
            could be chosen;
          - ``candidates``: all plausible IC refdes, nearest first (for a GUI
            chooser);
          - ``warning``: a human-readable caveat, or ``None`` when the match is
            unambiguous.
    """
    nets = []
    for p in cap.pads:
        if p.net and p.net not in nets:
            nets.append(p.net)
    if len(cap.pads) != 2 or len(nets) != 2:
        return (None, [], f"{cap.ref} has {len(nets)} pad-net(s); a decoupling "
                          "cap is expected to bridge two")

    grounds = [n for n in nets if _net_kind(n) == "ground"]
    powers = [n for n in nets if _net_kind(n) == "power"]
    others = [n for n in nets if _net_kind(n) == "signal"]

    note = None
    if powers:
        power_net = powers[0]
    elif grounds and others:
        power_net = others[0]            # fallback: the non-ground net is the rail
        note = (f"{cap.ref}: power net {power_net!r} not recognised by name; "
                "assuming it is the rail")
    else:
        return (None, [], f"{cap.ref} does not bridge power and ground; "
                          "may not be a decoupling cap")

    # Footprints with a pad on the power net (each counted once), excluding the
    # cap itself.
    owner: dict[int, object] = {}
    for fp in board.footprints:
        for p in fp.pads:
            owner[id(p)] = fp
    cand_fps: list = []
    seen: set[int] = set()
    for p in board.pads_by_net().get(power_net, []):
        fp = owner.get(id(p))
        if fp is None or fp is cap or id(fp) in seen:
            continue
        seen.add(id(fp))
        cand_fps.append(fp)

    ic_like = [fp for fp in cand_fps if _is_ic_like(fp)]
    pool = ic_like if ic_like else cand_fps
    if not pool:
        return (None, [], f"no IC found on net {power_net!r} for {cap.ref}")
    fallback_note = None if ic_like else (
        f"{cap.ref}: no obvious IC on net {power_net!r}; using nearest part")

    ccx, ccy = _fp_centroid(cap)

    def _dist(fp) -> float:
        x, y = _fp_centroid(fp)
        return math.hypot(x - ccx, y - ccy)

    pool.sort(key=_dist)
    candidates = [fp.ref for fp in pool]
    chosen = pool[0]
    warning = note or fallback_note

    if len(pool) >= 2 and _dist(pool[1]) <= _dist(pool[0]) * 1.15:
        warning = (f"{cap.ref} could serve {pool[0].ref} or {pool[1].ref}; "
                   f"chose nearest ({pool[0].ref}) — verify or pick manually")
    if sum(1 for fp in board.footprints if fp.ref == chosen.ref) > 1:
        dup = f"refdes {chosen.ref} is not unique on the board"
        warning = f"{warning}; {dup}" if warning else dup

    return (chosen.ref, candidates, warning)
