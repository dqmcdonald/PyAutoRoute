"""Auto-added NPTH mounting holes (``--mounting-holes``).

Resolves hole positions from corner / edge **location codes** (or explicit
``x,y`` coordinates), validates them against the board outline and the existing
copper / holes, and injects non-plated through-hole (NPTH) footprints into the
board so they are both written to the output **and** treated as fixed routing
obstacles (via `pyautoroute.geometry.board_obstacles`).

Location codes resolve against the board's bounding box, inset by a margin.
KiCad board coordinates are **Y-down**, so "top" means minimum *y*:

| Code            | Anchor (after ``margin`` inset)        |
|-----------------|----------------------------------------|
| ``TL`` ``TR`` ``BL`` ``BR`` | the four corners            |
| ``T`` ``B`` ``L`` ``R``     | the mid-point of that edge  |
| ``C``           | the board centre                       |
| ``x,y``         | an explicit absolute coordinate (mm)   |
"""

from __future__ import annotations

from shapely.geometry import Point

from . import geometry, pcb
from .pcb import Board, Footprint, Pad

# Corner / edge / centre codes -> a function of the inset bounding box.
# Each lambda takes (minx, miny, maxx, maxy, m) and returns (x, y).
_CODES = {
    "TL": lambda x0, y0, x1, y1, m: (x0 + m, y0 + m),
    "TR": lambda x0, y0, x1, y1, m: (x1 - m, y0 + m),
    "BL": lambda x0, y0, x1, y1, m: (x0 + m, y1 - m),
    "BR": lambda x0, y0, x1, y1, m: (x1 - m, y1 - m),
    "T":  lambda x0, y0, x1, y1, m: ((x0 + x1) / 2.0, y0 + m),
    "B":  lambda x0, y0, x1, y1, m: ((x0 + x1) / 2.0, y1 - m),
    "L":  lambda x0, y0, x1, y1, m: (x0 + m, (y0 + y1) / 2.0),
    "R":  lambda x0, y0, x1, y1, m: (x1 - m, (y0 + y1) / 2.0),
    "C":  lambda x0, y0, x1, y1, m: ((x0 + x1) / 2.0, (y0 + y1) / 2.0),
}


