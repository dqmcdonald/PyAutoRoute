# Feature suggestions

A set of proposed new features for PyAutoRoute (v0.24.0), grounded in a deep
read of the codebase. Each suggestion cites the code it builds on or the gap it
fills (`file:line`). Items are ordered by value-to-effort. This is a planning
document, not a commitment — items here are candidates, not scheduled work.

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

### 2. Differential pair routing

A common real-world need with **no support at any layer**: `diff_pair_width` /
`diff_pair_gap` are not even parsed in `rules.py` (only per-class `clearance`,
`track_width`, `via_diameter`, `via_drill` are), and `netlist.py` has no
awareness of `+`/`-` net-name suffixes — `greedy_order` keys purely on geometric
length (`netlist.py:101-102`). KiCad encodes diff pairs by net-name suffix and
net-class width/gap. Detect pairs in `netlist.py`, parse the two class fields in
`rules.py`, then route the partner under a coupling constraint in the A\* cost
model. High value; large but self-contained.

### 3. Incremental / partial re-routing ("route only these nets")

Today the writer strips all free vias and reroutes everything. A
`--keep-existing` / `--nets PATTERN` mode would lock already-routed segments as
obstacles and route only the named/unrouted nets, making the tool usable
iteratively on a partly hand-routed board. The grid already supports static
obstacles from existing copper (`geometry.board_obstacles`, `grid.py:246-259`),
and `RoutingState` already keys occupancy per connection with exact rip-up
(`router.py:124-197`), so the foundations exist. Biggest workflow win, moderate
architectural risk.

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

### 6. Exact custom-pad polygons

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

### 10. Wire up the "Suggest" button

`app.py:_suggest` (`:385`) is a placeholder dialog that does **not** call
`tune`/`--auto`; `RunConfig.auto` is hardcoded `False` (`controls.py:588`).
Connecting it to the existing `tune.sweep` probe (the same path `--auto` uses)
would make the advertised feature real.

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

1. **Bounded A\* search** — biggest performance win; `architecture.md:459` already
   names it the highest-value next optimisation.
2. **Partial re-routing (`--nets` / `--keep-existing`)** — biggest workflow win,
   built on existing per-connection occupancy.
3. **Differential pairs** — biggest "real PCB" capability gap; unsupported
   end-to-end today.
