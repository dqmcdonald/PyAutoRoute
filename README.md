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

## Usage

```bash
pyautoroute INPUT.kicad_pcb [options]
```

The original file is never modified — a routed copy is written alongside it.

| Option | Meaning |
|---|---|
| `--pro PROJECT.kicad_pro` | Project file with the design rules (default: the sibling `.kicad_pro`). |
| `-o, --output FILE` | Output path (default: `INPUT_routed.kicad_pcb`). |
| `--grid MM` | Routing grid pitch in mm (default: derived from the rules, ≈ `track/2 + clearance`). Finer = better coverage but slower. |
| `--iters N` | Run simulated-annealing optimisation for N iterations. |
| `--time SECONDS` | Run optimisation for a wall-clock budget instead. |
| `--unrouted-weight W` | Annealing energy penalty per unrouted connection (default 100). Higher ⇒ the optimiser tries harder to complete every connection, at the expense of wirelength/vias; lower ⇒ it tolerates leaving hard nets for manual routing. |
| `--anneal-temps START END` | Start/end temperature of the geometric cooling schedule (default `4.0 0.05`); `START > END > 0`. Higher `START` explores more (better escape from local minima, slower convergence); lower `END` exploits harder at the finish. |
| `--exclude-net PATTERN` | Leave matching nets un-routed (repeatable; glob, e.g. `GND` or `"/PWR*"`). Their pads still act as obstacles. |
| `--via-weight W` | Via cost in mm-equivalent (higher ⇒ fewer vias). Default 2.0. |
| `--seed S` | Random seed for the optimiser. |
| `--snapshots N` | During annealing, save `N` intermediate board snapshots to a `snapshots/` subdir (beside the output), so you can watch the optimisation progress. Requires `--iters` or `--time`. |
| `--log [FILE]` | Write a verbose log of the input parameters and routing/annealing progress. Bare `--log` writes `<output>.log`; `--log FILE` uses the given path. |
| `--debug-plot` | Also write a `.png` render of the routed board. |
| `--quiet` | Suppress the live progress display (final summary only). |
| `--version` | Print the version and exit. (The version is also printed on startup and written to the `--log` header.) |

`--iters` and `--time` are mutually exclusive; if neither is given, the board is
routed once (greedy order) without annealing.

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
```

### What you get

```
  output:        MyBoard_routed.kicad_pcb
  connections:   70/70 routed (100%)
  unrouted:      0  (reported, not drawn)
  wirelength:    1039.7 mm
  vias:          51
  self-check:    clean (0 clearance violations)
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

## Limitations (v1)

- Two copper layers only (F.Cu / B.Cu).
- No copper-pour generation. Existing zones are treated as obstacles and same-net connectivity, not regenerated.
- Custom-shaped pads are approximated by their bounding box.
- Runtime is dominated by a few long/awkward nets; a finer `--grid` improves coverage but is slower.
- The optimiser improves length and via count; it does not guarantee a global optimum.

## Tests

```bash
pip install -e ".[dev]"
pytest                # fast suite (large-board routing is skipped)
pytest --slow         # also route the large boards (Test1, Test4) — slow
```
