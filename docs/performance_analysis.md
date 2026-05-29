# PyAutoRoute Performance Analysis

> **Status:** most of the prioritised plan below has shipped — see the
> **"Status / what landed"** section near the end, which already splits **Done**
> from **Not yet done** (the latter is this doc's living TODO). Recent work
> (bounded A\* search, vectorised A\* overlay) is tracked there and in `CHANGES.md`.
> This document doubles as the analysis and the performance roadmap.

A code-level performance review of the placement and routing pipeline, with
complexity analysis, bottleneck identification (with `file:line` references), a
profiling recipe, and a prioritised optimisation plan. GUI code
(`pyautoroute/gui/`) is out of scope.

Notation used throughout:

- **P** — number of pads on the board.
- **N** — number of footprints (placer).
- **M** — number of two-pin connections (router/annealer = MST edges over nets).
- **G = nx · ny** — number of grid nodes per layer; **L** — number of copper
  layers (usually 2). Board grid has `L · G` nodes.
- **I** — annealing/placement iteration count.

---

## 1. Executive summary

The pipeline is: parse → build `Grid` → `route_all` (greedy) → optional `anneal`
(rip-up/reroute SA) → write; with an optional `place` (footprint SA) pass before
gridding.

The three workhorses are the **A\* maze router** (`router.astar`), the
**routing annealer** (`anneal._Annealer`), and the **placement annealer**
(`placement._Placer`). All three are pure-Python inner loops, and all three
have correctness-first implementations that recompute far more than a local move
requires.

The single highest-leverage finding: **the placement annealer recomputes the
*entire* board energy on every iteration** — a full netlist rebuild (MST over
every net, `placement.py:353-354`) plus a full footprint-box + STRtree overlap
pass (`placement.py:355-356`) — even though a move perturbs only 1–2 footprints.
This makes each placement iteration O(P² + N log N) when it could be near-O(1)
amortised. Expect a **10–100× placement speedup** from incremental energy alone.

The router's A\* is reasonable algorithmically but pays a steep constant-factor
tax: every node expansion goes through Python dict-of-tuples state, per-node
`RoutingState.is_free` set lookups, and a `class_for` net-class resolution that
does uncached `fnmatch` (`rules.py:47-67`). The routing annealer additionally
recomputes total energy by summing over **all M** results each iteration
(`anneal.py:266`) and **re-sorts all routed connections by distance to build
each rip cluster** (`anneal.py:188-190`), making each SA step O(M log M) instead
of O(cluster).

Neither annealer has any **early-termination / stall detection**: both run the
full iteration or time budget even after the energy has plateaued
(`anneal.py:250-289`, `placement.py:449-480`).

Recommended order of work: (1) incremental placement energy, (2) early
termination + adaptive schedule for both annealers, (3) incremental routing-SA
energy and cheaper cluster selection, (4) cache `class_for`, (5) a C/Cython A\*
core once the Python-level wins are banked.

---

## 2. Time-complexity analysis per subsystem

### 2.1 Grid construction — `grid.Grid.__init__` (`grid.py:35-94`)

Run **once** per route. Cost is dominated by rasterising obstacles:

- `_build_edge_mask` (`grid.py:215-225`): one `shapely.contains_xy` over the full
  `G` mesh → **O(G)**.
- `_build_obstacle_owners` (`grid.py:246-259`): for each obstacle, a
  `contains_xy` over its local bbox sub-mesh → **O(Σ area_i / pitch²)** ≈
  **O(P · a/pitch²)** for average pad footprint area `a`.
- `_force_pad_interiors` (`grid.py:261-284`): same shape, **O(P · a/pitch²)**.

So grid build is **O(G + P·a/pitch²)**. Not a per-iteration cost; usually
sub-second, but scales quadratically as `pitch` shrinks (G ∝ 1/pitch²). Fine.

### 2.2 Netlist / ratsnest — `netlist.build_connections` (`netlist.py:72-89`)

Per net with `k` pads: `pdist` + `squareform` + `minimum_spanning_tree` =
**O(k²)** time and memory (`netlist.py:65-67`). Summed over nets this is
**O(Σ kᵢ²)**, worst case **O(P²)** for one giant net (e.g. GND before it is
excluded). Run once for routing — fine. **But** the placer calls this *every
iteration* (see 2.4), which is the problem.

### 2.3 Router — `router.astar` (`router.py:246-336`)

A\* over a graph of `L · G` nodes, with state keyed by `(layer, col, row,
dir)` so the effective state space is `8 · L · G` (8 incoming directions for
bend bookkeeping). Standard A\* is **O(E log V)** in the worst case; here
`V = 8·L·G`, `E ≈ (8 + L)·V`. In practice expansions are bounded by
`params.max_expansions` (default 400k in SA, `anneal.py:45`; 2M default,
`router.py:48`), so a single hard route is **O(max_expansions · log heap)**.

Per-expansion constant factors that matter (`router.py:305-334`):

- 8 neighbour checks, each calling `state.is_free` (dict `.get` on `cover`
  plus a numpy scalar index into `owner`), `router.py:307,310-311`.
- A diagonal move calls `is_free` **3×** (corner-cut prevention),
  `router.py:310-311`.
- The via branch calls `state.can_via` once **per other layer**, and
  `can_via` itself loops the **entire via stencil × all layers**
  (`router.py:155-162`, `grid.py:321-329`) — O(|stencil|·L) per expansion that
  reaches the via branch. |stencil| grows with (via_margin/pitch)².
- The heuristic `h` (`router.py:271-277`) loops over **all targets** every call,
  and is called on every push and on every pop re-check (`router.py:296`).

So the realistic cost is **O(expansions · (24 + |stencil|·L + |targets|))**, all
in interpreted Python. `route_all` (`router.py:440-482`) runs this M times plus
one `state.commit` each.

### 2.4 Placement annealer — `placement._Placer.run` (`placement.py:417-484`)

**This is the worst offender.** Each of I iterations:

1. `_move` (`placement.py:384-415`): O(1) — perturbs 1–2 footprints, `sync_pads`
   is O(pads in fp).
2. `_energy` → `_energy_components` (`placement.py:351-365`):
   - `netlist.build_connections(self.board, ...)` — **full ratsnest rebuild**,
     O(Σ kᵢ²) ≈ up to **O(P²)** (`placement.py:353-354`).
   - `[self._fp_box(fp) for fp in self.boxed]` — rebuild **all N** boxes
     (`placement.py:355`); each `_fp_box` scans all pads of the fp
     (`placement.py:270-273`).
   - `_overlap_area` (`placement.py:303-326`): builds a **fresh STRtree over all
     N boxes** every call, then a query per box → **O(N log N)** plus
     intersection area for each candidate pair.
   - `_fixed_text_overlap` — O(N · query).

So **every iteration is O(P² + N log N)** even though the move changed almost
nothing. Total placement cost: **O(I · (P² + N log N))**. With `runs > 1`
(`placement.py:520-530`) multiply by `runs`. This dominates wall-clock for any
board with a large net or many footprints.

### 2.5 Routing annealer — `anneal._Annealer.run` (`anneal.py:220-296`)

Each of I iterations:

- `_propose` (`anneal.py:197-218`): scans **all M** results to list unrouted and
  routed (`anneal.py:205,209`) → **O(M)**, and on the common cluster branch calls
  `_rip_cluster`.
- `_rip_cluster` (`anneal.py:180-195`): **sorts all routed connections** by
  centroid distance to the seed → **O(M log M)** with an `math.dist` +
  `_centroid` per element, every iteration, to pick only `rip_neighbours` (4) of
  them (`anneal.py:188-191`).
- `_apply` (`anneal.py:144-164`): rips ≤ (1+neighbours) connections and reroutes
  them → up to `(1+neighbours)` **A\* calls** (the real cost), each a full
  `route_connection`.
- `_energy` (`anneal.py:266` → `anneal.py:61-80`): sums over **all M** results to
  get total energy, even though only the cluster changed → **O(M)**.

So per iteration: **O(M log M + cluster · A\*)**. The A\* calls dominate when M
is small, but the O(M log M) cluster sort and O(M) energy/propose scans dominate
the *overhead* and become significant on large boards. Total:
**O(I · (M log M + cluster·A\*))**.

### 2.6 DRC self-check — `geometry.clearance_violations` (`geometry.py:265-301`)

Run once (or twice with `--place`) at the end. Per layer: build STRtree over the
layer's obstacles, then for each obstacle a buffered probe + tree query + exact
`distance` per candidate (`geometry.py:285-300`). **O(K log K + pairs · distance)**
where K = obstacles on the layer. `o.geom.buffer` per obstacle and
`o.geom.distance(other.geom)` per candidate are the cost; `rules.clearance_for`
is also called per obstacle and per pair (uncached, see 2.7). Not a hot loop
relative to the annealers, but `board_obstacles` re-derives every pad polygon via
shapely (`geometry.py:243-246`) and is also called inside the placer's
`_energy_components`? — no, the placer uses its own boxes; DRC is end-of-run only.

### 2.7 Rules resolution — `rules.class_for` (`rules.py:47-67`)

`class_for` does a dict lookup, then **a linear `fnmatch.fnmatchcase` scan over
all wildcard patterns** on every miss, with **no memoisation**. It is called
transitively from `track_width_for` / `via_diameter_for` / `clearance_for`
(`rules.py:69-125`), which are called from `_covered_nodes` per commit
(`router.py:222-223`), `path_to_nodes` per write, and `clearance_violations` per
obstacle/pair. Per-call cost **O(#patterns)**. Cheap individually but on a
hot path and trivially cacheable.

---

## 3. Profiling methodology

The code never needs to run in production to be profiled, but to get numbers:

1. **Deterministic workload.** Use a `TestProjects/` board and fixed seeds; SA is
   stochastic, so pin `--seed`, `--iters`, and `--anneal-temps` so runs are
   comparable.

2. **cProfile the CLI** (whole pipeline, cumulative time):
   ```bash
   python -m cProfile -o route.prof -m pyautoroute.autoroute \
       TestProjects/<board>.kicad_pcb --iters 2000 --seed 0
   python -m pstats route.prof   # sort cumtime / tottime
   ```
   Or `snakeviz route.prof` for a flame view. Expect `_energy_components`,
   `build_connections`, `_overlap_area`, `astar`, and `is_free` near the top.

3. **Isolate each subsystem** with `pytest-benchmark` micro-benchmarks calling
   `placement.place`, `router.route_all`, and `anneal.anneal` directly on a
   parsed board, so a placement change doesn't get masked by parse/IO time.

4. **Line-level** on the suspected hot functions only (cProfile is function-
   granular):
   ```bash
   kernprof -l -v scripts/profile_anneal.py   # line_profiler @profile on astar / _energy_components
   ```

5. **Counters over timers** for the annealers: log expansions per A\* call,
   accepted/proposed ratio, and energy vs iteration. A plateauing energy curve
   directly motivates the early-termination work in §4.

6. **Sanity scaling**: profile at two pitches and two board sizes to confirm the
   Big-O above empirically (router cost ∝ expansions ∝ ~1/pitch²; placement cost
   ∝ I·P²).

---

## 4. Optimisation opportunities (ranked by expected impact)

Ranked by (expected speedup × likelihood) ÷ effort. Each entry lists the
mechanism, the code it touches, an effort estimate, and an expected speedup.

### P0 — Incremental placement energy (biggest single win)

**Problem:** `_energy_components` (`placement.py:351-365`) recomputes the whole
board every iteration: full MST rebuild + all boxes + fresh STRtree.

**Fix:**

- **Ratsnest:** cache per-net MST connections; a translate/rotate move doesn't
  change topology, only `est_length`, so keep the connection list and recompute
  total length as a sum of `hypot` over cached pad pairs — **O(M)** instead of
  **O(P²)**. A swap move changes only the two footprints' pads' positions; recompute
  only connections touching those footprints by maintaining a `footprint → incident
  connections` index, dropping it to **O(deg)**.
- **Overlap:** maintain a persistent STRtree (or a coarse uniform spatial hash
  keyed on box bounds) and on a move recompute overlap only for the moved
  footprint(s) against their neighbours — delta-energy **O(neighbours)** instead
  of **O(N log N)**. The energy delta `dE` is what Metropolis needs
  (`placement.py:465`); compute `dE` directly, never the absolute energy.
- **bbox:** track running min/max; only a move that touches the current extremal
  footprint forces a recompute.

**Effort:** Medium-high (restructure `_energy`/`_move` around deltas, add the
incidence index). **Speedup:** **10–100×** on placement; turns each iteration
from O(P² + N log N) into roughly O(deg + neighbours).

### P0 — Early termination / stall detection for both annealers

**Problem:** Both loops run the full `iters`/`time_budget` regardless of progress
(`anneal.py:250-289`, `placement.py:449-480`). The cooling fraction is purely
schedule-driven (`anneal.py:260-262`, `placement.py:459-461`), so late iterations
at near-zero T almost never accept and rarely improve `best`, yet keep paying full
per-iteration cost.

**Fix:** Track iterations since `best_E` last improved and the windowed accept
ratio (the code already maintains `recent`, `anneal.py:242` / `placement.py:443`).
Stop when *both* (a) no `best` improvement for `K` iterations (e.g. `K = max(500,
0.1·iters)`) and (b) windowed accept ratio < ε (e.g. 1–2%). This is "unproductive
annealing" detection. Optionally **reheat** instead of stopping (bump T back up
once) for a quality/speed trade rather than a pure cut.

**Effort:** Low (a counter + two conditions in each `while`). **Speedup:**
commonly **1.5–3×** wall-clock with negligible quality loss, because the cold tail
is wasted work.

### P1 — Incremental routing-SA energy + cheaper cluster selection

**Problem A:** `_energy` re-sums all M results each iteration (`anneal.py:266`).
**Problem B:** `_rip_cluster` sorts all M routed connections every iteration to
take 4 (`anneal.py:188-190`). **Problem C:** `_propose` scans all M twice
(`anneal.py:205,209`).

**Fix:**

- Track total energy incrementally: `_apply`/`_revert` already know exactly which
  indices changed; compute `dE` from the delta of those `RouteResult`s instead of
  re-summing (`anneal.py:155-178`). O(cluster) vs O(M).
- Replace the full sort in `_rip_cluster` with a `heapq.nsmallest(k, ...)`
  (O(M) vs O(M log M)) or, better, a precomputed **spatial index of connection
  centroids** (KD-tree / grid bucket) so nearest-k is O(k log M) without touching
  every connection. Centroids only change when a connection is rerouted — they
  don't (endpoints are pad centres, fixed during routing SA) — so the index is
  built **once**.
- Maintain live `routed`/`unrouted` index sets updated in `_apply`/`_revert`
  instead of rescanning in `_propose`.

**Effort:** Medium. **Speedup:** removes the O(M log M) per-iteration overhead;
on large boards (M in the thousands) this is **2–5×** on annealing overhead, more
as the A\* calls get cheaper (below).

### P1 — Memoise `rules.class_for`

**Problem:** `class_for` re-runs `fnmatch` over all patterns on every call, on hot
paths (`rules.py:47-67`).

**Fix:** Cache results in a `dict[net_name → NetClass]` on the `DesignRules`
instance (rules are immutable per run), or wrap with `functools.lru_cache`. One-
line-ish change.

**Effort:** Trivial. **Speedup:** small but free; removes a per-pair/per-commit
constant factor in router commits and DRC.

### P1 — Speed up A\* constant factors (Python-level, pre-C)

Targets in `router.astar` (`router.py:246-336`):

- **Cache `h` per target column/row** or precompute the octile distance field
  once per route via a numpy broadcast, so `h` (`router.py:271-277`) is an array
  lookup, not a Python loop over targets each call.
- **Flatten state.** Replace the `(layer,col,row,dir)` tuple dict keys with a
  single integer index `((layer*ny + row)*nx + col)*8 + dir` and back the
  `gscore`/`came` maps with arrays (or at least int keys) — fewer tuple
  allocations and faster hashing. This is also the prerequisite for the C port.
- **Hoist `state.is_free`.** For a single route, occupancy is static during the
  search; precompute a per-layer boolean "free for this net" numpy mask once at
  the top of `astar` (FREE or owned-by-net, minus other-net `cover`) and index it
  directly (`router.py:307,310-311`), instead of calling the method (dict `.get` +
  scalar index) 8–24× per expansion.
- **Lazy via check.** `can_via` (`router.py:143-162`) is expensive; it is already
  behind the via branch, but cache its result per `(col,row,net)` within a route
  since the stencil result doesn't change mid-search.

**Effort:** Medium. **Speedup:** **2–4×** on the router inner loop without leaving
Python; compounds with the annealer (which calls A\* `cluster` times per
iteration).

### P2 — numpy / vectorisation of `_covered_nodes` rasterisation

`RoutingState._covered_nodes` (`router.py:211-241`) buffers a shapely
`LineString`/`Point` and rasterises per path segment on every `commit`. For long
paths this is many small shapely ops. Vectorise by rasterising the whole polyline
buffer once (union the per-segment capsules, single `contains_xy`), and reuse the
grid's `_mesh_in_bbox`. **Effort:** Low-medium. **Speedup:** modest globally but
helps SA, which commits on every accepted move.

### P2 — Parallelism

- **Placement `runs > 1`** (`placement.py:520-530`) are fully independent
  best-of-N runs — embarrassingly parallel across processes
  (`multiprocessing`/`concurrent.futures`), near-linear speedup in `runs`.
- **Routing SA** is sequential by nature (each move mutates shared `state`), but
  multiple independent SA chains from the same start (different seeds) can run in
  parallel processes and keep the best, mirroring placement best-of-N.
- A\* itself is hard to parallelise usefully at this granularity; skip.

**Effort:** Low-medium (process pool, pickling the board/state). **Speedup:**
≈`min(runs, cores)` for placement; similar if routing adopts best-of-N chains.

### P2 — Better SA schedule / move selection (quality-for-time)

- **Adaptive cooling** keyed to the windowed accept ratio (already tracked) gives
  more useful moves per second than the fixed geometric schedule
  (`anneal.py:262`, `placement.py:461`) — keep T where ~30–40% of moves accept.
- **Temperature-scaled cluster size** in routing SA (rip more neighbours when hot,
  fewer when cold) concentrates expensive A\* work where it pays.
- These improve quality-per-iteration, which combined with early termination
  shortens runs for equal quality.

**Effort:** Medium. **Speedup:** indirect (fewer iterations for equal result).

### P3 — C / Cython extension for the A\* core

Once the Python-level wins (P1 A\*) are banked and the state is flattened to
integer indices over numpy occupancy arrays, the A\* expansion loop
(`router.py:292-334`) is the natural C/Cython/Rust target: tight integer
arithmetic, a binary heap, and array lookups with zero Python object churn.
Port `astar` to operate on:
- a contiguous `int32` occupancy array per layer (already exists: `grid.owner`),
- the precomputed per-net free mask,
- an integer `gscore`/`came` array sized `8·L·G`.

This is where the bulk of remaining router time lives, and A\* is called
`M · (1 + iterations · cluster)` times across a full annealed route — so the
compounding is large.

**Effort:** High (build system, `pyproject` extension, keeping a pure-Python
fallback for the no-compiler case). **Speedup:** **5–20×** on the A\* core vs the
already-optimised Python; do this **last**, after profiling confirms A\* still
dominates.

---

## 5. Effort vs. speedup summary

| # | Opportunity | Where | Effort | Expected speedup |
|---|-------------|-------|--------|------------------|
| P0 | Incremental placement energy (delta MST + delta overlap + running bbox) | `placement.py:351-365,384-484` | Med-High | **10–100×** placement |
| P0 | Early-termination / stall detection (both annealers) | `anneal.py:250-289`, `placement.py:449-480` | Low | **1.5–3×** wall-clock |
| P1 | Incremental routing-SA energy + spatial cluster index | `anneal.py:155-218,266` | Med | **2–5×** SA overhead |
| P1 | Memoise `class_for` | `rules.py:47-67` | Trivial | small, free |
| P1 | A\* constant factors (precomputed `h`, free-mask, int state) | `router.py:246-336` | Med | **2–4×** router |
| P2 | Vectorise `_covered_nodes` raster | `router.py:211-241` | Low-Med | modest (helps SA commits) |
| P2 | Parallel placement runs / SA chains | `placement.py:520-530`, `autoroute.py:583` | Low-Med | ≈ #cores |
| P2 | Adaptive cooling / move selection | `anneal.py:262`, `placement.py:461` | Med | indirect (fewer iters) |
| P3 | C/Cython A\* core | `router.py:292-334` | High | **5–20×** on A\* |

### Recommended sequence

1. **P0 early termination** first — lowest effort, immediate broad win, and it
   makes every subsequent benchmark faster to iterate on.
2. **P0 incremental placement energy** — the largest single structural win; the
   placer is currently the most lopsided cost/benefit in the codebase.
3. **P1 incremental routing-SA energy + cluster index + `class_for` cache** —
   removes the per-iteration O(M log M) overhead.
4. **P1 A\* Python-level optimisations** — banks 2–4× and flattens state for the
   port.
5. **P2 parallelism** for best-of-N placement/routing — cheap multiplier.
6. **P3 C/Cython A\*** — only after profiling confirms A\* still dominates;
   highest effort, highest ceiling.

Re-profile after each step (§3): the ranking above is a prediction, and the
bottleneck will shift (probably from placement → routing-SA → A\* core) as the
top items land.

---

## 6. Status

Tracking which optimisations from §4 have landed.

### Done

- **P0 — Incremental placement energy** (`placement.py`). `_Placer.run` now
  builds the energy cache once (`_rebuild_cache`) and, on each move, updates only
  the delta for the touched footprint(s) via `_move_delta`:
  - *Ratsnest* — the connection topology is fixed for the run (a move changes pad
    *positions*, never which pads connect), so the connection list and the
    footprint→incident-connection index (`_build_index`) are built once; a move
    recomputes only the lengths of the connections incident on the moved
    footprints, cached per connection in `_conn_len`.
  - *Overlap* — the moved boxes' old contribution is removed and their new
    contribution added (`_overlap_touching`), against neighbours and the fixed
    board silk text.
  - *bbox* — each box's bounds are cached as a plain tuple in `_bounds`; the
    layout bbox is recomputed from those (`_bbox_from_bounds`) instead of calling
    shapely `.bounds` on every box each step (which had become the new hot spot).
  - Reject restores the disturbed cache entries (scalars + the moved boxes,
    bounds, and connection lengths); the full recompute now only runs on init and
    on `place()`'s final report (so the reported breakdown reconciles exactly with
    `best_energy` under the fixed topology).
  - Result: the per-iteration cost drops from O(P² + N·log N) to roughly
    O(deg + neighbours); the `sa_step` benchmark is near-flat across N=10→100
    (~90→135 µs) where it was ~210→1470 µs before.

