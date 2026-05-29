# Feature suggestions

An analysis of the current codebase (v0.24.0) and a set of proposed new
features, grounded in what PyAutoRoute does today and the limitations it
already documents. Ordered roughly by value-to-effort. This is a planning
document, not a commitment — items here are candidates, not scheduled work.

## Context

PyAutoRoute is a 2-layer KiCad autorouter with a
parse → grid → A\* route → simulated-annealing pipeline (`pcb.py`, `grid.py`,
`router.py`, `anneal.py`), an experimental footprint-placement pass
(`placement.py`), tuning tools (`tune.py`), and a work-in-progress Tkinter GUI
(`gui/`). Routing is **DRC-clean by construction** (see
[`architecture.md`](architecture.md)). The suggestions below build on that
existing machinery rather than replacing it.

## High-value features

### 1. Bounded / windowed A\* search

`router.astar` currently searches the *entire* board grid for every
connection. This is the dominant runtime cost on large boards, and the README
already flags it as intended future work ("A\* is unbounded (future: search
bounding box). Runtime dominated by long/awkward nets.").

Proposal: constrain each connection's search to a bounding box around its
source and target (plus an adjustable padding margin), expanding the box and
retrying on failure. This is the single best performance win and fits the
existing grid model directly.

### 2. Differential pair routing

A common real-world need with no current support. KiCad encodes diff pairs by
net-name suffix (`_P` / `_N`) and net-class `diff_pair_width` /
`diff_pair_gap`, and the net→class resolver in `rules.py` already parses net
classes. Detect pairs in `netlist.py`, route the primary, then route the
partner under a coupling constraint in the A\* cost model. High value; reuses
the existing class machinery.

### 3. Incremental / partial re-routing ("route only these nets")

Today the writer strips all free vias and reroutes everything (`pcb.py`). A
`--keep-existing` / `--nets PATTERN` mode would lock already-routed segments as
obstacles and route only the named/unrouted nets, making the tool usable
iteratively on a partly hand-routed board. The grid already supports static
obstacles, so existing copper simply becomes occupancy — a major workflow
improvement at moderate cost.

### 4. Per-net-class clearance masks

`architecture.md` and README limitation #6 note the grid uses a *single global*
`margin = max(track/clearance)` across all classes, which over-reserves space
on mixed boards. Per-class inflation (or per-layer masks) would let tight
signal nets route where a global power-class clearance currently blocks them.
A documented known limitation worth closing.

## Medium-value features

### 5. Length tuning / matching

Add a `target_length` term to the A\* cost, or an annealing energy term for
length-matched groups (clocks, buses, diff pairs). The annealer already
minimizes wirelength, so a per-group length-match penalty is a natural
extension.

### 6. Finish the GUI

`gui/` is a skeleton per [`gui-plan.md`](gui-plan.md): app / canvas / controls
/ worker / events modules exist, but the pipeline wiring is incomplete. The
matplotlib canvas (`visualize.draw_board`) and a background worker are already
in place. Completing live progress display and a route/place button would
broaden the audience considerably.

### 7. Exact custom-pad polygons

README limitation #3: custom pads are approximated by their bounding box in
`geometry.py`. Shapely ≥ 2.0 (already a dependency) can build the true polygon
from the pad's primitives, tightening clearance on boards with non-rectangular
pads.

### 8. Explicit hole-to-hole checking

README limitation #7: hole-to-hole spacing is approximated via copper
clearance, not checked. Since `geometry.clearance_violations` already runs an
STRtree self-check, adding a drill-to-drill STRtree pass (using `hole_to_hole`
from `rules.py`) closes a real DRC gap in a small, well-bounded addition.

### 9. Routing report / DRC summary output

`report.py` already computes `RoutingStats` (routed count, length, vias,
violations). Surfacing this as a `--report FILE` (JSON / markdown) — completion
%, per-net status, unrouted list — would help users and CI, at low effort given
`report.py` exists.

## Lower-value / polish

- **Early termination / stall detection** in both annealers
  ([`performance_analysis.md`](performance_analysis.md) notes neither stops
  early); a "no improvement in N iterations" cutoff saves time on the `--time`
  budget.
- **Pad entry from any side / teardrops** — improve `path_to_nodes` stub
  generation.
- **Gerber / CSV export hook** via `kicad-cli` (already invoked for zone refill
  in `pcb.try_refill_zones`).
- **Fanout for BGA / dense parts** at the placement/route boundary.

## Top recommendations

If implementation effort is to be prioritized:

1. **Bounded A\* search** — biggest performance win, already flagged as intended.
2. **Partial re-routing (`--nets` / `--keep-existing`)** — biggest workflow win,
   low architectural risk.
3. **Differential pairs** — biggest "real PCB" capability gap.
