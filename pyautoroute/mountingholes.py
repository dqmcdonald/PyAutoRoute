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
from .pcb import Board, Pad

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
          pattern: str = "corners", hole_at: list[str] | None = None
          ) -> tuple[list, list[str]]:
    """Resolve, validate, and inject NPTH mounting holes into *board*.

    Footprint nodes are appended to ``board.tree`` and matching `Pad` objects to
    ``board.pads`` so the holes are written to the output and seen as fixed
    obstacles by the grid / DRC. A hole that lands outside the outline, collides
    with copper, or sits too close to another hole is skipped with a warning
    rather than failing the run.

    Args:
        board: the board to mutate (its outline defines the corners).
        rules: the `pyautoroute.rules.DesignRules` (copper clearance, hole
            spacing).
        diameter: drill diameter for every hole (mm).
        margin: inset of corner / edge holes from the board edge (mm).
        pattern: ``"corners"`` (seed TL/TR/BL/BR) or ``"custom"`` (``--hole-at``
            only).
        hole_at: raw ``--hole-at`` entries (each expanded via `expand_entry`).

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

    nodes: list = []
    placed: list[tuple[float, float]] = []
    for x, y, label in coords:
        pt = Point(x, y)
        if not poly.covers(pt):
            warnings.append(f"hole {label} at ({x:.1f}, {y:.1f}) is outside the "
                            "board outline — skipped")
            continue
        if any(abs(x - px) < 1e-3 and abs(y - py) < 1e-3 for px, py in placed):
            continue                          # duplicate request
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

        ref = f"MH{len(placed) + 1}"
        node = pcb.make_npth(x, y, diameter, ref=ref)
        board.tree.append(node)
        board.pads.append(Pad(
            net="", pad_type="np_thru_hole", shape="circle",
            cx=x, cy=y, w=diameter, h=diameter, angle=0.0,
            copper_layers=list(board.copper_layers), drill=diameter, fp_ref=ref,
        ))
        nodes.append(node)
        placed.append((x, y))

    if not placed and not warnings:
        warnings.append("mounting holes: no holes placed")
    return nodes, warnings
