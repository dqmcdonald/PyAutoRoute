# Plan: drill geometry, hole-to-hole DRC, and auto-added mounting holes

Status: **Shipped** (0.49.0 drill DRC; 0.50.0 mounting holes; 0.51.0 GUI).
All four phases landed. This is a design record. It expands
roadmap items **#5 (drill geometry + hole-to-hole DRC)** and **#6 (auto-add
mounting holes, `--mounting-holes`)** from
[`feature-suggestions.md`](feature-suggestions.md), and adds the requested
extension: **choosing how many holes and where** via corner codes (`TL`, `TR`,
`BL`, `BR`, edge midpoints, and explicit coordinates).

## Goal

Two tightly-related capabilities that share one piece of new machinery — *drill
geometry as a first-class obstacle/DRC entity*:

1. **Drill DRC (#5)** — model drill holes as geometry, honour the already-parsed
   `min_hole_to_hole` in the in-repo self-check, and treat plated/non-plated
   holes as routing keep-outs so the router never drives copper across a barrel.
2. **Mounting holes (#6)** — an opt-in step that drops NPTH (non-plated
   through-hole) mounting holes at chosen positions on the board outline, as
   real KiCad footprints, registered as fixed obstacles so routing (and
   placement) avoids them.

```
pyautoroute board.kicad_pcb --mounting-holes \
    [--hole-diameter 3.2] [--hole-margin 5.0] \
    [--hole-at TL,TR,BL,BR]            # corner/edge codes …
    [--hole-at 10,10 --hole-at 90,60]  # … and/or explicit x,y (mm)
```

## Motivation

Both items close real gaps with scaffolding that mostly already exists:

- `min_hole_to_hole` is **parsed and stored but never read** anywhere
  (`rules.py:45`, default `rules.py:195`, parse `rules.py:254`).
- `Pad.drill` and `Pad.pad_type` (`thru_hole` / `np_thru_hole`) are **modelled**
  (`pcb.py:132,142`) and **parsed** (`pcb.py:458-466`) but **never consulted** by
  the grid, geometry, or router — a through-hole barrel is treated as ordinary
  copper, and an NPTH hole with no copper layers becomes *nothing at all*.
- `geometry.board_obstacles` iterates `board.pads` using `pad.copper_layers`
  only (`geometry.py:243-246`); an `np_thru_hole` pad with no copper layers
  therefore contributes **no obstacle**, so the router can route straight
  through a mounting hole.
- `geometry.clearance_violations` checks only same-layer inter-net **copper**
  spacing (`geometry.py:265-301`); there is **no** drill-to-drill,
  hole-to-copper, or NPTH keep-out check.

Adding mounting holes by hand after an auto-route is a routine, fiddly task;
PyAutoRoute already knows the outline (`board.outline`,
`geometry.outline_to_polygon`, `geometry.py:173`) and already builds board
nodes in the board's own style, so it is well placed to drop them in.

## Key insight: a drill barrel is a net-agnostic circular keep-out

Copper obstacles in this codebase are **per-layer, net-tagged** polygons
(`geometry.Obstacle`, `geometry.py:225-229`). A drill barrel is different in two
ways and the design must reflect both:

- It is **all-layer** — a hole passes through every copper layer regardless of
  the pad's copper annular ring.
- It is **net-agnostic for spacing** — `min_hole_to_hole` is a single board rule
  (`rules.py:45`), not a per-net-class clearance. Two holes on the *same* net
  still must respect hole-to-hole spacing, unlike copper.

So drills get their **own** lightweight geometry type and their **own** STRtree
pass, rather than being shoehorned into the copper `Obstacle` list. Copper
keep-out around a hole (so the router avoids the barrel) is registered
**separately** as an all-layer copper obstacle with `clearance + drill_radius`,
reusing the existing inflation path.

## Reused infrastructure

| Piece | File | Role |
|---|---|---|
| `Pad.drill` / `Pad.pad_type` | pcb.py:132,142 | already-parsed hole size & type |
| `rules.min_hole_to_hole` | rules.py:45,254 | already-parsed h2h spacing rule |
| `geometry.board_obstacles` | geometry.py:232 | where copper keep-outs are collected |
| `geometry.clearance_violations` | geometry.py:265 | the self-check to extend |
| `STRtree` neighbour query | geometry.py:286-291 | reuse pattern for the h2h pass |
| `geometry.outline_to_polygon` / `.bounds` | geometry.py:173 | board polygon → corner/edge coords |
| `geometry.inflate` | geometry.py:108 | grow a keep-out disk by the margin |
| grid obstacle raster (`board_obstacles` → inflate) | grid.py:247-252 | holes become routing obstacles for free once in `board_obstacles` |
| `make_via` / `make_segment` / `make_edge_rect` / `_net_ref_node` | pcb.py:1024,1051,1133,990 | node-builder patterns to mirror for `make_npth` |
| `write_board(..., new_nodes=…)` | pcb.py:1563 | append the mounting-hole footprints |
| `groundplane.build(...) -> (nodes, warnings)` | groundplane.py:14 | exact shape to mirror for `mountingholes.build` |
| `report.RoutingStats` / `--report` (if landed) | report.py | surface drill-DRC counts |

**There is no built-in "mounting hole" concept** — NPTH footprints are
synthesized in KiCad's `MountingHole` style.

## Reference: a KiCad NPTH mounting-hole footprint

```lisp
(footprint "MountingHole:MountingHole_3.2mm_M3"
    (layer "F.Cu")
    (at 10 10)
    (uuid "…")
    (attr exclude_from_pos_files exclude_from_bom allow_missing_courtyard)
    (pad "" np_thru_hole circle
        (at 0 0) (size 3.2 3.2) (drill 3.2)
        (layers "*.Cu" "*.Mask")
        (uuid "…")))
```

Key points the builder must honour:

- `np_thru_hole` pad with **no `(net …)`** — it carries no copper net.
- `size == drill` (no annular ring; pure hole). Copper layers `*.Cu` only so the
  keep-out is recognised on every layer; mask layers are cosmetic.
- `(attr …)` excludes it from BoM / position files — it is mechanical.

## Components

### A. Drill geometry + hole-to-hole DRC (item #5)

#### A1. A drill-geometry helper

Add to `geometry.py`:

```python
@dataclass
class Drill:
    geom: Point          # barrel centre (shapely Point)
    radius: float        # drill_diameter / 2
    plated: bool         # thru_hole=True, np_thru_hole=False
    ref: str             # owning footprint refdes (for messages)

def board_drills(board: Board) -> list[Drill]:
    """Every drilled hole on the board: thru_hole and np_thru_hole pads
    (and routed vias, optionally) with a non-None drill."""
```

Source the holes from `board.pads` where `pad.pad_type in {thru_hole,
np_thru_hole}` and `pad.drill` is set (`pcb.py:458-466`). Vias also drill the
board (`Via.drill`, `pcb.py:151`); whether routed vias participate in the h2h
check is an **open question** below — start with **pads only**, since via
spacing is already governed by the grid's `via_margin` (`grid.py:83`).

#### A2. Hole-to-hole DRC pass

Add `geometry.drill_violations(board, rules)`:

```python
def drill_violations(board, rules) -> list[tuple[str, str, float]]:
    """(ref_a, ref_b, gap) for each hole pair closer than min_hole_to_hole.
    Edge-to-edge gap = centre_distance - r_a - r_b."""
```

Mirror the `clearance_violations` STRtree structure (`geometry.py:279-301`) but:
single all-layer pass, **no net exclusion** (same-net holes still count), and
the required spacing is the flat `rules.min_hole_to_hole` (`rules.py:45`). Wire
it into the self-check alongside copper violations and report the count.

#### A3. Holes as routing obstacles

The cleanest hook: make `board_obstacles` (`geometry.py:232`) emit an **all-layer
copper keep-out disk** for every drilled pad, regardless of its copper layers.
Today the loop keys off `pad.copper_layers` (`geometry.py:243-246`), so a layerless
NPTH pad is skipped. Add, after the pad loop:

```python
for pad in board.pads:
    if pad.pad_type in ("thru_hole", "np_thru_hole") and pad.drill:
        disk = Point(pad.cx, pad.cy).buffer(pad.drill / 2.0)
        for layer in board.copper_layers:
            obs.append(Obstacle(disk, pad.net, layer))   # net "" for NPTH
```

Because the grid inflates every obstacle by `margin` before rasterising
(`grid.py:247-252`, `margin = hypot(track/2 + clear, safety)`, `grid.py:82`), the
barrel automatically gains a `drill_radius + clearance` keep-out and the router
will not cross it — **no router change needed**. (A plated `thru_hole` pad's
*copper* is already an obstacle via `copper_layers`; this adds the **barrel** for
the NPTH case and guarantees the hole itself, not just the ring, is reserved.)

> **Invariant to document:** with this change the grid is DRC-clean *with respect
> to drilled holes too* — copper can never be routed across a barrel, on either
> layer, for the same reason it can't cross a pad: the inflated keep-out is baked
> into the grid before A\* runs.

### B. NPTH node builder

Add `pcb.make_npth(x, y, drill_mm, *, ref="MH", style="footprint") -> SList`
beside `make_via`/`make_segment` (`pcb.py:1024-1075`):

- **`footprint` style (default):** emit a `(footprint …)` node containing one
  `np_thru_hole` circle pad, `size == drill == drill_mm`, layers `"*.Cu" "*.Mask"`,
  no net, with `(attr exclude_from_pos_files exclude_from_bom)`. Matches KiCad's
  `MountingHole` library and round-trips through `pcb.parse` so the new hole is
  seen as a `Pad` on reload (and thus picked up by A1/A3 next run).
- Fresh `uuid` per node (mirror `make_via`, `pcb.py:1074`).
- A bare top-level `(pad "" np_thru_hole …)` is simpler but KiCad prefers
  footprints; the footprint form also makes the hole visible to A1's
  `board.pads` scan after reload, so prefer it.

The hole carries **no net**, so `_net_ref_node` is *not* used — important
difference from the via/segment builders.

### C. Mounting-hole placement + location codes (item #6 + extension)

New module `pyautoroute/mountingholes.py`, mirroring `groundplane.build`
(`groundplane.py:14`):

```python
def build(board, rules, *, diameter, margin, positions) -> tuple[list, list]:
    """Return (footprint_nodes, warnings) for the requested mounting holes."""
```

#### C1. Resolving positions — the location-code grammar

`--hole-at` accepts a comma-separated list; each token is either a **named
anchor** or an explicit **`x,y`** coordinate (mm). Named anchors resolve against
the board polygon's bounding box (`outline_to_polygon(board.outline).bounds` →
`(minx, miny, maxx, maxy)`, `geometry.py:173`), inset by `--hole-margin`:

