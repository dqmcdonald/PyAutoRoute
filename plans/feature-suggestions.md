# Feature suggestions / roadmap

A set of proposed new features for PyAutoRoute, grounded in a deep read of the
codebase. Each suggestion cites the code it builds on or the gap it fills
(`file:line`). Items are ordered by value-to-effort. This is a planning document,
not a commitment — items here are candidates, not scheduled work.

This is the project's **central roadmap**: the completed-feature plan docs
(`autorouter_plan.md`, `gui-plan.md`, `place-feature-plan.md`) and the
performance/tuning docs point here for cross-cutting outstanding work, and link
back for their own residual TODOs.

> **Recently landed** (see `CHANGES.md`):
> - **Bounded A\* search** (`--search-margin`) — shipped in 0.25.0.
> - **Vectorised A\* overlay** — shipped in 0.25.3.
> - **Ground plane** (`--ground-plane`, `--stitch-vias`, `--ground-plane-layer`) —
>   shipped; see [`ground-plane-plan.md`](ground-plane-plan.md).
> - **Board comparison** (`pyautoroute-compare`) — shipped; see
>   [`board-comparison-plan.md`](board-comparison-plan.md).
> - **Partial / incremental re-routing** (`--existing-routes preserve`) — shipped;
>   keeps existing copper, detects pre-routed connections via union-find, routes
>   only the remainder. Fixes the doubled-tracks bug on re-route (`clear` mode
>   now also strips segments, not just vias). Closes item #3 below.
> - **Edge-aware placement + place↔route coupling** — shipped; see
>   [`placement-improvements-plan.md`](placement-improvements-plan.md).
> - **Interactive footprint constraints** (GUI click-to-set edge/lock/overlap) —
>   shipped; see [`footprint-interaction-plan.md`](footprint-interaction-plan.md).
> - **Differential pair routing** (`--diff-pairs`, `--diff-pair-gap`) — shipped in
>   0.38.0; see [`diffpair-plan.md`](diffpair-plan.md). Closes item #2 below.
> - **`--scatter` + ranked cycle summary** — shipped in 0.45.0. `--scatter`
>   randomises all unlocked footprint positions/rotations before each `--cycles`
>   pass, diversifying annealer starting layouts; `--cycles` now prints a ranked
>   energy table at the end (winner marked ★). Also exposed in the GUI as a
>   *Scatter start* checkbox.
> - **Placement polish** (`--place-polish`) — shipped in 0.46.0. After annealing
>   settles, a steepest-descent pass (central-difference gradient, backtracking
>   line search) relaxes close contacts and slides parts to their local energy
>   minimum — monotone, so it can never worsen the annealed result. Knobs:
>   `--place-polish-iters`, `--place-polish-eps`, `--place-polish-time`. GUI
>   controls added shortly after. See [`placement-polish-plan.md`](placement-polish-plan.md).
> - **Decoupling-cap marking** (`Autoroute-decouple`, `--place-decouple-weight`)
>   — shipped in 0.47.0. A footprint property (`auto` or explicit IC ref) pulls a
>   cap toward its IC during `--place`; IC resolved by searching the cap's power
>   net for the nearest IC-like part. Settable via GUI right-click menu.
>   See [`decoupling-cap-plan.md`](decoupling-cap-plan.md).

## Context

PyAutoRoute is a 2-layer KiCad autorouter with a
parse → grid → A\* route → simulated-annealing pipeline (`pcb.py`, `grid.py`,
`router.py`, `anneal.py`), an experimental footprint-placement pass
(`placement.py`), a parameter-sweep tuner (`tune.py`), and a functional Tkinter
GUI (`gui/`). Routing is **DRC-clean by construction** — the grid inflates every
obstacle by `margin = hypot(max_track/2 + max_clearance, safety)` so A\* can only
return clearance-legal paths (`grid.py:69-88`, [`architecture.md`](architecture.md)).
The suggestions below extend that machinery rather than replacing it.

## High-value features

### 1. Bounded / windowed A\* search

