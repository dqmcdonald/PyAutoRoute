# Plan: decoupling capacitors — mark a cap, auto-find its IC, keep them together

> **Status: ✅ Shipped in 0.47.0 (2026-06-05).** Landed after
> [`placement-polish-plan.md`](placement-polish-plan.md) as intended. See
> `CHANGES.md` for the full entry. The implementation followed the plan below;
> no major deviations.

## Motivation

Decoupling capacitors must sit immediately next to the power pin of the IC they
serve. In the `--place` pass they don't: a cap's only pull toward its IC is the
**ratsnest** term, and a decoupling cap's two pads sit on a **power net** and
**ground** — both high-fanout nets. The MST that builds the ratsnest
(`netlist._mst_connections`, netlist.py:210) chains each high-fanout net through
its *nearest* pads, so a cap's GND/VCC connections often hop to some *other*
nearby part on those rails, not to its IC. The cap feels no specific attraction
to its IC and drifts away.

The user can already pin parts with KiCad **groups** (placement.py:362 moves a
group as a rigid body), but that is inflexible: it freezes the cap's relative
pose and orientation, needs manual group setup in KiCad, and doesn't express the
soft "stay close, but settle naturally" relationship we actually want.

**Goal:** mark a capacitor as a *decoupling cap* via the GUI right-click menu;
the tool then **searches the nets to identify the associated IC automatically**,
**warns if the choice is ambiguous or doesn't make sense**, and during placement
applies a **soft attraction** that keeps the cap next to that IC without the
rigidity of a group.

## Does the idea have issues? (raised up front)

The idea is sound and fits the existing `Autoroute-*` property + GUI + placement
patterns. The non-obvious risks, and how this plan handles them:

1. **Net matching alone is ambiguous.** A decoupling cap connects a power net and
   GND; both fan out to many footprints, so "footprints sharing a net" returns a
   crowd. → **Resolution combines net + IC-likeness + proximity** (below), and
   *always* surfaces a warning when the result is doubtful, with a manual
   override.

2. **When to resolve matters.** Resolving by *proximity* only works while caps are
   still near their ICs — i.e. in the **initial** layout, before placement scatters
   them. → Resolve **at mark-time in the GUI** (which already restricts constraint
   editing to the "Initial" view, app.py:560) and store the **concrete IC refdes**
   in the property. Placement then just looks up that refdes — deterministic, no
   re-derivation from moved positions. A literal `auto` value is still supported
   for hand-editors (resolved at placement time, with the proximity caveat).