| Code | Anchor (after `margin` inset) |
|---|---|
| `TL` | top-left corner |
| `TR` | top-right corner |
| `BL` | bottom-left corner |
| `BR` | bottom-right corner |
| `T` / `B` / `L` / `R` | mid-point of that edge |
| `C` | board centre |
| `x,y` | explicit absolute coordinate (mm), used verbatim (margin ignored) |

**Y-axis note:** KiCad board coords are **Y-down** (`geometry.py:3`), so "top"
means **minimum y**. `TL = (minx + margin, miny + margin)`,
`BR = (maxx - margin, maxy - margin)`, etc. This must be stated in `--help` and
the README to avoid the classic flipped-corner bug.

Convenience: `--hole-pattern corners` is sugar for `--hole-at TL,TR,BL,BR`
(four-corner is by far the common case). Default when `--mounting-holes` is given
bare is `corners`. "How many holes" is therefore answered directly by the length
of `--hole-at` (or `corners` → 4).

#### C2. Validity guards (collected as warnings, not hard failures)

For each resolved `(x, y)`:

- **Inside the outline?** Point must lie within `outline_to_polygon(...)` (after a
  small tolerance). If the inset pushed a corner outside a non-rectangular
  outline, warn and skip that hole.
- **Clear of copper / other holes?** Check the hole's `drill/2 + margin` disk
  against `board_obstacles` (copper) and `board_drills` (other holes,
  `min_hole_to_hole`). If it collides with a pad or routed track, **warn and
  skip** (a mounting hole that shorts copper is worse than a missing one). Do not
  auto-nudge in v1 — predictable positions matter more; nudging is a later
  enhancement.