`router.astar` is spatially unbounded: it searches the whole grid for every
connection, capped only by `params.max_expansions` (default 2,000,000,
`router.py:55`). [`architecture.md:459`](architecture.md) calls this out as
**"the highest-value next optimisation"** — "A\* is unbounded in search area, so
a few long nets dominate runtime."

Proposal: constrain each connection's search to a slack box around its source
and target, expanding the box and retrying on failure. The heuristic field is
already precomputed (`router.py:310-327`), so clamping the frontier is a
localized change. Biggest performance win; explicitly intended future work.

### 2. ✅ Differential pair routing (`--diff-pairs`) — **shipped in 0.38.0**

Detects paired nets by naming convention (`+`/`-`, `P`/`N`, `_P`/`_N`) via
`netlist.find_diff_pairs()`; routes them with a coupled A* in `diffpair.py` that
advances both traces simultaneously using a fixed grid-node offset — guaranteeing
zero length skew by construction. Diff pair copper is baked into the grid's static
`owner` array after the pre-routing pass so the annealing loop needs no changes.
`rules.dp_gap_for()` reads `differential_pair_gap` from the net class (falling back
to clearance). After routing, a per-pair table reports length, skew, vias, and
estimated differential impedance (IPC-2141A microstrip) using substrate parameters
from the new `Board.stackup` (parsed from the PCB file's `(setup (stackup …))`
block). See [`diffpair-plan.md`](diffpair-plan.md) for the full design record.

### 3. ✅ Incremental / partial re-routing (`--existing-routes preserve`) — **shipped**

`--existing-routes {clear,preserve}` (default `clear`). In `preserve` mode: keep
existing copper, run a layer-aware union-find over segments/vias/THT pads to
classify each MST connection as pre-routed or unrouted, pass only unrouted
connections to the router, and treat all existing copper as obstacles. `clear` mode
(the new default) also strips segments before writing — fixing the doubled-tracks
bug that occurred when re-routing an already-routed board.

### 4. Per-net-class clearance masks

The grid inflates **every** obstacle by a single global worst-case margin:
`max_track`, `max_via`, and `_max_clear` are each taken as the maximum across
*all* net classes (`grid.py:78-80`), and committed copper inherits the same
margin (`router.py:231-235`). On a mixed-class board a thin low-clearance signal
is reserved as much space as the widest power net needs.
[`architecture.md:457`](architecture.md) notes "exact per-net masks would route
denser mixed-rule boards." Per-class (or per-layer) inflation closes a documented
limitation.

## Medium-value features

### 5. Drill geometry + hole-to-hole DRC

The richest low-hanging item, because the scaffolding is already half-built:

- `min_hole_to_hole` is **parsed and stored but never read** anywhere
  (`rules.py:45,174,231`).
- `Pad.drill` and `Pad.pad_type` (thru_hole / np_thru_hole) are modelled
  (`pcb.py:124,134`) but **never consulted** by the grid/geometry/router — a
  through-hole pad is treated identically to SMD copper.
- `geometry.clearance_violations` checks only same-layer inter-net **copper**
  spacing (`geometry.py:265-301`); there is no drill geometry, so no
  drill-to-drill, hole-to-copper, or NPTH keep-out check.

Proposal: model drill holes as geometry, add a drill-to-drill STRtree pass to the
self-check using the already-parsed `min_hole_to_hole`, and (optionally) treat
holes as routing obstacles. Closes a real DRC gap with machinery that mostly
exists.

### 6. Auto-add mounting holes (`--mounting-holes`)

A common post-routing task: add NPTH (non-plated through-hole) mounting holes
at the corners (or other standard positions) of the board outline so the PCB
can be mechanically fastened.

**What this needs:**

- A `--mounting-holes` flag with sub-options:
  - `--hole-diameter MM` (e.g. 3.2 mm for M3)
  - `--hole-margin MM` (inset from board corners / edge; default ~2 mm)
  - `--hole-pattern {corners|custom}` — `corners` auto-places four holes
    symmetrically inset from the bounding rectangle corners; `custom` accepts
    explicit (x, y) positions
