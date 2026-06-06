"""Settings and progress dialogs for the PyAutoRoute KiCad plugin."""

from __future__ import annotations

import configparser
import os
import subprocess
import threading
from pathlib import Path

import wx

from . import settings as _settings

_CAPTION = "PyAutoRoute"
_INI_SECTION = "pyautoroute"


# ── INI bridge ────────────────────────────────────────────────────────────────

def _read_ini(board_path: str) -> dict:
    """Load the board's .ini (if present) and return the [pyautoroute] values."""
    ini = Path(board_path).with_suffix(".ini")
    cfg = configparser.ConfigParser()
    cfg.read(str(ini))
    if _INI_SECTION in cfg:
        return dict(cfg[_INI_SECTION])
    return {}


def _scrub_ini(board_path: str, keys: list[str]) -> None:
    """Remove ``keys`` from the board's .ini [pyautoroute] section if present."""
    ini = Path(board_path).with_suffix(".ini")
    cfg = configparser.ConfigParser()
    cfg.read(str(ini))
    changed = False
    if _INI_SECTION in cfg:
        for k in keys:
            if k in cfg[_INI_SECTION]:
                del cfg[_INI_SECTION][k]
                changed = True
    if changed:
        with open(str(ini), "w") as fh:
            cfg.write(fh)


def _write_ini(board_path: str, values: dict) -> None:
    """Merge ``values`` into the board's .ini file (creates it if absent)."""
    ini = Path(board_path).with_suffix(".ini")
    cfg = configparser.ConfigParser()
    cfg.read(str(ini))
    if _INI_SECTION not in cfg:
        cfg[_INI_SECTION] = {}
    for k, v in values.items():
        cfg[_INI_SECTION][k] = v
    with open(str(ini), "w") as fh:
        cfg.write(fh)


# ── Settings dialog ────────────────────────────────────────────────────────────

