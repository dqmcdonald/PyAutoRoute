# PyAutoRoute

Autoroute a **2-layer KiCad PCB** in pure Python. Give it a board with placed
footprints and assigned nets but no (or partial) tracks, and it writes a *copy*
with candidate routing — DRC-clean by construction.

It parses the `.kicad_pcb` s-expression directly, so it does **not** need
`pcbnew` and runs in any normal Python environment.

## What it optimises

In priority order:

1. **No DRC violations** — clearance to other tracks, pads, vias and the board edge is honoured by construction.
2. **Minimise total track length.**
3. **Minimise vias** (used only where a layer change pays off).
4. **Prefer the front layer (F.Cu).**
5. **Prefer 45° tracks over 90°.**

Anything it cannot route is **left unrouted and reported** — never drawn as a short.

## Requirements

- Python ≥ 3.10 with **numpy**, **scipy**, **shapely ≥ 2.0**.
- **matplotlib** — optional, only for `--debug-plot`.
- **KiCad** — optional, only if you want to run `kicad-cli pcb drc` to independently verify the output. Not needed to route.

## Install

```bash
pip install -e .            # from the repo root
```

This installs the dependencies and a `pyautoroute` command. You can also skip the
install and run it as a module with any interpreter that has the dependencies:

```bash
python -m pyautoroute.autoroute board.kicad_pcb
```

### Optional fast A* (native extension)

The A* maze router has an optional Cython core that runs the inner search loop in
native code for a roughly 5-20x speedup on larger grids (it produces bit-for-bit
identical routes). It is entirely optional: without it the router uses its
optimised pure-Python search and nothing else changes.

To build it, install the `fast` extras (Cython + numpy) and compile the
extension in place:

```bash
pip install -e ".[fast]"
python setup.py build_ext --inplace
```

Check whether the native core is active:

```python
import pyautoroute
print(pyautoroute.HAS_C_ASTAR)   # True once the extension is built
```

If Cython is unavailable at build time the extension is simply skipped and the
package installs as pure Python.

## Usage

```bash
pyautoroute INPUT.kicad_pcb [options]
```

The original file is never modified — a routed copy is written alongside it.

| Option | Meaning |
|---|---|
| `--pro PROJECT.kicad_pro` | Project file with the design rules (default: the sibling `.kicad_pro`). |
| `-o, --output FILE` | Output path. Default is named for the run: `INPUT_routed` (route), `INPUT_placed_routed` (`--place`), or `INPUT_placed` (`--place-only`). |
| `--place-only` | Place the footprints (see `--place`) and write `INPUT_placed.kicad_pcb` **without routing**. |
| `--grid MM` | Routing grid pitch in mm (default: derived from the rules, ≈ `track/2 + clearance`). Finer = better coverage but slower. A pitch more than ~2× the derived one prints a warning: a coarse grid can't fit a node in the gap beside a pad and so forces vias where a finer grid would route on one layer. |
| `--iters N` | Run simulated-annealing optimisation for N iterations. |
| `--time SECONDS` | Run optimisation for a wall-clock budget instead. |
| `--runs N` | Route `N` times with different annealing seeds and keep the lowest-energy result (best-of-N). Default 1. Multiplies runtime ~N×; only varies the result when annealing (`--iters`/`--time`) is on. |
| `--auto` | Probe a few grid/via settings on this board, pick the best, and (on a terminal) ask to confirm before routing with them. `--auto-yes` skips the prompt; `--auto-probe-time S` sets the budget per probed setting. |
| `--unrouted-weight W` | Annealing energy penalty per unrouted connection (default 100). Higher ⇒ the optimiser tries harder to complete every connection, at the expense of wirelength/vias; lower ⇒ it tolerates leaving hard nets for manual routing. |
| `--anneal-temps START END` | Start/end temperature of the geometric cooling schedule (default `4.0 0.05`); `START > END > 0`. Higher `START` explores more (better escape from local minima, slower convergence); lower `END` exploits harder at the finish. |
| `--exclude-net PATTERN` | Leave matching nets un-routed (repeatable; glob, e.g. `GND` or `"/PWR*"`). Their pads still act as obstacles. |
| `--via-weight W` | Via cost in mm-equivalent (higher ⇒ fewer vias). Default 2.0. |
| `--seed S` | Random seed for the optimiser. |
| `--snapshots N` | During annealing, save `N` intermediate board snapshots to a `snapshots/` subdir (beside the output), so you can watch the optimisation progress. Requires `--iters` or `--time`. |
| `--config FILE` | Read options from an INI settings file (see below). Options given on the command line override it. |
| `--write-config [FILE]` | Write the effective settings to an INI file and exit. Bare `--write-config` writes `<input>.pyautoroute.cfg` beside the board. |
| `--log [FILE]` | Write a verbose log of the input parameters and routing/annealing progress. Bare `--log` writes `<output>.log`; `--log FILE` uses the given path. |
| `--fix-values` | Move footprint **Value** text to the silkscreen layer before routing. KiCad libraries often default to `F.Fab`/`B.Fab` for Value text; this flag moves it to `F.SilkS`/`B.SilkS` so it appears on the physical silkscreen. The fix is applied in-memory and written to the output file. Off by default. Also available as a standalone `pyautoroute-fix --values` command and in the GUI's Advanced settings. |
| `--debug-plot` | Also write a `.png` render of the routed board. |
| `--quiet` | Suppress the live progress display (final summary only). |
| `--version` | Print the version and exit. (The version is also printed on startup and written to the `--log` header.) |