- An NPTH pad node builder: `pcb.make_npth(x, y, drill_mm)` — a
  `(footprint ...)` node containing a single `np_thru_hole` pad with no net,
  placed on `Edge.Cuts` + drill layers (same pattern as KiCad's built-in
  MountingHole footprint). Alternatively emit a bare `(pad "" np_thru_hole
  circle ...)` at the top level — simpler, though KiCad prefers footprints.
- **Obstacle registration**: NPTH holes must appear as routing obstacles (a
  circular keepout with `drill + clearance` radius) so the router doesn't
  route copper through them. `geometry.board_obstacles` currently ignores
  `np_thru_hole` pads (`pcb.py:124`); this would fix that gap and link
  naturally with item 5 (drill geometry DRC).
- **Placement interaction**: when `--place` is used the holes should be
  **fixed** obstacles — placed before the annealer runs so footprints are
  pushed away from them. Simplest: inject the NPTH pads into the board before
  placement so the grid respects them automatically.
- **`--keep-outline` compatibility**: when the board outline is fixed, corners
  are well-defined; when PyAutoRoute generates the outline (`--place` without
  `--keep-outline`), holes are placed after the outline is finalised.

**Effort:** low-to-medium. `make_npth` is a small node builder; the routing
obstacle registration is the same fix needed for item 5; the corner-placement
geometry is trivial. The main complexity is the placement interaction.

### 7. Exact custom-pad polygons

`_base_pad_shape` renders circle/oval/roundrect/trapezoid exactly, but **rect,
custom, and unknown shapes all fall back to their bounding box**
(`geometry.py:64-65`). Custom pads can be far smaller than their bbox, over-
reserving space and blocking valid routes. Shapely ≥ 2.0 (already a dependency)
can build the true polygon from the pad's primitives.

### 7. Routing report / DRC summary output (`--report`)

`report.py` already computes a full `RoutingStats` (total/routed/unrouted MST
connections, length, vias, violations via `clearance_violations`) — but it is
**internal only**: used by `autoroute.run` for the initial-board summary and by
the GUI, with **no `--report` CLI flag** (confirmed: no such `add_argument`).
Exposing it as `--report FILE` (JSON/markdown — completion %, per-net status,
unrouted list) is low effort and useful for CI.

### 8. Length tuning / matching

The annealer minimizes `E = wirelength + via_weight·#vias + unrouted_weight·#unrouted`
(`anneal.py:70-89`) with no length-matching term and fixed pad endpoints. Adding
a per-group `target_length` penalty (clocks, buses, diff pairs) is a natural
extension of the existing incremental energy bookkeeping.

### 9. Smarter tuning

[`tuning.md:82-92`](tuning.md) lists an explicit, unbuilt roadmap for `tune.py`:
baked-in default presets keyed by pad count/density (so `--auto` need not probe
large boards), smarter search than the coarse 3×3 grid (`tune.default_grid`,
`tune.py:176`) — random / coarse-to-fine / Bayesian — placement parameters in
the sweep, parallel evaluation across configs/seeds, and CSV + plots output
behind the `[viz]` extra.

## GUI follow-ups

The GUI is **largely complete and functional** — the Run button executes the real
pipeline in a daemon thread (`worker.py:_pipeline`) with live render, energy plot
(`plots.py`), metrics, cooperative cancel, and a working Apply-to-Project with
timestamped backup (`app.py:348`). The remaining gaps are targeted:

### 10. ~~Wire up the "Suggest" button~~ — **removed**

The Suggest… button and its associated `_suggest` placeholder, `on_suggest`
callback parameter, `auto_probe_time` field in `RunConfig`, and the
"Auto probe time" entry in the Advanced dialog were all removed (2026-06-05).
The feature was never wired to `tune.sweep` and was deemed not practical enough
to implement.

### 11. Share one pipeline between CLI and GUI

`worker.py` **duplicates** the `autoroute.run` orchestration rather than sharing
it (the `pipeline.place_board`/`route_board` refactor proposed in
[`gui-plan.md`](gui-plan.md) was never done). As a result the GUI lacks `--jobs`
parallelism, snapshot-file output, log output, and the coarse-grid warning, and
the two paths can silently drift. Extracting a shared `pipeline` module removes
the duplication.

