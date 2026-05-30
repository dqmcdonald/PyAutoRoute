# Changelog

Human-readable summary of each released version and its significant changes.
PyAutoRoute follows SemVer adapted for pre-1.0 (see `CLAUDE.md`): a **minor**
bump for each major addition (feature, CLI flag, output, or algorithm change),
a **patch** bump for fixes and small corrections. Newest first.

## 0.34.0

- **Board comparison tool (`pyautoroute-compare`)** — compare 2–3 routed boards from
  different sources (PyAutoRoute, hand-routed, competing tool) head-to-head. Outputs
  a columnar report with metrics (completion, wirelength, vias, directness, DRC) and
  a prose analysis. Reuses existing connectivity analysis (`routing_stats`) with a new
  optional `exclude` parameter to filter copper-pour nets consistently across boards.
  Reports which board routes most efficiently and flags DRC violations.

## 0.33.0

- **GUI: interactive footprint constraints.** Click a footprint in the Initial
  board view to set per-footprint placement constraints (edge affinity, lock,
  overlap OK) via context menu. Constraint changes show live on the canvas
  (lock markers and edge arrows), and the "Save Constraints" button writes
  changes back to the `.kicad_pcb` file. Adds `pcb.footprint_at()` for hit-testing,
  `pcb.set_footprint_edge/locked/overlap()` helpers, and lock-marker visualization
  in `visualize._draw_autoroute_markers`.

## 0.32.0

- **GUI: rats-nest overlay.** A "Rats-nest" toggle in the board view bar overlays
  the unrouted connections as thin dashed airwires — the *full* rats-nest before
  routing (so you can judge a placement and see what needs routing) and only the
  *remaining* unrouted connections once routing has run (the overlay shrinks as
  the board completes). `visualize.draw_board` gains a `rats_nest` parameter; the
  airwires are computed from `netlist.build_connections` with the same net
  exclusions the router uses.
- **Removed `--debug-plot`** (and the `visualize.render` PNG writer / the `viz`
  optional-dependency). The interactive GUI superseded the static PNG dump;
  matplotlib is now a GUI-only dependency.

## 0.31.0

- **GUI: best-of-cycles + congestion feedback.** The graphical front-end now
  exposes the `--cycles` outer loop and `--place-feedback` / `--congestion-weight`
  that previously existed only on the CLI: a *Cycles* entry, a *Congestion
  feedback* checkbox, and a *Congestion wt* entry in the Placement panel (and they
  round-trip through the settings file). When Cycles > 1 in place+route mode the
  worker drives the shared `pipeline.run_cycle` in the same loop the CLI uses —
  per-cycle progress streams to the canvas/metrics, and the best-routing cycle is
  kept. No new orchestration: the worker reuses `run_cycle` and the Phase-4
  congestion helpers.

## 0.30.0

- **Congestion-aware re-placement feedback (`--place-feedback`).** With
  `--cycles N`, each cycle now learns from the previous one's routing
  (PathFinder-style): a coarse board-wide **congestion field** is built from the
  routed result — high where routing struggled (dense copper, vias, and the
  regions of unrouted nets) — and the next cycle re-places under it, spreading
  footprints **out of** the hot zones. The field accumulates (decayed) across
  cycles; cycles run sequentially while feedback is on. Each cycle still re-places
  from scratch and the best **routed** cycle is kept, so feedback can only help or
  be discarded. New `router.CongestionField` / `congestion_frame` /
  `congestion_heatmap` (routing untouched — the field is read off the results) and
  a `PlaceParams.congestion_field` / `congestion_weight` placement term.
  `--congestion-weight` tunes the spread strength. Opt-in and experimental.

## 0.29.1

- **Unified the CLI and GUI place→route orchestration.** Both now run through the
  shared `pipeline.run_placement` / `pipeline.run_routing` (best-of-`place_runs`
  then best-of-`runs`, sequential or parallel), driven by a `PipelineHooks` that
  each front-end maps to its own progress (the CLI's `Reporter` lines; the GUI's
  `Phase`/`Progress`/`BoardSnap` events). Removes the duplicated orchestration the
  GUI worker carried (the Phase-3 follow-up); behaviour unchanged. Adds a headless
  GUI-worker test.

