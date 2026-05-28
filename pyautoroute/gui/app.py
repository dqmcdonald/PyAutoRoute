"""Main application window for the PyAutoRoute GUI."""

from __future__ import annotations

import queue
import shutil
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

from pyautoroute import __version__

from .canvas import BoardCanvas
from .controls import ControlsPanel
from .events import BoardSnap, Done, Error, Phase, Progress
from .plots import EnergyPlot
from .worker import Worker


# How often the main thread drains the event queue (ms).
_DRAIN_MS = 50
# Min seconds between energy plot refreshes.
_PLOT_REFRESH_S = 0.2


class _MetricsPanel(ttk.Frame):
    """Right-side panel showing phase, iteration, energy, accept %, etc."""

    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        self._vars: dict[str, tk.StringVar] = {}
        rows = [
            ("phase",    "Phase:",      "Current pipeline phase"),
            ("elapsed",  "Elapsed:",    "Wall-clock time since Run was pressed"),
            ("iter",     "Iteration:",  "Current / total iterations"),
            ("energy",   "Energy:",     "Current / best energy"),
            ("accept",   "Accept %:",   "Fraction of recent annealing moves accepted"),
            ("temp",     "Temp:",       "Current annealing temperature"),
            ("routed",   "Routed:",     "Connections routed so far"),
            ("unrouted", "Unrouted:",   "Connections that could not be routed"),
            ("check",    "Self-check:", "DRC clearance violations after routing"),
        ]
        for i, (key, lbl, tip) in enumerate(rows):
            var = tk.StringVar(value="—")
            self._vars[key] = var
            ttk.Label(self, text=lbl, anchor=tk.E).grid(
                row=i, column=0, sticky=tk.E, padx=(6, 2), pady=2)
            ttk.Label(self, textvariable=var, anchor=tk.W,
                      foreground="#222").grid(
                row=i, column=1, sticky=tk.W, padx=(2, 6), pady=2)
        self.columnconfigure(1, weight=1)

        self._pb_var = tk.IntVar(value=0)
        pb = ttk.Progressbar(self, variable=self._pb_var, maximum=100,
                             orient=tk.HORIZONTAL, length=180, mode="determinate")
        pb.grid(row=len(rows), column=0, columnspan=2,
                padx=6, pady=(4, 2), sticky=tk.EW)
        self._pb = pb

        self._t0: float | None = None

    def reset(self) -> None:
        for var in self._vars.values():
            var.set("—")
        self._pb_var.set(0)
        self._t0 = time.monotonic()

    def set_phase(self, name: str) -> None:
        self._vars["phase"].set(name)

    def update(self, ev: Progress) -> None:
        if self._t0 is not None:
            self._vars["elapsed"].set(f"{time.monotonic() - self._t0:.1f}s")
        total = ev.total or 1
        pct = min(100, int(100 * ev.it / total))
        self._pb_var.set(pct)
        self._vars["iter"].set(f"{ev.it} / {ev.total}")
        if ev.kind in ("annealing", "placing"):
            self._vars["energy"].set(f"{ev.energy:.1f} / {ev.best:.1f}")
            self._vars["accept"].set(f"{ev.accept * 100:.0f}%")
            self._vars["temp"].set(f"{ev.temp:.3f}")
        if ev.kind in ("routing", "annealing"):
            self._vars["routed"].set(str(ev.routed))
            self._vars["unrouted"].set(str(ev.unrouted))

    def set_done(self, ev: Done) -> None:
        if self._t0 is not None:
            self._vars["elapsed"].set(f"{time.monotonic() - self._t0:.1f}s")
        self._pb_var.set(100)
        self._vars["routed"].set(str(ev.routed))
        self._vars["unrouted"].set(str(ev.unrouted))
        n_viol = len(ev.violations)
        self._vars["check"].set("PASS" if n_viol == 0
                                else f"{n_viol} violation(s)")

    def set_initial_stats(self, stats) -> None:
        """Populate the panel with pre-run board routing statistics."""
        for key in ("elapsed", "iter", "energy", "accept", "temp"):
            self._vars[key].set("—")
        self._pb_var.set(0)
        self._t0 = None
        self._vars["phase"].set("Initial board state")
        self._vars["routed"].set(
            f"{stats.routed}/{stats.total}  ({stats.length:.1f} mm, {stats.vias} vias)")
        self._vars["unrouted"].set(str(stats.unrouted))
        n_viol = len(stats.violations)
        self._vars["check"].set("PASS" if n_viol == 0
                                else f"{n_viol} violation(s)")