`--iters` and `--time` are mutually exclusive; if neither is given, the board is
routed once (greedy order) without annealing.

`--runs N` repeats the whole route + anneal with seeds `seed, seed+1, …` and keeps
the result with the lowest annealing energy (`wirelength + via_weight·vias +
unrouted_weight·unrouted`) — simulated annealing is stochastic, so the best of a
few short runs often beats one long run. (`--snapshots` needs a single run.)

### Examples

```bash
# Quick greedy route with the derived grid:
pyautoroute MyBoard.kicad_pcb

# Finer grid + a 2-minute optimisation pass + a debug image:
pyautoroute MyBoard.kicad_pcb --grid 0.2 --time 120 --debug-plot

# Route everything except power nets, to a named output:
pyautoroute MyBoard.kicad_pcb --exclude-net GND --exclude-net "/VBUS*" -o routed.kicad_pcb

# Optimise, capturing 10 progress snapshots and a verbose log:
pyautoroute MyBoard.kicad_pcb --iters 5000 --snapshots 10 --log
# -> snapshots/MyBoard_anneal_01of10.kicad_pcb ... 10of10, and MyBoard_routed.log

# Experimental: place the footprints first (30 s budget), then route:
# -> MyBoard_placed_routed.kicad_pcb
pyautoroute MyBoard.kicad_pcb --place --place-time 30 --time 60 --debug-plot

# Place only, no routing: -> MyBoard_placed.kicad_pcb
pyautoroute MyBoard.kicad_pcb --place-only --place-time 30 --debug-plot
```

### Settings file

To re-run a board with the same options without re-typing them, keep the settings
in a small INI file and pass it with `--config`:

```ini
[pyautoroute]
grid = 0.2
time_budget = 120
via_weight = 2.0
anneal_temps = 4.0, 0.05
exclude_net = GND, /PWR*
place = true
place_buffer = 0.5
runs = 4
```

Precedence is **defaults < config file < command line** — any option given on the
command line overrides the file. Keys are the long option names (the `--time`
budget is stored as `time_budget`); list options like `exclude_net` are
comma-separated, and flags take `true`/`false`. An unknown key or bad value is
reported as an error.

Generate a starting file with `--write-config` (it dumps every effective setting,
so it doubles as a template):