## 0.29.0

- **Best-of-cycles placement (`--cycles N`).** With `--place`, run `N` independent
  place→route cycles and keep the one that *routes* best — fewest unrouted, then
  lowest routed energy — selecting on the true objective instead of placement
  energy alone. Parallelised by `--jobs` (cycle workers), like `--runs`.
  `--place-runs`/`--runs` remain available as inner loops; `--cycles 1` (default)
  is unchanged. Phase 3 of `plans/placement-improvements-plan.md` (B1+B2+B3).
- **New `pipeline.py`** — the shared, picklable `run_cycle` place→route→score unit
  (`CycleResult` / `select_best`), used by the sequential and parallel cycle paths.
  The routing helpers `_route_one_run` / `_route_run_worker` moved here (routing is
  a cycle's second half). Routing the GUI worker through the same unit (removing the
  CLI/GUI orchestration duplication) is the remaining Phase-3 follow-up.

## 0.28.0

- **Split the `Autoroute` footprint property into namespaced fields.** The single
  overloaded `Autoroute` value is replaced by `Autoroute-overlap = yes` and
  `Autoroute-edge = <side>` (`any` / `left` / `right` / `top` / `bottom`), so each
  intent is its own KiCad property and there's room for future `Autoroute-*` flags.
  **Breaking:** the old combined `Autoroute = "overlap, edge-top"` form is no longer
  read — re-tag affected footprints.
- **Edge-flagged parts now orient flat against their edge.** The edge-affinity term
  measures to the *far* side of the footprint box (gap + perpendicular depth), so a
  connector is aligned with its long axis parallel to the boundary — all its pins
  near the edge — instead of being free to rotate so only one pad reached it.
  Strength is still `--place-edge-weight`.
- **Bigger Autoroute markers.** The board-canvas edge arrows/stars and overlap rings
  are drawn 2× larger so they're easier to spot.

## 0.27.0

- **`--keep-outline` placement mode.** During `--place`, keep the board's existing
  `Edge.Cuts` and contain the footprints within it (a soft distance + protruding-
  area penalty) instead of regenerating a bounding-box outline — for boards with a
  real mechanical shape. Edge-flagged parts (`Autoroute=edge`) then snap to the
  real board edge. Needs a closed outline; warns and falls back otherwise. Phase 2
  of `plans/placement-improvements-plan.md`. *(Also fixes the placement SA revert to
  roll back the new containment/edge cache terms.)*

## 0.26.0

- **Edge-aware placement.** Footprints flagged `Autoroute=edge` (or
  `edge-left` / `-right` / `-top` / `-bottom`) are pulled to the board boundary
  during `--place` — for connectors, headers and the like that must reach the
  edge. A new placement-energy term (`--place-edge-weight`, default 2.0) measures
  each flagged part's distance from its target edge; tokens combine with
  `overlap`. Off by default (zero cost when nothing is flagged). Phase 1 of
  `plans/placement-improvements-plan.md`.

## 0.25.3

- **Perf: vectorised A\* dynamic-copper overlay.** Each reroute previously
  re-scanned every committed-copper node in Python to mask "blocked by another
  net"; this is now one vectorised numpy op backed by an incrementally-maintained
  per-node owner array. ~1.26× faster annealing on the default (no-flag) path,
  with identical routes.

## 0.25.2

- **Fix: rationalise the config-file extension to `.ini`.** Bare `--write-config`
  (and the GUI Save dialog) now write `<board>.ini` — the same file auto-loaded on
  the next run — instead of `<board>.pyautoroute.cfg`. `--config FILE` still
  accepts any path. Existing `*.pyautoroute.cfg` files are no longer auto-loaded.

## 0.25.0

- **Optional bounded A\* search (`--search-margin MM`).** Confines each
  connection's search (and its precompute) to a box around the endpoints,
  widening and retrying on failure, falling back to the full grid. ~1.2× faster
  greedy routing / annealing on large boards at a small cost to optimality;
  unset (default) is unchanged.

## 0.24.1

- **Fix: GUI placement preview.** During the live placement pass the board view
  now rescales as footprints compress, and footprint outlines no longer drift out
  of alignment with their pads (the snapshot froze pads but shared the live
  footprint poses). Factored the pad-bounding-rectangle into `pcb.pad_bounding_outline`.

