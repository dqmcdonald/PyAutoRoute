"""Tests for the INI settings file (--config / --write-config)."""

from __future__ import annotations

import pathlib

import pytest

from pyautoroute import autoroute

_TEST_BOARD = (pathlib.Path(__file__).resolve().parents[1]
               / "TestProjects" / "Test5" / "Test5.kicad_pcb")


def _parse_with_config(cfg_path, argv):
    """Mimic main(): load the config as defaults, then parse argv over them."""
    parser = autoroute.build_parser()
    parser.set_defaults(**autoroute.load_config(cfg_path, parser))
    return parser.parse_args(argv)


def _write(tmp_path, body):
    p = tmp_path / "settings.cfg"
    p.write_text("[pyautoroute]\n" + body)
    return p


def test_config_values_applied(tmp_path):
    cfg = _write(tmp_path, "grid = 0.3\ntime_budget = 120\nvia_weight = 4.0\n"
                           "place = true\nanneal_temps = 5.0, 0.1\n"
                           "exclude_net = GND, /PWR*\n")
    a = _parse_with_config(cfg, ["b.kicad_pcb"])
    assert a.grid == 0.3
    assert a.time_budget == 120.0
    assert a.via_weight == 4.0
    assert a.place is True
    assert list(a.anneal_temps) == [5.0, 0.1]
    assert a.exclude_net == ["GND", "/PWR*"]


def test_cli_overrides_config(tmp_path):
    cfg = _write(tmp_path, "grid = 0.3\nvia_weight = 4.0\n")
    a = _parse_with_config(cfg, ["b.kicad_pcb", "--grid", "0.5"])
    assert a.grid == 0.5                 # CLI wins
    assert a.via_weight == 4.0           # config value kept
    assert a.seed == 0                   # default when in neither


def test_config_unknown_key_errors(tmp_path):
    cfg = _write(tmp_path, "nonsuch = 1\n")
    with pytest.raises(SystemExit):
        _parse_with_config(cfg, ["b.kicad_pcb"])


def test_config_bad_value_errors(tmp_path):
    cfg = _write(tmp_path, "grid = not_a_number\n")
    with pytest.raises(SystemExit):
        _parse_with_config(cfg, ["b.kicad_pcb"])


def test_config_missing_file_errors():
    parser = autoroute.build_parser()
    with pytest.raises(SystemExit):
        autoroute.load_config("/no/such/file.cfg", parser)


def test_config_missing_section_errors(tmp_path):
    p = tmp_path / "s.cfg"
    p.write_text("[other]\ngrid = 0.3\n")
    parser = autoroute.build_parser()
    with pytest.raises(SystemExit):
        autoroute.load_config(p, parser)


def test_write_then_read_round_trip(tmp_path):
    # parse a rich command line, write the effective config, read it back, and
    # confirm the values survive the round trip
    parser = autoroute.build_parser()
    args = parser.parse_args(
        ["b.kicad_pcb", "--grid", "0.25", "--via-weight", "3", "--place",
         "--place-buffer", "0.8", "--anneal-temps", "5", "0.1",
         "--exclude-net", "GND", "--exclude-net", "VCC", "--runs", "4"])
    cfg = tmp_path / "out.cfg"
    autoroute.write_config(parser, args, cfg)

    back = autoroute.load_config(cfg, parser)
    assert back["grid"] == 0.25
    assert back["via_weight"] == 3.0
    assert back["place"] is True
    assert back["place_buffer"] == 0.8
    assert back["anneal_temps"] == [5.0, 0.1]
    assert back["exclude_net"] == ["GND", "VCC"]
    assert back["runs"] == 4


@pytest.mark.skipif(not _TEST_BOARD.exists(), reason="Test5 board not present")
def test_cli_write_config_default_name(tmp_path):
    import shutil
    src = tmp_path / "Test5.kicad_pcb"
    shutil.copy(_TEST_BOARD, src)
    rc = autoroute.main([str(src), "--write-config", "--grid", "0.2"])
    assert rc == 0
    cfg = tmp_path / "Test5.pyautoroute.cfg"
    assert cfg.exists()
    assert "grid = 0.2" in cfg.read_text()
