# Plan: placement quality — edge-aware placement + place↔route coupling

> **Status: ✅ Implemented.** Both Part A (edge-aware placement: `Footprint.edge_affinity`,
> `--keep-outline`, `--place-edge-weight`) and Part B (place↔route coupling: `--cycles`,
> `--place-feedback`, `--congestion-weight`, `--jobs`) are shipped. See `CHANGES.md`. It follows the same plan-doc convention as
> [`place-feature-plan.md`](place-feature-plan.md) and
> [`gui-plan.md`](gui-plan.md): a design artefact reviewed before code, then
> implemented in phases, each shipping with docs + a version bump + a `CHANGES.md`
> entry (per `CLAUDE.md`).

## Motivation

Two gaps in the experimental `--place` pass (`placement.py`,
[`place-feature-plan.md`](place-feature-plan.md)):

1. **No way to keep edge components at the edge.** Connectors, USB/headers,
   mounting hardware must sit on the board boundary. Today the only constraint is
   KiCad's native lock, which pins a part in *absolute* space — it can't express
   "stay near the edge". Worse, **placement has no fixed edge to anchor to**:
   `pcb.apply_placement` (pcb.py:916) discards the parsed Edge.Cuts and
   regenerates it as a bounding rectangle around wherever the parts landed
   (`pad_bounding_outline`, pcb.py:896). The edge is an *output*, not an input.

2. **Placement is chosen by a proxy, not by how it routes.** Orchestration today
   (autoroute.py `run`) is strictly phased: placement does best-of-N picked by
   **placement energy** (`ratsnest + overlap·w + bbox·w`, placement.py:8), then
   routing does best-of-N on that single winning placement. The placement winner
   is never scored by the routed result, so a "best-looking" placement (short
   rats-nest, compact) can route worse — more vias, or nets it can't complete.

This plan addresses both, in four shippable phases.

---

## Part A — Edge-aware placement

Two complementary mechanisms; together they let a part be pulled to the boundary,
and let the board keep a real (mechanical) outline.

### A1. Property convention (extends the existing `Autoroute` field)

Footprint intent is already carried by a user-defined `Autoroute` property, parsed
in `_footprint_overlap_ok` (pcb.py:455) — a value *containing* `overlap` opts a
footprint into body overlap. Extend this to a small token set (case-insensitive,
comma/space separated), so one property can carry several intents:

| `Autoroute` value | meaning |
|---|---|
| `overlap` | (existing) body may overlap others — e.g. a shield |
| `edge` | pull toward the **nearest** boundary edge |
| `edge-left` / `edge-right` / `edge-top` / `edge-bottom` | pull toward a **named** side |

So a left-hand connector is `Autoroute=edge-left`; a shield that also hugs the top
is `Autoroute=overlap, edge-top`. Backward compatible: bare `overlap` is unchanged.

**Model change** (pcb.py): add `Footprint.edge_affinity: str | None`
(`None | "any" | "left" | "right" | "top" | "bottom"`), parsed alongside
`overlap_ok` in `load_board`. A new `_footprint_edge_affinity(fp_node)` helper
mirrors `_footprint_overlap_ok`.

### A2. `--keep-outline` — honour a fixed board outline

When a board has an **intentional** Edge.Cuts (enclosure, mounting holes,
mechanical constraints), regenerating it is wrong. Add `--keep-outline` (CLI +
GUI checkbox):

- **Don't** regenerate Edge.Cuts: `apply_placement(board, keep_outline=True)`
  leaves `board.outline` as parsed; `sync_tree_from_placement` skips the Edge.Cuts
  swap (pcb.py:1040) so the original outline nodes are written verbatim.
- **Contain** movable footprints inside the outline polygon. The polygon is
  already assembled for the grid (`geometry`), so reuse it. Containment is a soft
  energy penalty (distance a footprint box protrudes outside the inset polygon ×
  a large weight) rather than a hard clamp, so the annealer can climb out of
  invalid starts; the high weight drives it inside by the end.
- No Edge.Cuts present → `--keep-outline` warns and falls back to bbox
  regeneration (nothing to keep).

This is broadly useful beyond connectors: it makes `--place` usable on boards with
a real outline, not just greenfield layouts.

### A3. Edge-affinity energy term

Add one term, `edge_weight · Σ_flagged dist(fp, target_edge)`, where the target
depends on the mode:

- **`--keep-outline`:** distance from the footprint box to the fixed outline
  boundary (or, for a named side, to that side of the outline's bounding box).
- **default (bbox regen):** distance to the relevant side of the **current layout
  bounding box** — already cached as `self._bbox` bounds (placement.py:336). This
  is *not* circular: an `edge-left` part minimises `(fp.minx − layout.minx)`,
  i.e. it is rewarded for being the left-most part = "on the left edge". `edge`
  (any) minimises the distance to the nearest of the four sides, pulling it
  outward onto the perimeter.

**Incrementality.** The term depends only on the flagged footprints' boxes and the
layout-bbox bounds, both already recomputed cheaply in `_move_delta`
(placement.py:485). It slots into the cached-energy structure without a full
rescan, preserving the incremental-energy speedup (0.22.0).

**Tuning.** New `--place-edge-weight` (default chosen so a connector reliably
reaches the boundary without distorting the rest of the layout); exposed in the
GUI Advanced panel and the tuner's sweep space.

### A4. Orientation (phase-2 note, not in scope here)

Connectors usually also want to **face outward** (opening toward the edge). That
needs a per-footprint "outward axis", which is ambiguous to infer. Deferred; if
pursued, encode it explicitly (e.g. `edge-left` implies the footprint's local +X
faces left) and add a rotation-alignment term.

### A5. Files & tests (Part A)

- `pcb.py` — `Footprint.edge_affinity` + parser; `apply_placement(keep_outline)`;
  `sync_tree_from_placement` skips Edge.Cuts regen when keeping the outline.
- `placement.py` — `PlaceParams.edge_weight`, the affinity term, the containment
  term, the outline polygon passed in; all wired into `_move_delta`/`_cached_energy`.
- `autoroute.py` — `--keep-outline`, `--place-edge-weight`; thread into `PlaceParams`.
- `gui/controls.py` — keep-outline checkbox; edge-weight in Advanced.
- `tests/test_placement.py` — (i) an `edge-left` part ends up left-most; (ii) `edge`
  part ends on the perimeter; (iii) `--keep-outline` keeps all parts inside the
  parsed polygon and leaves the Edge.Cuts node unchanged in the output; (iv)
  `overlap` + `edge` combine; locked parts still fixed.

---

## Part B — Place↔route coupling (best-of-cycles + feedback)

### B1. Best-of-cycles (the core)

Replace "best placement by proxy, then route it" with "best **routed** result over
N independent place→route attempts":

```
for k in range(cycles):
    board_k  = load_board(input)              # clean state per cycle
    place(board_k, seed = seed + k)           # + apply_placement
    grid_k   = Grid(board_k, rules, pitch)
    results_k = route + anneal(grid_k, seed = seed + k)
    score_k  = (unrouted_k, routed_energy_k)  # lexicographic
keep argmin(score_k); write its board + results
```

- **Selection metric:** fewest unrouted connections first, then routed energy
  (`anneal._energy = length + via_weight·vias + unrouted_weight·unrouted`,
  already computed). Completing nets dominates; ties broken by length/vias.
- **Clean state:** re-`load_board` per cycle (cheap next to place+route) avoids
  placement/routing state leaking between cycles.
- **Flag:** `--cycles N` (active only with `--place`). `N=1` = today's behaviour.

### B2. Relationship to `--runs` / `--place-runs`

`--cycles` is the **outer** loop and the primary knob once present. To avoid a
combinatorial blow-up, each cycle runs **one** placement and **one** routing
(with annealing). `--place-runs` (best-of-N placement by *placement* energy) and
`--runs` (best-of-N routing) remain available as **inner** loops for power users,
but `--cycles` is the recommended way to get a better board, because it selects on
the true (routed) objective. Documented explicitly to avoid confusion.

### B3. Parallelism + the shared-pipeline refactor (synergy)

Cycles are independent → parallelise across processes exactly like `--jobs` does
for routing today (`_route_run_worker` + `ProcessPoolExecutor`, autoroute.py:692).
But the worker must now do **place + route**, not just route. This is the moment
to extract the place→route→score unit into a **`pipeline.py`** helper:

- One picklable `run_cycle(input, rules, pitch, params, seed) -> CycleResult`
  used by both the sequential and parallel paths.
- **Pays down a known TODO:** `gui/worker.py:_pipeline` currently *duplicates*
  `autoroute.run`'s orchestration (flagged in [`gui-plan.md`](gui-plan.md)).
  Routing the GUI worker and the CLI through the same `pipeline` helper removes
  that drift and is the prerequisite the GUI TODO already calls for.

`--jobs` then governs cycle workers; per-cycle live progress is suppressed in
parallel mode (as routing already does), with a one-line completion per cycle.

### B4. Feedback — congestion-aware re-placement (the deeper coupling)

Best-of-cycles is *independent* attempts. Feedback makes each cycle **learn** from
the previous routed result (PathFinder-style):

- **Signal.** After routing a cycle, build a coarse **congestion field** (a
  low-res heatmap over the board) from: (a) connections left **unrouted** (mark
  the region between their endpoints) and (b) **contention** — cells with high
  track/via density or high rip-up churn during annealing. The annealer already
  holds the routed `results`; the heatmap is derived from them.
- **Use.** Add an opt-in placement term `congestion_weight · Σ_fp field(fp.centroid)`
  that pushes footprints **out of** hot zones, spreading the layout exactly where
  routing struggled. The field is blended/decayed across cycles so it accumulates
  signal without oscillating.
- **Strategy (decided): fresh re-placement.** Cycle 0 is plain placement; each
  later cycle runs a **fresh** placement (its own seed) under the accumulated
  congestion term — not a perturbation of the previous best. This keeps every
  cycle an independent, exploratory attempt (consistent with best-of-cycles in
  B1), with the congestion field as the only memory carried forward. The best
  result is always kept by **routed** score, so feedback can only help or be
  discarded. (Perturb-the-best was considered and rejected: it risks locking into
  one basin, which is exactly what cycles are meant to avoid.)
- **Guardrails.** Feedback is opt-in (`--place-feedback`), bounded blend factor,
  and measured before any thought of enabling by default — coupled loops can
  destabilise, so the keep-best gate is essential.

### B5. Files & tests (Part B)

- `pipeline.py` (new) — `run_cycle(...)` + `CycleResult`; the shared place→route→
  score unit (sequential + picklable for workers).
- `autoroute.py` — `--cycles`, `--place-feedback`, `--congestion-weight`; the cycle
  loop + selection + reporting (`cycle x/N`); route GUI and CLI through `pipeline`.
- `placement.py` — `PlaceParams.congestion_field` / `congestion_weight` + term.
- `anneal.py` / `router.py` — expose a congestion/contention signal from `results`
  (a `congestion_heatmap(results, grid)` helper; no behaviour change to routing).
- `gui/worker.py` — call the shared `pipeline` unit; surface cycle progress.
- `tests/` — (i) `--cycles N` returns the lowest-routed-energy cycle and is
  deterministic for a fixed seed; (ii) on a board engineered to need spreading,
  feedback reduces unrouted/vias versus independent cycles; (iii) `pipeline.run_cycle`
  parity with the in-line path.

---

## Phasing

Each phase is independently shippable (docs + minor version bump + `CHANGES.md`):

| Phase | Scope | Why this order |
|---|---|---|
| **1. Edge-affinity** | A1 + A3 (property flag + energy term, bbox mode) | Smallest change; solves the connector ask directly. |
| **2. Keep-outline** | A2 (+ affinity vs fixed outline) | Makes `--place` usable on boards with real outlines. |
| **3. Best-of-cycles** | B1 + B2 + B3 (incl. `pipeline.py` extraction) | Selects on the true objective; pays down the GUI/CLI duplication. |
| **4. Feedback** | B4 (congestion-aware re-placement) | The research-y coupling; prototype and **measure** before defaulting. |

## Risks & open questions

- **Edge target consistency** — the affinity term must behave sensibly in both
  bbox mode and keep-outline mode; named-side vs nearest-side semantics need a
  clear spec (A3).
- **Cycle cost** — `--cycles N` is ~N×(place+route); parallelism (B3) is essential
  for it to be usable, so phase 3 includes the worker refactor, not just the loop.
- **Feedback stability** — coupled place/route loops can oscillate; mitigated by
  the keep-best gate, a bounded blend factor, and treating phase 4 as opt-in until
  measured.
- **Re-load vs deep-copy per cycle** — re-loading the board is simpler and avoids
  state leakage; the cost is negligible beside place+route. (Deep-copy is the
  alternative if load ever becomes significant.)

## Resolved decisions

- **Property syntax: hyphen** — `Autoroute=edge-left` (not `edge:left`). Reads
  cleanly, combines as a token (`overlap, edge-top`), and avoids any colon parsing.
- **Feedback strategy: fresh re-placement** — each feedback cycle re-places from
  scratch under the accumulated congestion field rather than perturbing the
  best-so-far (B4).
