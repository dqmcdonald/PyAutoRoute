# Plan: placement polish — gradient descent refinement after annealing

> **Status: ✅ Shipped in 0.46.0 (2026-06-05).** GUI controls for the polish pass
> were added shortly after in the same minor. See `CHANGES.md` for the full
> entry. The implementation followed the plan below; no major deviations.

## Motivation

The `--place` pass (`placement.py`) optimises footprint poses by **simulated
annealing** (`_Placer.run`, placement.py:916). SA is a global, stochastic
explorer: it escapes local minima well, but its moves are random jumps drawn from
a cooling temperature schedule, so the placement it lands on is rarely sitting at
the *bottom* of its local energy basin. Two symptoms:

1. **Close contacts that never quite relax.** The overlap term
   (`overlap_weight · overlap_area`, placement.py:252) pushes footprints apart
   only when a random translate move happens to point the right way *and* survives
   the Metropolis test. Near the end of the schedule the step size has shrunk
   (`step · temp_frac`, placement.py:909) and the accept ratio is low, so pairs
   often stay a fraction of a millimetre too close — exactly the tight placements
   the buffer term exists to prevent.

2. **General slack.** Ratsnest length, bbox area, and edge distance are all
   smooth-ish functions of position that SA leaves a few percent above their
   local optimum, because it stops jittering before it has finished sliding parts
   into their nearest minimum.

The user's idea — **steepest descent (or similar) after annealing** — is the
classic *hybrid* recipe: **anneal to explore globally, then descend to exploit
locally.** It is a natural fit here because:

- The placement energy is mostly differentiable (see *Differentiability*, below),
  and where it is not (overlap/containment), a finite-difference gradient still
  recovers the meaningful descent direction.
- The existing **incremental energy cache** (`_move_delta`, placement.py:757;
  `_cached_energy`, placement.py:620) already makes a single footprint's energy
  change cheap to evaluate and revert — exactly the primitive a finite-difference
  gradient needs. We get the descent loop almost for free off machinery that is
  already there and already tested.

The idea makes sense and is practical. This plan specifies it.

---

## Design overview

Add an **optional, monotone, post-anneal polish stage** to placement:

> After the SA loop converges to its best placement, run a **normalised steepest
> descent with backtracking line search** over the movable units. The gradient is
> estimated by **central finite differences** reusing the incremental energy
> cache; each step is **only committed if it strictly lowers the true energy**, so
> the polish can never worsen the annealed result.

Five deliberate scoping decisions:

1. **Translations only; angles held fixed.** The descent variables are each
   movable unit's `(x, y)`. Rotation is left to SA: in `ortho` mode the angle is
   discrete (no gradient), and even in `free` mode the overlap-area dependence on
   angle is noisy and rarely the bottleneck. (A discrete rotation-retry sweep is
   noted as a future extension, not built here.)

2. **Operates on move *units*, not raw footprints.** Reuse `_move_units`
   (placement.py — ungrouped singles + KiCad groups). A group translates as a
   rigid body, exactly as in SA's translate move. Locked footprints are not in
   `movable`/`_move_units`, so they are untouched — preserving the lock invariant.

3. **Monotone by construction.** Every committed step strictly decreases the true
   `_cached_energy()`. Therefore `best_energy_after_polish ≤ best_energy_from_SA`
   always — the existing invariant
   `test_place_never_worsens_energy_and_respects_locks` is upheld, and best-of-N
   ranking stays meaningful.

