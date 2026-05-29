"""Embedded energy graph widget (current + best energy vs iteration)."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

# Downsample the series when it exceeds this length to keep Tk responsive.
_MAX_POINTS = 2000


def _downsample(xs, ys, max_pts):
    if len(xs) <= max_pts:
        return xs, ys
    step = len(xs) // max_pts
    return xs[::step], ys[::step]


class EnergyPlot(ttk.Frame):
    """A small embedded line chart of current and best energy over iterations."""

    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        self._fig = Figure(figsize=(3, 2), tight_layout=True)
        self._ax = self._fig.add_subplot(111)
        self._mpl = FigureCanvasTkAgg(self._fig, master=self)
        self._mpl.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self._iters: list[int] = []
        self._cur: list[float] = []
        self._best: list[float] = []
        self._overall_best: float = float("inf")
        self._draw_empty()

    def reset(self) -> None:
        self._iters.clear()
        self._cur.clear()
        self._best.clear()
        self._overall_best = float("inf")
        self._draw_empty()
        self._mpl.draw_idle()

    def add_point(self, it: int, cur: float, best: float) -> None:
        # Detect a new run: iteration counter reset (new SA run started).
        # Save the run's best to the overall best, then clear.
        if self._iters and it <= self._iters[-1]:
            run_best = min(self._best) if self._best else float("inf")
            if run_best < self._overall_best:
                self._overall_best = run_best
            self._iters.clear()
            self._cur.clear()
            self._best.clear()
        self._iters.append(it)
        self._cur.append(cur)
        self._best.append(best)

    def refresh(self) -> None:
        if not self._iters:
            return
        xs, best = _downsample(self._iters, self._best, _MAX_POINTS)
        ax = self._ax
        ax.clear()
        ax.plot(xs, best, lw=1.5, color="#3366cc", label="best")
        if self._overall_best < float("inf"):
            ax.axhline(self._overall_best, color="#228833", lw=1,
                       linestyle="--", alpha=0.7, label="prev best")
        ax.set_xlabel("iter", fontsize=7)
        ax.set_ylabel("energy", fontsize=7)
        ax.tick_params(labelsize=6)
        ax.legend(fontsize=6, loc="upper right")
        self._mpl.draw_idle()

    def _draw_empty(self) -> None:
        self._ax.clear()
        self._ax.set_title("Energy", fontsize=8)
        self._ax.text(0.5, 0.5, "Run to see graph",
                      ha="center", va="center",
                      transform=self._ax.transAxes,
                      color="#aaa", fontsize=8)
        self._ax.axis("off")
