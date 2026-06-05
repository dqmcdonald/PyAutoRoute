"""Settings panel: grouped controls auto-typed from the argparse parser."""

from __future__ import annotations

import argparse
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from .tooltip import add_tooltip


# ── helpers ──────────────────────────────────────────────────────────────────

def _lbl(parent, text, **kw):
    return ttk.Label(parent, text=text, **kw)


def _entry(parent, var, width=12, **kw):
    return ttk.Entry(parent, textvariable=var, width=width, **kw)


def _row(parent, row, label, widget, tip="", col=0):
    ttk.Label(parent, text=label).grid(row=row, column=col,
                                       sticky=tk.W, padx=4, pady=2)
    widget.grid(row=row, column=col + 1, sticky=tk.EW, padx=4, pady=2)
    if tip:
        add_tooltip(widget, tip)


def _section(parent, text):
    lf = ttk.LabelFrame(parent, text=text, padding=(6, 4))
    lf.pack(fill=tk.X, padx=6, pady=(4, 0))
    lf.columnconfigure(1, weight=1)
    return lf


# ── scrollable container ─────────────────────────────────────────────────────

class _ScrollFrame(ttk.Frame):
    """A vertically scrollable frame — inner content goes in ``self.inner``."""

    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        vbar = ttk.Scrollbar(self, orient=tk.VERTICAL)
        self._canvas = tk.Canvas(self, yscrollcommand=vbar.set,
                                  highlightthickness=0)
        vbar.config(command=self._canvas.yview)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.inner = ttk.Frame(self._canvas)
        self._win_id = self._canvas.create_window(
            (0, 0), window=self.inner, anchor=tk.NW)
        self.inner.bind("<Configure>", self._on_inner)
        self._canvas.bind("<Configure>", self._on_canvas)
        self._canvas.bind("<MouseWheel>", self._on_scroll)
        self._canvas.bind("<Button-4>", self._on_scroll)
        self._canvas.bind("<Button-5>", self._on_scroll)

    def _on_inner(self, _e=None):
        self._canvas.configure(
            scrollregion=self._canvas.bbox(tk.ALL))

    def _on_canvas(self, e):
        self._canvas.itemconfig(self._win_id, width=e.width)

    def _on_scroll(self, e):
        if e.num == 4:
            self._canvas.yview_scroll(-1, "units")
        elif e.num == 5:
            self._canvas.yview_scroll(1, "units")
        else:
            self._canvas.yview_scroll(int(-1 * e.delta / 120), "units")


# ── RunConfig ─────────────────────────────────────────────────────────────────

@dataclass
class RunConfig:
    """Plain container for all pipeline parameters (mirrors argparse Namespace)."""
    input: object = None
    pro: object = None
    output: object = None
    place: object = None
    place_only: object = None
    grid: object = None
    iters: object = None
    time_budget: object = None
    runs: object = None
    exclude_net: object = None
    via_weight: object = None
    unrouted_weight: object = None
    anneal_temps: object = None
    seed: object = None
    place_iters: object = None
    place_time: object = None
    place_margin: object = None
    place_buffer: object = None
    place_overlap_weight: object = None
    place_compact_weight: object = None
    place_edge_weight: object = None
    place_temps: object = None
    place_step: object = None
    place_rotate: object = None
    place_swap_prob: object = None
    place_runs: object = None
    place_polish: object = None
    place_polish_iters: object = None
    place_polish_time: object = None
    place_polish_eps: object = None
    cycles: object = None
    scatter_start: object = None
    place_feedback: object = None
    congestion_weight: object = None
    snapshots: object = None
    quiet: object = None
    log: object = None
    auto: object = None
    auto_yes: object = None
    silk_labels: object = None
    keep_outline: object = None
    ground_plane: object = None
    ground_net: object = None
    ground_plane_layer: object = None
    ground_plane_margin: object = None
    stitch_vias: object = None
    existing_routes: object = None
    greedy_order: object = None


# ── ControlsPanel ────────────────────────────────────────────────────────────