### 12. GUI test coverage

The entire `gui/` package is **untested** — no tests touch `Worker`, `RunConfig`,
the event protocol, the queue-drain/collapse logic, or the Apply-to-Project
backup/replace, despite [`gui-plan.md`](gui-plan.md) proposing exactly those.

### 11. Keep the best-N routing and placement results (`--keep-best`)

Today `--runs N` and `--place-runs N` each run N times but discard all but the
single winner. A `--keep-best [N]` flag (default 3 when bare) would write the
top N results as ranked sibling files alongside the main output:

```
board_routed.kicad_pcb          ← rank 1 (best), as today
board_routed_rank2.kicad_pcb
board_routed_rank3.kicad_pcb
```

For placement the same applies to `--place-runs`:

```
board_placed.kicad_pcb          ← best placement
board_placed_rank2.kicad_pcb
board_placed_rank3.kicad_pcb
```

**What this needs:**

- A `--keep-best` argument (`nargs="?", const=3`) in `build_parser()`.
- `run_routing()` (pipeline.py) currently tracks only the single best
  `(energy, results, metrics)`. Add a list that collects every run's result;
  sort by energy; expose the top-N as a new `all_run_results` field on
  `PipelineResult`.
- `run_placement()` similarly — `PlaceResult` already tracks energy; collect
  per-run placements and expose the top-N.
- In `autoroute.run()`, after writing the main output, iterate the ranked
  results and write each with `write_board(..., new_nodes=...)` using a
  `_rank{k}` suffix on the output stem. Apply ground-plane / zone refill to
  each (or optionally only to the winner to save time).
- `--keep-best` is silently capped at `--runs` (can't keep more boards than
  were routed) and at 1 when `--runs 1` (no-op, since there's only one
  result).

**Effort:** low-to-medium. The main change is collecting per-run results in
`pipeline.py` instead of tracking only the winner; the write loop in
`autoroute.py` is straightforward. The parallel path already receives each
future's result individually so collection is natural there.

## Lower-value / polish

- **Expose / default-enable stall detection.** Early-termination is already
  implemented (`anneal.py:51-54,391-398`) but disabled by default
  (`stall_patience=0`) and not exposed via any CLI flag — a one-flag win that can
  cut wall-clock on a `--time` budget.
- **Parallel placement runs & adaptive cooling** — the only remaining items in
  the performance roadmap ([`performance_analysis.md:534-537`](performance_analysis.md));
  routing best-of-N is already parallel via `--jobs`.
- **Pour-aware routing.** Filled copper zones are auto-excluded and not treated as
  obstacles (`geometry.py:254-255`); the router refills via `kicad-cli` afterward
  but has no awareness of where the pour will flow. True pour-aware routing /
  pour generation remains a noted future extension.
- **Per-layer directional bias (H/V).** `back_layer_penalty` is a uniform per-step
  tax favouring F.Cu (`router.py:407-408,419`), not a true per-layer
  horizontal/vertical preference that would reduce same-layer crossings.
- **Teardrops / multi-side pad entry** — improve `path_to_nodes` stub generation
  (`router.py:579-661`).
- **Close remaining test gaps** — `report.py`/`routing_stats` and the
  `pyautoroute-fix` CLI are untested.

## Top recommendations

If implementation effort is to be prioritized:

1. ~~**Differential pairs**~~ — **shipped in 0.38.0** (see item #2 above).
2. **Per-net-class clearance masks** — routes denser mixed-rule boards; the grid
   currently uses a single worst-case margin across all net classes.
3. **Drill geometry + hole-to-hole DRC** — closes a real self-check gap;
   `min_hole_to_hole` is already parsed and `Pad.drill` is already modelled,
   so most of the scaffolding exists.
4. **Diff pair annealing integration** — current implementation pre-routes pairs
   once and bakes them as fixed obstacles; integrating them into the rip-up/reroute
   annealing loop (treating each pair as an atomic unit) would allow global
   optimisation across single-ended and diff pair nets together.
