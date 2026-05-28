# Finding good settings — the tuning tool and `--auto`

PyAutoRoute's results depend on a handful of knobs (grid pitch, via weight,
annealing schedule and budget). Simulated annealing is stochastic, and the knobs
trade completion against wirelength, vias, and runtime. `pyautoroute.tune` makes
that trade-off measurable: it scores a routing with a single objective and sweeps
the critical parameters over one or more boards to find the best setting. The same
machinery powers the opt-in `--auto` probe.

This document is both the **design proposal** and the documentation for what is
implemented today; the "Future work" section lists what is deliberately left out
of v1.

## The objective

A single scalar per routing, **lower is better**:

```
score = unrouted_weight·unrouted + length + via_weight·vias + time_weight·runtime
```

This is the annealer's own energy (completion first, then wirelength, then vias)
plus an optional small runtime tiebreaker so that, among settings of equal
quality, the fastest wins. Because the router is **DRC-clean by construction**,
correctness is *not* part of the score — only completion / length / vias / time.
`tune.score(metrics, …)` computes it; the weights default to the routing
defaults (`unrouted_weight=100`, `via_weight=2`, `time_weight=0`).

## Method

- **Search space.** `tune.Config` is one point: `grid_mult` (pitch as a multiple
  of the rules-derived pitch), `via_weight`, `unrouted_weight`, `temps`, and a
  budget (`iters` or `time_budget`). `tune.default_grid()` is a small coarse grid —
  grid multipliers `{0.75, 1.0, 1.5}` × via weights `{1, 2, 4}` — kept small so a
  sweep is tractable. Random search or coarse-to-fine refinement around the best
  point are easy extensions (see Future work).
- **Stochasticity.** Each config is evaluated over **several seeds** and scored by
  the **median** (`sweep(..., seeds=(0,1,2))`), so a lucky seed doesn't win. This
  is the single most important methodological point — without it the "best" setting
  is noise.
- **Per board.** `sweep_board(path, pro, configs)` loads a board once and reuses
  the parsed board and one grid per pitch across configs, only varying the cheap
  knobs. Sweep each `TestProjects/Test{1..5}` independently (they differ in
  size/density, 8–138 pads) and record the per-board winner.
- **Aggregate default.** To pick a single global default, normalise each board's
  scores to its own best and choose the config minimising the aggregate normalised
  score — a robust default — while still reporting per-board winners. (v1 reports
  per-board; aggregation is a thin layer on top.)

## The tool

```bash
pyautoroute-tune BOARD.kicad_pcb [BOARD2 …] [--time S] [--seeds N]
# or: python -m pyautoroute.tune …
```

For each board it runs the default grid (annealing `--time` seconds per config,
`--seeds` seeds each), prints a markdown table of the top configs and the
recommended `grid_mult` / `via_weight`. It is a developer/research tool — slow by
nature — so it is **not** part of the default `pytest` run and is surfaced from the
`pyautoroute.sh` menu.

API (all importable): `score`, `Config`, `evaluate`, `sweep`, `sweep_board`,
`best_config`, `default_grid`.

## `--auto` (online, opt-in)

A fast, end-user version that picks settings for the board in front of it:

1. `--auto` runs a **quick reduced probe** — the default grid with a small
   `--auto-probe-time` budget per setting (default 3 s, seed = `--seed`) — on the
   board *as it will be routed* (after `--place`, if used).
2. It prints the chosen `--grid` / `--via-weight` and the probe's metrics, then —
   on a terminal — **asks you to confirm**. `--auto-yes` (or a non-interactive
   stdin) applies them without prompting. On confirm it routes with those settings;
   declining keeps whatever you passed.

`--auto` is heuristic: the probe is short and ignores `--exclude-net`, so treat its
choice as a strong starting point, not a guarantee. Combine with `--write-config`
to capture the chosen settings for repeatable runs.

## Future work

- **Aggregate global defaults** baked into the package (a lookup keyed by board
  characteristics such as pad count / density), so `--auto` can pick a strong start
  *without* probing on large boards.
- **Smarter search** than the coarse grid: random search, coarse-to-fine, or
  Bayesian optimisation over a wider space (schedule, budget, placement weights).
- **Placement parameters** in the sweep (`--place-buffer`, overlap/compact weights,
  `--place-temps`) for `--place` runs.
- **Parallel evaluation** across configs/seeds (they are independent).
- **CSV + plots** output (score vs grid pitch, etc.) behind the `[viz]` extra.