class ControlsPanel(ttk.Frame):
    """Left-side settings panel with Route / Place tabs.

    Args:
        parent: the parent widget.
        on_run: callback(RunConfig) invoked when Run is pressed.
        on_stop: callback() invoked when Stop is pressed.
        on_apply: callback() invoked when Apply to Project is pressed.
        on_save_as: callback() invoked when Save As… is pressed.
    """

    def __init__(self, parent,
                 on_run: Callable, on_stop: Callable,
                 on_apply: Callable,
                 on_open: Callable | None = None,
                 on_save_constraints: Callable | None = None,
                 on_save_as: Callable | None = None,
                 **kw):
        super().__init__(parent, **kw)
        self._on_run = on_run
        self._on_stop = on_stop
        self._on_apply = on_apply
        self._on_open = on_open
        self._on_save_constraints = on_save_constraints
        self._on_save_as = on_save_as

        # ── vars ──
        self._input_path = tk.StringVar()
        self._pro_path = tk.StringVar()
        self._output_path = tk.StringVar()
        # Mode: "route" | "place_route" | "place_only"
        self._mode = tk.StringVar(value="route")
        # Routing
        self._grid = tk.StringVar(value="")
        self._budget_kind = tk.StringVar(value="iters")  # "iters" | "time"
        self._budget_val = tk.StringVar(value="")
        self._runs = tk.StringVar(value="1")
        self._exclude_net = tk.StringVar(value="")
        self._existing_routes = tk.StringVar(value="clear")
        self._greedy_order = tk.StringVar(value="short")
        # Placement
        self._place_budget_kind = tk.StringVar(value="iters")
        self._place_budget_val = tk.StringVar(value="")
        self._place_runs = tk.StringVar(value="1")
        self._place_rotate = tk.StringVar(value="ortho")
        self._place_margin = tk.StringVar(value="2.0")
        self._place_buffer = tk.StringVar(value="")
        # Post-anneal placement polish
        self._place_polish = tk.BooleanVar(value=False)
        self._place_polish_iters = tk.StringVar(value="20")
        self._place_polish_time = tk.StringVar(value="")
        self._place_polish_eps = tk.StringVar(value="0.05")
        # Best-of-cycles + congestion feedback (place+route outer loop)
        self._cycles = tk.StringVar(value="1")
        self._scatter_start = tk.BooleanVar(value=False)
        self._place_feedback = tk.BooleanVar(value=False)
        self._congestion_weight = tk.StringVar(value="5.0")
        # Output
        self._log = tk.BooleanVar(value=False)
        self._quiet = tk.BooleanVar(value=False)
        # Advanced (stored as strings for easy edit)
        self._seed = tk.StringVar(value="0")
        self._via_weight = tk.StringVar(value="2.0")
        self._unrouted_weight = tk.StringVar(value="100.0")
        self._anneal_t_start = tk.StringVar(value="4.0")
        self._anneal_t_end = tk.StringVar(value="0.05")
        self._place_t_start = tk.StringVar(value="8.0")
        self._place_t_end = tk.StringVar(value="0.05")
        self._place_step = tk.StringVar(value="20.0")
        self._place_swap_prob = tk.StringVar(value="0.2")
        self._place_ow = tk.StringVar(value="20.0")
        self._place_cw = tk.StringVar(value="0.02")
        self._place_ew = tk.StringVar(value="2.0")
        self._snapshots = tk.StringVar(value="0")
        self._silk_labels = tk.BooleanVar(value=False)
        self._keep_outline = tk.BooleanVar(value=False)
        # Ground plane
        self._ground_plane = tk.BooleanVar(value=False)
        self._ground_net = tk.StringVar(value="")
        self._ground_plane_layer = tk.StringVar(value="B.Cu")
        self._ground_plane_margin = tk.StringVar(value="")
        self._stitch_vias = tk.StringVar(value="")

        self._build_ui()
        self._mode.trace_add("write", self._on_mode_change)
        self._on_mode_change()

    # ── UI construction ──────────────────────────────────────────────

    def _build_ui(self):
        # ── Board section (always visible, above tabs) ──
        board_sf = ttk.Frame(self)
        board_sf.pack(fill=tk.X)
        bf = _section(board_sf, "Board")
        ttk.Button(bf, text="Open…", command=self._browse_board,
                   width=8).grid(row=0, column=0, padx=4, pady=2, sticky=tk.W)
        ttk.Label(bf, textvariable=self._input_path,
                  wraplength=200, foreground="#444"
                  ).grid(row=0, column=1, sticky=tk.EW, padx=4)
        ttk.Label(bf, text="Project:").grid(
            row=1, column=0, sticky=tk.W, padx=4)
        ttk.Label(bf, textvariable=self._pro_path,
                  foreground="#666", font=("TkDefaultFont", 9)
                  ).grid(row=1, column=1, sticky=tk.EW, padx=4)

        # ── Mode section (always visible, above tabs) ──
        mode_sf = ttk.Frame(self)
        mode_sf.pack(fill=tk.X)
        mf = _section(mode_sf, "Mode")
        for i, (val, lbl, tip) in enumerate([
            ("route",       "Route only",
             "Greedy + optional annealing; no footprint placement."),
            ("place_route", "Place + Route",
             "Anneal footprint positions, then route."),
            ("place_only",  "Place only",
             "Optimise placement and write the placed board without routing."),
        ]):
            rb = ttk.Radiobutton(mf, text=lbl, variable=self._mode, value=val)
            rb.grid(row=i, column=0, columnspan=2, sticky=tk.W, padx=4, pady=1)
            add_tooltip(rb, tip)

        # ── Notebook: Route | Place ──
        self._notebook = ttk.Notebook(self)
        self._notebook.pack(fill=tk.BOTH, expand=True, padx=4, pady=(4, 0))

        self._route_tab = _ScrollFrame(self._notebook)
        self._notebook.add(self._route_tab, text="Route")

        self._place_tab = _ScrollFrame(self._notebook)
        self._notebook.add(self._place_tab, text="Place")

        self._build_route_tab(self._route_tab.inner)
        self._build_place_tab(self._place_tab.inner)

        # ── Button rows (always visible, below tabs) ──
        self._build_buttons(self)

    def _build_route_tab(self, p):
        rf = _section(p, "Routing")
        rf.columnconfigure(1, weight=1)

        grid_e = _entry(rf, self._grid)
        _row(rf, 0, "Grid (mm):", grid_e,
             "Routing grid pitch in mm. Leave blank to derive from board rules.")

        # Budget row (iters / time toggle)
        brow = ttk.Frame(rf)
        brow.grid(row=1, column=0, columnspan=2, sticky=tk.EW, padx=4, pady=2)
        ttk.Label(brow, text="Budget:").pack(side=tk.LEFT)
        for val, lbl in (("iters", "Iters"), ("time", "Time (s)")):
            ttk.Radiobutton(brow, text=lbl, variable=self._budget_kind,
                            value=val).pack(side=tk.LEFT, padx=2)
        budget_e = _entry(rf, self._budget_val)
        budget_e.grid(row=2, column=1, sticky=tk.EW, padx=4)
        add_tooltip(budget_e,
                    "Iteration count or time budget (seconds) for annealing. "
                    "Leave blank for greedy-only routing.")

        runs_e = _entry(rf, self._runs, width=6)
        _row(rf, 3, "Runs:", runs_e,
             "Number of independent routing runs; the lowest-energy result is kept. "
             "Only useful with an iteration/time budget.")

        excl_e = _entry(rf, self._exclude_net, width=20)
        _row(rf, 4, "Exclude net:", excl_e,
             "Comma-separated net names or glob patterns to leave un-routed "
             "(e.g. GND, PWR_*).")

        er_row = ttk.Frame(rf)
        er_row.grid(row=5, column=0, columnspan=2, sticky=tk.EW, padx=4, pady=2)
        ttk.Label(er_row, text="Existing routes:").pack(side=tk.LEFT)
        for val, lbl in (("clear", "Clear"), ("preserve", "Preserve")):
            ttk.Radiobutton(er_row, text=lbl,
                            variable=self._existing_routes,
                            value=val).pack(side=tk.LEFT, padx=4)
        add_tooltip(er_row,
                    "Clear: strip all existing tracks/vias before routing (default). "
                    "Preserve: keep existing copper and only route unconnected nets.")

        go_row = ttk.Frame(rf)
        go_row.grid(row=6, column=0, columnspan=2, sticky=tk.EW, padx=4, pady=2)
        ttk.Label(go_row, text="Greedy order:").pack(side=tk.LEFT)
        for val, lbl in (("short", "Short"), ("long", "Long"), ("shuffle", "Shuffle")):
            ttk.Radiobutton(go_row, text=lbl,
                            variable=self._greedy_order,
                            value=val).pack(side=tk.LEFT, padx=4)
        add_tooltip(go_row,
                    "Order for the initial greedy routing pass.\n"
                    "Short (default): shortest connections first.\n"
                    "Long: longest first — routes hard long-distance connections "
                    "while the board is clear.\n"
                    "Shuffle: random order per run/cycle — varies the starting "
                    "state so the annealer explores different configurations.")

    def _build_place_tab(self, p):
        pf = _section(p, "Placement")
        pf.columnconfigure(1, weight=1)

        pbrow = ttk.Frame(pf)
        pbrow.grid(row=0, column=0, columnspan=2, sticky=tk.EW, padx=4, pady=2)
        ttk.Label(pbrow, text="Budget:").pack(side=tk.LEFT)
        for val, lbl in (("iters", "Iters"), ("time", "Time (s)")):
            ttk.Radiobutton(pbrow, text=lbl,
                            variable=self._place_budget_kind,
                            value=val).pack(side=tk.LEFT, padx=2)
        pbudget_e = _entry(pf, self._place_budget_val)
        pbudget_e.grid(row=1, column=1, sticky=tk.EW, padx=4)
        add_tooltip(pbudget_e,
                    "Iteration count or time budget for placement annealing.")

        pruns_e = _entry(pf, self._place_runs, width=6)
        _row(pf, 2, "Runs:", pruns_e,
             "Number of independent placement runs; the lowest-energy is kept.")

        pm_e = _entry(pf, self._place_margin, width=8)
        _row(pf, 3, "Margin (mm):", pm_e,
             "Gap (mm) around parts for the regenerated board outline.")

        pb_e = _entry(pf, self._place_buffer, width=8)
        _row(pf, 4, "Buffer (mm):", pb_e,
             "Keep-out gap between footprints during placement. "
             "Leave blank to derive from design-rule clearance.")

        rot_cb = ttk.Combobox(pf, textvariable=self._place_rotate,
                              values=["ortho", "free", "none"],
                              state="readonly", width=10)
        _row(pf, 5, "Rotation:", rot_cb,
             "Placement rotation moves: ortho (±90/180°), free (any angle), "
             "or none.")

        swap_e = _entry(pf, self._place_swap_prob, width=8)
        _row(pf, 6, "Swap prob:", swap_e,
             "Probability of attempting a swap move each iteration (0–1). "
             "Raise for boards with many interchangeable ICs (e.g. repeated "
             "74HC-series logic) to explore position swaps more aggressively. "
             "Default 0.2.")

        # ── Polish sub-section ──
        pol_f = _section(p, "Polish")
        pol_f.columnconfigure(1, weight=1)

        self._polish_cb = ttk.Checkbutton(
            pol_f, text="Post-anneal polish", variable=self._place_polish)
        self._polish_cb.grid(row=0, column=0, columnspan=2, sticky=tk.W,
                             padx=4, pady=2)
        add_tooltip(self._polish_cb,
                    "After placement annealing, run a steepest-descent refinement "
                    "pass that slides each footprint to its local energy minimum "
                    "using finite-difference gradients and a backtracking line "
                    "search. Strictly monotone — can never worsen the annealed "
                    "result. Useful for tightening up a placement after SA has "
                    "found a good global configuration.")

        pol_iters_e = _entry(pol_f, self._place_polish_iters, width=8)
        _row(pol_f, 1, "Max sweeps:", pol_iters_e,
             "Maximum descent sweeps over all movable footprints (default 20). "
             "Each sweep tries every movable unit once.")

        pol_time_e = _entry(pol_f, self._place_polish_time, width=8)
        _row(pol_f, 2, "Time cap (s):", pol_time_e,
             "Optional wall-clock cap in seconds for the polish stage. "
             "Leave blank for no cap.")

        pol_eps_e = _entry(pol_f, self._place_polish_eps, width=8)
        _row(pol_f, 3, "Eps (mm):", pol_eps_e,
             "Finite-difference step (mm) used to estimate the translation "
             "gradient (default 0.05). Smaller values give a more accurate "
             "gradient but are noisier for small energy differences.")

        # ── Cycles / Congestion sub-section ──
        cyc_f = _section(p, "Cycles & Congestion")
        cyc_f.columnconfigure(1, weight=1)

        cyc_e = _entry(cyc_f, self._cycles, width=6)
        _row(cyc_f, 0, "Cycles:", cyc_e,
             "Run N independent place+route cycles and keep the one that *routes* "
             "best (fewest unrouted, then lowest energy). 1 = single pass.")

        self._scatter_cb = ttk.Checkbutton(
            cyc_f, text="Scatter start", variable=self._scatter_start)
        self._scatter_cb.grid(row=1, column=0, columnspan=2, sticky=tk.W,
                              padx=4, pady=2)
        add_tooltip(self._scatter_cb,
                    "With Cycles > 1: randomise every unlocked footprint's position "
                    "and rotation before each cycle's placement pass, so the annealer "
                    "explores completely different starting layouts rather than always "
                    "refining the as-designed configuration. Increases diversity; pair "
                    "with a generous placement budget.")

        self._feedback_cb = ttk.Checkbutton(
            cyc_f, text="Congestion feedback", variable=self._place_feedback)
        self._feedback_cb.grid(row=2, column=0, columnspan=2, sticky=tk.W,
                               padx=4, pady=2)
        add_tooltip(self._feedback_cb,
                    "With Cycles > 1: feed each cycle's routing back into the next "
                    "placement, spreading footprints out of the cells where routing "
                    "struggled (PathFinder-style). Cycles then run sequentially.")

        cw_e = _entry(cyc_f, self._congestion_weight, width=8)
        _row(cyc_f, 3, "Congestion wt:", cw_e,
             "With Congestion feedback: how hard to spread parts out of the routed "
             "hot zones (cost per unit congestion at a footprint centroid; "
             "default 5.0).")

    def _build_buttons(self, parent):
        bf2 = ttk.Frame(parent)
        bf2.pack(fill=tk.X, padx=6, pady=(4, 2))
        self._run_btn = ttk.Button(bf2, text="Run",
                                    command=self._do_run, style="Accent.TButton")
        self._run_btn.pack(side=tk.LEFT, padx=2)
        self._stop_btn = ttk.Button(bf2, text="Stop",
                                     command=self._on_stop, state=tk.DISABLED)
        self._stop_btn.pack(side=tk.LEFT, padx=2)
        add_tooltip(self._run_btn, "Start the place/route pipeline. (Cmd+R)")
        add_tooltip(self._stop_btn, "Stop the running pipeline, keeping the best result so far.")

        af = ttk.Frame(parent)
        af.pack(fill=tk.X, padx=6, pady=2)
        self._apply_btn = ttk.Button(af, text="Apply to Project",
                                      command=self._on_apply,
                                      state=tk.DISABLED)
        self._apply_btn.pack(side=tk.LEFT, padx=2)
        add_tooltip(self._apply_btn,
                    "Back up the original .kicad_pcb and replace it with the "
                    "routed result. Requires a completed run.")
        self._save_as_btn = ttk.Button(af, text="Save As…",
                                        command=lambda: self._on_save_as and self._on_save_as(),
                                        state=tk.DISABLED)
        self._save_as_btn.pack(side=tk.LEFT, padx=2)
        add_tooltip(self._save_as_btn,
                    "Save the routed result to a chosen file. Requires a completed run.")
        self._save_constraints_btn = ttk.Button(af, text="Save Constraints",
                                                 command=self._on_save_constraints,
                                                 state=tk.DISABLED)
        self._save_constraints_btn.pack(side=tk.LEFT, padx=2)
        add_tooltip(self._save_constraints_btn,
                    "Write per-footprint constraint properties back to the .kicad_pcb.")

        sf2 = ttk.Frame(parent)
        sf2.pack(fill=tk.X, padx=6, pady=2)
        ttk.Button(sf2, text="Save Settings",
                   command=self._save_settings).pack(side=tk.LEFT, padx=2)
        ttk.Button(sf2, text="Load Settings",
                   command=self._load_settings).pack(side=tk.LEFT, padx=2)
        add_tooltip(sf2.winfo_children()[0], "Save current settings to an .ini file.")
        add_tooltip(sf2.winfo_children()[1], "Load settings from an .ini file.")

        adv_f = ttk.Frame(parent)
        adv_f.pack(fill=tk.X, padx=6, pady=(2, 6))
        ttk.Button(adv_f, text="Advanced…",
                   command=self._open_advanced).pack(side=tk.LEFT, padx=2)
        add_tooltip(adv_f.winfo_children()[0],
                    "Edit less common options: seed, weights, temperature schedules, "
                    "ground plane, post-processing.")

    # ── mode logic ───────────────────────────────────────────────────

    def _on_mode_change(self, *_):
        mode = self._mode.get()
        route_tab_idx = self._notebook.index(self._route_tab)
        place_tab_idx = self._notebook.index(self._place_tab)

        if mode == "route":
            self._notebook.tab(route_tab_idx, state="normal")
            self._notebook.tab(place_tab_idx, state="disabled")
            self._notebook.select(route_tab_idx)
        elif mode == "place_only":
            self._notebook.tab(route_tab_idx, state="disabled")
            self._notebook.tab(place_tab_idx, state="normal")
            self._notebook.select(place_tab_idx)
        else:  # place_route
            self._notebook.tab(route_tab_idx, state="normal")
            self._notebook.tab(place_tab_idx, state="normal")

    # ── file browse ──────────────────────────────────────────────────

    def _browse_board(self):
        path = filedialog.askopenfilename(
            title="Open KiCad board",
            filetypes=[("KiCad PCB", "*.kicad_pcb"), ("All files", "*.*")])
        if not path:
            return
        self.set_input(path)
        if self._on_open:
            self._on_open(path)

    def set_input(self, path: str) -> None:
        p = Path(path)
        self._input_path.set(p.name)
        # Auto-detect .pro
        pro = p.with_suffix(".kicad_pro")
        if not pro.exists():
            pro = p.with_name(p.stem + ".kicad_pro")
        self._pro_path.set(pro.name if pro.exists() else "(not found)")
        self._full_input = str(p)
        # Auto-load the project config <stem>.ini, if present.
        proj_ini = p.with_suffix(".ini")
        if proj_ini.exists():
            self._load_cfg(str(proj_ini))

    def _full_input_path(self) -> str | None:
        return getattr(self, "_full_input", None)

    # ── settings I/O ─────────────────────────────────────────────────

    def _save_settings(self):
        inp = self._full_input_path()
        init = str(Path(inp).with_suffix(".ini")) if inp else ""
        path = filedialog.asksaveasfilename(
            title="Save settings",
            initialfile=Path(init).stem if init else "",
            initialdir=str(Path(init).parent) if init else ".",
            defaultextension=".ini",
            filetypes=[("INI config", "*.ini"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            from pyautoroute.autoroute import build_parser, write_config
            parser = build_parser()
            args = self._to_namespace(parser)
            write_config(parser, args, path)
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))

    def _load_settings(self):
        path = filedialog.askopenfilename(
            title="Load settings",
            filetypes=[("INI config", "*.ini"),
                       ("All files", "*.*")])
        if path:
            self._load_cfg(path)

    def _load_cfg(self, path: str) -> None:
        try:
            from pyautoroute.autoroute import build_parser, load_config
            parser = build_parser()
            d = load_config(path, parser)
            self._apply_dict(d)
        except Exception as exc:
            messagebox.showerror("Load failed", str(exc))

    def _apply_dict(self, d: dict) -> None:
        """Apply a {dest: value} dict (from load_config) to the UI vars."""
        if "place" in d and d["place"]:
            self._mode.set("place_route")
        if "place_only" in d and d["place_only"]:
            self._mode.set("place_only")
        _sv = lambda key, var: var.set(str(d[key])) if key in d and d[key] is not None else None
        _sv("grid", self._grid)
        _sv("runs", self._runs)
        if "routing_iters" in d and d["routing_iters"]:
            self._budget_kind.set("iters")
            self._budget_val.set(str(d["routing_iters"]))
        if "routing_time" in d and d["routing_time"]:
            self._budget_kind.set("time")
            self._budget_val.set(str(d["routing_time"]))
        if "exclude_net" in d and d["exclude_net"]:
            self._exclude_net.set(", ".join(d["exclude_net"]))
        _sv("place_runs", self._place_runs)
        _sv("cycles", self._cycles)
        _sv("congestion_weight", self._congestion_weight)
        if "scatter" in d:
            self._scatter_start.set(bool(d["scatter"]))
        if "place_feedback" in d:
            self._place_feedback.set(bool(d["place_feedback"]))
        _sv("place_margin", self._place_margin)
        _sv("place_buffer", self._place_buffer)
        if "place_rotate" in d and d["place_rotate"]:
            self._place_rotate.set(d["place_rotate"])
        _sv("place_swap_prob", self._place_swap_prob)
        if "place_polish" in d:
            self._place_polish.set(bool(d["place_polish"]))
        _sv("place_polish_iters", self._place_polish_iters)
        _sv("place_polish_time", self._place_polish_time)
        _sv("place_polish_eps", self._place_polish_eps)
        if "place_iters" in d and d["place_iters"]:
            self._place_budget_kind.set("iters")
            self._place_budget_val.set(str(d["place_iters"]))
        if "place_time" in d and d["place_time"]:
            self._place_budget_kind.set("time")
            self._place_budget_val.set(str(d["place_time"]))
        _sv("seed", self._seed)
        _sv("via_weight", self._via_weight)
        _sv("unrouted_weight", self._unrouted_weight)
        if "anneal_temps" in d and d["anneal_temps"]:
            self._anneal_t_start.set(str(d["anneal_temps"][0]))
            self._anneal_t_end.set(str(d["anneal_temps"][1]))
        if "place_temps" in d and d["place_temps"]:
            self._place_t_start.set(str(d["place_temps"][0]))
            self._place_t_end.set(str(d["place_temps"][1]))
        _sv("place_step", self._place_step)
        _sv("place_overlap_weight", self._place_ow)
        _sv("place_compact_weight", self._place_cw)
        _sv("place_edge_weight", self._place_ew)
        if "log" in d:
            self._log.set(d["log"] is not None)
        if "quiet" in d:
            self._quiet.set(bool(d["quiet"]))
        if "silk_labels" in d:
            self._silk_labels.set(bool(d["silk_labels"]))
        if "keep_outline" in d:
            self._keep_outline.set(bool(d["keep_outline"]))
        if "ground_plane" in d:
            self._ground_plane.set(bool(d["ground_plane"]))
        _sv("ground_net", self._ground_net)
        if "ground_plane_layer" in d and d["ground_plane_layer"]:
            self._ground_plane_layer.set(d["ground_plane_layer"])
        _sv("ground_plane_margin", self._ground_plane_margin)
        _sv("stitch_vias", self._stitch_vias)
        if "existing_routes" in d and d["existing_routes"] in ("clear", "preserve"):
            self._existing_routes.set(d["existing_routes"])
        if "greedy_order" in d and d["greedy_order"] in ("short", "long", "shuffle"):
            self._greedy_order.set(d["greedy_order"])

    # ── advanced dialog ───────────────────────────────────────────────

    def _open_advanced(self):
        dlg = tk.Toplevel(self)
        dlg.title("Advanced Settings")
        dlg.resizable(True, True)
        outer = ttk.Frame(dlg, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        # Two-column top section
        cols = ttk.Frame(outer)
        cols.pack(fill=tk.X, pady=(0, 6))
        cols.columnconfigure(0, weight=1)
        cols.columnconfigure(1, weight=1)

        # ── Left column: Routing ──
        r_lf = ttk.LabelFrame(cols, text="Routing", padding=(8, 4))
        r_lf.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 4))
        r_lf.columnconfigure(1, weight=1)

        routing_rows = [
            ("Via weight:",       self._via_weight,
             "Via cost in mm-equivalent. Higher discourages vias."),
            ("Unrouted weight:",  self._unrouted_weight,
             "Annealing penalty per unrouted connection. Higher pushes harder to complete routes."),
            ("Anneal T start:",   self._anneal_t_start,
             "Annealing start temperature (higher = more random exploration)."),
            ("Anneal T end:",     self._anneal_t_end,
             "Annealing end temperature (lower = fine-tuning)."),
            ("Seed:",             self._seed,
             "Random seed (0 = random each run)."),
        ]
        for r, (lbl, var, tip) in enumerate(routing_rows):
            ttk.Label(r_lf, text=lbl).grid(row=r, column=0, sticky=tk.W,
                                            padx=4, pady=2)
            e = ttk.Entry(r_lf, textvariable=var, width=10)
            e.grid(row=r, column=1, sticky=tk.EW, padx=4, pady=2)
            add_tooltip(e, tip)

        # ── Right column: Placement ──
        p_lf = ttk.LabelFrame(cols, text="Placement", padding=(8, 4))
        p_lf.grid(row=0, column=1, sticky=tk.NSEW, padx=(4, 0))
        p_lf.columnconfigure(1, weight=1)

        placement_rows = [
            ("Place T start:",   self._place_t_start,
             "Placement annealing start temperature."),
            ("Place T end:",     self._place_t_end,
             "Placement annealing end temperature."),
            ("Step (mm):",       self._place_step,
             "Max footprint translate distance at start temperature."),
            ("Overlap wt:",      self._place_ow,
             "Placement cost per mm² of footprint overlap."),
            ("Compact wt:",      self._place_cw,
             "Placement cost per mm² of bounding-box area (encourages compactness)."),
            ("Edge wt:",         self._place_ew,
             "Cost per mm an edge-flagged footprint sits from its board edge."),
        ]
        for r, (lbl, var, tip) in enumerate(placement_rows):
            ttk.Label(p_lf, text=lbl).grid(row=r, column=0, sticky=tk.W,
                                            padx=4, pady=2)
            e = ttk.Entry(p_lf, textvariable=var, width=10)
            e.grid(row=r, column=1, sticky=tk.EW, padx=4, pady=2)
            add_tooltip(e, tip)

        # ── Post-processing (full width) ──
        pp_lf = ttk.LabelFrame(outer, text="Post-processing", padding=(8, 4))
        pp_lf.pack(fill=tk.X, pady=(0, 6))
        pp_lf.columnconfigure(1, weight=1)
        pp_lf.columnconfigure(3, weight=1)

        cb_fv = ttk.Checkbutton(pp_lf, text="Silk labels",
                                 variable=self._silk_labels)
        cb_fv.grid(row=0, column=0, columnspan=2, sticky=tk.W, padx=4, pady=2)
        add_tooltip(cb_fv,
                    "Move footprint Value text to silkscreen (F.SilkS / B.SilkS) "
                    "and Reference text to fab (F.Fab / B.Fab) before routing.")

        cb_ko = ttk.Checkbutton(pp_lf, text="Keep board outline",
                                variable=self._keep_outline)
        cb_ko.grid(row=0, column=2, columnspan=2, sticky=tk.W, padx=4, pady=2)
        add_tooltip(cb_ko,
                    "During placement, contain footprints within the board's "
                    "existing Edge.Cuts instead of regenerating a bounding box.")

        cb_gp = ttk.Checkbutton(pp_lf, text="Ground plane",
                                variable=self._ground_plane)
        cb_gp.grid(row=1, column=0, columnspan=4, sticky=tk.W, padx=4, pady=(6, 2))
        add_tooltip(cb_gp,
                    "After routing, emit a GND copper pour zone boundary. "
                    "KiCad computes the fill.")

        # Ground plane sub-fields (indented)
        gp_inner = ttk.Frame(pp_lf)
        gp_inner.grid(row=2, column=0, columnspan=4, sticky=tk.EW, padx=(20, 4), pady=(0, 4))
        gp_inner.columnconfigure(1, weight=1)
        gp_inner.columnconfigure(3, weight=1)

        ttk.Label(gp_inner, text="Net:").grid(row=0, column=0, sticky=tk.W, padx=4, pady=2)
        e_gnet = ttk.Entry(gp_inner, textvariable=self._ground_net, width=12)
        e_gnet.grid(row=0, column=1, sticky=tk.EW, padx=4, pady=2)
        add_tooltip(e_gnet, "Net name (e.g. GND). Leave empty to auto-detect.")

        ttk.Label(gp_inner, text="Layer:").grid(row=0, column=2, sticky=tk.W, padx=(12, 4), pady=2)
        layer_f = ttk.Frame(gp_inner)
        layer_f.grid(row=0, column=3, sticky=tk.EW, padx=4, pady=2)
        for lyr in ("B.Cu", "F.Cu", "both"):
            ttk.Radiobutton(layer_f, text=lyr,
                            variable=self._ground_plane_layer,
                            value=lyr).pack(side=tk.LEFT, padx=2)
        add_tooltip(layer_f, "Layer(s) for the ground pour.")

        ttk.Label(gp_inner, text="Margin (mm):").grid(row=1, column=0, sticky=tk.W, padx=4, pady=2)
        e_margin = ttk.Entry(gp_inner, textvariable=self._ground_plane_margin, width=10)
        e_margin.grid(row=1, column=1, sticky=tk.EW, padx=4, pady=2)
        add_tooltip(e_margin,
                    "Inset from board outline. Leave empty for board default clearance.")

        ttk.Label(gp_inner, text="Stitch vias (mm):").grid(row=1, column=2, sticky=tk.W,
                                                             padx=(12, 4), pady=2)
        e_stitch = ttk.Entry(gp_inner, textvariable=self._stitch_vias, width=10)
        e_stitch.grid(row=1, column=3, sticky=tk.EW, padx=4, pady=2)
        add_tooltip(e_stitch,
                    "Pitch for a regular grid of stitching vias. Leave empty to disable.")

        # ── Output (full width) ──
        os_lf = ttk.LabelFrame(outer, text="Output", padding=(8, 4))
        os_lf.pack(fill=tk.X, pady=(0, 6))
        os_lf.columnconfigure(1, weight=1)
        os_lf.columnconfigure(3, weight=1)

        cb_log = ttk.Checkbutton(os_lf, text="Write log file", variable=self._log)
        cb_log.grid(row=0, column=0, columnspan=2, sticky=tk.W, padx=4, pady=2)
        add_tooltip(cb_log, "Write a verbose log (<output>.log).")

        cb_q = ttk.Checkbutton(os_lf, text="Quiet (no progress)", variable=self._quiet)
        cb_q.grid(row=0, column=2, columnspan=2, sticky=tk.W, padx=4, pady=2)
        add_tooltip(cb_q, "Suppress live progress output.")

        ttk.Button(outer, text="OK", command=dlg.destroy).pack(pady=(4, 0))
        dlg.transient(self)
        dlg.grab_set()
        self.wait_window(dlg)

    # ── run state ────────────────────────────────────────────────────

    def set_running(self, running: bool) -> None:
        self._run_btn.configure(state=tk.DISABLED if running else tk.NORMAL)
        self._stop_btn.configure(state=tk.NORMAL if running else tk.DISABLED)

    def set_apply_enabled(self, enabled: bool) -> None:
        self._apply_btn.configure(
            state=tk.NORMAL if enabled else tk.DISABLED)
        self._save_as_btn.configure(
            state=tk.NORMAL if enabled else tk.DISABLED)

    def set_save_constraints_enabled(self, enabled: bool) -> None:
        self._save_constraints_btn.configure(
            state=tk.NORMAL if enabled else tk.DISABLED)

    # ── collect config ────────────────────────────────────────────────

    def _do_run(self):
        inp = self._full_input_path()
        if not inp:
            messagebox.showwarning("No board", "Please open a .kicad_pcb file first.")
            return
        cfg = self.get_run_config()
        self._on_run(cfg)

    def exclude_nets(self) -> list[str]:
        """The current exclude-net patterns (comma/space-split), for display use."""
        raw = self._exclude_net.get().strip()
        return [x.strip() for x in raw.split(",") if x.strip()] if raw else []

    def get_run_config(self) -> RunConfig:
        def _f(var, default=None):
            s = var.get().strip()
            if not s:
                return default
            try:
                return float(s)
            except ValueError:
                return default

        def _i(var, default=None):
            s = var.get().strip()
            if not s:
                return default
            try:
                return int(s)
            except ValueError:
                return default

        mode = self._mode.get()
        bk = self._budget_kind.get()
        bv = _f(self._budget_val)
        pbk = self._place_budget_kind.get()
        pbv = _f(self._place_budget_val)
        excl = self.exclude_nets()

        return RunConfig(
            input=self._full_input_path(),
            pro=None,
            output=None,
            place=(mode == "place_route"),
            place_only=(mode == "place_only"),
            grid=_f(self._grid),
            iters=_i(self._budget_val) if bk == "iters" and bv else None,
            time_budget=bv if bk == "time" else None,
            runs=_i(self._runs, 1),
            exclude_net=excl,
            via_weight=_f(self._via_weight, 2.0),
            unrouted_weight=_f(self._unrouted_weight, 100.0),
            anneal_temps=[_f(self._anneal_t_start, 4.0),
                          _f(self._anneal_t_end, 0.05)],
            seed=_i(self._seed, 0),
            place_iters=_i(self._place_budget_val) if pbk == "iters" and pbv else None,
            place_time=pbv if pbk == "time" else None,
            place_margin=_f(self._place_margin, 2.0),
            place_buffer=_f(self._place_buffer),
            place_overlap_weight=_f(self._place_ow, 20.0),
            place_compact_weight=_f(self._place_cw, 0.02),
            place_edge_weight=_f(self._place_ew, 2.0),
            place_temps=[_f(self._place_t_start, 8.0),
                         _f(self._place_t_end, 0.05)],
            place_step=_f(self._place_step, 20.0),
            place_rotate=self._place_rotate.get() or "ortho",
            place_swap_prob=_f(self._place_swap_prob, 0.2),
            place_runs=_i(self._place_runs, 1),
            place_polish=self._place_polish.get(),
            place_polish_iters=_i(self._place_polish_iters, 20),
            place_polish_time=_f(self._place_polish_time),
            place_polish_eps=_f(self._place_polish_eps, 0.05),
            cycles=_i(self._cycles, 1),
            scatter_start=self._scatter_start.get(),
            place_feedback=self._place_feedback.get(),
            congestion_weight=_f(self._congestion_weight, 5.0),
            snapshots=0,
            quiet=self._quiet.get(),
            log="" if self._log.get() else None,
            auto=False,
            auto_yes=False,
            silk_labels=self._silk_labels.get(),
            keep_outline=self._keep_outline.get(),
            ground_plane=self._ground_plane.get(),
            ground_net=self._ground_net.get() or None,
            ground_plane_layer=self._ground_plane_layer.get(),
            ground_plane_margin=_f(self._ground_plane_margin),
            stitch_vias=_f(self._stitch_vias),
            existing_routes=self._existing_routes.get() or "clear",
            greedy_order=self._greedy_order.get() or "short",
        )

    def _to_namespace(self, parser) -> argparse.Namespace:
        """Convert current UI state to an argparse.Namespace for write_config."""
        cfg = self.get_run_config()
        inp = self._full_input_path() or "board.kicad_pcb"
        return argparse.Namespace(
            input=inp,
            pro=cfg.pro,
            output=cfg.output,
            place=cfg.place,
            place_only=cfg.place_only,
            grid=cfg.grid,
            routing_iters=cfg.iters,
            routing_time=cfg.time_budget,
            runs=cfg.runs or 1,
            exclude_net=cfg.exclude_net or [],
            via_weight=cfg.via_weight or 2.0,
            unrouted_weight=cfg.unrouted_weight or 100.0,
            anneal_temps=cfg.anneal_temps or [4.0, 0.05],
            seed=cfg.seed or 0,
            place_iters=cfg.place_iters,
            place_time=cfg.place_time,
            place_margin=cfg.place_margin or 2.0,
            place_buffer=cfg.place_buffer,
            place_overlap_weight=cfg.place_overlap_weight or 20.0,
            place_compact_weight=cfg.place_compact_weight or 0.02,
            place_edge_weight=cfg.place_edge_weight or 2.0,
            place_temps=cfg.place_temps or [8.0, 0.05],
            place_step=cfg.place_step or 20.0,
            place_rotate=cfg.place_rotate or "ortho",
            place_swap_prob=cfg.place_swap_prob or 0.2,
            place_runs=cfg.place_runs or 1,
            place_polish=cfg.place_polish or False,
            place_polish_iters=cfg.place_polish_iters or 20,
            place_polish_time=cfg.place_polish_time,
            place_polish_eps=cfg.place_polish_eps or 0.05,
            cycles=cfg.cycles or 1,
            scatter=cfg.scatter_start or False,
            place_feedback=cfg.place_feedback or False,
            congestion_weight=cfg.congestion_weight or 5.0,
            snapshots=0,
            quiet=cfg.quiet,
            log=cfg.log,
            auto=False,
            auto_yes=False,
            silk_labels=cfg.silk_labels or False,
            keep_outline=cfg.keep_outline or False,
            ground_plane=cfg.ground_plane or False,
            ground_net=cfg.ground_net,
            ground_plane_layer=cfg.ground_plane_layer or "B.Cu",
            ground_plane_margin=cfg.ground_plane_margin,
            stitch_vias=cfg.stitch_vias,
            existing_routes=cfg.existing_routes or "clear",
            greedy_order=cfg.greedy_order or "short",
        )