class SettingsDialog(wx.Dialog):
    """Minimal settings dialog for PyAutoRoute.

    Reads defaults from the board's .ini and writes updated values back on OK.
    """

    def __init__(self, parent, board_path: str):
        super().__init__(parent, title=_CAPTION,
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._board_path = board_path
        ini = _read_ini(board_path)
        exe = _settings.find_executable()

        # ── Executable row ────────────────────────────────────────────────
        exe_label = wx.StaticText(self, label="Executable:")
        self._exe = wx.TextCtrl(self, value=exe, size=(320, -1))
        locate_btn = wx.Button(self, label="Locate…")
        locate_btn.Bind(wx.EVT_BUTTON, self._on_locate)

        exe_row = wx.BoxSizer(wx.HORIZONTAL)
        exe_row.Add(self._exe, 1, wx.EXPAND | wx.RIGHT, 4)
        exe_row.Add(locate_btn, 0)

        # ── Mode ─────────────────────────────────────────────────────────
        do_place = ini.get("place", "false").lower() in ("true", "1", "yes")
        self._mode = wx.RadioBox(
            self, label="Mode",
            choices=["Route only", "Place + Route"],
            majorDimension=1, style=wx.RA_SPECIFY_ROWS,
        )
        self._mode.SetSelection(1 if do_place else 0)

        # ── Grid ─────────────────────────────────────────────────────────
        grid_label = wx.StaticText(self, label="Grid (mm):")
        self._grid = wx.TextCtrl(self, value=ini.get("grid", ""), size=(80, -1))
        _tooltip(self._grid,
                 "Routing grid pitch in mm. Leave blank to derive from design rules.")

        # ── Placement time ───────────────────────────────────────────────
        place_time_label = wx.StaticText(self, label="Placement time (s):")
        self._place_time = wx.TextCtrl(
            self, value=ini.get("place_time", ""), size=(80, -1))
        _tooltip(self._place_time,
                 "Placement-annealing time budget in seconds (Place + Route "
                 "mode only). Leave blank for the built-in default.")

        # ── Routing time ─────────────────────────────────────────────────
        time_label = wx.StaticText(self, label="Routing time (s):")
        self._routing_time = wx.TextCtrl(
            self, value=ini.get("routing_time", "120"), size=(80, -1))
        _tooltip(self._routing_time, "Routing-annealing time budget in seconds.")

        # ── Exclude nets ─────────────────────────────────────────────────
        exc_label = wx.StaticText(self, label="Exclude nets:")
        excl_raw = ini.get("exclude_net", "")
        # INI stores repeated keys as newline-joined; normalise to comma-separated.
        excl_display = ", ".join(
            v.strip() for v in excl_raw.replace("\n", ",").split(",") if v.strip()
        )
        self._exclude = wx.TextCtrl(self, value=excl_display, size=(200, -1))
        _tooltip(self._exclude,
                 "Comma-separated net names/globs to skip (e.g. GND, PWR*).")

        # ── Existing routes ───────────────────────────────────────────────
        er_label = wx.StaticText(self, label="Existing routes:")
        self._existing = wx.Choice(self, choices=["clear", "preserve"])
        self._existing.SetStringSelection(ini.get("existing_routes", "clear"))
        _tooltip(self._existing,
                 "clear: strip all tracks before routing.\n"
                 "preserve: keep existing tracks, route only the remainder.")

        # ── Keep outline ─────────────────────────────────────────────────
        ko_val = ini.get("keep_outline", "true").lower() in ("true", "1", "yes")
        self._keep_outline = wx.CheckBox(self, label="Keep board outline")
        self._keep_outline.SetValue(ko_val)
        _tooltip(self._keep_outline,
                 "Place + Route mode only. Keep the existing Edge.Cuts outline "
                 "and constrain footprints to stay inside it, instead of "
                 "regenerating the outline as a bounding box. Ignored if the "
                 "board has no real outline.")

        # ── Ground plane ─────────────────────────────────────────────────
        gp_val = ini.get("ground_plane", "false").lower() in ("true", "1", "yes")
        self._ground_plane = wx.CheckBox(self, label="Add ground plane")
        self._ground_plane.SetValue(gp_val)
        _tooltip(self._ground_plane,
                 "Pour a GND copper zone on B.Cu after routing.")

        # ── Cycles ───────────────────────────────────────────────────────
        cyc_label = wx.StaticText(self, label="Cycles:")
        try:
            cyc_val = int(ini.get("cycles", "1"))
        except ValueError:
            cyc_val = 1
        self._cycles = wx.SpinCtrl(self, value=str(cyc_val), min=1, max=50,
                                   size=(60, -1))
        _tooltip(self._cycles,
                 "Run N independent place+route cycles and keep the best result.")

        # ── Auto-reload ───────────────────────────────────────────────────
        ar_val = _settings.load_settings().get("auto_reload", True)
        self._auto_reload = wx.CheckBox(self, label="Reload tracks into KiCad when done")
        self._auto_reload.SetValue(ar_val)
        _tooltip(self._auto_reload,
                 "After routing, remove the current tracks from the editor and inject "
                 "the newly routed tracks directly into the live board. "
                 "If placement (--place) was also run, close and reopen the file to "
                 "pick up the new footprint positions.")

        # ── Layout ───────────────────────────────────────────────────────
        grid_sizer = wx.FlexGridSizer(cols=2, hgap=8, vgap=6)
        grid_sizer.AddGrowableCol(1)

        def _row(label_widget, ctrl_widget):
            grid_sizer.Add(label_widget, 0, wx.ALIGN_CENTER_VERTICAL)
            grid_sizer.Add(ctrl_widget, 1, wx.EXPAND)

        _row(exe_label, exe_row)
        _row(wx.StaticText(self, label="Mode:"), self._mode)
        _row(grid_label, self._grid)
        _row(place_time_label, self._place_time)
        _row(time_label, self._routing_time)
        _row(exc_label, self._exclude)
        _row(er_label, self._existing)
        _row(wx.StaticText(self, label=""), self._keep_outline)
        _row(wx.StaticText(self, label=""), self._ground_plane)
        _row(cyc_label, self._cycles)
        _row(wx.StaticText(self, label=""), self._auto_reload)

        # ── Buttons ───────────────────────────────────────────────────────
        open_gui_btn = wx.Button(self, label="Open Full GUI…")
        open_gui_btn.Bind(wx.EVT_BUTTON, self._on_open_gui)
        btn_sizer = wx.StdDialogButtonSizer()
        ok_btn = wx.Button(self, wx.ID_OK, "Route")
        ok_btn.SetDefault()
        cancel_btn = wx.Button(self, wx.ID_CANCEL)
        btn_sizer.AddButton(ok_btn)
        btn_sizer.AddButton(cancel_btn)
        btn_sizer.Realize()

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(grid_sizer, 1, wx.EXPAND | wx.ALL, 12)
        outer.Add(open_gui_btn, 0, wx.LEFT | wx.BOTTOM, 12)
        outer.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        outer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 8)

        self.SetSizerAndFit(outer)
        self.Centre()

    # ── public API ────────────────────────────────────────────────────────

    def get_executable(self) -> str:
        return self._exe.GetValue().strip()

    def get_auto_reload(self) -> bool:
        return self._auto_reload.GetValue()

    def build_extra_args(self) -> list[str]:
        """Return extra CLI arguments derived from the dialog state."""
        args: list[str] = []
        do_place = self._mode.GetSelection() == 1
        if do_place:
            args += ["--place"]
            # --place-time is rejected by the CLI without --place, so only emit
            # it in Place + Route mode.
            pt = self._place_time.GetValue().strip()
            if pt:
                args += ["--place-time", pt]
            # Likewise --keep-outline only applies to a placement pass.
            if self._keep_outline.GetValue():
                args += ["--keep-outline"]
        grid = self._grid.GetValue().strip()
        if grid:
            args += ["--grid", grid]
        rt = self._routing_time.GetValue().strip()
        if rt:
            args += ["--routing-time", rt]
        excl = [v.strip() for v in self._exclude.GetValue().split(",") if v.strip()]
        for net in excl:
            args += ["--exclude-net", net]
        args += ["--existing-routes", self._existing.GetStringSelection()]
        if self._ground_plane.GetValue():
            args += ["--ground-plane"]
        cyc = self._cycles.GetValue()
        if cyc > 1:
            args += ["--cycles", str(cyc)]
        return args

    def save_to_ini(self) -> None:
        """Write current dialog values back into the board's .ini file."""
        do_place = self._mode.GetSelection() == 1
        excl = [v.strip() for v in self._exclude.GetValue().split(",") if v.strip()]
        values: dict[str, str] = {
            "place": "true" if do_place else "false",
            "existing_routes": self._existing.GetStringSelection(),
            "keep_outline": "true" if self._keep_outline.GetValue() else "false",
            "ground_plane": "true" if self._ground_plane.GetValue() else "false",
            "cycles": str(self._cycles.GetValue()),
        }
        grid = self._grid.GetValue().strip()
        if grid:
            values["grid"] = grid
        rt = self._routing_time.GetValue().strip()
        if rt:
            values["routing_time"] = rt
        pt = self._place_time.GetValue().strip()
        if pt:
            values["place_time"] = pt
        if excl:
            values["exclude_net"] = ", ".join(excl)
        _write_ini(self._board_path, values)
        # Remove any plugin-only keys that were mistakenly written to the INI
        # in a previous version and would cause --config to fail.
        _scrub_ini(self._board_path, ["auto_reload"])

    def save_exe_setting(self) -> None:
        """Persist the executable path and plugin-only settings to the JSON store."""
        data = _settings.load_settings()
        data["executable"] = self.get_executable()
        data["auto_reload"] = self._auto_reload.GetValue()
        _settings.save_settings(data)

    # ── event handlers ────────────────────────────────────────────────────

    def _on_locate(self, _evt):
        dlg = wx.FileDialog(self, "Locate pyautoroute executable",
                            wildcard="*", style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        if dlg.ShowModal() == wx.ID_OK:
            self._exe.SetValue(dlg.GetPath())
        dlg.Destroy()

    def _on_open_gui(self, _evt):
        exe = self.get_executable()
        gui_exe = str(Path(exe).parent / "pyautoroute-gui") if exe else "pyautoroute-gui"
        try:
            subprocess.Popen([gui_exe, self._board_path], env=_subprocess_env())
        except Exception as exc:
            wx.MessageBox(f"Could not launch GUI:\n{exc}", _CAPTION,
                          wx.OK | wx.ICON_ERROR)


# ── Progress dialog ────────────────────────────────────────────────────────────

class ProgressDialog(wx.Dialog):
    """Shows a pulsing gauge and scrolling log while PyAutoRoute runs.

    The subprocess is launched in a daemon thread; stdout lines are appended
    to the log via ``wx.CallAfter`` so the GUI stays responsive.  The "Cancel"
    button terminates the subprocess.
    """

    def __init__(self, parent, command: list[str]):
        super().__init__(parent, title=_CAPTION,
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
                         size=(560, 380))
        self._command = command
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._result: int | None = None  # subprocess return code

        self._gauge = wx.Gauge(self, range=100, size=(-1, 14),
                               style=wx.GA_HORIZONTAL | wx.GA_SMOOTH)
        self._log = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL,
            size=(-1, 280))
        self._log.SetFont(wx.Font(
            9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))

        self._cancel_btn = wx.Button(self, wx.ID_CANCEL, "Cancel")
        self._cancel_btn.Bind(wx.EVT_BUTTON, self._on_cancel)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self._gauge, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(self._log, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        sizer.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self._cancel_btn, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
        self.SetSizer(sizer)
        self.Centre()

        # Pulse timer keeps the gauge animated while routing runs.
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda _: self._gauge.Pulse(), self._timer)
        self._timer.Start(80)

    def start(self) -> None:
        """Launch the subprocess and return immediately (non-blocking)."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def get_result(self) -> int | None:
        """Return the subprocess exit code, or ``None`` if not yet finished."""
        return self._result

    # ── internals ─────────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            self._proc = subprocess.Popen(
                self._command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=_subprocess_env(),
            )
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                wx.CallAfter(self._append_log, line.rstrip("\r\n"))
            self._proc.wait()
            self._result = self._proc.returncode
        except Exception as exc:
            wx.CallAfter(self._append_log, f"[error] {exc}")
            self._result = -1
        finally:
            wx.CallAfter(self._on_finished)

    def _append_log(self, text: str) -> None:
        self._log.AppendText(text + "\n")

    def _on_finished(self) -> None:
        self._timer.Stop()
        self._gauge.SetValue(100 if self._result == 0 else 0)
        self._cancel_btn.SetLabel("Close")
        self._cancel_btn.SetId(wx.ID_OK)
        # Re-point the button at a "close" handler.  SetId() does not change the
        # existing EVT_BUTTON binding, so without this the original _on_cancel
        # still fires and ShowModal() returns ID_CANCEL — which makes the caller
        # bail out before reloading the routed tracks.
        self._cancel_btn.Unbind(wx.EVT_BUTTON, handler=self._on_cancel)
        self._cancel_btn.Bind(wx.EVT_BUTTON, self._on_close)
        if self._result != 0:
            self._append_log(
                f"\n[PyAutoRoute exited with code {self._result}]")

    def _on_cancel(self, _evt) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
        self.EndModal(wx.ID_CANCEL)

    def _on_close(self, _evt) -> None:
        # The subprocess has finished; report its outcome via the dialog result.
        # The caller still guards on the exit code, so a non-zero result here is
        # harmless (it won't trigger a track reload).
        self.EndModal(wx.ID_OK if self._result == 0 else wx.ID_CANCEL)


# ── helpers ────────────────────────────────────────────────────────────────────

def _subprocess_env() -> dict:
    """Return a clean environment for subprocess calls.

    KiCad sets PYTHONHOME and PYTHONPATH to its own bundled Python 3.9.
    If the pyautoroute subprocess runs under a different Python (e.g. 3.12 in
    a venv), it inherits these variables, Python tries to find its standard
    library (including ``encodings``) inside the KiCad bundle, and crashes with
    ``ModuleNotFoundError: No module named 'encodings'``.  Stripping both
    variables lets each Python interpreter find its own stdlib normally.
    """
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    return env


def _tooltip(widget, text: str) -> None:
    widget.SetToolTip(wx.ToolTip(text))