- **Duplicate position?** De-dupe coincident requests.

Failures are per-hole warnings printed like the ground-plane warnings
(`autoroute.py:997-998`), never aborting the run.

#### C3. Ordering relative to routing & placement

- **Default (route-only):** resolve and validate holes **before routing**, inject
  the NPTH footprints into the board *and* into `board.pads` so the grid treats
  them as fixed obstacles (B + A3), then route around them. Append the nodes via
  `write_board(new_nodes=…)`.
- **With `--place`:** holes are **fixed obstacles** — inject them *before* the
  annealer runs so footprints are pushed away (`placement.py` reads the same
  obstacle geometry). Per item #6 this is the main subtlety. With
  `--keep-outline` the outline (hence corners) is fixed and well-defined; without
  it, the outline is generated first (`pcb.pad_bounding_outline`, `pcb.py:1173`),
  **then** holes are placed against the finalised bounds.
- **With `--cycles` / best-of-N:** add holes to the winning board before its
  write (same place the ground plane hooks in, `autoroute.py:1050-1062`,
  `1272-1287`).

### D. CLI wiring (autoroute.py)

In `build_parser` (`autoroute.py:1859`), beside the ground-plane block
(`autoroute.py:2046-2057`):

```python
p.add_argument("--mounting-holes", action="store_true",
               help="add NPTH mounting holes (default pattern: four corners)")
p.add_argument("--hole-diameter", type=float, default=3.2, metavar="MM",
               help="mounting-hole drill diameter (default 3.2 for M3)")
p.add_argument("--hole-margin", type=float, default=5.0, metavar="MM",
               help="inset of corner/edge holes from the board edge")
p.add_argument("--hole-pattern", choices=["corners", "custom"], default="corners",
               help="'corners' = TL,TR,BL,BR; 'custom' = use --hole-at only")
p.add_argument("--hole-at", action="append", default=None, metavar="POS",
               help="hole position: a code (TL/TR/BL/BR/T/B/L/R/C) or 'x,y' mm; "
                    "repeatable and/or comma-separated")
```