4. **Coordinate (per-unit) descent, not a single global gradient step.** For each
   unit we compute *its own* 2-D gradient and line-search *its own* move. This is
   cheap (only the unit's incident energy terms recompute via `_move_delta`),
   robust to the heterogeneous term scales, and trivially monotone. The global
   "move everything along the full gradient at once" variant is discussed under
   *Alternatives*; it is a possible later upgrade but not the first cut.

5. **Separate budget; off by default.** Polish has its own iteration/tolerance
   budget so it never eats the SA time budget. It is opt-in via `--place-polish`.

### Where it runs

Inside `_Placer.run()` (placement.py:916), **after** the SA loop restores the best
placement and rebuilds the cache (placement.py:1025–1031), **before** the
`PlaceResult` is returned:

```
SA loop … → self._restore(best) → self._rebuild_cache() → best_E = …
            → self._polish()                              ← NEW
            → recompute best_E, return PlaceResult(…)
```

Running it here (rather than only on the best-of-N winner in `place()`) means:
- each run is polished, so best-of-N compares **polished** energies — apples to
  apples and strictly better;
- `recenter()` (placement.py:1036) still applies afterwards unchanged (polish is
  translation-invariant in aggregate only per-unit, but recenter's rigid shift
  composes fine);
- the cost multiplies by `runs`, which is acceptable because polish is bounded by
  its own small budget and is monotone (early-exits when a sweep stops helping).

---

## Differentiability of the energy (why FD, and with what ε)

`_cached_energy()` (placement.py:620) sums these terms; their behaviour under a
small translation of one unit:

| Term | Smoothness | Gradient under FD |
|---|---|---|
| ratsnest (Σ L2 connection length) | C¹ except at pad coincidence (measure zero) | exact, smooth — the main driver |
| overlap (Σ intersection area of inflated boxes) | C⁰: smooth where boxes overlap, **zero where separated**, kinked at first contact | well-defined *repulsion* while overlapping; zero when apart (correct — no spurious force) |
| compact (bbox area) | piecewise-linear; gradient jumps when the extremal footprint changes | exact within a piece |
| edge (Σ distance of edge-flagged units to target side) | piecewise-linear | exact within a piece |
| containment (area outside kept outline) | C⁰, kinked at the outline boundary | well-defined where protruding |
| congestion (Σ field(centroid)) | depends on field interpolation; typically C⁰/C¹ | usable |
| spread (Σ cell-count²) | **piecewise-constant** in grid cells → gradient ≈ 0 a.e., noisy at cell edges | mostly zero; harmless under line search (see below) |

**Why finite differences rather than analytic gradients:** the overlap and
containment terms are Shapely polygon-clipping areas — differentiating through
them analytically is fiddly and brittle. Central FD,
`g ≈ (E(+ε) − E(−ε)) / 2ε`, sidesteps that, captures the kinked/repulsive terms
correctly, and **reuses `_move_delta` verbatim**.

**Why noise is safe:** we never trust the gradient blindly — the line search
evaluates the *true* energy at the trial point and commits only if it improves.
A noisy or zero gradient (e.g. from the spread term) at worst wastes a few
evaluations; it can never push the placement uphill.

**ε choice:** default `polish_eps = 0.05 mm` — small relative to the buffer
(default 0.5 mm) and pad pitches so the FD stays local, large enough to step
across Shapely's numerical noise floor. Exposed as `--place-polish-eps`.

---

## Algorithm (per the recommended coordinate-descent variant)

For each unit `u` with boxed-indices `idxs`:

1. **Estimate gradient** by central differences on the unit's translation:
   `gx = (E(+εx) − E(−εx)) / 2ε`, `gy = (E(+εy) − E(−εy)) / 2ε`.
   Each `E(·)` is a *perturb → `_move_delta(idxs)` → read `_cached_energy()` →
   revert* round-trip (4 evals/unit/sweep).
2. **Descend along the normalised direction** `d = −g / |g|` (unit vector in mm).
   Normalising decouples the step from the wildly different term scales
   (`overlap_weight=20`/mm² vs ratsnest ~mm), keeping the trial distances in
   honest millimetres.
3. **Backtracking line search:** try distances `s, s/2, s/4, …` down to
   `polish_min_step`, commit the first that strictly lowers the true energy
   (Armijo-lite). `s` starts at `polish_step` (default = buffer ≈ 0.5 mm).
4. **Sweep** over all units; repeat for up to `polish_iters` sweeps. **Stop early**
   when a whole sweep improves total energy by less than
   `polish_tol · max(1, |E|)` (default `polish_tol = 1e-3`), or when `cancel` is
   set, or a `polish_time` budget elapses.

Monotonicity: only strictly-improving steps are committed, so `E` is
non-increasing across the whole stage.

### Illustrative code (grounded in existing method names)

First, a small refactor to remove duplication — factor the inline cache
save/restore in `run()` (placement.py:977–1008) into reusable helpers, then the
polish reuses them:

```python
# placement.py — new helpers on _Placer

def _save_cache(self, idxs, touched_conns):
    """Snapshot the cache entries a move over `idxs` can disturb (cf. run())."""
    sp = self.p.spread_weight > 0 and self._cell_size_x > 0
    return (self._rats, self._overlap, self._bbox, self._edge, self._containment,
            {i: (self._boxes[i], self._bounds[i]) for i in idxs},
            {ci: self._conn_len[ci] for ci in touched_conns},
            self._spread,
            {i: self._fp_cell[i] for i in idxs} if sp else {},
            self._cell_counts.copy() if sp else {})

def _load_cache(self, save, sp):
    (self._rats, self._overlap, self._bbox, self._edge, self._containment,
     boxes, lens, self._spread, fp_cells, cc) = save
    for i, (b, bnd) in boxes.items():
        self._boxes[i] = b; self._bounds[i] = bnd
    for ci, ln in lens.items():
        self._conn_len[ci] = ln
    if sp:
        for i, cell in fp_cells.items():
            self._fp_cell[i] = cell
        self._cell_counts = cc

def _energy_after_translate(self, unit, idxs, dx, dy):
    """Energy if `unit` were shifted by (dx,dy); poses + cache left UNCHANGED."""
    snap = self._snapshot(unit)
    touched = {ci for i in idxs for ci in self._fp_conns.get(i, ())}
    save = self._save_cache(idxs, touched)
    sp = self.p.spread_weight > 0 and self._cell_size_x > 0
    for fp in unit:
        fp.x += dx; fp.y += dy; fp.sync_pads()
    self._move_delta(idxs)
    E = self._cached_energy()
    self._restore(snap)
    self._load_cache(save, sp)
    return E

def _commit_translate(self, unit, idxs, dx, dy):
    """Apply a shift permanently and return the new energy."""
    for fp in unit:
        fp.x += dx; fp.y += dy; fp.sync_pads()
    self._move_delta(idxs)
    return self._cached_energy()
```

(`run()` is then rewritten to call `_save_cache`/`_load_cache`, so the
save/restore logic lives in one place.)

```python
def _polish(self, on_progress=None, cancel=None):
    """Steepest-descent polish after SA. Returns (sweeps_done, improvement)."""
    if not self.p.polish or not self._move_units:
        return 0, 0.0
    eps, t0 = self.p.polish_eps, time.time()
    units = [(u, {self._idx_of_fp[id(fp)] for fp in u}) for u in self._move_units]
    E = E0 = self._cached_energy()
    sweeps = 0
    for _ in range(self.p.polish_iters):
        if cancel is not None and cancel.is_set():
            break
        if self.p.polish_time is not None and time.time() - t0 >= self.p.polish_time:
            break
        improved = 0.0
        for unit, idxs in units:
            gx = (self._energy_after_translate(unit, idxs, eps, 0.0)
                  - self._energy_after_translate(unit, idxs, -eps, 0.0)) / (2 * eps)
            gy = (self._energy_after_translate(unit, idxs, 0.0, eps)
                  - self._energy_after_translate(unit, idxs, 0.0, -eps)) / (2 * eps)
            g = math.hypot(gx, gy)
            if g < 1e-9:
                continue
            dx, dy = -gx / g, -gy / g            # unit descent direction
            step = self.p.polish_step
            while step >= self.p.polish_min_step:
                if self._energy_after_translate(unit, idxs, dx*step, dy*step) < E - 1e-12:
                    E = self._commit_translate(unit, idxs, dx*step, dy*step)
                    improved += (E0 - E)         # bookkeeping only
                    break
                step *= 0.5
        sweeps += 1
        if on_progress is not None:
            on_progress(sweeps, self.p.polish_iters, E)
        # stop when a whole sweep barely helps
        if improved <= self.p.polish_tol * max(1.0, abs(E)) and sweeps > 1:
            break
    return sweeps, E0 - E
```

*(Note: the per-sweep stop test above is illustrative; the implementation will
track per-sweep delta rather than cumulative `improved`. Detail to settle in
code review.)*

---

## Parameters

Add to `PlaceParams` (placement.py:246), defaults chosen to be safe no-ops unless
enabled:

```python
polish: bool = False            # run steepest-descent polish after annealing
polish_iters: int = 20          # max descent sweeps over all units
polish_time: float | None = None  # optional wall-clock cap for the polish stage
polish_eps: float = 0.05        # finite-difference step (mm) for the gradient
polish_step: float = 0.5        # initial line-search distance (mm)
polish_min_step: float = 0.01   # smallest line-search distance before giving up
polish_tol: float = 1e-3        # relative per-sweep improvement to stop early
```

Add to `PlaceResult` (placement.py:281) for reporting/testing:

```python
polish_sweeps: int = 0          # descent sweeps actually run
polish_improvement: float = 0.0 # energy reduction from polish (≥ 0)
```

`run()` sets these on the returned result; `place()` (placement.py:1079) passes
them through unchanged (it already returns the inner `PlaceResult`).

---

## CLI integration

In `build_parser` (autoroute.py, near the other `--place-*` flags ~line 1869):

```
--place-polish                 enable steepest-descent polish after annealing
--place-polish-iters N         max descent sweeps (default 20)
--place-polish-time S          wall-clock cap for the polish stage (optional)
--place-polish-eps MM          finite-difference step for the gradient (default 0.05)
```

Keep `--place-polish-step` / `--place-polish-tol` as advanced knobs only if
review wants them; the four above are enough for users.

Wire them in `_place_params_from_args` (autoroute.py:506, the `PlaceParams(...)`
construction at line 533):

```python
polish=getattr(args, "place_polish", False),
polish_iters=args.place_polish_iters,
polish_time=args.place_polish_time,
polish_eps=args.place_polish_eps,
```

Validation in the arg-check block (autoroute.py ~2131): `--place-polish-iters >= 0`,
`--place-polish-eps > 0`, and a note that the polish flags require `--place`/`--place-only`
(mirroring the existing `--place-iters` guard at line 2146).

Reporting: extend the place metrics summary (autoroute.py ~476 / `_report_place`
~1444) with a line like
`place polish   {sweeps} sweeps, ΔE −{improvement:.2f}` when polish ran.

`pipeline.py` needs no signature change — it threads a `PlaceParams` through
(`run_placement`, pipeline.py:387), so the new fields ride along automatically.

---

## What about routing?

**Short answer: gradient/steepest descent does not transfer to routing, and that's
expected — but there is a worthwhile *different* refinement, kept out of scope
here.**

Routing (`router.py` A* maze routing + `anneal.py` rip-up/reroute SA) lives on a
**discrete grid** (`grid.py`): a route is a lattice path, and the "energy"
(`anneal._energy`, anneal.py:75 — length + bends + vias + crossings) is a function
of *combinatorial* choices, not continuous coordinates. There is no continuous
variable to take a derivative with respect to, so steepest descent has nothing to
act on. The discrete analogue of "local descent" is already present: A*'s bend/via
penalties bias toward clean paths, and the annealer's rip-up/reroute *is* the
local-improvement operator.

The genuinely useful routing analogue would be a **deterministic post-route
geometric cleanup** — *off-grid* path tightening (pull segments taut against
clearance, remove staircase jogs, drop redundant vias). That is push-and-shove /
shortest-path-homotopy territory: combinatorial and geometric, not gradient-based,
and a substantial feature in its own right. **Recommendation:** note it in
[`feature-suggestions.md`](feature-suggestions.md) as a separate future item; do
not bundle it with this placement work.

---

## Alternatives considered

- **Global steepest descent (one full-gradient step over all units, with a global
  line search).** Better at escaping coordinate-descent stalls where two parts
  must move *together* (e.g. a symmetric overlapping pair). But: a full gradient
  costs `4·N` energy evals per step, the heterogeneous term scales make a single
  global step size finicky, and reverting a global move is costlier. Coordinate
  descent is cheaper, robustly monotone, and directly targets the stated goal
  (relaxing pairwise close contacts, which it does as soon as it visits either
  member). **Keep global descent as a possible `--place-polish-mode global`
  upgrade later.**

- **Smooth surrogate for overlap (replace intersection-area with a C¹ quadratic
  repulsion `(buffer − gap)²`).** Enables true analytic gradients and smoother
  descent, but changes the *objective* SA already optimises (two energy
  definitions to keep consistent) and is a bigger change. FD on the existing
  energy avoids that entirely. Revisit only if FD descent proves too noisy in
  practice.

- **Polish only the best-of-N winner** (in `place()` rather than per-run in
  `run()`). Cheaper, but then best-of-N ranks *un*-polished placements and a
  polish-friendly runner-up can be missed. Per-run polish is the cleaner
  semantics; its cost is bounded by the early-exit.

---

## Phases

Each phase ships independently with its docs + `CHANGES.md` entry; the version
bump to **0.45.0** lands with Phase 1 (first user-visible behaviour).

### Phase 1 — core polish stage (no CLI yet)
- Refactor `run()`'s inline cache save/restore into `_save_cache` / `_load_cache`.
- Add `_energy_after_translate`, `_commit_translate`, `_polish`.
- Add the `PlaceParams` polish fields and `PlaceResult` polish metrics.
- Call `_polish()` in `run()` after `_restore(best)` / `_rebuild_cache()`; recompute
  `best_E` and populate the result metrics.
- **Tests** (tests/test_placement.py):
  - `test_polish_never_worsens_energy` — with `polish=True`, returned
    `best_energy ≤` the SA-only result on the same seed, and
    `polish_improvement ≥ 0`.
  - `test_polish_relaxes_close_contact` — two footprints seeded just inside the
    buffer (overlapping inflated boxes); run with `iters` tiny + `polish=True`
    and assert the final overlap term drops (or the gap reaches `buffer`).
  - `test_polish_respects_locks` — a locked footprint's pose is byte-for-byte
    unchanged after polish.
  - `test_polish_respects_groups` — a KiCad group translates rigidly (internal
    offsets preserved) and never rotates during polish.
  - `test_polish_deterministic` — same seed ⇒ identical result (polish itself is
    deterministic; no RNG).
  - `test_polish_disabled_is_noop` — `polish=False` reproduces the current result
    exactly (guards against regressions from the cache refactor).

### Phase 2 — CLI + reporting
- Add `--place-polish[-iters|-time|-eps]`, validation, and the place-metrics line.
- `_place_params_from_args` wiring.
- **Tests** (tests/test_cli.py or equivalent): flag parsing, validation errors,
  and that `--place-only --place-polish` lowers reported energy on a fixture board.

### Phase 3 — docs + version (per `CLAUDE.md`, part of "done")
- **README.md**: new `--place-polish*` options under the placement section; a
  one-line "what it does / when to use it" and the monotonicity guarantee.
- **docs/architecture.md**: add the polish stage to the placement data-flow
  (SA → restore best → **polish** → recenter), document the FD-gradient approach
  and the **invariant: polish is monotone and translation-only** (so it never
  worsens energy and never breaks locks/orientations).
- **API docs**: docstrings on the new methods/params (Google style), then
  `pdoc -d google --mermaid pyautoroute -o docs/api`.
- **pyproject.toml**: bump to `0.45.0`; **CHANGES.md**: newest-first entry.

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Cache refactor introduces a subtle revert bug | `test_polish_disabled_is_noop` + existing SA tests pin behaviour; refactor is mechanical (move existing lines into helpers). |
| FD gradient noisy near Shapely kinks → wasted evals | Line search on the *true* energy makes wasted evals harmless (never uphill); ε tuned above the noise floor. |
| Coordinate descent stalls on coupled pairs | Acceptable for v1 (goal is pairwise relaxation, which it achieves); `global` mode noted as a later upgrade. |
| Per-run polish multiplies cost by `runs` | Bounded by `polish_iters`/`polish_time` and early-exit; off by default. |
| Spread/congestion terms add gradient noise | Line-search safety as above; both are typically disabled (weight 0) anyway. |

---

## Acceptance criteria

- `--place-polish` lowers (or equals) the reported placement energy on every test
  fixture vs the same run without it — never worsens.
- On a board with deliberately tight seeding, polish increases the minimum
  inter-footprint gap toward `buffer` (close contacts relaxed).
- Locks and group rigidity preserved; orientations unchanged.
- All existing placement/router/e2e tests still pass; new tests added per phase.
- README, architecture.md, API docs, version, and CHANGES.md all updated.
