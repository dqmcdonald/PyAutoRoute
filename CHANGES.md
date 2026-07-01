# Changelog

Human-readable summary of each released version and its significant changes.
PyAutoRoute follows SemVer adapted for pre-1.0 (see `CLAUDE.md`): a **minor**
bump for each major addition (feature, CLI flag, output, or algorithm change),
a **patch** bump for fixes and small corrections. Newest first.

## 0.56.1

- **fix**: `--ground-plane` connectivity vias no longer get stranded on
  isolated copper islands. A candidate via position could pass the local
  clearance check yet still land in a pocket that other-net traces had moated
  off from the rest of the plane; since KiCad only prunes fill islands with
  *no* connection to the zone's net, a via anchored in such a pocket kept it
  alive through the fill without ever reaching the rest of GND — leaving that
  pad electrically floating despite having "a via to GND". `groundplane.py`
  now checks candidate positions against the pour's actual connected
  components and rejects ones outside the main plane body, printing a warning
  instead of silently wiring a pad to dead copper.

## 0.56.0

- **new (experimental)**: `--place-polish-interleave K` (+
  `--place-polish-interleave-start FRAC`) — basin-hopping placement: run one
  monotone polish descent sweep every K anneal iterations so the Metropolis
  chain explores from locally relaxed states. Benchmarks
  (`scripts/bench_interleave.py`) show it does **not** beat plain SA + final
  `--place-polish` at equal wall-clock (cold-gated sweeps break even; ungated
  sweeps lose), so it ships off by default as a documented negative result;
  the discrete swap/rotate moves, not within-basin relaxation, dominate
  placement quality. `PlaceResult` gains
  `interleave_sweeps`/`interleave_improvement`.

## 0.55.2

- **fix**: placement now uses the courtyard layer (`F.CrtYd`/`F.Courtyard`) as the
  footprint body extent instead of pad positions alone. Fixes electrolytic capacitors
  (and other parts whose physical body extends well beyond their pad span) overlapping
  in the placed result.

## 0.55.1

- **new**: KiCad plugin toolbar icon. `plugin.py` referenced `icon_24x24.png`
  but no file existed, so the button was blank. Added a PCB-tile icon (green
  board, gold traces routing to a via) at 24×24 and 48×48.

## 0.55.0

- **new**: KiCad plugin gains a "Placement time (s)" field, wired to the
  autorouter's `--place-time` budget (Place + Route mode only). Previously only
  the routing-anneal budget was adjustable from the dialog.
- **new**: KiCad plugin gains a "Keep board outline" checkbox (default on),
  wired to `--keep-outline`: placement keeps the existing Edge.Cuts and
  constrains footprints to stay inside it, instead of regenerating the outline
  as a bounding box. The kept outline also means the live reload no longer needs
  a reopen to fix the board shape.
- **fix**: KiCad plugin "Reload tracks into KiCad when done" did nothing. The
  progress dialog's button kept its original Cancel handler after routing
  finished, so clicking "Close" returned `ID_CANCEL` and the caller bailed out
  before injecting the routed tracks. The button is now rebound to a close
  handler that reports the real outcome.
- **fix**: KiCad plugin reload then produced a board with *no* tracks. It called
  `LoadBoard()` on the currently-open board file, which aliases the live editor
  board, so clearing the editor's tracks also emptied the board being read back.
  The plugin now routes to an explicit `_routed` sidecar and injects from that
  (snapshotting the tracks before mutating the editor board).
- **fix**: Place + Route reload produced a garbled board — the placement pass
  moves and rotates footprints, but the editor kept the old poses while the new
  tracks followed the new layout. The reload now also copies each footprint's
  position and rotation from the routed board (matched by reference). The board
  outline and copper zones are still not updated in place; reopen the file if
  placement resized the outline or a ground plane was added.

## 0.54.0