Hook `mountingholes.build` into `run()` at the points the ground plane already
uses (`autoroute.py:978-1008` for the main path; mirror in the `--cycles` and
best-of-N paths). When `--place` is active, the injection must happen earlier,
before the placement pass — add a small pre-place hook rather than reusing the
post-route one.

## Files & tests

**Code**
- `pyautoroute/geometry.py` — `Drill`, `board_drills`, `drill_violations`; extend
  `board_obstacles` to emit all-layer barrel keep-outs for drilled pads.
- `pyautoroute/pcb.py` — `make_npth(...)`.
- `pyautoroute/mountingholes.py` (new) — `build(board, rules, *, diameter,
  margin, positions) -> (nodes, warnings)`, position-code resolver, guards.
- `pyautoroute/autoroute.py` — the five flags above; pre-place + post-route hooks;
  warning printout; drill-DRC count in the self-check summary.

**Tests** (headless — node generation + geometry, no kicad-cli needed)
- `make_npth` round-trips: build → `write_board` → reload → the hole appears as
  an `np_thru_hole` `Pad` with the right `drill` and no net.
- `board_drills` collects thru_hole + np_thru_hole pads; skips SMD.
- `drill_violations`: two holes closer than `min_hole_to_hole` → one violation;
  same-net holes still flagged; spaced holes → none.
- `board_obstacles` now reserves a barrel: a synthetic NPTH pad makes the
  covering grid nodes non-free (`grid.is_free` False over the disk), and a route
  that previously crossed the hole is forced around it.
- Location codes: on a known rectangular outline, `TL/TR/BL/BR/T/B/L/R/C` resolve
  to the expected `(x, y)` with the **Y-down** convention; `x,y` parses verbatim;
  bad token → warning.
- Guards: a hole requested on top of a pad/track → warned + skipped; a hole inset
  outside a non-rect outline → warned + skipped.
- Placement interaction: with `--place`, an injected hole repels footprints (a
  footprint that would overlap the hole disk is moved off it).

**Docs / versioning** (per `CLAUDE.md` — part of "done")
- `README.md` — a "Mounting holes & drill DRC" subsection: the flags, the
  location-code table (with the Y-down/top=min-y caveat), examples, and the
  "holes are routing obstacles" note.