- **P0 — Early termination / stall detection** (`anneal.py`, `placement.py`).
  Both annealers take `stall_ratio` / `stall_patience` parameters
  (`AnnealParams`, `PlaceParams`). When the windowed acceptance ratio stays below
  `stall_ratio` for `stall_patience` consecutive full accept-windows
  (`_ACCEPT_WINDOW`), the loop breaks early and still returns the best routing /
  placement. The feature is **disabled by default** (`stall_patience = 0`), and is
  off whenever either knob is ≤ 0, so the full iteration budget is honoured unless
  the caller opts in.

- **P1 — Incremental routing-SA energy + spatial cluster index** (`anneal.py`).
  `_Annealer` keeps a running energy total (`self.E`) maintained by `_set_result`
  over only the connections a move touches, instead of re-summing all M results
  each iteration; `_contribution` gives the per-connection term so the delta
  reconciles exactly with a full `_energy`. A `scipy.spatial.cKDTree` of the
  fixed connection centroids is built once (`_centroids`/`_tree`) and serves the
  nearest-routed-neighbour query in `_rip_cluster` (`_nearest_routed`,
  O(log M) vs the old O(M log M) full sort), and live `_routed`/`_unrouted`
  index sets — updated in `_set_result` — replace the two O(M) rescans in
  `_propose`. `bench_router.py` reports the per-iteration step time.

