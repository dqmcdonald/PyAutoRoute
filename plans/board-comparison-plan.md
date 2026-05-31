# Plan: compare routed boards (`pyautoroute-compare`)

Status: **✅ Implemented.** `pyautoroute-compare` shipped as a console script.
Post-ship fix: `_resolve_pro` crashed with `TypeError: Path(None)` when no
`.kicad_pro` sibling existed (e.g. `Test1_routed.kicad_pcb` → looked for
`Test1_routed.kicad_pro`, not `Test1.kicad_pro`). Fixed: scan the board
directory for any `*.kicad_pro` and fall back to default rules when none found;
`load_rules` now accepts `None`.

## Goal

A command-line tool that takes **two or three routed boards** of the *same design*
— e.g. PyAutoRoute's output, a hand-routed board, and another tool's output — and
prints a **plain-text report** that scores each, breaks the scores down by metric,
and analyses routing quality so they can be compared head-to-head.

```
pyautoroute-compare ours.kicad_pcb hand.kicad_pcb othertool.kicad_pcb \
    [--pro design.kicad_pro] [--label "PyAutoRoute" --label "Hand" --label "Tool X"] \
    [--exclude-net PATTERN ...] [--via-weight 2.0] [--unrouted-weight 100.0]
```

## Key insight: most of this already exists

`report.routing_stats(board, rules)` (report.py) already does the tool-agnostic
hard part. Given **any** loaded KiCad board it:

- builds the netlist from pads (`netlist.build_connections`),
- runs **union-find over the actual copper** (segment endpoints + pad centres;
  vias are handled implicitly because the F.Cu and B.Cu segments meeting at a via
  share net + position), yielding **routed / total / unrouted connections**,
- sums **track length** and counts **vias** (`_parse_free_vias` captures *every*
  top-level `(via …)`, so external boards count correctly), and
- runs the **DRC self-check** (`geometry.clearance_violations`) when rules are given.

So "is this net actually connected, and how much copper did it cost?" — the part
that's genuinely annoying to answer in a tool-neutral way — is **already solved and
works on hand/other-tool boards**. This feature is therefore mostly a **thin driver**
over existing pieces plus a report formatter, with a few small, backward-compatible
extensions to `report.py`.

Reused as-is / lightly extended:

| Piece | File | Role |
|---|---|---|
| `pcb.load_board` | pcb.py | parse any `.kicad_pcb` |
| `rules.load_rules` | rules.py | shared design rules (for DRC) |
| `report.routing_stats` | report.py | completion / length / vias / DRC per board |
| `tune.TuneMetrics` / `tune.score` | tune.py | the single headline score |
| `netlist.Connection.est_length` | netlist.py | straight-line lower bound → directness ratio |
| `pcb.zone_fill_nets` | pcb.py | the copper-pour nets to ignore |

## Resolved decisions (from review)

- **Ignore copper pours.** Pour nets (the GND/power planes) are excluded from the
  comparison entirely — completion, length, vias, and directness — so a board isn't
  penalised for doing GND/power the normal way. See *Fairness* below for the exact
  exclude set.
- **Quality metrics:** the table below (no DFM/jaggedness signals).
- **Output:** plain text only (no CSV/JSON).

---

## Metrics (per board)

