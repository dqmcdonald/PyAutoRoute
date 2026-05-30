"""Embedded matplotlib board canvas widget."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from pyautoroute.visualize import draw_board


class BoardCanvas(ttk.Frame):
    """A Tk frame that embeds a matplotlib board view."""

    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        self._fig = Figure(figsize=(6, 6), tight_layout=True)
        self._ax = self._fig.add_subplot(111)
        self._ax.set_aspect("equal")
        self._ax.axis("off")
        self._ax.text(0.5, 0.5, "Open a board to begin",
                      ha="center", va="center",
                      transform=self._ax.transAxes,
                      color="#999", fontsize=12)
        self._mpl = FigureCanvasTkAgg(self._fig, master=self)
        self._mpl.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self._has_board = False

    def show_board(self, board, results=None, grid=None,
                   title: str | None = None, rats_nest=None) -> None:
        """Render *board* onto the canvas (may be called from the main thread)."""
        self._has_board = True
        draw_board(self._ax, board, results=results, grid=grid,
                   rats_nest=rats_nest, title=title)
        self._ax.axis("on")
        self._mpl.draw()

    def clear(self) -> None:
        self._ax.clear()
        self._ax.axis("off")
        self._ax.text(0.5, 0.5, "Open a board to begin",
                      ha="center", va="center",
                      transform=self._ax.transAxes,
                      color="#999", fontsize=12)
        self._has_board = False
        self._mpl.draw_idle()
