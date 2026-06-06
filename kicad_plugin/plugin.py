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
        keep_outline = "--keep-outline" in extra_args
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
        # Route to an explicit sidecar file as well as updating the board
        # in place.  We reload tracks from this sidecar — never from board_path
        # itself, because LoadBoard() on the currently-open file aliases the live
        # editor board (KiCad keeps one BOARD per open file), so clearing the
        # editor's tracks would also empty the board we're reading back.
        src = Path(board_path)
        tag = "_placed_routed" if do_place else "_routed"
        routed_path = str(src.with_name(src.stem + tag + src.suffix))
        command = [
            exe, board_path, "--in-place", "--output", routed_path,
            "--config", ini_path,
        ] + extra_args

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
                _inject_tracks(routed_path, sync_footprints=do_place)
                if do_place:
                    outline_note = (
                        "" if keep_outline else
                        "The board outline may have been regenerated as a bounding "
                        "box; close and reopen the file to pick up the new outline. "
                    )
                    wx.MessageBox(
                        "Placed + routed result reloaded: footprint positions, "
                        "rotations and tracks were updated in the editor.\n\n"
                        f"{outline_note}Copper zones are not updated in place — if "
                        "you added a ground plane, reopen the file and run "
                        "Edit → Fill All Zones.",
                        _CAPTION, wx.OK | wx.ICON_INFORMATION,
                    )
            except Exception as exc:
                wx.MessageBox(
                    f"Routing complete, but auto-reload failed:\n{exc}\n\n"
                    "Close and reopen the file to see the result.",
                    _CAPTION, wx.OK | wx.ICON_WARNING,
                )


def _inject_tracks(routed_path: str, sync_footprints: bool = False) -> None:
    """Replace the live editor's tracks with those from the routed file.

    Loads the routed .kicad_pcb (a sidecar file, *not* the open board's own
    path), removes all existing tracks and vias from the current editor board,
    then copies across the new ones.  Zones (copper pours) are left untouched —
    use KiCad's Edit → Fill All Zones afterwards if a ground plane was added.

    When ``sync_footprints`` is set (a Place + Route run), each footprint's new
    position and rotation is copied from the routed board into the live editor
    *before* the tracks, matched by reference designator.  Without this the
    editor keeps the old footprint poses while the tracks reflect the new
    layout, so the freshly routed tracks land on top of the old pad positions.

    Args:
        routed_path: path to the routed sidecar .kicad_pcb file. Must differ
            from the currently-open board file: ``LoadBoard()`` on the open path
            aliases the live editor board, which would make the snapshot below
            empty as soon as the editor's tracks are removed.
        sync_footprints: also copy footprint positions/rotations from the routed
            board into the live editor (use after a placement pass).
    """
    routed = pcbnew.LoadBoard(routed_path)
    # Snapshot the routed tracks/vias *before* mutating the editor board, so the
    # copy is independent of whatever object LoadBoard() returned.
    new_items = [item.Duplicate() for item in routed.Tracks()]

    current = pcbnew.GetBoard()

    if sync_footprints:
        for fp in routed.GetFootprints():
            live = current.FindFootprintByReference(fp.GetReference())
            if live is not None:
                live.SetPosition(fp.GetPosition())
                live.SetOrientation(fp.GetOrientation())

    for item in list(current.Tracks()):
        current.Remove(item)

    for item in new_items:
        current.Add(item)

    current.BuildConnectivity()
    pcbnew.Refresh()


# Provision a wx.App if needed (matches Freerouting pattern for phoenix wx).
if "phoenix" in wx.PlatformInfo:
    if not wx.GetApp():
        _app = wx.App()

PyAutoRoutePlugin().register()