| Metric | How | Why |
|---|---|---|
| **Completion** routed/total (count + %) | `routing_stats` (pour nets excluded) | Dominant: an incomplete board isn't comparable on the rest |
| **DRC violations** | `clearance_violations` vs the shared rules | A board with violations must not silently "win" — flagged prominently |
| **Wirelength** (mm) | Σ segment length, non-pour nets | Core cost |
| **Directness** = length ÷ Σ`est_length` | `routing_stats` + `Connection.est_length` | **Normalised, size-independent** quality number: 1.0 = perfectly straight; higher = more detours/congestion. The best single quality measure |
| **Vias** (count + per-connection) | non-pour vias | Manufacturing cost + density |
| **Layer balance** (length per copper layer, # layers used) | segments grouped by `.layer` | Catches "the other tool used 4 layers"; flags boards PyAutoRoute couldn't have produced (it is 2-layer) |
| **Score** | `tune.score` | Headline ranking (below) |

**Headline score** (lower is better), reusing `tune.score` with the runtime term
dropped (runtime isn't available for hand/other boards):

```
score = unrouted_weight·unrouted + length + via_weight·vias
```

`--via-weight` / `--unrouted-weight` are flags (defaults 2.0 / 100.0, matching the
router). Boards are ranked by score; **a board with DRC violations is marked
invalid and excluded from the "winner" call** (or flagged with a warning), since an
unmanufacturable board shouldn't win on length.

---

## Fairness — the parts that need care

These three things are what separate a meaningful comparison from a misleading one.

### 1. Ignore copper pours, consistently across all boards

`routing_stats`'s union-find covers segments + pads but **not zones**, so a poured
GND net would otherwise show every GND pad as "unrouted." The fix is to **exclude
the pour nets** — but the exclude set must be **identical for every board**, or the
`total` connection counts diverge and completion becomes incomparable.

> **Exclude set = union of every board's pour nets (`pcb.zone_fill_nets`) ∪ any
> `--exclude-net` patterns**, applied to all boards.

Excluded nets are dropped from connections **and** from the length / via / directness
sums (filter `board.segments` / `board.free_vias` by `.net`), so a pour net is fully
invisible to every metric. *Documented consequence:* if a net is poured on one board
but routed with tracks on another, it is excluded on **both** — that routing effort
won't show. This is the intended "ignore copper pours" behaviour and keeps the
signal-net comparison clean; note it in the report header (which nets were ignored).

### 2. Verify the boards are the same design

Per-connection metrics only compare if the boards share a netlist. Each board is
parsed independently, so add a **sanity check**: same set of (non-excluded) net
names, same pad-per-net counts, same `total` connection count. If they diverge,
**warn loudly** in the report (different revisions, or a tool dropped/merged a net)
— the numbers below it are then suspect. Use board #1 as the reference.

### 3. One shared rule set for DRC

DRC needs design rules. All boards are the same design, so take **one `--pro`**
(auto-detected from board #1's sibling `.kicad_pro` if omitted; KiCad defaults if
none found) and measure **every** board against it — DRC then reflects the design's
intent regardless of which tool emitted the board. `clearance_violations` re-derives
copper per layer from each board, so it works on any of them.

---

## Small extensions to `report.py`

Keep the logic in `report.py` (its home) rather than duplicating union-find in the
new module. Backward-compatible:

- `routing_stats(board, rules=None, exclude=None)` — new optional `exclude` list,
  passed to `build_connections`, **and** used to filter `board.segments` (length)
  and `board.free_vias` (via count) by net. `exclude=None` (the default) preserves
  today's behaviour exactly (the GUI/CLI initial-stats callers are unaffected).
- `RoutingStats` gains **`ideal_length: float`** (Σ `est_length` over the
  non-excluded connections) so directness = `length / ideal_length` is available
  without rebuilding the netlist. Optionally `length_by_layer: dict[str, float]`,
  or compute the layer breakdown in `compare.py` from `board.segments` (presentation
  detail — either is fine).

---

## The new module — `pyautoroute/compare.py`

```python
def compare(paths, *, pro=None, labels=None, exclude=(),
            via_weight=2.0, unrouted_weight=100.0) -> str:
    """Load 2–3 boards, score each, return the plain-text report."""
    boards = [pcb.load_board(p) for p in paths]
    rules  = rules_mod.load_rules(_resolve_pro(pro, paths[0]))
    pour   = set().union(*(pcb.zone_fill_nets(b) for b in boards))
    excl   = sorted(pour) + list(exclude)            # union, applied to all
    stats  = [report.routing_stats(b, rules, exclude=excl) for b in boards]
    _check_same_design(stats, boards)                 # warn on divergence
    return _format_report(labels or _auto_labels(paths), stats, excl,
                          via_weight, unrouted_weight)
```

- `main(argv=None)` — argparse front end (the positional board paths, `--pro`,
  repeatable `--label`, repeatable `--exclude-net`, `--via-weight`,
  `--unrouted-weight`), prints `compare(...)`. Entry point
  `pyautoroute-compare = "pyautoroute.compare:main"` in `[project.scripts]`.
- `_auto_labels` — base filename per board unless `--label` given (matched by order).
- `_format_report` — see below.

## Report layout (plain text)

```
PyAutoRoute board comparison
  design:  design.kicad_pro   (3 boards, 142 connections)
  ignored: GND, +3V3  (copper-pour nets)
  ⚠ Tool X has 138 connections (expected 142) — different netlist?

                         PyAutoRoute      Hand        Tool X
  completion             142/142 100%   142/142 100%  138/142  97%
  DRC violations              0  clean       0 clean       2  ✗
  wirelength (mm)         1843.2          1620.5        1701.0
  directness (×ideal)        1.31           1.16          1.22
  vias                          47             18            29
    per connection            0.33           0.13          0.20
  layers used                    2              2             2
    F.Cu / B.Cu (mm)   1102 / 741      980 / 640     900 / 801
  score                     2030.2         1656.5     1759.0*

  ranking:  1. Hand  2. Tool X*  3. PyAutoRoute
  (* Tool X has DRC violations — excluded from the winner call)

  analysis:
  - Hand routing is the most direct (1.16× ideal) and via-frugal (18), and
    completes every net cleanly — the benchmark.
  - PyAutoRoute completes every net but spends 14% more wirelength and ~2.6×
    the vias vs hand; the high via count is the main quality gap.
  - Tool X is denser than PyAutoRoute but leaves 4 nets open and 2 DRC
    violations, so it is not directly comparable on length.
```

- Per-metric **best value highlighted**; the narrative is generated from simple
  comparisons (best/worst per metric, deltas vs the best). Keep the prose
  rule-based and short.
- Header always states **which nets were ignored** and any **same-design warning**.
- 2-board mode drops the third column and the ranking degrades to a head-to-head
  delta.

---

## Files & tests

**Code**
- `pyautoroute/compare.py` (new) — `compare()`, `main()`, `_format_report`,
  `_check_same_design`, `_auto_labels`, `_resolve_pro`.
- `report.py` — `routing_stats(..., exclude=None)` (filter connections + segments +
  vias); `RoutingStats.ideal_length` (and optional `length_by_layer`).
- `pyproject.toml` — `pyautoroute-compare` console script.

**Tests** (all headless; the test boards in `TestProjects/` are real KiCad boards)
- `report.routing_stats` with `exclude`: excluding a pour net drops its connections,
  segment length, and vias; `exclude=None` is byte-for-byte the current behaviour
  (guards the GUI/CLI callers); `ideal_length` equals Σ `est_length`.
- `compare()` on two copies of the same routed board → identical metrics, zero
  deltas, no same-design warning.
- `compare()` on a board vs a deliberately-worse variant (e.g. one with an extra
  detour / extra via) → the worse board scores higher and ranks lower.
- Same-design check fires a warning when net sets differ.
- DRC-dirty board is flagged and excluded from the winner call.
- Pour-net handling: a board with a GND pour reports GND as ignored, not unrouted.
- `_format_report` renders 2- and 3-board cases without error (snapshot-ish
  assertions on key lines).

**Docs / versioning**
- `README.md` — a short "Comparing boards" section with the command and a sample
  report.
- `docs/architecture.md` — `compare.py`'s role and the `routing_stats` `exclude`
  extension; note it reuses the connectivity union-find.
- `CHANGES.md` + minor version bump.

---

## Phasing

1. **`report.py` extension** — `exclude` + `ideal_length`, with tests (fully
   independent, useful on its own).
2. **`compare.py` core** — `compare()` returning metrics for 2–3 boards + the
   same-design / pour-net handling + score, with tests. No formatting yet (return a
   small results dataclass).
3. **Report formatter + CLI** — `_format_report`, `main()`, the console script,
   docs.

## Risks & open questions

- **Pour net asymmetry** (documented above): a net poured on one board but tracked
  on another is excluded everywhere. Accepted as the meaning of "ignore copper
  pours"; surfaced in the report header so it's never silent.
- **Endpoint snapping.** `routing_stats` snaps endpoints to a 0.01 mm grid for the
  union-find. A tool that ends tracks a hair off the pad centre (sub-0.01 mm) could
  read as unrouted; if a real board trips this, widen `_SNAP_MM` or snap to nearest
  pad. Worth a sanity pass on the first real other-tool board.
- **Vias not on signal nets.** Stitching vias on a pour net are excluded with the
  pour; fan-out/micro-vias on signal nets count normally. Fine.
- **Multi-layer boards.** Length-per-layer handles N layers, but PyAutoRoute is
  2-layer; the report notes layer count so a 4-layer competitor is contextualised
  rather than naively out-scored on length.
- **Same-design enforcement.** Warn, don't hard-fail — the user may *want* to compare
  near-identical revisions; the warning makes the caveat explicit.
- **DRC rule source.** All boards scored against one `--pro`. If a competitor used
  different clearances, DRC still measures against this design's rules (the intent),
  which is the fair basis; documented.