- `docs/architecture.md` — the drill-geometry obstacle type, the `board_drills`
  / `drill_violations` self-check pass, the `mountingholes` module, and the new
  invariant ("copper is never routed across a barrel — the inflated keep-out is
  baked into the grid before A\*").
- Regenerate API docs (`pdoc -d google --mermaid …`); add/refresh docstrings on
  the new functions.
- `CHANGES.md` + a **minor** version bump (new feature + new CLI flags). Drill
  DRC and mounting holes can land as one `0.49.0` or split across two minors
  (DRC first, then `--mounting-holes`) — see Phasing.

## Phasing

1. ✅ **Drill DRC core (#5)** — `Drill` / `board_drills` / `drill_violations`, the
   `board_obstacles` barrel keep-out, self-check integration + a `drill-check:`
   report line. Shipped in 0.49.0.
2. ✅ **`make_npth` + `--mounting-holes corners`** — NPTH holes injected as fixed
   obstacles; warnings for collisions. Shipped in 0.50.0.
3. ✅ **Location codes + `--hole-at` + placement interaction** — the full code
   grammar (`TL`/edge/`C`/`x,y`), `--hole-pattern custom`, and post-placement /
   pre-grid injection so holes sit on the finalised outline and the router
   respects them. Shipped in 0.50.0.
4. ✅ **GUI exposure** — a "Mounting holes" checkbox + drill / edge-margin
   fields, a corners/custom pattern picker, and an extra-positions entry in the
   Post-processing panel, mirroring the ground-plane controls; the worker injects
   the holes before the grid is built (after placement), like the CLI. Shipped in
   0.51.0.

> **Implementation note (vs. the original plan).** Injection happens at a single
> point in `autoroute.run` — after any placement finalises the outline and before
> the grid is built — covering the place-only, place+route, and route-only paths
> uniformly (rather than a separate pre-place hook). The `--cycles` path routes
> each cycle on a board reloaded from disk, so holes are injected into the winning
> board afterwards; a track crossing a hole is then surfaced by the self-/drill-
> check rather than avoided. Collisions are skipped-with-warning (no auto-nudge),
> as planned.

## Risks & open questions

- **Vias in the h2h check.** Should routed vias (`Via.drill`, `pcb.py:151`)
  participate in `drill_violations`? Their spacing is already constrained by the
  grid's `via_margin` (`grid.py:83`), so v1 checks **pads only** to avoid
  double-counting / false positives on legal dense via fields. Revisit if real
  boards show via↔THT-pad hole conflicts.
- **`min_hole_to_hole` is board-global.** It is a single rule (`rules.py:45`),
  not per-class; the DRC pass is therefore net-agnostic. If KiCad later grows
  per-class hole rules, the pass would need the same per-class treatment as
  copper — out of scope here.
- **Oval / slotted drills.** `_parse_pad` collapses an oval drill to its larger
  dimension (`pcb.py:466`); the keep-out disk is then conservative (slightly
  over-reserves). True slot geometry is a later refinement.
- **Outline corners on non-rectangular boards.** Corner codes use the bounding
  box, so on an L-shaped or rounded outline a `TL` inset can land *outside* the
  copper region; the C2 inside-outline guard catches this (warn + skip). A future
  enhancement could snap to the nearest in-board point.
- **Y-down convention.** "Top" = min y. Easy to get backwards; covered by an
  explicit test and called out in `--help`/README.
- **Collision policy.** v1 **skips** (warns) a hole that hits copper rather than
  nudging it, because predictable mechanical positions matter; auto-nudge/spiral
  search (like the ground-plane connectivity vias, `groundplane.py:293`) is a
  candidate later enhancement.
- **Refill interaction.** If `--ground-plane` is also on, the NPTH barrels must
  show up as keep-outs in the pour. They already will if injected into
  `board.pads` before the zone is written and KiCad refills (KiCad clears copper
  around NPTH holes during fill) — confirm on a real board with kicad-cli.
- **`--keep-outline` + generated outline timing.** Holes must be resolved after
  whichever outline is final (kept vs. generated, `autoroute.py:525-527`); the
  pre-place hook must run after outline finalisation.