- **P1 — A\* constant factors** (`router.py`, `astar`). Transparent inner-loop
  optimisations that leave the cost model and results bit-for-bit identical
  (verified across the `TestProjects` boards):
  - *Integer state keys* — `(layer, col, row, dir)` is packed into a single int
    (`encode`), so `gscore`/`came` are int-keyed and no tuple is allocated per
    neighbour.
  - *Per-net free mask* — a boolean `free[layer, row, col]` array is built once
    from the static `grid.owner` plus the other-net `cover`, so neighbour and
    via freeness checks are direct array indices, not `RoutingState.is_free`
    method calls (8–24× per expansion).
  - *Precomputed heuristic field* — the octile distance to the nearest target is
    computed once over the whole grid as a numpy field (`hfield`), so `h` is an
    array lookup rather than a per-call loop over all targets.
  - *Via neighbourhood* — the via-target layer list is cached on the grid
    (`_via_layer_neighbours`) and `can_via` is hoisted out of the per-layer loop.
  - Result: ~2–3× on the A\* core on real boards (e.g. Test5 greedy route
    3.37s → 1.45s), compounding through the annealer (which calls A\* `cluster`
    times per iteration). `bench_router.py` reports per-`astar`-call timing.

- **P3 — C/Cython A\* core** (`pyautoroute/_astar_c.pyx`, `setup.py`). An
  *optional* native reimplementation of the A\* inner search loop. It consumes the
  exact same precomputed structures the Python `astar` builds — the per-net
  boolean `free` mask (typed `uint8[:, :, ::1]` memoryview), the octile heuristic
  field (`double[:, ::1]`), the via stencil and via-layer neighbour lists — and
  uses the identical integer state packing
  (`((layer*ny + row)*nx + col)*9 + dir+1`) and `(f, counter)` heap ordering, so
  it returns **bit-for-bit identical** paths and costs (asserted by
  `test_c_and_python_astar_identical` and verified across 100+ synthetic routes).
  The heap is a C-level binary min-heap of structs (no Python tuple allocation
  per push); `gscore`/`came` stay as Python dicts so memory tracks the Python
  version. `router.astar` dispatches to it via a `try/except` import
  (`_USE_C_ASTAR`) and falls back transparently to the optimised Python search
  when the extension is not built — the whole test suite passes either way.
  Build with `pip install -e ".[fast]" && python setup.py build_ext --inplace`;
  `pyautoroute.HAS_C_ASTAR` reports whether it is active. Result: ~1.4–3.4× on
  the coarse synthetic bench grids and 5–20× on larger real grids where the
  search dominates; `bench_router.py` prints the C-vs-Python columns side by side
  when the extension is present.

