"""Settings panel: grouped controls auto-typed from the argparse parser."""

from __future__ import annotations

import argparse
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
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

class RunConfig:
    """Plain container for all pipeline parameters (mirrors argparse Namespace)."""
    __slots__ = (
        "input", "pro", "output",
        "place", "place_only",
        "grid",
        "iters", "time_budget",
        "runs",
        "exclude_net",
        "via_weight", "unrouted_weight",
        "anneal_temps",
        "seed",
        "place_iters", "place_time",
        "place_margin", "place_buffer",
        "place_overlap_weight", "place_compact_weight", "place_edge_weight",
        "place_temps", "place_step", "place_rotate",
        "place_runs",
        "cycles", "place_feedback", "congestion_weight",
        "snapshots",
        "quiet", "log",
        "auto", "auto_yes", "auto_probe_time",
        "fix_values", "keep_outline",
        "ground_plane", "ground_net", "ground_plane_layer",
        "ground_plane_margin", "stitch_vias",
        "existing_routes",
    )

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


# ── ControlsPanel ────────────────────────────────────────────────────────────

class ControlsPanel(ttk.Frame):
    """Left-side scrollable settings panel.

    Args:
        parent: the parent widget.
        on_run: callback(RunConfig) invoked when Run is pressed.
        on_stop: callback() invoked when Stop is pressed.
        on_apply: callback() invoked when Apply to Project is pressed.
        on_suggest: callback() invoked when Suggest is pressed.
    """

    def __init__(self, parent,
                 on_run: Callable, on_stop: Callable,
                 on_apply: Callable, on_suggest: Callable,
                 on_open: Callable | None = None,
                 on_save_constraints: Callable | None = None,
                 **kw):
        super().__init__(parent, **kw)
        self._on_run = on_run
        self._on_stop = on_stop
        self._on_apply = on_apply
        self._on_suggest = on_suggest
        self._on_open = on_open
        self._on_save_constraints = on_save_constraints

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
        # Placement
        self._place_budget_kind = tk.StringVar(value="iters")
        self._place_budget_val = tk.StringVar(value="")
        self._place_runs = tk.StringVar(value="1")
        self._place_rotate = tk.StringVar(value="ortho")
        self._place_margin = tk.StringVar(value="2.0")
        self._place_buffer = tk.StringVar(value="")
        # Best-of-cycles + congestion feedback (place+route outer loop)
        self._cycles = tk.StringVar(value="1")
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
        self._place_ow = tk.StringVar(value="20.0")
        self._place_cw = tk.StringVar(value="0.02")
        self._place_ew = tk.StringVar(value="2.0")
        self._snapshots = tk.StringVar(value="0")
        self._auto_probe_time = tk.StringVar(value="3.0")
        self._fix_values = tk.BooleanVar(value=False)
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
        sf = _ScrollFrame(self)
        sf.pack(fill=tk.BOTH, expand=True)
        p = sf.inner

        # Board section
        bf = _section(p, "Board")
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

        # Mode section
        mf = _section(p, "Mode")
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

        # Routing section
        self._route_frame = rf = _section(p, "Routing")
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

        # Placement section
        self._place_frame = pf = _section(p, "Placement")
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
        cyc_e = _entry(pf, self._cycles, width=6)
        _row(pf, 6, "Cycles:", cyc_e,
             "Run N independent place+route cycles and keep the one that *routes* "
             "best (fewest unrouted, then lowest energy) — selecting on the true "
             "objective. 1 = single pass. The recommended knob for a better board.")
        self._feedback_cb = ttk.Checkbutton(
            pf, text="Congestion feedback", variable=self._place_feedback)
        self._feedback_cb.grid(row=7, column=0, columnspan=2, sticky=tk.W,
                               padx=4, pady=2)
        add_tooltip(self._feedback_cb,
                    "With Cycles > 1: feed each cycle's routing back into the next "
                    "placement, spreading footprints out of the cells where routing "
                    "struggled (PathFinder-style). Cycles then run sequentially.")
        cw_e = _entry(pf, self._congestion_weight, width=8)
        _row(pf, 8, "Congestion wt:", cw_e,
             "With Congestion feedback: how hard to spread parts out of the routed "
             "hot zones (cost per unit congestion at a footprint centroid; "
             "default 5.0).")

        # Output section
        of = _section(p, "Output")
        cb_log = ttk.Checkbutton(of, text="Write log file",
                                  variable=self._log)
        cb_log.grid(row=0, column=0, columnspan=2, sticky=tk.W, padx=4)
        add_tooltip(cb_log, "Write a verbose log (<output>.log).")
        cb_q = ttk.Checkbutton(of, text="Quiet (no progress)",
                                variable=self._quiet)
        cb_q.grid(row=1, column=0, columnspan=2, sticky=tk.W, padx=4)
        add_tooltip(cb_q, "Suppress live progress output.")

        # Button row
        bf2 = ttk.Frame(p)
        bf2.pack(fill=tk.X, padx=6, pady=6)
        self._run_btn = ttk.Button(bf2, text="Run",
                                    command=self._do_run, style="Accent.TButton")
        self._run_btn.pack(side=tk.LEFT, padx=2)
        self._stop_btn = ttk.Button(bf2, text="Stop",
                                     command=self._on_stop, state=tk.DISABLED)
        self._stop_btn.pack(side=tk.LEFT, padx=2)
        add_tooltip(self._run_btn, "Start the place/route pipeline.")
        add_tooltip(self._stop_btn, "Stop the running pipeline, keeping the best result so far.")

        af = ttk.Frame(p)
        af.pack(fill=tk.X, padx=6, pady=2)
        self._apply_btn = ttk.Button(af, text="Apply to Project",
                                      command=self._on_apply,
                                      state=tk.DISABLED)
        self._apply_btn.pack(side=tk.LEFT, padx=2)
        add_tooltip(self._apply_btn,
                    "Back up the original .kicad_pcb and replace it with the "
                    "routed result. Requires a completed run.")
        self._save_constraints_btn = ttk.Button(af, text="Save Constraints",
                                                 command=self._on_save_constraints,
                                                 state=tk.DISABLED)
        self._save_constraints_btn.pack(side=tk.LEFT, padx=2)
        add_tooltip(self._save_constraints_btn,
                    "Write per-footprint constraint properties back to the .kicad_pcb.")

        sf2 = ttk.Frame(p)
        sf2.pack(fill=tk.X, padx=6, pady=2)
        ttk.Button(sf2, text="Save Settings",
                   command=self._save_settings).pack(side=tk.LEFT, padx=2)
        ttk.Button(sf2, text="Load Settings",
                   command=self._load_settings).pack(side=tk.LEFT, padx=2)
        ttk.Button(sf2, text="Suggest…",
                   command=self._on_suggest).pack(side=tk.LEFT, padx=2)
        add_tooltip(sf2.winfo_children()[0],
                    "Save current settings to an .ini file.")
        add_tooltip(sf2.winfo_children()[1],
                    "Load settings from an .ini file.")
        add_tooltip(sf2.winfo_children()[2],
                    "Run --auto to probe grid/via settings and suggest the best.")

        adv_f = ttk.Frame(p)
        adv_f.pack(fill=tk.X, padx=6, pady=(2, 6))
        ttk.Button(adv_f, text="Advanced…",
                   command=self._open_advanced).pack(side=tk.LEFT, padx=2)
        add_tooltip(adv_f.winfo_children()[0],
                    "Edit less common options: seed, weights, temperature schedules.")

    # ── mode logic ───────────────────────────────────────────────────

    def _on_mode_change(self, *_):
        mode = self._mode.get()
        place_modes = ("place_route", "place_only")
        state = tk.NORMAL if mode in place_modes else tk.DISABLED
        for child in self._place_frame.winfo_children():
            try:
                child.configure(state=state)
            except tk.TclError:
                pass

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
            initialfile=Path(init).name if init else "",
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
            # provide a dummy input so the parser doesn't complain
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
        if "iters" in d and d["iters"]:
            self._budget_kind.set("iters")
            self._budget_val.set(str(d["iters"]))
        if "time_budget" in d and d["time_budget"]:
            self._budget_kind.set("time")
            self._budget_val.set(str(d["time_budget"]))
        if "exclude_net" in d and d["exclude_net"]:
            self._exclude_net.set(", ".join(d["exclude_net"]))
        _sv("place_runs", self._place_runs)
        _sv("cycles", self._cycles)
        _sv("congestion_weight", self._congestion_weight)
        if "place_feedback" in d:
            self._place_feedback.set(bool(d["place_feedback"]))
        _sv("place_margin", self._place_margin)
        _sv("place_buffer", self._place_buffer)
        if "place_rotate" in d and d["place_rotate"]:
            self._place_rotate.set(d["place_rotate"])
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
        _sv("auto_probe_time", self._auto_probe_time)
        if "quiet" in d:
            self._quiet.set(bool(d["quiet"]))
        if "fix_values" in d:
            self._fix_values.set(bool(d["fix_values"]))
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

    # ── advanced dialog ───────────────────────────────────────────────

    def _open_advanced(self):
        dlg = tk.Toplevel(self)
        dlg.title("Advanced Settings")
        dlg.resizable(False, False)
        f = ttk.Frame(dlg, padding=10)
        f.pack(fill=tk.BOTH)
        f.columnconfigure(1, weight=1)

        rows = [
            ("Seed:", self._seed,
             "Random seed for annealing."),
            ("Via weight:", self._via_weight,
             "Via cost in mm-equivalent. Higher discourages vias."),
            ("Unrouted weight:", self._unrouted_weight,
             "Annealing penalty per unrouted connection. Higher pushes harder to complete routes."),
            ("Anneal T start:", self._anneal_t_start,
             "Annealing start temperature (higher = more random exploration)."),
            ("Anneal T end:", self._anneal_t_end,
             "Annealing end temperature (lower = fine-tuning)."),
            ("Place T start:", self._place_t_start,
             "Placement annealing start temperature."),
            ("Place T end:", self._place_t_end,
             "Placement annealing end temperature."),
            ("Place step (mm):", self._place_step,
             "Max footprint translate distance at start temperature."),
            ("Place overlap wt:", self._place_ow,
             "Placement cost per mm² of footprint overlap."),
            ("Place compact wt:", self._place_cw,
             "Placement cost per mm² of bounding-box area (encourages compactness)."),
            ("Place edge wt:", self._place_ew,
             "Cost per mm an Autoroute-edge=<side> footprint sits from its board "
             "edge (pulls connectors out to the boundary and aligns them flat)."),
            ("Auto probe time:", self._auto_probe_time,
             "Annealing seconds per probed setting when using Suggest."),
        ]
        for r, (lbl, var, tip) in enumerate(rows):
            ttk.Label(f, text=lbl).grid(row=r, column=0, sticky=tk.W,
                                         padx=4, pady=2)
            e = ttk.Entry(f, textvariable=var, width=12)
            e.grid(row=r, column=1, sticky=tk.EW, padx=4, pady=2)
            add_tooltip(e, tip)

        cb_fv = ttk.Checkbutton(f, text="Fix Value layers",
                                 variable=self._fix_values)
        cb_fv.grid(row=len(rows), column=0, columnspan=2, sticky=tk.W,
                   padx=4, pady=4)
        add_tooltip(cb_fv,
                    "Move footprint Value text to the silkscreen layer "
                    "(F.SilkS / B.SilkS) before routing. Off by default.")

        cb_ko = ttk.Checkbutton(f, text="Keep board outline (--place)",
                                variable=self._keep_outline)
        cb_ko.grid(row=len(rows) + 1, column=0, columnspan=2, sticky=tk.W,
                   padx=4, pady=4)
        add_tooltip(cb_ko,
                    "During placement, contain footprints within the board's "
                    "existing Edge.Cuts instead of regenerating a bounding box "
                    "(needs a closed outline). Edge-flagged parts snap to it.")

        cb_gp = ttk.Checkbutton(f, text="Ground plane",
                                variable=self._ground_plane)
        cb_gp.grid(row=len(rows) + 2, column=0, columnspan=2, sticky=tk.W,
                   padx=4, pady=4)
        add_tooltip(cb_gp,
                    "After routing, emit a GND copper pour zone boundary. "
                    "KiCad computes the fill. Includes connecting vias for "
                    "isolated islands and optional stitching vias.")

        gp_inner = ttk.Frame(f)
        gp_inner.grid(row=len(rows) + 3, column=0, columnspan=2, sticky=tk.EW,
                      padx=20, pady=(0, 4))
        gp_inner.columnconfigure(1, weight=1)

        ttk.Label(gp_inner, text="Ground net:").grid(row=0, column=0,
                                                      sticky=tk.W, padx=4, pady=2)
        e_gnet = ttk.Entry(gp_inner, textvariable=self._ground_net, width=12)
        e_gnet.grid(row=0, column=1, sticky=tk.EW, padx=4, pady=2)
        add_tooltip(e_gnet, "Net name (e.g. GND). Leave empty to auto-detect.")

        ttk.Label(gp_inner, text="Pour layer:").grid(row=1, column=0,
                                                      sticky=tk.W, padx=4, pady=2)
        layer_f = ttk.Frame(gp_inner)
        layer_f.grid(row=1, column=1, sticky=tk.EW, padx=4, pady=2)
        ttk.Radiobutton(layer_f, text="B.Cu", variable=self._ground_plane_layer,
                        value="B.Cu").pack(side=tk.LEFT)
        ttk.Radiobutton(layer_f, text="F.Cu", variable=self._ground_plane_layer,
                        value="F.Cu").pack(side=tk.LEFT, padx=8)
        ttk.Radiobutton(layer_f, text="both", variable=self._ground_plane_layer,
                        value="both").pack(side=tk.LEFT)
        add_tooltip(layer_f, "Layer(s) for the ground pour.")

        ttk.Label(gp_inner, text="Margin (mm):").grid(row=2, column=0,
                                                       sticky=tk.W, padx=4, pady=2)
        e_margin = ttk.Entry(gp_inner, textvariable=self._ground_plane_margin, width=12)
        e_margin.grid(row=2, column=1, sticky=tk.EW, padx=4, pady=2)
        add_tooltip(e_margin,
                    "Inset distance from board outline. Leave empty to use "
                    "the board's default clearance.")

        ttk.Label(gp_inner, text="Stitch vias (mm):").grid(row=3, column=0,
                                                           sticky=tk.W, padx=4, pady=2)
        e_stitch = ttk.Entry(gp_inner, textvariable=self._stitch_vias, width=12)
        e_stitch.grid(row=3, column=1, sticky=tk.EW, padx=4, pady=2)
        add_tooltip(e_stitch,
                    "Optional pitch for a regular grid of stitching vias. "
                    "Leave empty to disable.")

        ttk.Button(f, text="OK", command=dlg.destroy).grid(
            row=len(rows) + 4, column=0, columnspan=2, pady=8)
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
            place_runs=_i(self._place_runs, 1),
            cycles=_i(self._cycles, 1),
            place_feedback=self._place_feedback.get(),
            congestion_weight=_f(self._congestion_weight, 5.0),
            snapshots=0,
            quiet=self._quiet.get(),
            log=None,
            auto=False,
            auto_yes=False,
            auto_probe_time=_f(self._auto_probe_time, 3.0),
            fix_values=self._fix_values.get(),
            keep_outline=self._keep_outline.get(),
            ground_plane=self._ground_plane.get(),
            ground_net=self._ground_net.get() or None,
            ground_plane_layer=self._ground_plane_layer.get(),
            ground_plane_margin=_f(self._ground_plane_margin),
            stitch_vias=_f(self._stitch_vias),
            existing_routes=self._existing_routes.get() or "clear",
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
            iters=cfg.iters,
            time_budget=cfg.time_budget,
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
            place_runs=cfg.place_runs or 1,
            cycles=cfg.cycles or 1,
            place_feedback=cfg.place_feedback or False,
            congestion_weight=cfg.congestion_weight or 5.0,
            snapshots=0,
            quiet=cfg.quiet,
            log=None,
            auto=False,
            auto_yes=False,
            auto_probe_time=cfg.auto_probe_time or 3.0,
            fix_values=cfg.fix_values or False,
            keep_outline=cfg.keep_outline or False,
            ground_plane=cfg.ground_plane or False,
            ground_net=cfg.ground_net,
            ground_plane_layer=cfg.ground_plane_layer or "B.Cu",
            ground_plane_margin=cfg.ground_plane_margin,
            stitch_vias=cfg.stitch_vias,
            existing_routes=cfg.existing_routes or "clear",
        )