```bash
pyautoroute MyBoard.kicad_pcb --grid 0.2 --time 120 --write-config
# -> MyBoard.pyautoroute.cfg, then later:
pyautoroute MyBoard.kicad_pcb --config MyBoard.pyautoroute.cfg
```

### Auto-placement (experimental)

`--place` adds an opt-in pass that **arranges the footprints before routing**, the
placement analogue of the routing annealer: simulated annealing moves footprint
positions/rotations to minimise rats-nest length while keeping bodies from
overlapping and pulling the layout together. When it finishes, the `Edge.Cuts`
board outline is **replaced** with a rectangle bounding the placed parts (plus
`--place-margin`), and the board is routed normally in the same run.

The placer keeps footprints at least `--place-buffer` mm apart (default derived
from the design-rule clearance) so the placed board leaves room for routing and
stays DRC-clean. Rotated footprints keep their pads correctly oriented in the
output (KiCad stores pad angles absolutely, so the footprint rotation is
propagated into each pad).

It also keeps footprints clear of **silkscreen text** — both each footprint's own
visible Reference/Value labels and any standalone board text (`gr_text`, e.g.
connector pin labels or a title block), so parts aren't dropped on top of existing
silkscreen annotations. (`Autoroute = overlap` footprints, below, are exempt.)

Two footprint attributes steer it:

- **Locked footprints stay put.** Lock a footprint in KiCad (it stores `(locked
  yes)` / a bare `locked`) and the placer treats it as a fixed obstacle — useful
  for connectors, mounting holes, or anything that must keep its position.
- **`Autoroute = overlap`** — add a footprint **property** named `Autoroute` with a
  value of `overlap` (Footprint Properties → Fields → `+`) and that footprint's
  *body* may overlap others (e.g. an Arduino shield sitting over the board it plugs
  into). Its **pads** are still kept clear of other copper.

When `--place` runs and routing follows, the output is named `INPUT_placed_routed`.
Use `--place-only` to stop after placement and write `INPUT_placed` (no routing) —
handy for reviewing or hand-tweaking the layout before routing it.

Placement options (all also work with `--place-only`):

| Option | Meaning |
|---|---|
| `--place-iters N` / `--place-time S` | Placement budget (iterations or wall-clock seconds). |
| `--place-runs N` | Run placement `N` times (different seeds) and keep the lowest-energy placement (best-of-N). Default 1. |
| `--place-temps START END` | Start/end temperature of the placement cooling schedule (default `8.0 0.05`); `START > END > 0`. |
| `--place-step MM` | Max translate step (mm) at the start temperature (default 20). Shrinks as the schedule cools. |
| `--place-rotate {ortho,free,none}` | Rotation moves: `ortho` (±90/180, default), `free` (any angle), or `none`. |
| `--place-buffer MM` | Keep-out gap enforced between footprints (default: derived from the design-rule clearance). |
| `--place-margin MM` | Margin around the parts for the regenerated outline (default 2). |
| `--place-overlap-weight W` / `--place-compact-weight W` | Energy weights for overlap area and layout compactness. |

The live placement progress shows the temperature, current/best energy, and the
recent **acceptance ratio** (`acc=…%`, which falls as the schedule cools); the
end-of-placement summary reports the acceptance ratio and an energy breakdown
(ratsnest length, overlap area, bounding-box area).

It is experimental: it optimises placement heuristically and does not understand
mechanical/thermal intent, so review the result. Because it rewrites footprint
positions and the outline, inspect the output in KiCad before relying on it.

### What you get

```
  output:        MyBoard_routed.kicad_pcb
  connections:   70/70 routed (100%)
  unrouted:      0  (reported, not drawn)
  wirelength:    1039.7 mm
  vias:          51
  self-check:    clean (0 clearance violations)
  runtime:       12.34s real, 12.10s cpu
```

The tool runs an in-repo geometric **self-check** on its own output and reports
any clearance violation it finds (there should be none). Open the
`*_routed.kicad_pcb` in KiCad to inspect it.