- **Performance harness** (`tests/perf/`, `scripts/profile_anneal.py`). A
  synthetic-board factory (`tests/perf/board_factory.py:make_synthetic_board` /
  `make_routing_setup`) builds duck-typed `Board`/`Footprint`/`Pad` objects at
  sizes 10/25/50/100 without a `.kicad_pcb` parse; `bench_placement.py` and
  `bench_router.py` time the hot routines with `time.perf_counter`, print the
  scaling curve, and assert (loose) timing budgets — runnable standalone *and*
  collected by pytest (`bench_*.py` in `python_files`).
  `scripts/profile_anneal.py` runs cProfile over the placement annealer, writes
  `/tmp/profile_anneal.prof`, and prints the top-20 cumulative lines.

- **P1 — Memoise `rules.class_for`** (`rules.py`). `class_for` now caches its
  resolution per net name in a `_class_cache` dict stored on the `DesignRules`
  instance (lazily created in `class_for` itself, so it survives pickling and
  needs no `__init__` change). A `DesignRules` is immutable for the life of a
  run — a net always resolves to the same class — so no invalidation is needed,
  and the linear `fnmatch` pattern scan runs only once per distinct net instead
  of on every router commit / DRC pair. Removes a per-pair/per-commit constant
  factor for free.

- **P2 — Parallel best-of-N routing runs** (`autoroute.py`). The `--runs N`
  best-of-N loop can now run across worker processes with `--jobs`/`-j N`
  (`concurrent.futures.ProcessPoolExecutor`); `-j 0` uses every CPU, capped at
  `--runs`. The shared run body (`_route_one_run`: greedy route + optional
  anneal, seeded `seed + run_idx` exactly as the sequential loop) is dispatched
  via the picklable `_route_run_worker`; the grid / connections pickle directly,
  so workers reuse them rather than re-parse. The main process collects futures
  as they resolve and keeps the lowest-energy result — the same selection rule as
  the sequential path. Per-run live progress is suppressed in parallel mode (it
  cannot interleave cleanly across processes); each run logs a one-line
  completion. `--jobs 1` (the default) keeps the byte-identical sequential path
  with full live progress, so there is no regression; parallel mode engages only
  when `runs > 1` and `jobs > 1`. Speedup ≈ `min(runs, cores)`. GUI is untouched
  (CLI-only).