## 0.24.0

- **Perf: parallel best-of-N routing** (`--jobs`/`-j N`) across worker processes,
  plus **memoised `rules.class_for`** (per-net class resolution cached). Default
  (`-j 1`) path is byte-identical to before.

## 0.23.1

- **Fix:** recenter placed footprints after the placement pass so they don't
  drift off-board.

## 0.23.0

- **Perf: optional native A\* core (Cython), 5–20× on larger grids**, with a
  transparent pure-Python fallback when the extension isn't built
  (`pip install -e ".[fast]"`; `pyautoroute.HAS_C_ASTAR` reports which is active).

## 0.22.1

- **Perf:** incremental routing-SA energy + spatial (cKDTree) cluster index, and
  A\* constant-factor reductions (integer state key, precomputed heuristic field,
  per-net free mask).

## 0.22.0

- **Perf:** incremental placement energy (10–100× on the placement annealer),
  annealer stall detection (opt-in), and the `tests/perf/` benchmark harness.

## 0.21.0

- **Placement** now steers footprints clear of board-level silkscreen text.

## 0.20.0

- **Copper fills:** zones with `(fill yes)` are auto-detected — their net is
  excluded and the pour is skipped as an obstacle; after writing, the board is
  refilled via `kicad-cli` when available.

## 0.19.0

- **Placement** includes silkscreen text extents in its overlap term.

## 0.18.0

- **GUI:** renders silkscreen text; fixes footprint text angles after placement.

## 0.17.0

- `--fix-values` flag added to the main CLI and GUI Advanced settings.

## 0.16.0

- New `pyautoroute-fix --values` tool: moves footprint Value text to the
  silkscreen layer (`F.SilkS`/`B.SilkS`).

## 0.15.0

- **Tkinter GUI** (`pyautoroute-gui`): open a project, run place/route with live
  board rendering and convergence telemetry (energy graph, acceptance ratio,
  metrics), Run/Stop with cooperative cancel, and Apply-to-project (backup +
  overwrite the original `.kicad_pcb`). See `plans/gui-plan.md`.

## 0.14.0

- **Parameter-sweep tool** (`pyautoroute-tune`) and the opt-in **`--auto`** probe
  that picks grid/via settings for the board in front of you. See `docs/tuning.md`.

## 0.13.0

- `pyautoroute.sh` interactive helper menu.

## 0.12.0

- **Settings file:** `--config FILE` / `--write-config` (INI; defaults < project
  `.ini` < `--config` < CLI flags).

## 0.11.0

- **Best-of-N runs** (`--runs N`) for placement and routing — keep the
  lowest-energy result.

## 0.10.0

- More placement-anneal controls (temps, step, rotate mode) + acceptance-ratio
  reporting.

## 0.9.0

- `--place-only` and placement-aware output names (`_placed`, `_placed_routed`).

## 0.7.0 – 0.8.0

- Routed tracks terminate on the pad anchor (centre); `--place` output kept
  DRC-clean (pad rotation + clearance buffer fixes).

## 0.6.0

- **Experimental footprint auto-placement** (`--place`): a simulated-annealing
  pass that arranges footprints (rats-nest + overlap + compactness energy),
  honouring KiCad locks and an `Autoroute=overlap` property, then regenerates
  Edge.Cuts and routes. See `plans/place-feature-plan.md`.

## 0.5.0 – 0.5.3

- Version surfaced (startup banner, log header, `--version`); acceptance-ratio
  and wall-clock/CPU runtime reporting; coarse-grid warning.

## 0.4.0

- Versioning policy recorded (see `CLAUDE.md`).

## 0.1.0

- **Initial autorouter.** Pure-Python pipeline that parses a `.kicad_pcb`
  s-expression directly (no `pcbnew`), builds a clearance-aware 2-layer routing
  grid, decomposes nets into two-pin connections via an MST rats-nest, routes
  each with an A\* maze router (45°-preferring cost model), and optionally
  optimises routing order / rip-up with simulated annealing. Output is
  **DRC-clean by construction**; an in-repo self-check verifies clearances. See
  `autorouter_plan.md` and `docs/architecture.md`.
