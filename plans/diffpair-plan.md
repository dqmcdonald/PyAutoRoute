# Differential Pair Routing Plan — v0.38.0

> **Status: shipped** (PR #39, 2026-06-02). See `CHANGES.md` for the summary
> and `plans/feature-suggestions.md` item #2 for the original proposal.

## Problem

High-speed signals (USB, LVDS, HDMI, Ethernet) require two complementary traces
that must be routed together: constant spacing, equal length (zero skew), and
preferably on the same layer. PyAutoRoute had no support at any level — diff-pair
width/gap were not parsed in `rules.py`, and `netlist.py` had no awareness of
`+`/`-` net-name suffixes.

## Design

### Pair detection (`netlist.py`)

`find_diff_pairs(board)` iterates all net names and checks each against the
suffix pairs `(+,-)`, `(_P,_N)`, `(P,N)` and their lowercase variants. If the
companion net exists, a `DiffPairSpec(net_p, net_n)` is emitted. Each pair is
returned exactly once.

`build_diff_pair_connections(board, pairs)` matches pads between the two nets —
preferring same-footprint pairs, falling back to globally nearest — and builds
`DiffPairConnection` objects (source p/n pads → destination p/n pads). Multi-pad
diff pairs are reduced via MST over matched-pair midpoints.

### Coupled A* (`diffpair.py`)

`route_diff_pair(state, dp_conn, dp_gap, params)` runs a standard A* over the
**+ trace position** with one extra constraint: the companion **− trace** (at a
fixed grid-node offset `(dc_off, dr_off)`) must also be free at every expansion.

Key insight: because the offset is constant throughout the route, the **− path
is derived in O(n) from the + path** after the search — the state space is
identical in size to a single-net A* search. Length matching is exact by
construction (both traces advance one step per A* move).

The offset is computed from the source and destination pad positions, averaged
so the route works even when the pad layouts differ slightly between the two
endpoints.

### Grid integration — bake strategy

After the pre-routing pass, `bake_routing_state(state, grid)` transfers committed
diff pair copper from the temporary `RoutingState` into the grid's static `owner`
array. This means every subsequent `RoutingState` instance — across parallel runs,
annealing iterations, and `run_routing()` calls — sees diff pair copper as a fixed
static obstacle. **No changes to `pipeline.py` or `anneal.py` were required.**

### Design rules (`rules.py`)

`DesignRules.dp_gap_for(net_p, net_n)` reads `differential_pair_gap` from the
net class (falling back to `pair_clearance()`).

### Stackup parsing (`pcb.py`)

`Stackup(copper_thickness, dielectric_h, epsilon_r)` is populated from the PCB
file's `(setup (stackup …))` block. Defaults (FR4, 1.6 mm, 1 oz) are used when
the block is absent.

### Reporting (`report.py`)

After routing, `diff_pair_stats()` produces a `DiffPairStats` per pair.
`format_diff_pair_table()` renders a table with length+/−, skew, vias, layer,
track width, gap, and estimated `~Zdiff`. The impedance uses the IPC-2141A
microstrip formula (`_zdiff(w, gap, h, Er, t)`) for outer-layer routes; inner-
layer / multi-layer routes show `—`.

## Files changed

| File | Role |
|------|------|
| `pyautoroute/diffpair.py` | **New.** Coupled A*, `bake_routing_state`, offset computation |
| `pyautoroute/netlist.py` | `DiffPairSpec`, `DiffPairConnection`, `find_diff_pairs`, `build_diff_pair_connections` |
| `pyautoroute/pcb.py` | `Stackup` dataclass, `_parse_stackup` |
| `pyautoroute/rules.py` | `dp_gap_for` |
| `pyautoroute/report.py` | `DiffPairStats`, `diff_pair_stats`, `_zdiff`, `format_diff_pair_table` |
| `pyautoroute/autoroute.py` | `--diff-pairs` / `--diff-pair-gap` CLI flags; pre-routing pass |
| `tests/test_diffpair.py` | **New.** 24 tests |
| `tests/test_pcb.py` | 2 stackup tests |

## Known limitations / future work

- **Annealing integration**: diff pairs are pre-routed once and baked as fixed
  obstacles. Integrating them as atomic units into the rip-up/reroute annealing
  loop would allow global optimisation across single-ended and diff pair nets.
  (Tracked in `plans/feature-suggestions.md` top-recommendations item #4.)
- **Inner-layer / stripline impedance**: `_zdiff` uses the microstrip formula only;
  routes that cross layers show `—` for impedance.
- **Length-matching meanders**: if a pair's source and destination pad layouts
  differ enough that the route's natural length is unequal, a meander pass would
  equalise them. Currently skew is guaranteed zero only when both ends have the
  same spatial offset (the common case).