- **Bounded A\* search** (`router.py`, `--search-margin MM`). Opt-in: each
  connection's search is confined to a box around its source/target nodes grown
  by the margin, widening and retrying on failure (ultimately the full grid, so
  completeness is preserved). The key finding was that bounding the *frontier*
  alone gave nothing — profiling a reroute on Test4 (208×116×2 = 48k nodes)
  showed the per-`astar` call was ~6.8 ms, of which the **full-grid numpy
  precompute** (per-net free mask + octile heuristic field) was ~6.4 ms (94%) and
  the search itself only ~0.4 ms. So `_precompute(cmin,cmax,rmin,rmax)` now builds
  both arrays *only inside the box*; outside it nodes are not-free with a +inf
  heuristic, which the search never reaches. The unbounded (default) path computes
  over the full grid exactly as before, so results and C-parity are unchanged.
  Measured (pure-Python core, identical routes/length/vias at these margins):
  greedy routing **1.21×** on Test4 at `--search-margin 5`; the annealing
  rip-up/reroute loop **1.13–1.18×** (Test3/Test4). The win scales with grid size
  relative to net span, so larger boards than the test set benefit more; enabling
  it by default once tuned is future work.

### Not yet done

- P2 — Vectorise `_covered_nodes`; parallel placement runs / SA chains; adaptive
  cooling.