- **new**: `pyautoroute-assign` — assigns footprints to unassigned KiCad schematic
  symbols from a TOML preference database (`~/.config/pyautoroute/footprint_prefs.toml`).
  Supports per-prefix tech defaults (`R:THT`), value-keyed rules for ICs
  (`U:74AHC244=Package_DIP:DIP-20_W7.62mm_Socket_LongPads`), `--dry-run`, `--all`
  (reassign existing), and `--init-prefs` to bootstrap the preference file.
  Round-trip-safe: only changed nodes are reformatted. Adds `CP` (polarised
  capacitor) prefix to default prefs.
- **new**: `--rebuild-index` scans all 15 000+ KiCad footprint libraries into a
  3.5 MB JSON index; unknown-prefix components then receive ranked keyword
  suggestions from the index automatically. Multi-unit IC symbols are deduplicated
  so each IC receives exactly one footprint assignment.

## 0.53.2

- **fix**: grouped `gr_text` items now track their footprint live in the GUI placement preview (`_silk_text_items` computes the current text position from `fp.x/y/angle` at each render frame rather than reading the stale tree position).
- **fix**: grouped `gr_text` items no longer appear in the fixed-obstacle list at their original position; instead their extent is folded into the associated footprint's bounding box (`_fp_box`), so the placer correctly avoids placing other components on top of the group's text label.
- Adds public helper `pcb.gr_text_group_fps(board)` used by both the placer and the renderer.

## 0.53.1

- **fix**: `gr_text` items grouped with footprints are now correctly transformed when placement moves their footprint. `sync_tree_from_placement` (called after every placement run) now applies the full original→final transformation to grouped text, using `fp.x0/y0` as the stable reference. Scatter no longer tries to move text nodes directly (which was incorrect across multi-run placements); text exclusively follows via the final sync step.

## 0.53.0

- **GUI**: a single end-of-run **summary dialog** now lists every non-fatal issue — unrouted connections, DRC self-check violations (clearance and hole-to-hole), and warnings raised during the run (skipped mounting holes, ground-plane, placement) — and is shown **only when there are issues** (a clean run shows nothing). This replaces the old clearance-only popup. The worker collects warnings via `Worker._warn` and carries them on the `Done` event; `events.collect_issues` (tkinter-free, unit-tested) assembles the list.

## 0.52.0

- **Feature**: mounting holes are now "felt" during placement. When a hole's position is known before the annealer runs — explicit `x,y` always, or corner/edge codes under `--keep-outline` — it is injected as a **locked footprint** so footprints are pushed away from it (and it shows during the placement animation); `mountingholes.positions_known_preplacement()` decides this, and corner holes on an auto-generated outline fall back to post-placement injection with a printed note. Applied symmetrically in the CLI and GUI worker.
- **Feature**: boards that already carry holes are handled — a requested position coinciding with an existing hole is reported as already-drilled (re-running `--mounting-holes` is now idempotent), existing drills are honoured for hole-to-hole spacing, and new `MH<n>` reference designators no longer collide with refs already on the board.

## 0.51.0

- **GUI**: mounting holes (drill-hole plan, phase 4). A **Mounting holes** checkbox with drill-diameter / edge-margin fields, a corners/custom pattern picker, and an extra-positions entry now live in the GUI's Post-processing panel. The worker injects the holes after placement and before the grid is built — the same point as the CLI — so they act as fixed routing obstacles. The GUI self-check count now also includes hole-to-hole (drill) violations.

## 0.50.0

- **Feature**: auto-add NPTH mounting holes, `--mounting-holes` (drill-hole plan, phases 2–3). New flags `--hole-diameter` (default 3.2 mm, M3), `--hole-margin` (default 5 mm), `--hole-pattern {corners,custom}`, and a repeatable `--hole-at` taking either a location code (`TL`/`TR`/`BL`/`BR` corners, `T`/`B`/`L`/`R` edge mid-points, `C` centre — Y-down) or an explicit `x,y` in mm. Holes are emitted by the new `pcb.make_npth()` as `MountingHole` footprints and injected (after placement finalises the outline, before the grid is built) as fixed routing keep-outs, so the router never crosses a barrel. Holes that fall outside the outline, overlap copper, or sit too close to another hole are skipped with a warning. New `pyautoroute.mountingholes` module.