3. **"Doesn't look like a decoupling cap."** A 2-pad part whose nets are *both*
   signal (e.g. an AC-coupling cap), or a part with ≠ 2 pads, isn't a decoupling
   cap. → The resolver detects this and warns ("does not bridge power and
   ground"), refusing to guess.

4. **Bulk caps / shared rails.** A bulk cap near a regulator may belong to a rail,
   not one IC. "Nearest IC on the power net" gives a reasonable answer; the user
   can override or leave it unmarked. Documented, not over-engineered.

5. **Attraction target granularity.** Pulling the cap toward the IC *centroid* (not
   its specific power pad) means on a large IC the cap seats near body-centre, not
   exactly at the power pin. Acceptable for v1; pad-level attraction noted as a
   refinement.

6. **Duplicate refdes / locked IC / missing IC.** Handled explicitly: duplicate
   refdes → warn; locked IC → still works (cap attracted to a fixed anchor, which
   is ideal); unresolvable target → warn and skip that cap.

None of these is a blocker; each has a defined behaviour and a warning.

---

## Design overview

Three pieces, mirroring how `edge_affinity`/`overlap_ok` already work end-to-end:

1. **Model + property** (`pcb.py`): a new `Footprint.decouple_target: str | None`
   field, parsed from / written to an **`Autoroute-decouple`** custom property
   (value = the associated IC's refdes, or `auto`). Parse/serialize helpers
   mirror `_footprint_edge_affinity` / `set_footprint_edge`.

2. **IC resolver** (`netlist.py`): `resolve_decoupling_ic(board, cap)` — searches
   the nets to find the associated IC, returns `(ic_ref, candidates, warning)`.
   Pure function, unit-testable, used by both the GUI (at mark-time) and placement
   (for `auto`).

3. **Placement attraction** (`placement.py`): a new **soft energy term**
   `decouple_weight · Σ dist(cap_centroid, ic_centroid)` over resolved cap→IC
   pairs, wired into the incremental cache exactly like the ratsnest term.

Plus the **GUI menu action** and **CLI weight knob**.

---

## 1. Model + property (`pcb.py`)

### Footprint field

Add to the `Footprint` dataclass (pcb.py:176), alongside `edge_affinity`:

```python
decouple_target: str | None = None   # IC refdes this decoupling cap serves,
                                      # or "auto" (resolve by net search), or None
```

### Parse

New helper mirroring `_footprint_edge_affinity` (pcb.py:533):

```python
def _footprint_decouple(fp_node: SList) -> str | None:
    """Return a footprint's decoupling-IC target, or None.

    Reads the user-defined ``Autoroute-decouple`` property. Its value is the
    reference designator of the IC this decoupling cap serves (e.g. ``U3``), or
    ``auto`` to resolve the IC automatically by searching shared power/ground
    nets at placement time. Absent/empty → None (not a decoupling cap).

    Args:
        fp_node: the ``(footprint ...)`` node.

    Returns:
        The target refdes, the string ``"auto"``, or ``None``.
    """
    for prop in children(fp_node, "property"):
        vals = atoms_after_head(prop)
        if len(vals) >= 2 and vals[0].text == "Autoroute-decouple":
            v = vals[1].text.strip()
            return v if v else None
    return None
```

Call it where `overlap_ok` / `edge_affinity` are populated during parse
(pcb.py:883–884): `decouple_target=_footprint_decouple(fp_node)`.

### Write

New helper mirroring `set_footprint_edge` (pcb.py:1700), reusing the existing
`set_footprint_property` machinery (which already forces re-serialisation via
`span = None`):

```python
def set_footprint_decoupling(fp: Footprint, target: str | None) -> None:
    """Set/clear a footprint's decoupling-cap target.

    Updates both `Footprint.decouple_target` and the sexpr tree
    (``Autoroute-decouple`` property). `target` is an IC refdes, ``"auto"``, or
    ``None`` to clear.
    """
    fp.decouple_target = target
    set_footprint_property(fp, "Autoroute-decouple", target)
```

---

## 2. IC resolver (`netlist.py`)

The heart of the feature: given a cap, find the IC it decouples. Net analysis
lives in `netlist.py` (it already owns `pads_by_net` consumers and the MST).

### Heuristics

- **Ground nets:** name matches (case-insensitive) `GND`, `GROUND`, `AGND`,
  `DGND`, `VSS`, `0V` (a small regexp set, extensible).
- **Power nets:** name matches `VCC`, `VDD`, `VBAT`, `VIN`, `V<digit>`, `+<num>`
  (e.g. `+3V3`, `+5V`), `-<num>`, or — as a fallback — a net whose fanout is
  high relative to the median (a rail) and isn't a ground net.
- **IC-like footprint:** refdes starts with `U` or `IC`, **or** pad count ≥ 4
  (a configurable threshold; 2-pin/3-pin passives and discretes are excluded).

### Algorithm

```python
def resolve_decoupling_ic(board, cap):
    """Find the IC a decoupling cap serves by searching its nets.

    Returns:
        (ic_ref, candidates, warning):
          ic_ref     — chosen IC refdes, or None if none could be chosen;
          candidates — all plausible IC refdes (nearest first), for a GUI chooser;
          warning    — a human-readable string if the result is doubtful/invalid,
                       else None.
    """
```

Steps:

1. **Sanity / "makes sense" check.** Collect `nets = {p.net for p in cap.pads if p.net}`.
   If the cap has ≠ 2 pads or < 2 distinct nets → return
   `(None, [], "C? has {n} pad-nets; a decoupling cap is expected to bridge two")`.

2. **Classify the two nets.** Identify which is ground and which is power. If
   *neither* is ground/power → `(None, [], "C? does not bridge power and ground; "
   "may not be a decoupling cap")`. If one is ground and the other unclassified,
   treat the non-ground net as the power net (with a soft note).

3. **Candidates.** From `board.pads_by_net()[power_net]`, map pads back to their
   footprints (via the parent footprint owning the pad — `fp.pads` membership, or
   `Pad.fp_ref`). Keep IC-like ones, excluding the cap itself. If none are IC-like,
   fall back to *all* footprints on the power net (with a note that no obvious IC
   was found).

4. **Rank by proximity** to the cap centroid (current positions). `candidates` =
   refdes sorted nearest-first.

5. **Choose + ambiguity warning.**
   - none → `(None, [], "no IC found on net {power_net} for C?")`.
   - one → `(ref, [ref], None)`.
   - several → choose the nearest; if the 2nd-nearest is within a small margin
     (e.g. ≤ 15 % farther, or both share the power *and* ground nets equally) →
     `(nearest, candidates, "C? could serve {a} or {b}; chose nearest ({a}) — "
     "verify or pick manually")`.

6. **Duplicate refdes** guard: if the chosen refdes is non-unique on the board →
   append a warning and prefer matching by the nearest footprint's `uuid`
   internally (the property still stores the refdes for readability).

Pure, deterministic, and unit-testable with small synthetic boards.

---

## 3. Placement attraction term (`placement.py`)

A **soft** pull — flexible where a group is rigid. Add a term to the energy
(placement.py:8 docstring + `_cached_energy`, placement.py:620):

```
E += decouple_weight · Σ_pairs  dist(centroid(cap), centroid(ic))
```

Why centroid distance (not zero-target): the **overlap/buffer** term
(placement.py:252) already prevents the cap from landing on top of the IC, so a
monotone "pull closer" attraction naturally seats the cap *adjacent* to the IC at
the buffer gap — the desired result — without a tunable target distance.

### Parameter

Add to `PlaceParams` (placement.py:246):

```python
decouple_weight: float = 5.0   # mm-cost per mm a decoupling cap sits from its IC
                               # (0 disables the attraction)
```

`5.0` is a strong pull (cf. `edge_weight = 2.0`); decoupling placement is a hard
intent, so it should dominate the generic ratsnest. Exposed as
`--place-decouple-weight`.

### Wiring into the incremental cache

This term plugs into the same machinery as the ratsnest (placement.py:757,
`_move_delta`; placement.py:620, `_cached_energy`; placement.py:460,
`_rebuild_cache`). In `_Placer.__init__`, after the index/groups are built:

```python
# Decoupling pairs: (cap boxed-index, ic boxed-index). Resolved from each
# footprint's decouple_target; "auto" is resolved here via netlist search.
self._decouple_pairs: list[tuple[int, int]] = []
self._decouple_warnings: list[str] = []
ref_to_idx = {fp.ref: i for i, fp in enumerate(self.boxed)}   # last-wins; dup-guard below
for ci, fp in enumerate(self.boxed):
    tgt = fp.decouple_target
    if not tgt:
        continue
    if tgt == "auto":
        ref, _cands, warn = netlist.resolve_decoupling_ic(self.board, fp)
        if warn:
            self._decouple_warnings.append(warn)
        tgt = ref
    if tgt and tgt in ref_to_idx and ref_to_idx[tgt] != ci:
        self._decouple_pairs.append((ci, ref_to_idx[tgt]))
    elif tgt:
        self._decouple_warnings.append(f"{fp.ref}: decouple target {tgt!r} not found")
# index: boxed-index -> incident decouple-pair indices (for incremental updates)
self._fp_decouple: dict[int, list[int]] = {}
for pi, (a, b) in enumerate(self._decouple_pairs):
    self._fp_decouple.setdefault(a, []).append(pi)
    self._fp_decouple.setdefault(b, []).append(pi)
self._decouple_len: list[float] = []     # per-pair distance, filled in _rebuild_cache
self._decouple = 0.0                      # cached Σ distance
```

- `_rebuild_cache` (placement.py:460): compute `self._decouple_len = [centroid
  distance for each pair]`, `self._decouple = sum(...)`. Centroid = mean of the
  footprint's pad centres (already used elsewhere) or `(fp.x, fp.y)`.
- `_cached_energy` (placement.py:620): add `+ self.p.decouple_weight * self._decouple`.
- `_move_delta` (placement.py:757): for moved `idxs`, subtract the old lengths of
  incident pairs (`self._fp_decouple`), recompute, add back — identical pattern to
  the ratsnest block already there.
- **Save/restore** in `run()` (placement.py:977–1008): include `self._decouple`
  and the touched `_decouple_len` entries in the `cache_save` tuple, exactly like
  `_conn_len`. *(See* Interaction with the polish plan *— if that lands first,
  this rides on its `_save_cache`/`_load_cache` helpers instead.)*

### Result + warnings surfacing

Add to `PlaceResult` (placement.py:281):

```python
warnings: list[str] = field(default_factory=list)   # e.g. unresolved decouple targets
```

`run()` copies `self._decouple_warnings` into the result. The CLI reporter
(autoroute.py `_report_place` ~1444) prints them (`⚠ …`); the GUI surfaces them in
the status bar.

---

## 4. GUI menu action (`gui/app.py`)

Extend the existing footprint context menu (`_on_footprint_pick`, app.py:560).
Because the target can be ambiguous, the menu does more than a checkbutton — it
**resolves on open and offers a chooser**:

```python
# --- Decoupling cap submenu ---
from pyautoroute import netlist
dec_menu = tk.Menu(menu, tearoff=0)
dec_var = tk.StringVar(value=fp.decouple_target or "")
dec_menu.add_radiobutton(label="Off", value="", variable=dec_var,
                         command=lambda: self._set_decouple(fp, None))
ref, candidates, warning = netlist.resolve_decoupling_ic(self._initial_board, fp)
if ref:
    dec_menu.add_radiobutton(
        label=f"Auto → {ref}", value=ref, variable=dec_var,
        command=lambda r=ref: self._set_decouple(fp, r, warning))
# let the user override among other plausible ICs
for cand in candidates:
    if cand != ref:
        dec_menu.add_radiobutton(label=cand, value=cand, variable=dec_var,
                                 command=lambda r=cand: self._set_decouple(fp, r))
if not candidates:
    dec_menu.add_command(label="(no IC found)", state=tk.DISABLED)
menu.add_cascade(label="Decoupling cap", menu=dec_menu)
```

Handler mirroring `_set_edge` (app.py:612):

```python
def _set_decouple(self, fp, target, warning=None):
    from pyautoroute import pcb
    pcb.set_footprint_decoupling(fp, target)
    if warning:
        self._status_var.set(f"⚠ {fp.ref}: {warning}")
    elif target:
        self._status_var.set(f"{fp.ref} → decouples {target}")
    else:
        self._status_var.set(f"{fp.ref}: decoupling cleared")
    self._redraw()   # optional: draw a faint cap→IC link in Initial view
```

Persistence is already handled: "Save Constraints to board" → `write_board`
(app.py:639) serialises the new `Autoroute-decouple` property.

**Optional nicety (Phase 4):** draw a thin dashed line from each marked cap to its
IC in the Initial view, so the user can eyeball the associations.

---

## 5. CLI (`autoroute.py`)

One tuning knob; the feature is otherwise driven entirely by the property (like
edge/overlap — no enable flag needed):

```
--place-decouple-weight W   mm-cost per mm a decoupling cap sits from its IC
                            (default 5.0; 0 disables)
```

Add the argument near the other `--place-*` weights (autoroute.py ~1890), default
`placement.PlaceParams.decouple_weight`, validate `>= 0`, and wire it in
`build_place_params` (autoroute.py:533):
`decouple_weight=args.place_decouple_weight`. Print resolved-pair count + any
warnings in the place report.

---

## Interaction with the polish plan

[`placement-polish-plan.md`](placement-polish-plan.md) refactors `run()`'s inline
cache save/restore into `_save_cache` / `_load_cache` helpers and adds
`_energy_after_translate`. The decoupling term is a new cached scalar
(`self._decouple` + `self._decouple_len`), so **whichever lands second must add the
decouple entries to those helpers** (and to `_move_delta` / `_rebuild_cache` /
`_cached_energy`). If the polish plan lands first, this plan extends its helpers;
if this lands first, the polish plan must include `_decouple` in the helpers it
extracts. Either order works — just sequence them and update the one cache
save/restore site accordingly. No deeper conflict.

---

## Phases

Each phase ships with its docs + `CHANGES.md` entry; the version bump lands with
Phase 1.

### Phase 1 — model + resolver (no GUI/placement effect yet)
- `Footprint.decouple_target`; `_footprint_decouple` parse; `set_footprint_decoupling`
  write helper.
- `netlist.resolve_decoupling_ic` + the net-classification helpers.
- **Tests** (tests/test_netlist.py, tests/test_pcb.py):
  - resolver picks the nearest IC on the power net (synthetic board: 1 cap, 2 ICs,
    shared VCC/GND) — and returns the farther one as a candidate.
  - resolver warns on: non-2-pad cap; cap whose nets are both signal; power net
    with no IC; ambiguous near-tie (returns both candidates).
  - property round-trip: set `auto`/`U3`/clear → write → reload → field matches
    (mirror `test_constraint_round_trip_to_file`).

### Phase 2 — placement attraction
- `PlaceParams.decouple_weight`; `PlaceResult.warnings`.
- Build `_decouple_pairs` / `_fp_decouple` in `__init__` (resolving `auto`).
- Wire `_decouple` into `_rebuild_cache`, `_cached_energy`, `_move_delta`, and the
  `run()` save/restore (or the polish helpers).
- **Tests** (tests/test_placement.py):
  - a cap marked for an IC ends up adjacent to it (gap ≈ buffer) after place,
    where an unmarked control cap drifts — assert marked cap's final distance to
    its IC is smaller.
  - `decouple_weight=0` reproduces current behaviour (no-op guard).
  - locked IC: cap is pulled to the fixed IC; IC stays put.
  - unresolved target → recorded in `PlaceResult.warnings`, placement still runs.
  - determinism with seed; locks/groups still respected.

### Phase 3 — GUI + CLI
- Context-menu "Decoupling cap" submenu + `_set_decouple`, resolving on open and
  offering the candidate chooser; status-bar warnings.
- `--place-decouple-weight` flag, validation, wiring, report line.
- **Tests:** CLI flag parse/validate; a place-only run on a fixture with an
  `Autoroute-decouple` property seats the cap by its IC. (GUI logic is thin;
  exercise `resolve_decoupling_ic` + `set_footprint_decoupling` directly as the
  GUI does.)

### Phase 4 — docs + version (+ optional cap↔IC link drawing)
- **README.md**: the `Autoroute-decouple` property, the GUI menu, the
  `--place-decouple-weight` knob, and the auto-detection + warning behaviour.
- **docs/architecture.md**: the new energy term, the resolver's net heuristics and
  data flow, and the **invariant** (decoupling attraction is a *soft* term — it
  biases, never pins; locks/groups still win).
- **API docs**: docstrings (Google style) on the new functions/fields, then
  `pdoc -d google --mermaid pyautoroute -o docs/api`.
- **pyproject.toml**: minor bump; **CHANGES.md**: newest-first entry.
- Optional: dashed cap→IC overlay in the Initial view.

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Wrong IC chosen on dense boards | GUI resolves at mark-time on the initial layout + offers a manual chooser; every doubtful result warns. |
| `auto` resolved at placement time after caps moved | Documented; GUI marking (storing a concrete refdes) is the recommended path and avoids it. |
| Power/ground name patterns miss a board's convention | Fanout fallback for power; resolver still warns and the user can pick manually; patterns are extensible. |
| Attraction fights edge-affinity / containment | All are soft weighted terms; tune `decouple_weight`. A cap marked decoupling shouldn't also be edge-flagged (note in docs). |
| Cache save/restore omission for the new term | Covered by the `decouple_weight=0` no-op test + existing SA monotonicity tests; single save/restore site (shared with polish plan). |
| Duplicate refdes | Resolver warns and disambiguates by nearest uuid internally. |

## Acceptance criteria

- Marking a cap (GUI or property) makes it settle adjacent to its IC after
  `--place`, where it previously drifted — verified by a test comparing marked vs
  unmarked cap distance-to-IC.
- The resolver returns the sensible IC on a clear board and **warns** on ambiguous
  / nonsensical inputs (both covered by tests).
- `--place-decouple-weight 0` and "no marked caps" reproduce current behaviour
  exactly.
- Locks, groups, and edge affinity still honoured; warnings surfaced in CLI and
  GUI.
- README, architecture.md, API docs, version, and CHANGES.md updated.