# ── App ───────────────────────────────────────────────────────────────────────

class App:
    """The main PyAutoRoute GUI window."""

    def __init__(self, initial_file: str | None = None):
        self._root = tk.Tk()
        self._root.title(f"PyAutoRoute {__version__}")
        self._root.minsize(900, 600)

        self._queue: queue.Queue = queue.Queue()
        self._cancel = threading.Event()
        self._worker: Worker | None = None
        self._last_done: Done | None = None
        self._last_plot_refresh = 0.0
        self._t_run_start = 0.0
        self._initial_board = None
        self._initial_stats = None
        self._show_initial = tk.BooleanVar(value=False)

        self._build_menu()
        self._build_layout()
        self._root.after(_DRAIN_MS, self._drain)

        if initial_file:
            self._controls.set_input(initial_file)
            self._open_board(initial_file)

    # ── layout ────────────────────────────────────────────────────────

    def _build_menu(self) -> None:
        mb = tk.Menu(self._root)
        self._root.config(menu=mb)

        fm = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="File", menu=fm)
        fm.add_command(label="Open Board…", command=self._menu_open,
                       accelerator="Cmd+O")
        fm.add_separator()
        fm.add_command(label="Save Settings", command=self._menu_save)
        fm.add_command(label="Load Settings", command=self._menu_load)
        fm.add_separator()
        fm.add_command(label="Quit", command=self._root.quit,
                       accelerator="Cmd+Q")
        self._root.bind("<Command-o>", lambda _: self._menu_open())
        self._root.bind("<Command-q>", lambda _: self._root.quit())

        rm = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="Run", menu=rm)
        rm.add_command(label="Run", command=self._menu_run)
        rm.add_command(label="Stop", command=self._cancel_run)

        hm = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="Help", menu=hm)
        hm.add_command(label="About", command=self._about)

    def _build_layout(self) -> None:
        # Main paned window (horizontal: controls | canvas | metrics)
        pw = ttk.PanedWindow(self._root, orient=tk.HORIZONTAL)
        pw.pack(fill=tk.BOTH, expand=True)

        # Left: controls
        self._controls = ControlsPanel(
            pw,
            on_run=self._start_run,
            on_stop=self._cancel_run,
            on_apply=self._apply_to_project,
            on_suggest=self._suggest,
            on_open=self._open_board,
        )
        pw.add(self._controls, weight=0)

        # Centre: board canvas + view toggle
        canvas_frame = ttk.Frame(pw)
        pw.add(canvas_frame, weight=3)
        self._board_canvas = BoardCanvas(canvas_frame)
        self._board_canvas.pack(fill=tk.BOTH, expand=True)
        view_bar = ttk.Frame(canvas_frame)
        view_bar.pack(fill=tk.X, padx=4, pady=2)
        self._toggle_cb = ttk.Checkbutton(
            view_bar, text="Show initial state",
            variable=self._show_initial,
            command=self._on_view_toggle,
            state=tk.DISABLED,
        )
        self._toggle_cb.pack(side=tk.LEFT)
        self._toggle_label = ttk.Label(view_bar, text="", foreground="#666")
        self._toggle_label.pack(side=tk.LEFT, padx=8)

        # Right: metrics + energy graph (vertical stack)
        right = ttk.Frame(pw)
        pw.add(right, weight=1)
        self._metrics = _MetricsPanel(right)
        self._metrics.pack(fill=tk.X)
        ttk.Separator(right, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=4)
        self._energy_plot = EnergyPlot(right)
        self._energy_plot.pack(fill=tk.BOTH, expand=True)

        # Bottom: status bar
        status_bar = ttk.Frame(self._root, relief=tk.SUNKEN)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        self._status_var = tk.StringVar(value="Ready")
        ttk.Label(status_bar, textvariable=self._status_var,
                  anchor=tk.W).pack(side=tk.LEFT, padx=6, pady=2)

    # ── queue drain ───────────────────────────────────────────────────

    def _drain(self) -> None:
        # Drain all pending events, but collapse redundant ones so the main
        # thread is never overwhelmed even if the worker posts rapidly:
        #   - keep only the last BoardSnap (rendering is expensive)
        #   - keep only the last Progress per kind (metrics just need latest)
        #   - keep all Phase / Done / Error events (rare, always process)
        events: list = []
        try:
            while True:
                events.append(self._queue.get_nowait())
        except queue.Empty:
            pass

        # Identify last BoardSnap and last Progress per kind.
        last_snap_idx: int | None = None
        last_prog_idx: dict[str, int] = {}
        for i, ev in enumerate(events):
            if isinstance(ev, BoardSnap):
                last_snap_idx = i
            elif isinstance(ev, Progress):
                last_prog_idx[ev.kind] = i

        for i, ev in enumerate(events):
            if isinstance(ev, BoardSnap) and i != last_snap_idx:
                continue
            if isinstance(ev, Progress) and last_prog_idx.get(ev.kind) != i:
                continue
            try:
                self._handle_event(ev)
            except Exception:
                pass  # don't let one bad event kill the drain loop

        # Update elapsed time label while running
        if self._worker is not None and not self._worker.join(0):
            elapsed = time.monotonic() - self._t_run_start
            self._metrics._vars["elapsed"].set(f"{elapsed:.1f}s")
        self._root.after(_DRAIN_MS, self._drain)

    def _handle_event(self, event) -> None:
        if isinstance(event, Phase):
            self._status_var.set(event.name)
            self._metrics.set_phase(event.name)
        elif isinstance(event, Progress):
            self._metrics.update(event)
            self._energy_plot.add_point(event.it, event.energy, event.best)
            now = time.monotonic()
            if now - self._last_plot_refresh > _PLOT_REFRESH_S:
                self._energy_plot.refresh()
                self._last_plot_refresh = now
        elif isinstance(event, BoardSnap):
            self._board_canvas.show_board(
                event.board, event.results, event.grid)
        elif isinstance(event, Done):
            self._on_done(event)
        elif isinstance(event, Error):
            self._on_error(event)

    # ── run / stop ────────────────────────────────────────────────────

    def _start_run(self, cfg) -> None:
        if self._worker and not self._worker.join(0):
            return  # already running
        self._cancel.clear()
        self._worker = Worker(self._queue, self._cancel)
        self._controls.set_running(True)
        self._controls.set_apply_enabled(False)
        self._last_done = None
        self._show_initial.set(False)
        self._toggle_cb.configure(state=tk.DISABLED)
        self._toggle_label.configure(text="")
        self._metrics.reset()
        self._energy_plot.reset()
        self._t_run_start = time.monotonic()
        self._status_var.set("Starting…")
        self._worker.start(cfg)

    def _cancel_run(self) -> None:
        self._cancel.set()
        self._status_var.set("Stopping (keeping best so far)…")

    def _on_done(self, ev: Done) -> None:
        self._last_done = ev
        self._controls.set_running(False)
        self._controls.set_apply_enabled(True)
        self._energy_plot.refresh()
        self._metrics.set_done(ev)
        n_viol = len(ev.violations)
        check = "PASS" if n_viol == 0 else f"{n_viol} clearance violation(s)"
        msg = (f"Done — {ev.routed}/{ev.total} routed, "
               f"{ev.length:.0f} mm, {ev.vias} vias.  {check}")
        self._status_var.set(msg)
        # Enable initial/final toggle when we have both states
        if self._initial_board is not None:
            self._toggle_cb.configure(state=tk.NORMAL)
            if self._initial_stats is not None:
                s = self._initial_stats
                self._toggle_label.configure(
                    text=f"Initial: {s.routed}/{s.total} routed, "
                         f"{s.length:.0f} mm, {s.vias} vias")
        if n_viol:
            messagebox.showwarning("Self-check",
                                   f"{n_viol} clearance violation(s) found.\n"
                                   f"Review the board in KiCad.")

    def _on_error(self, ev: Error) -> None:
        self._controls.set_running(False)
        self._status_var.set(f"Error: {ev.exc}")
        messagebox.showerror("Pipeline error",
                             f"{ev.exc}\n\n{ev.tb}")

    # ── apply to project ──────────────────────────────────────────────

    def _apply_to_project(self) -> None:
        if self._last_done is None:
            return
        out_path = Path(self._last_done.out_path)
        inp = self._controls._full_input_path()
        if not inp:
            return
        orig = Path(inp)
        if not orig.exists():
            messagebox.showerror("Apply failed",
                                 f"Original not found: {orig}")
            return

        # Confirm
        if not messagebox.askyesno(
                "Apply to project",
                f"Replace\n  {orig.name}\nwith the routed result?\n\n"
                f"A backup will be created."):
            return

        # Create timestamped backup
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = orig.with_suffix(f".{ts}.kicad_pcb.bak")
        try:
            shutil.copy2(orig, bak)
            shutil.copy2(out_path, orig)
        except OSError as exc:
            messagebox.showerror("Apply failed", str(exc))
            return

        messagebox.showinfo("Applied",
                            f"Replaced {orig.name}.\n"
                            f"Backup: {bak.name}")
        self._status_var.set(f"Applied — backup: {bak.name}")

    # ── suggest (--auto) ─────────────────────────────────────────────

    def _suggest(self) -> None:
        inp = self._controls._full_input_path()
        if not inp:
            messagebox.showwarning("No board", "Open a board first.")
            return
        messagebox.showinfo(
            "Suggest",
            "Suggest will probe several grid/via combinations and recommend "
            "the best settings.\n\nThis runs in the background and may take "
            "~30–60 seconds for large boards.\n\n"
            "(Full implementation: apply suggested grid/via-weight to controls "
            "and show results.)")

    # ── board open ────────────────────────────────────────────────────

    def _open_board(self, path: str) -> None:
        try:
            from pyautoroute import pcb
            from pyautoroute.report import routing_stats
            from pyautoroute.rules import load_rules
            board = pcb.load_board(path)
            self._initial_board = board
            self._initial_stats = None
            self._last_done = None
            self._show_initial.set(False)
            self._toggle_cb.configure(state=tk.DISABLED)
            self._toggle_label.configure(text="")

            self._board_canvas.show_board(board, title=Path(path).name)

            outline_note = (
                "  ⚠ No Edge.Cuts found — default outline added."
                if board.outline_synthesized else ""
            )
            base_msg = (
                f"Opened {Path(path).name} — "
                f"{len(board.pads)} pads, {len(board.copper_layers)} Cu layers"
                f"{outline_note}"
            )

            if board.segments:
                pro_path = Path(path).with_suffix(".kicad_pro")
                if not pro_path.exists():
                    pro_path = Path(path).with_name(Path(path).stem + ".kicad_pro")
                try:
                    rules = load_rules(pro_path)
                except Exception:
                    rules = None
                stats = routing_stats(board, rules)
                self._initial_stats = stats
                self._metrics.set_initial_stats(stats)
                self._status_var.set(f"{base_msg}  —  initial: {stats.summary()}")
            else:
                self._status_var.set(base_msg)
        except Exception as exc:
            messagebox.showerror("Open failed", str(exc))

    def _on_view_toggle(self) -> None:
        """Switch the canvas between initial and final board state."""
        inp = self._controls._full_input_path()
        title_base = Path(inp).name if inp else "board"
        if self._show_initial.get():
            if self._initial_board is not None:
                self._board_canvas.show_board(
                    self._initial_board, title=f"{title_base} (initial)")
            if self._initial_stats is not None:
                self._metrics.set_initial_stats(self._initial_stats)
        else:
            ev = self._last_done
            if ev is not None and ev.board is not None:
                self._board_canvas.show_board(ev.board, title=f"{title_base} (routed)")
                self._metrics.set_done(ev)

    # ── menu actions ─────────────────────────────────────────────────

    def _menu_open(self) -> None:
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Open KiCad board",
            filetypes=[("KiCad PCB", "*.kicad_pcb"),
                       ("All files", "*.*")])
        if path:
            self._controls.set_input(path)
            self._open_board(path)

    def _menu_save(self) -> None:
        self._controls._save_settings()

    def _menu_load(self) -> None:
        self._controls._load_settings()

    def _menu_run(self) -> None:
        cfg = self._controls.get_run_config()
        if cfg.input:
            self._start_run(cfg)

    def _about(self) -> None:
        messagebox.showinfo(
            "About PyAutoRoute",
            f"PyAutoRoute {__version__}\n\n"
            "Simulated-annealing autorouter for 2-layer KiCad PCBs.\n\n"
            "GUI: Tkinter + matplotlib")

    # ── main loop ─────────────────────────────────────────────────────

    def run(self) -> None:
        self._root.mainloop()


# ── entry point ───────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    import sys
    files = argv if argv is not None else sys.argv[1:]
    initial = files[0] if files else None
    app = App(initial_file=initial)
    app.run()
    return 0
