# Plan: interactive footprint constraints in the GUI

Status: **✅ Implemented.** Click a footprint in the Initial view to set
edge affinity, locked, and overlap-ok constraints; canvas markers update
immediately; "Save constraints to board" writes back with a `.bak` backup.
Implemented via `pcb.footprint_at`, `pcb.set_footprint_edge/locked/overlap`,
and the context-menu interaction in `app.py`.

## Goal

Let the user **click a footprint on the board canvas** and, for that footprint:

1. **Set its edge constraint** — `Autoroute-edge` ∈ {none, any, left, right, top,
   bottom} (`Footprint.edge_affinity`);
2. **Lock / unlock it** — `Footprint.locked` (a fixed obstacle during `--place`);
3. **Set its overlap state** — `Autoroute-overlap` (`Footprint.overlap_ok`, the
   "body may sit over others" shield case).

Plus **canvas markers** so lock and overlap state are visible at a glance (edge
affinity already has markers; overlap has a ring; **lock has none today**).

These three are exactly the placement *constraints* PyAutoRoute already reads from
the board (`pcb._footprint_locked` / `_footprint_overlap_ok` /
`_footprint_edge_affinity`, pcb.py:434–509) — they're normally hand-set as KiCad
footprint properties / the lock flag. This feature makes them settable from the
GUI and **written back to the source board** so the next `--place` run (and KiCad)
sees them.

Per the request: **no multi-select / "selection set" concept is required** — each
action targets one footprint. A transient *highlight* of the clicked footprint is
proposed only as UX feedback, not a persistent selection model.

## Motivation

Setting these constraints today means hand-editing KiCad custom fields
(`Autoroute-edge`, `Autoroute-overlap`) and the lock flag, then re-running. The GUI
already renders the board and the existing markers; clicking to set a constraint is
the natural, discoverable workflow and closes the loop "see the placement → adjust
a constraint → re-run" entirely inside the GUI.

---

## UX / interaction model

Three viable interaction styles; **recommended: a small popup panel anchored at the
click** (B), with a canvas highlight of the target footprint.