def _is_float(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _is_coord_token(tok: str) -> bool:
    """True if *tok* is an explicit ``x,y`` coordinate (vs. a location code)."""
    parts = [p.strip() for p in tok.split(",")]
    return len(parts) == 2 and all(_is_float(p) for p in parts)


def positions_known_preplacement(pattern: str, hole_at, keep_outline: bool) -> bool:
    """Whether every requested hole position is resolvable *before* placement.

    Corner / edge / centre codes resolve against the board outline, so they are
    only known up front when the outline is fixed (``keep_outline``). Explicit
    ``x,y`` positions are always known. This decides whether the holes can be
    injected before the annealer (as fixed keep-outs it is pushed away from) or
    must wait until placement has generated the outline.

    Args:
        pattern: ``"corners"`` or ``"custom"``.
        hole_at: the raw ``--hole-at`` entries (or ``None``).
        keep_outline: whether placement keeps the board's existing outline.

    Returns:
        True if all positions are resolvable before placement.
    """
    if keep_outline:
        return True
    if pattern == "corners":
        return False                      # TL/TR/BL/BR need the (generated) outline
    tokens: list[str] = []
    for entry in (hole_at or []):
        tokens += expand_entry(entry)
    return bool(tokens) and all(_is_coord_token(t) for t in tokens)


def expand_entry(entry: str) -> list[str]:
    """Split one ``--hole-at`` value into individual position tokens.

    An entry is treated as a single ``x,y`` coordinate when it is exactly two
    comma-separated numbers; otherwise it is split on commas into one or more
    location codes (so ``"TL,TR,BL"`` yields three codes).

    Args:
        entry: a raw ``--hole-at`` value.

    Returns:
        The individual position tokens (codes and/or a single ``"x,y"``).
    """
    parts = [p.strip() for p in entry.split(",")]
    if len(parts) == 2 and all(_is_float(p) for p in parts):
        return [f"{parts[0]},{parts[1]}"]
    return [p for p in parts if p]


def resolve_positions(bounds: tuple[float, float, float, float],
                      tokens: list[str], margin: float
                      ) -> tuple[list[tuple[float, float, str]], list[str]]:
    """Resolve position tokens to absolute ``(x, y)`` coordinates.

    Args:
        bounds: the board bounding box ``(minx, miny, maxx, maxy)`` (mm).
        tokens: location codes and/or ``"x,y"`` strings.
        margin: inset from the bounding box for the corner / edge codes (mm).

    Returns:
        ``(coords, warnings)`` where ``coords`` is a list of
        ``(x, y, label)`` triples and ``warnings`` lists unrecognised tokens.
    """
    x0, y0, x1, y1 = bounds
    coords: list[tuple[float, float, str]] = []
    warnings: list[str] = []
    for tok in tokens:
        code = tok.strip().upper()
        if code in _CODES:
            x, y = _CODES[code](x0, y0, x1, y1, margin)
            coords.append((x, y, code))
            continue
        parts = [p.strip() for p in tok.split(",")]
        if len(parts) == 2 and all(_is_float(p) for p in parts):
            coords.append((float(parts[0]), float(parts[1]), tok.strip()))
            continue
        warnings.append(f"unrecognised hole position '{tok}'")
    return coords, warnings


def build(board: Board, rules, *, diameter: float, margin: float,
          pattern: str = "corners", hole_at: list[str] | None = None,
          lock: bool = False) -> tuple[list, list[str]]:
    """Resolve, validate, and inject NPTH mounting holes into *board*.

    Footprint nodes are appended to ``board.tree`` and matching `Pad` objects to
    ``board.pads`` so the holes are written to the output and seen as fixed
    obstacles by the grid / DRC. A hole that lands outside the outline, collides
    with copper, or sits too close to another hole is skipped with a warning
    rather than failing the run.

    Boards that already carry holes are handled: a requested position that
    coincides with an existing hole is treated as already satisfied (skipped with
    an informational note, so re-running is idempotent), existing drills are
    honoured for hole-to-hole spacing, and new reference designators are chosen to
    avoid colliding with refs already on the board.

    When ``lock`` is set, each accepted hole is also registered as a **locked
    footprint** in ``board.footprints`` (not just a pad), so the placement
    annealer treats it as a fixed obstacle and pushes footprints away from it.
    Use this only when injecting *before* placement (see
    `positions_known_preplacement`).

    Args:
        board: the board to mutate (its outline defines the corners).
        rules: the `pyautoroute.rules.DesignRules` (copper clearance, hole
            spacing).
        diameter: drill diameter for every hole (mm).
        margin: inset of corner / edge holes from the board edge (mm).
        pattern: ``"corners"`` (seed TL/TR/BL/BR) or ``"custom"`` (``--hole-at``
            only).
        hole_at: raw ``--hole-at`` entries (each expanded via `expand_entry`).
        lock: also register each hole as a locked footprint (placement keep-out).

    Returns:
        ``(nodes, warnings)`` — the appended footprint nodes (for reference) and
        any per-hole warning strings.
    """
    warnings: list[str] = []
    try:
        poly = geometry.outline_to_polygon(board.outline)
    except Exception:
        return [], ["mounting holes skipped: no closed board outline"]

    tokens: list[str] = ["TL", "TR", "BL", "BR"] if pattern == "corners" else []
    for entry in (hole_at or []):
        tokens += expand_entry(entry)
    if not tokens:
        return [], ["mounting holes requested but no positions given "
                    "(use --hole-at or --hole-pattern corners)"]

    coords, warns = resolve_positions(poly.bounds, tokens, margin)
    warnings += warns

    radius = diameter / 2.0
    clear = rules.clearance_for("")          # copper keep-out around the hole
    obs = geometry.board_obstacles(board)
    existing = geometry.board_drills(board)
    next_ref = _ref_allocator(board)
    # A requested hole this close (centre-to-centre) to an existing hole is
    # treated as "already there" rather than a spacing failure — keeps re-runs
    # idempotent and lets a user re-request a corner that is already drilled.
    coincide = max(radius, 0.5)

    nodes: list = []
    placed: list[tuple[float, float]] = []
    for x, y, label in coords:
        pt = Point(x, y)
        if not poly.covers(pt):
            warnings.append(f"hole {label} at ({x:.1f}, {y:.1f}) is outside the "
                            "board outline — skipped")
            continue
        if any(abs(x - px) < 1e-3 and abs(y - py) < 1e-3 for px, py in placed):
            continue                          # duplicate request in this call
        # already a hole here (existing board hole or a previous run)?
        if any(pt.distance(d.geom) <= coincide for d in existing):
            warnings.append(f"hole {label} at ({x:.1f}, {y:.1f}): a hole already "
                            "exists here — skipped")
            continue
        # clear of copper?
        disk = pt.buffer(radius + clear)
        if any(o.geom.intersects(disk) for o in obs):
            warnings.append(f"hole {label} at ({x:.1f}, {y:.1f}) overlaps copper "
                            "— skipped")
            continue
        # clear of existing + already-placed holes (hole-to-hole spacing)?
        too_close = any(
            pt.distance(d.geom) - radius - d.radius < rules.min_hole_to_hole - 1e-6
            for d in existing
        ) or any(
            ((x - px) ** 2 + (y - py) ** 2) ** 0.5 - 2 * radius
            < rules.min_hole_to_hole - 1e-6
            for px, py in placed
        )
        if too_close:
            warnings.append(f"hole {label} at ({x:.1f}, {y:.1f}) too close to "
                            "another hole — skipped")
            continue

        ref = next_ref()
        node = pcb.make_npth(x, y, diameter, ref=ref)
        board.tree.append(node)
        pad = Pad(
            net="", pad_type="np_thru_hole", shape="circle",
            cx=x, cy=y, w=diameter, h=diameter, angle=0.0,
            copper_layers=list(board.copper_layers), drill=diameter, fp_ref=ref,
        )
        board.pads.append(pad)
        if lock:
            board.footprints.append(Footprint(
                ref=ref, x=x, y=y, angle=0.0, locked=True, overlap_ok=False,
                pads=[pad], local_offsets=[(0.0, 0.0, 0.0)],
                at_node=pcb.child(node, "at"), fp_node=node,
                x0=x, y0=y, angle0=0.0,
            ))
        nodes.append(node)
        placed.append((x, y))

    if not placed and not warnings:
        warnings.append("mounting holes: no holes placed")
    return nodes, warnings


def _ref_allocator(board: Board):
    """Return a callable that yields fresh ``MH<n>`` refs not already in use.

    Scans existing footprint refs and pad ``fp_ref`` values so re-running on a
    board that already has mounting holes (or other ``MH*`` refs) never produces
    a duplicate reference designator.

    Args:
        board: the board whose existing refs are reserved.

    Returns:
        A zero-argument function returning the next unused ``"MH<n>"`` ref.
    """
    used = {fp.ref for fp in board.footprints if fp.ref}
    used |= {p.fp_ref for p in board.pads if p.fp_ref}
    counter = {"n": 0}

    def _next() -> str:
        while True:
            counter["n"] += 1
            ref = f"MH{counter['n']}"
            if ref not in used:
                used.add(ref)
                return ref

    return _next
