"""Simple hover tooltip for Tk widgets."""

from __future__ import annotations

import tkinter as tk


class ToolTip:
    """Show a small popup tooltip when the mouse hovers over *widget*."""

    _DELAY_MS = 600
    _WRAP = 260

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self._widget = widget
        self._text = text
        self._tip: tk.Toplevel | None = None
        self._after_id: str | None = None
        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<ButtonPress>", self._on_leave, add="+")

    def _on_enter(self, _event=None) -> None:
        self._cancel()
        self._after_id = self._widget.after(self._DELAY_MS, self._show)

    def _on_leave(self, _event=None) -> None:
        self._cancel()
        self._hide()

    def _cancel(self) -> None:
        if self._after_id is not None:
            self._widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self) -> None:
        if self._tip is not None:
            return
        x = self._widget.winfo_rootx() + 16
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        self._tip = tw = tk.Toplevel(self._widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tw, text=self._text, justify=tk.LEFT,
            background="#ffffe0", relief=tk.SOLID, borderwidth=1,
            font=("TkSmallCaptionFont",), wraplength=self._WRAP,
            padx=4, pady=2,
        ).pack()

    def _hide(self) -> None:
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


def add_tooltip(widget: tk.Widget, text: str) -> ToolTip:
    """Attach a tooltip to *widget* and return it."""
    return ToolTip(widget, text)