## Verifying with KiCad (optional)

If KiCad is installed, you can independently confirm the result:

```bash
kicad-cli pcb drc --severity-error MyBoard_routed.kicad_pcb
```

Expect **0 clearance violations**. Unconnected items, if any, correspond exactly
to the connections the tool reported as unrouted.

## How it works (short version)

A hybrid of a grid **A\*/Lee maze router** (per two-pin connection, with a
45°-biased, via-aware cost model) and **simulated annealing** for global
optimisation (rip-up & reroute over connection order and layer choice). Clearance
is enforced on a clearance-inflated routing grid, so routes are DRC-clean by
construction. See [`docs/architecture.md`](docs/architecture.md) for the details.

Each track **terminates on the pad anchor (centre)**: the maze search enters a pad
at the most convenient grid node, then a short stub (inside the pad, so it adds no
clearance) carries the endpoint to the pad centre. KiCad therefore treats the track
as attached to the pad and keeps it connected when you move the footprint.

## Limitations (v1)

- Two copper layers only (F.Cu / B.Cu).
- Copper fills (zones with `fill yes`) are automatically detected: their net is excluded from routing and placement scoring, and the zone polygon is not treated as a routing obstacle. After writing the output board, PyAutoRoute tries to refill the zones using `kicad-cli` if it is installed; otherwise it prints a note to open the board in KiCad and run _Edit → Fill All Zones_ manually.
- Custom-shaped pads are approximated by their bounding box.
- Runtime is dominated by a few long/awkward nets; a finer `--grid` improves coverage but is slower.
- The optimiser improves length and via count; it does not guarantee a global optimum.

## Finding good settings

Results depend on a few knobs (grid pitch, via weight, schedule, budget).
`pyautoroute-tune` sweeps the critical parameters over one or more boards, scores
each routing with a single objective (completion, then wirelength, then vias, with
an optional runtime tiebreaker), and reports the best setting per board:

```bash
pyautoroute-tune MyBoard.kicad_pcb --time 5 --seeds 3
```

The opt-in `--auto` flag is the online version: it runs a quick probe on the board
in front of it, picks the best grid/via setting, and (on a terminal) asks you to
confirm before routing — pair it with `--write-config` to save the choice. See
[`docs/tuning.md`](docs/tuning.md) for the objective, method, and roadmap.

## Board fixups (`pyautoroute-fix`)

`pyautoroute-fix` corrects common KiCad PCB file issues that don't affect routing
but matter for manufacturing.

```bash
pyautoroute-fix --values BOARD.kicad_pcb           # overwrite in place
pyautoroute-fix --values BOARD.kicad_pcb -o OUT.kicad_pcb
pyautoroute-fix --values BOARD.kicad_pcb --dry-run  # report only, no write
```

| Flag | Meaning |
|---|---|
| `--values` | Move footprint **Value** text to the matching silkscreen layer (`F.SilkS` for front-side footprints, `B.SilkS` for back-side). KiCad footprint libraries often place the Value text on `F.Fab`/`B.Fab`, where it is visible only on the fabrication layer, not on the physical silkscreen. This flag reassigns those nodes to the correct silkscreen layer. |
| `-o OUT` | Write fixed board to `OUT` instead of overwriting the input. |
| `--dry-run` | Print what would change and exit without writing. |

## Helper script

`./pyautoroute.sh` is an interactive menu of common tasks — install the package,
regenerate the API docs from the code, run the short/long test suite, route a test
board, write a settings file, or clean generated outputs. Each action echoes the
command it runs, so the script doubles as a cheat-sheet. Override the interpreter
with `PYTHON=/path/to/python ./pyautoroute.sh`.

## Tests

```bash
pip install -e ".[dev]"
pytest                # fast suite (large-board routing is skipped)
pytest --slow         # also route the large boards (Test1, Test4) — slow
```