| | Style | Pros | Cons |
|---|---|---|---|
| **A** | **Context menu** (`tk.Menu` posted at the cursor): a disabled `Footprint <ref>` header, an *Edge* cascade (None/Any/Left/Right/Top/Bottom, current one checked), *Locked* and *Overlap OK* checkbutton items | Tiny, no layout change, fast, shows current state via checkmarks | Slightly hidden; transient (no persistent echo of what's set) |
| **B (recommended)** | **Popup panel** (`tk.Toplevel`, near the click): the ref, an Edge `ttk.Combobox`/radio row, Lock + Overlap checkbuttons, Close | Clear current state, discoverable, room for a short legend, easy to extend | A bit more code than a menu |
| **C** | **Docked inspector** section in the left controls panel that populates on click | Always visible; no popup churn | Permanent screen real-estate; weaker spatial link to the clicked part |

All three drive the **same apply path** (below), so the choice is cosmetic and can
change late. **A** is the smallest first cut; **B** is the recommended target.
Recommend implementing the apply path + markers first (testable, headless), then a
context menu (A) as the minimum interactive surface, optionally upgrading to (B).

**Which board is edited.** The canvas shows either the loaded source board
(`_initial_board`, app.py:134) or a post-run *snapshot copy* (`BoardSnap.board`).
Constraints must be written to the **source** board. To avoid mapping clicks
through moved snapshot coordinates back to source footprints, **enable
footprint-constraint editing only in the "Initial" view** (the un-run source board).
This is also the natural place to set constraints *before* a run. The click handler
is a no-op (or shows a hint) in the current/best/overall-best views. *(A later
enhancement could map snapshot clicks back to the source footprint by `ref`, but
that is explicitly out of scope here.)*

**Lifecycle / persistence.** Edits mutate the in-memory `Footprint` dataclass **and**
the sexpr tree, redraw the canvas (markers update immediately), and mark the board
**dirty**. Persistence is an **explicit "Save constraints to board"** action that
writes the source `.kicad_pcb` with a timestamped `.bak` backup — mirroring
`_apply_to_project` (app.py:363–396). Auto-writing the user's source file on every
click is rejected as too surprising/destructive. Closing/opening/running with unsaved
constraint edits should prompt.

---

## Architecture & data flow

```
click (Tk/mpl) ──▶ BoardCanvas: button_press_event ──▶ (board_x, board_y)
        │
        ▼
pcb.footprint_at(board, x, y)  ──▶ Footprint | None        (pure, testable)
        │ (Initial view only)
        ▼
app: open action UI (menu/panel) for that footprint
        │  user picks edge / lock / overlap
        ▼
pcb.set_footprint_edge(fp, side|None)   ┐  update dataclass field
pcb.set_footprint_locked(fp, bool)      ├─ AND mutate fp.fp_node tree
pcb.set_footprint_overlap(fp, bool)     ┘  (span-invalidate, like fix_value_layers)
        │
        ▼
app: redraw canvas (markers reflect new state) + mark dirty
        │
        ▼  "Save constraints to board"
pcb.write_board(source_board, source_path)  (+ .bak)
```

The key idea: **one pure hit-test helper** and **three pure tree-mutation helpers**
in `pcb.py` (unit-testable, no Tk), with the GUI as a thin driver.

---

## Components

### 1. Hit-testing — `pcb.footprint_at(board, x, y)` (new, pure)

A footprint's body extent is the pad-derived box used by placement
(`placement._fp_box`, placement.py:563–586, minus the buffer/text inflation). Factor
the **bare pad bbox** so both the placer and the hit-test share it:

```python
def footprint_bbox(fp: Footprint) -> tuple[float, float, float, float]:
    """Axis-aligned (minx, miny, maxx, maxy) over the footprint's pads (mm)."""
    he = lambda p: 0.5 * math.hypot(p.w, p.h)        # rotation-independent half-extent
    xs0 = min(p.cx - he(p) for p in fp.pads)
    ys0 = min(p.cy - he(p) for p in fp.pads)
    xs1 = max(p.cx + he(p) for p in fp.pads)
    ys1 = max(p.cy + he(p) for p in fp.pads)
    return xs0, ys0, xs1, ys1

def footprint_at(board: Board, x: float, y: float) -> Footprint | None:
    """The footprint whose body box contains (x, y); the smallest if several do."""
    hits = []
    for fp in board.footprints:
        if not fp.pads:
            continue
        x0, y0, x1, y1 = footprint_bbox(fp)
        if x0 <= x <= x1 and y0 <= y <= y1:
            hits.append((( x1 - x0) * (y1 - y0), fp))
    return min(hits, key=lambda t: t[0])[1] if hits else None
```

- **Overlapping footprints:** pick the **smallest-area** box containing the point
  (a click inside a big connector that overlaps a small part resolves to the small
  one). Topmost-by-draw-order is an alternative; smallest-area is simpler and
  predictable.
- `(_half_extent` already exists at placement.py; move/duplicate the trivial lambda
  or import it. Cleanest: add `footprint_bbox` to `pcb.py` and have
  `placement._fp_box` build on it.)

### 2. Click handling — `gui/canvas.py` + `gui/app.py`

`BoardCanvas` (canvas.py:16–51) owns `self._mpl` (a `FigureCanvasTkAgg`) and
`self._ax`. Attach once in `__init__`:

```python
self._mpl.mpl_connect("button_press_event", self._on_click)
self._on_pick = None        # set by the app: callback(board_x, board_y, mouse_event)

def _on_click(self, event):
    if event.inaxes is not self._ax or event.xdata is None:
        return
    if self._on_pick is not None:
        self._on_pick(event.xdata, event.ydata, event)
```

- `event.xdata / event.ydata` are **board mm** directly — `draw_board` inverts the
  Y axis (visualize.py), so matplotlib's data coords already match KiCad Y-down.