## 0.49.0

- **Feature**: drill geometry + hole-to-hole DRC (drill-hole plan, phase 1). The previously-parsed-but-unused `min_hole_to_hole` rule is now enforced by a new `geometry.drill_violations` self-check, printed as a `drill-check:` line and folded into the exit code. Drilled through-hole / NPTH pads are now registered as all-layer routing keep-outs (`board_obstacles` emits a barrel disk on the layers a pad lacks copper), so the router never drives copper across a hole. `RoutingStats` gains a `drill_violations` field.

## 0.48.0

- **Feature**: `--scatter` now also randomises footprint starting positions between `--place-runs` (best-of-N placement) runs, not only between `--cycles`. Previously scatter only fired once per cycle; with `--place-runs N > 1`, each run now gets a freshly randomised layout.
- **GUI**: **Save As…** button in the results area writes the current best routed board to a user-chosen path (opens a save-file dialog). Disabled until a result is available.
- **GUI**: **Energy heat map** toggle in the view bar overlays footprints and ratsnest connections with a `RdYlBu_r` colour scale (blue = low placement energy, red = high) using the same placement energy machinery (`energy_heatmap`) as the annealer. Useful for diagnosing which parts and connections are driving placement cost.

## 0.47.0

- **New**: mark a capacitor as a **decoupling cap** so placement keeps it next to its IC. A footprint with an `Autoroute-decouple` property (value = the IC's reference designator, or `auto` to find it automatically) is softly pulled toward that IC during `--place`, so the cap settles beside it instead of drifting — a flexible alternative to a rigid KiCad group. The IC is found by searching the cap's power net for the nearest IC-like part (`netlist.resolve_decoupling_ic`), warning when the match is ambiguous or the part doesn't look like a decoupling cap. Set it via the GUI right-click menu (*Decoupling cap* → resolves the IC on open and offers a chooser) or the property directly; tune with `--place-decouple-weight` (default 5.0; 0 disables). Unresolved targets are reported in the placement summary. See `plans/decoupling-cap-plan.md`.

## 0.46.0

- **New**: optional post-anneal placement **polish** (`--place-polish`). After simulated annealing settles on its best placement, a steepest-descent refinement pass relaxes close contacts and slides parts into their local energy minimum. It estimates each movable unit's translation gradient by central finite differences (reusing the incremental energy cache) and takes backtracking-line-search steps, committing only strictly-improving moves — so it is **monotone** and can never worsen the annealed result. Translations only (angles left to annealing); locks and KiCad groups are respected. Tuning knobs: `--place-polish-iters` (max descent sweeps, default 20), `--place-polish-eps` (finite-difference step, default 0.05 mm), `--place-polish-time` (optional wall-clock cap). Off by default. See `plans/placement-polish-plan.md`.

## 0.45.0

- **Feature**: `--scatter` (with `--cycles`). Randomises every unlocked footprint's position and rotation before each cycle's placement pass, giving the annealer completely different starting layouts across cycles instead of always refining the as-designed configuration. Increases exploration diversity; pair with a generous placement budget. Available in the GUI as a *Scatter start* checkbox in the Cycles & Congestion panel.
- **Feature**: `--cycles` now prints a ranked summary table at the end showing every cycle's result sorted by energy, with the winner marked ★ (to screen and log).

## 0.44.1

- **Fix**: board-level silkscreen `gr_text` keep-out boxes were computed as squares using the circumscribed-circle half-diagonal, producing a ~30× area overestimate for wide, flat title-block text (e.g. a 52-character label at 1.5 mm font height produced a 56×56 mm square keep-out instead of a 54×2 mm rectangle). The keep-out now uses a tight rotated rectangle matching the actual text extent, so a long title block no longer blocks a large fraction of the board from footprint placement.

## 0.44.0

- **Feature**: `--place-spread-weight` (placement parameter `spread_weight`). Adds a density-uniformity term to placement energy: the board is divided into a grid and `Σ count²` across cells is penalised, which by Cauchy-Schwarz is minimised by a uniform distribution. Fixes the cluster-in-one-corner failure mode when `--keep-outline` is used with locked corner parts — the locked parts pin the bounding-box term to a constant, making `--place-compact-weight` inert and leaving no force to spread the layout. A value of `~3.0` works well for most boards. Default `0.0` (off) preserves existing behaviour.

## 0.43.5

- **Fix**: connectivity-via obstacle tree used old routing segments as blockers in `--existing-routes clear` mode, even though those segments are stripped from the output. This caused the spiral search to find zero valid via positions for pads surrounded by original routing, leaving R9/R10/R17/R16/J3 unconnected. In clear mode the obstacle tree is now built from pad copper only (permanent) plus fresh routing from `routed_nodes`.
- **Fix**: new GND segments from `routed_nodes` were not matched against `gnd_net` on numbered-net boards (KiCad 9 format), because `make_segment` stores a net code (`11`) while the comparison used the net name (`"GND"`). Pre-compute the expected token in the board's own reference style.

## 0.43.4

- **Fix**: SMD GND pads already connected by the router's fresh GND traces were incorrectly flagged as isolated in `--existing-routes clear` mode, because `routed_nodes` was only used for obstacle checking, not for the union-find. This caused spurious (usually failed) connectivity-via attempts and left pads unconnected. Freshly-routed GND segments and vias are now registered in the union-find.
- **Fix**: fallback connectivity-via placement (when the preferred offset-via spiral fails) omitted the stub track from the pad to the via, leaving the via electrically floating. The fallback now always adds a stub track to the nearest GND pad.

## 0.43.3

- **Fix**: SMD GND pads connected to THT pads only via *pre-existing* F.Cu segments now correctly get connectivity vias in `--existing-routes clear` mode. Previously the union-find read `board.segments` (which still held the original segments in memory) and concluded no via was needed, even though those segments were being stripped from the output.

## 0.43.2

- **Fix**: ground-plane zone missing `(net_name "GND")` on numbered-net boards — KiCad's fill engine requires both `(net <code>)` and `(net_name "name")` to connect the pour to the net.

## 0.43.1

- **Fix**: crash loading boards with oval through-hole pad drills (`(drill oval dx dy)`) — the `oval` shape keyword was incorrectly passed to `float()`.

## 0.43.0

- **`--save-cycles`** — with `--cycles N`, writes each cycle's placed+routed board to `<output>_cycle_NNofMM.kicad_pcb` as it completes (including the ground-plane zone if `--ground-plane` is set, but without zone refill). Useful for inspecting intermediate results while a long multi-cycle job is still running.

## 0.42.0

- **`--greedy-order {short,long,shuffle}`** — controls the initial greedy routing pass order. `short` (default, unchanged) routes shortest connections first. `long` routes longest-first, giving hard long-distance connections first pick of the empty grid. `shuffle` randomises the order per run/cycle so the annealer starts from genuinely different configurations across `--runs`/`--cycles`. Applied in both the single-pass and best-of-cycles paths.

## 0.41.1

- **fix(preserve): remove duplicate vias when writing in `--existing-routes preserve` mode.** The annealer could re-route existing connections placing new vias at the same positions as preserved ones; the ground-plane pass could also add fresh stitching vias on top of old ones. Both caused co-located drilled holes in DRC. The write step now strips free vias whose position is superseded by a co-located via in the new routing output.
- **fix(anneal): handle empty connection list gracefully.** Running `--existing-routes preserve` on an already-fully-routed board passed zero connections to the annealer, causing a `ValueError: empty range for randrange()` crash. The annealer now returns immediately with the current state when there is nothing to optimise.

## 0.41.0

- **KiCad action plugin** (`kicad_plugin/`). Adds a "PyAutoRoute" entry to KiCad's Tools menu and toolbar. The plugin saves the live board, invokes `pyautoroute --in-place` as a subprocess (bypassing the Python 3.9/3.12 version mismatch), streams output to a scrolling progress dialog, then reloads the result into pcbnew. Settings (grid, time, mode, exclude nets, ground plane, cycles, existing routes) are read from and written back to the board's `.ini` file. Install with `pyautoroute-install-plugin` (new console script).

## 0.40.0

- **`--place-swap-prob`** — new CLI flag (and `PlaceParams.swap_prob`) to control what fraction of placement annealing iterations attempt a swap move (exchanging two footprints' positions). Default 0.2 (unchanged behaviour); raise it for boards with many interchangeable ICs to explore position swaps more aggressively.

## 0.39.0

- **Native KiCad group placement.** Footprints grouped together in KiCad's UI (via Edit → Group) now move as a rigid body during `--place`. All three move types (translate, rotate, swap) are applied to the whole group simultaneously, keeping relative positions and angles intact. Groups where any member is locked are excluded from grouping (conservative policy — the locked member stays fixed so the group can't be treated as rigid). Single-member groups are silently ignored. Grouped footprints are highlighted in the GUI canvas with a teal diamond marker and dashed connecting lines.

## 0.38.0

- **Differential pair routing (`--diff-pairs`).** Detects paired nets by naming convention (`+`/`-`, `P`/`N`, `_P`/`_N`) and routes them with a coupled A* that advances both traces simultaneously — guaranteeing zero length skew and constant spacing. Add `--diff-pair-gap MM` to override the intra-pair spacing (default: from design-rule clearance). After routing, prints a per-pair table showing length, skew, vias, and estimated differential impedance (IPC-2141A microstrip formula using the board's stackup).
- **Stackup parsing.** The board's `(setup (stackup …))` block is now read into `Board.stackup` (copper thickness, dielectric height, and ε_r) and used for impedance estimates.

## 0.37.1

- **`--auto-time-weight` / `--time-weight` (default 1.0).** Adds a runtime penalty to the auto-probe and tune scoring so that a marginally finer grid no longer automatically wins when quality is essentially equal. Each extra second of routing costs 1 score unit; raise the weight to prefer coarser/faster grids more strongly, or set to 0 to rank by quality only.

## 0.37.0

- **`--silk-labels` replaces `--fix-values`.** Moves footprint Value text to the silkscreen layer (unchanged) and also moves Reference text to the fabrication layer — keeping refs off the physical board while still available for assembly drawings. The standalone `pyautoroute-fix` tool gains a matching `--refs` flag.

## 0.36.0

- **`--in-place` flag.** After routing, if the result scores better than the input (same formula used by `--cycles` selection: `unrouted × weight + wirelength + vias × weight`), the input board is backed up to `INPUT.kicad_pcb.bak` and replaced with the routed output. Useful for iterative reruns directly on the working file.

## 0.35.0

- **Startup listing of footprint constraints.** When the CLI starts it now prints
  any footprints that carry placement constraints — an `Autoroute-edge` affinity, a
  lock, or `Autoroute-overlap` — with their reference and value (e.g. `J1
  edge=left`), so you can confirm what's pinned before a run. Nothing is printed
  when no footprint is constrained.
- **Auto-add ground plane (`--ground-plane`)** — after routing, emit a GND copper
  pour (zone) boundary following the board outline (inset by margin). Adds connecting
  vias where GND copper is isolated to only one layer (e.g. SMD-only islands on F.Cu),
  and optional stitching vias (`--stitch-vias`) to tie the planes together. KiCad
  computes the actual fill; PyAutoRoute emits only the boundary and the connecting
  vias. Works with `--place` and `--cycles`. Flags as "self-check excludes the pour"
  since KiCad's fill (delegated to kicad-cli) is the DRC authority.

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
