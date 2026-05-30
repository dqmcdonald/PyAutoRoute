# Changelog

Human-readable summary of each released version and its significant changes.
PyAutoRoute follows SemVer adapted for pre-1.0 (see `CLAUDE.md`): a **minor**
bump for each major addition (feature, CLI flag, output, or algorithm change),
a **patch** bump for fixes and small corrections. Newest first.

## 0.26.0

- **Edge-aware placement.** Footprints flagged `Autoroute=edge` (or
  `edge-left` / `-right` / `-top` / `-bottom`) are pulled to the board boundary
  during `--place` — for connectors, headers and the like that must reach the
  edge. A new placement-energy term (`--place-edge-weight`, default 2.0) measures
  each flagged part's distance from its target edge; tokens combine with
  `overlap`. Off by default (zero cost when nothing is flagged). Phase 1 of
  `docs/placement-improvements-plan.md`.

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
  overwrite the original `.kicad_pcb`). See `docs/gui-plan.md`.

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
  Edge.Cuts and routes. See `docs/place-feature-plan.md`.

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