- The app sets `self._board_canvas._on_pick = self._on_footprint_pick` (or a setter).
- `_on_footprint_pick` in `app.py`:
  - ignore unless `self._view_mode.get() == "initial"` and `self._initial_board`
    is set (else a one-line status hint: "switch to Initial view to edit
    constraints");
  - `fp = pcb.footprint_at(self._initial_board, x, y)`; if `None`, ignore;
  - highlight `fp` (optional: redraw with the target footprint's box outlined) and
    open the action UI (menu/panel) for it;
  - on each chosen action, call the matching `pcb.set_*` helper, then
    `self._render(self._initial_board, …)` to refresh markers, and set a dirty flag.

For the **screen position** of a popup (style A/B), use `event.guiEvent` (the Tk
event, carries `x_root`/`y_root`) to place the `tk.Menu`/`Toplevel` at the cursor.

### 3. The action UI

Minimum (style A): build a `tk.Menu` on each pick:

```python
m = tk.Menu(self._root, tearoff=0)
m.add_command(label=f"Footprint {fp.ref}", state="disabled")
m.add_separator()
edge = tk.Menu(m, tearoff=0)
for side in (None, "any", "left", "right", "top", "bottom"):
    edge.add_radiobutton(label=str(side or "none"),
                         value=str(side), variable=self._edge_var,
                         command=lambda s=side: self._set_edge(fp, s))
m.add_cascade(label="Edge constraint", menu=edge)
m.add_checkbutton(label="Locked", onvalue=1, offvalue=0,
                  variable=self._lock_var, command=lambda: self._set_lock(fp, ...))
m.add_checkbutton(label="Overlap OK", variable=self._ovl_var,
                  command=lambda: self._set_overlap(fp, ...))
m.tk_popup(event.guiEvent.x_root, event.guiEvent.y_root)
```

Initialise the vars from the footprint's current `edge_affinity` / `locked` /
`overlap_ok` so the menu shows current state. Style B replaces the menu with a
`Toplevel` carrying the same controls plus a short marker legend.

### 4. Applying changes — tree-mutation helpers in `pcb.py` (new, pure)

Follow the **exact `fix_value_layers` pattern** (pcb.py:1192–1264): find/replace the
child atom(s), then `span = None` up to `fp_node` so the serializer regenerates only
those lines (everything else round-trips byte-for-byte). Each helper updates **both**
the dataclass field (so markers/energy see it without re-parsing) **and** the tree
(so it persists).

**Property setter (edge + overlap share this):**

```python
def set_footprint_property(fp: Footprint, name: str, value: str | None) -> None:
    """Set/replace/remove a footprint custom field by name in the sexpr tree.

    value=None removes the property; otherwise its value atom is set (creating the
    property node if absent). Spans are invalidated so only this footprint
    re-serialises.
    """
    fp_node = fp.fp_node
    existing = None
    for prop in children(fp_node, "property"):
        vals = atoms_after_head(prop)
        if vals and vals[0].text == name:
            existing = prop
            break
    if value is None:
        if existing is not None:
            fp_node.remove(existing); fp_node.span = None
        return
    if existing is not None:
        vals = atoms_after_head(existing)
        existing[existing.index(vals[1])] = sexpr.string(value)  # replace value atom
        existing.span = None
    else:
        fp_node.append(_make_property_node(fp, name, value))      # see KiCad-format note
    fp_node.span = None
```

Wrappers that also set the dataclass field:

```python
def set_footprint_edge(fp, side):       # side ∈ {None,"any","left","right","top","bottom"}
    fp.edge_affinity = side
    set_footprint_property(fp, "Autoroute-edge", None if side is None else side)

def set_footprint_overlap(fp, on: bool):
    fp.overlap_ok = on
    set_footprint_property(fp, "Autoroute-overlap", "yes" if on else None)
```

**Lock setter** (footprint-level `locked` atom / `(locked yes)`; pcb.py:434–453):

```python
def set_footprint_locked(fp, locked: bool) -> None:
    fp.locked = locked
    node = fp.fp_node
    # drop any existing bare `locked` atom or (locked …) sublist
    node[:] = [it for it in node
               if not (isinstance(it, Atom) and it.raw == "locked")
               and not (isinstance(it, SList) and sexpr.head_symbol(it) == "locked")]
    if locked:
        node.insert(1, SList([sexpr.sym("locked"), sexpr.sym("yes")]))  # (locked yes)
    node.span = None
```

> **KiCad property-node format (design decision).** PyAutoRoute's parser only needs
> the first two atoms `(property "Name" "Value")`, but KiCad 7–9 stores custom fields
> as a full node with `(at …) (layer …) (uuid …) (hide yes) (effects (font …))`
> (see Test3.kicad_pcb:138–159). **Recommendation:** when *creating* a new
> `Autoroute-*` field, emit a **well-formed hidden field** so KiCad accepts it
> without complaint — `_make_property_node(fp, name, value)` builds
> `(property "name" "value" (at fp.x fp.y fp.angle) (layer "F.Fab") (hide yes)
> (uuid <new>) (effects (font (size 1 1) (thickness 0.15))))`, reusing the font block
> from an existing property on the same footprint when present, and a fresh `uuid`
> (`uuid.uuid4`). When the field already exists (the common case — user added it in
> KiCad), only the value atom is replaced. This is the single most important detail
> to get right and should be validated by reopening an edited board in KiCad.

### 5. Persistence

- A **dirty flag** on the app, set by any `set_*` call; reflected in the window
  title / status ("● unsaved constraints").
- A **"Save constraints to board"** button (in the controls panel near
  "Apply to Project") and/or a File-menu item. It writes `self._initial_board` to
  the source path via `pcb.write_board(board, src_path)` after copying a timestamped
  `.bak`, mirroring `_apply_to_project` (app.py:363–396). `write_board` with
  `new_nodes=None` writes the board as-is, and because only the touched footprints'
  spans were cleared, the diff is limited to the edited `(property …)` / `(locked …)`
  / `(at …)` lines.
- Prompt to save on **Open another board / Quit / Run** when dirty. (Running uses a
  fresh `load_board` of the source path, so unsaved in-memory edits would be lost —
  hence either auto-save-before-run or a prompt. Recommend: prompt, defaulting to
  save, so a run honours the just-set constraints.)

### 6. Markers — `visualize.py`

Extend `_draw_autoroute_markers` (visualize.py:238–273); rename conceptually to
"footprint constraint markers". Existing: orange edge arrows / `*` for `any`
(`_EDGE_COLOR`), purple `o` ring for overlap (`_OVERLAP_COLOR`). **Add a lock
marker.**

- **Lock marker:** a distinct colour + glyph that doesn't clash with orange/purple
  and is font-safe (avoid emoji — matplotlib's default font lacks 🔒). Recommended:
  a **red filled square outline** marker at `(fp.x, fp.y)`, e.g.
  `ax.plot(lx, ly, marker="s", mfc="none", mec="#cc0000", ms=16, mew=2.5, zorder=6)`,
  or a tiny red rectangle patch resembling a padlock body. Add `_LOCK_COLOR =
  "#cc0000"`. Collect `fp.locked` footprints alongside the existing `any`/overlap
  batches and draw them in one `ax.plot`.
- **Legend / key (recommended):** a small always-on legend on the canvas (or in the
  popup panel) mapping marker → meaning (edge arrow/star, overlap ring, lock square),
  since there are now three marker types. A 3-row text box in a corner via
  `ax.legend` with proxy artists, or static `ax.text`.
- The markers already redraw every `draw_board` call, so step 2's `self._render`
  after each edit updates them with no extra work.

---

## Files & tests

**Code**
- `pcb.py` — `footprint_bbox`, `footprint_at`, `set_footprint_property`,
  `set_footprint_edge`, `set_footprint_overlap`, `set_footprint_locked`,
  `_make_property_node` (KiCad-valid hidden field). (`placement._fp_box` can be left
  as-is or refactored onto `footprint_bbox`.)
- `visualize.py` — lock marker (+ `_LOCK_COLOR`) in `_draw_autoroute_markers`;
  optional legend.
- `gui/canvas.py` — `mpl_connect("button_press_event", …)` + an `on_pick` callback
  hook.
- `gui/app.py` — `_on_footprint_pick` (Initial-view guard, hit-test, action UI,
  apply + redraw + dirty), a Save-constraints action, dirty/prompt handling.
- (optional) `gui/fp_actions.py` — the popup panel (style B), if not inlined.

**Tests** (the pure helpers are fully headless; the Tk surface is not)
- `pcb.footprint_at`: a click inside a footprint returns it; empty space → `None`;
  overlapping boxes → the smaller; footprints with no pads skipped.
- `set_footprint_edge` / `_overlap` / `_locked`: round-trip — set on a parsed board,
  `write_board` to a temp file, reload, assert `_footprint_edge_affinity` /
  `_overlap_ok` / `_locked` reflect the change; setting `None`/`False` removes the
  property / lock; the diff touches only the edited footprint (other footprints'
  spans intact → byte-identical elsewhere).
- `_make_property_node`: produces a node re-parsed correctly and (manual/maintained)
  reopens cleanly in KiCad.
- `visualize`: a locked footprint adds the lock marker artist; an unlocked one does
  not.
- Headless GUI: factor `_on_footprint_pick`'s logic so the hit-test + apply path can
  be driven without a display (as `test_gui_worker.py` drives the worker); the raw
  Tk menu/popup is left to manual testing.

**Docs / versioning** (CLAUDE.md "done" bar)
- `README.md` — a short GUI section on clicking a footprint to set
  edge/lock/overlap and the markers.
- `docs/architecture.md` — the new `pcb` constraint-writer helpers and the
  `visualize` lock marker; note the GUI click→hit-test→tree-edit→save flow.
- `CHANGES.md` + version bump (minor, e.g. `0.33.0`).
- (API docs are no longer in version control as of `f8e88ef`, so just keep
  docstrings current.)

---

## Phasing (each independently shippable)

1. **Pure core** — `footprint_at`/`footprint_bbox` + the three `set_*` tree writers
   + `write_board` persistence + tests. No UI; fully headless. *(This is the bulk of
   the risk — the KiCad property format and the span-invalidation round-trip.)*
2. **Markers** — lock marker (+ legend) in `visualize`. Visible immediately in any
   board that already carries a lock/overlap/edge constraint.
3. **Interaction** — canvas click → hit-test → context menu (style A) → apply +
   redraw + dirty + Save. Ship here.
4. **Polish (optional)** — upgrade to the popup panel (style B), clicked-footprint
   highlight, save-on-run/quit prompts.

---

## Risks & open questions

- **KiCad property-node validity** (highest risk). A minimal `(property "n" "v")`
  parses in PyAutoRoute but may be rewritten/rejected by KiCad. Mitigation: emit a
  well-formed hidden field (`_make_property_node`) and **verify by reopening an
  edited board in KiCad**. Decide whether to match KiCad's exact child ordering.
- **Which board / moved coordinates.** Editing is scoped to the **Initial view** to
  avoid mapping snapshot clicks back to source footprints. If users expect to edit
  from a post-run view, a `ref`-based remap is a follow-up.
- **Run vs unsaved edits.** A run re-loads the source file, dropping unsaved
  in-memory constraints. Resolve via a save-before-run prompt (recommended) or
  auto-save-before-run.
- **Overlapping-footprint disambiguation.** Smallest-area-box wins; revisit if it
  feels wrong on dense boards (alternative: cycle through overlapping hits on
  repeated clicks).
- **Marker clutter / clarity.** Three marker types plus pads/tracks; the legend and
  distinct colours (orange/purple/red) should keep them readable — confirm on a
  busy board.
- **Lock semantics.** PyAutoRoute treats `locked` as "fixed obstacle during
  `--place`" (placement.py); this matches KiCad's footprint lock, but document that
  the GUI toggle writes the *board* lock flag (also affects KiCad).

## Recommended decisions (for review)

- **Interaction:** context menu first (A), popup panel (B) as the polish target.
- **Scope editing to the Initial view**; act on `_initial_board` directly.
- **Explicit save** with a `.bak` backup (no auto-write); prompt on run/open/quit
  when dirty.
- **Create well-formed hidden KiCad fields**; only replace the value atom when the
  field already exists.
- **Lock marker:** red square outline at the footprint origin; add a small legend.
