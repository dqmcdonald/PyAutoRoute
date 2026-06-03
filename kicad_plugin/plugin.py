"""KiCad action plugin entry point for PyAutoRoute."""

from __future__ import annotations

from pathlib import Path

import pcbnew
import wx

from .dialog import ProgressDialog, SettingsDialog, _CAPTION
from . import settings as _settings


class PyAutoRoutePlugin(pcbnew.ActionPlugin):
    """KiCad action plugin that invokes PyAutoRoute on the current board."""

    def defaults(self) -> None:
        self.name = "PyAutoRoute"
        self.category = "PCB auto routing"
        self.description = (
            "Simulated-annealing autorouter — routes the current board "
            "using PyAutoRoute and reloads the result."
        )
        self.show_toolbar_button = True
        icon = Path(__file__).parent / "icon_24x24.png"
        if icon.is_file():
            self.icon_file_name = str(icon)

    def Run(self) -> None:
        board = pcbnew.GetBoard()
        board_path = board.GetFileName()

        if not board_path:
            wx.MessageBox(
                "Please save the board to a file before running PyAutoRoute.",
                _CAPTION, wx.OK | wx.ICON_WARNING,
            )
            return

        # ── Show settings dialog ──────────────────────────────────────────
        dlg = SettingsDialog(None, board_path)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return

        exe = dlg.get_executable()
        extra_args = dlg.build_extra_args()
        auto_reload = dlg.get_auto_reload()
        do_place = "--place" in extra_args
        dlg.save_to_ini()
        dlg.save_exe_setting()
        dlg.Destroy()

        if not exe or not Path(exe).is_file():
            wx.MessageBox(
                f"pyautoroute executable not found:\n  {exe or '(not set)'}\n\n"
                "Set the path in the Executable field.",
                _CAPTION, wx.OK | wx.ICON_ERROR,
            )
            return

        # ── Flush live edits to disk ──────────────────────────────────────
        pcbnew.SaveBoard(board_path, board)

        # ── Build command ─────────────────────────────────────────────────
        ini_path = str(Path(board_path).with_suffix(".ini"))
        command = [exe, board_path, "--in-place", "--config", ini_path] + extra_args

        # ── Launch progress dialog + subprocess ───────────────────────────
        progress = ProgressDialog(None, command)
        progress.start()
        result = progress.ShowModal()
        exit_code = progress.get_result()
        progress.Destroy()

        if result == wx.ID_CANCEL or exit_code != 0:
            return

        # ── Optionally inject routed tracks into the live board ───────────
        if auto_reload:
            try:
                _inject_tracks(board_path)
                if do_place:
                    wx.MessageBox(
                        "Routing reloaded. Footprint positions were also updated on "
                        "disk — close and reopen the file to pick up the new placement.",
                        _CAPTION, wx.OK | wx.ICON_INFORMATION,
                    )
            except Exception as exc:
                wx.MessageBox(
                    f"Routing complete, but auto-reload failed:\n{exc}\n\n"
                    "Close and reopen the file to see the result.",
                    _CAPTION, wx.OK | wx.ICON_WARNING,
                )


def _inject_tracks(board_path: str) -> None:
    """Replace the live editor's tracks with those from the routed file.

    Loads the routed .kicad_pcb, removes all existing tracks and vias from
    the current editor board, then copies across the new ones.  Zones (copper
    pours) are left untouched — use KiCad's Edit → Fill All Zones afterwards
    if a ground plane was added.
    """
    routed = pcbnew.LoadBoard(board_path)
    current = pcbnew.GetBoard()

    for item in list(current.Tracks()):
        current.Remove(item)

    for item in routed.Tracks():
        current.Add(item.Duplicate())

    current.BuildConnectivity()
    pcbnew.Refresh()


# Provision a wx.App if needed (matches Freerouting pattern for phoenix wx).
if "phoenix" in wx.PlatformInfo:
    if not wx.GetApp():
        _app = wx.App()

PyAutoRoutePlugin().register()
