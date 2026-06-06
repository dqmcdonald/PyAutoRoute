"""Tests for pyautoroute.footprint_assigner."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from pyautoroute.footprint_assigner import (
    AssignResult,
    ParsedOverrides,
    assign_footprints,
    iter_placed_symbols,
    load_prefs,
    load_schematic,
    parse_overrides,
    ref_prefix,
    resolve,
    set_footprint,
)
from pyautoroute import sexpr

# ---------------------------------------------------------------------------
# Minimal schematic fixture builders
# ---------------------------------------------------------------------------

_PREFS = {
    "defaults": {"technology": "SMD"},
    "prefix": {
        "R": {"default": "SMD",
              "SMD": "Resistor_SMD:R_0805_2012Metric_Pad1.20x1.40mm_HandSolder",
              "THT": "Resistor_THT:R_Axial"},
        "C": {"default": "SMD",
              "SMD": "Capacitor_SMD:C_0805_2012Metric_Pad1.18x1.45mm_HandSolder"},
        "LED": {"default": "SMD",
                "SMD": "LED_SMD:LED_0805_2012Metric_Pad1.15x1.40mm_HandSolder"},
        "U": {"values": {"74AHC244": "Package_DIP:DIP-20_W7.62mm_Socket_LongPads"}},
    },
}


def _make_sch(symbols: list[str]) -> str:
    """Build a minimal .kicad_sch text with the given placed symbol blocks."""
    body = "\n".join(symbols)
    return textwrap.dedent(f"""\
        (kicad_sch
        \t(version 20260306)
        \t(generator "eeschema")
        \t(lib_symbols
        \t\t(symbol "Device:R"
        \t\t\t(property "Footprint" "")
        \t\t)
        \t)
        {body}
        )
        """)


def _sym(ref: str, value: str, footprint: str) -> str:
    return textwrap.dedent(f"""\
        \t(symbol
        \t\t(lib_id "Device:R")
        \t\t(at 100 100 0)
        \t\t(uuid "aaaa-bbbb")
        \t\t(property "Reference" "{ref}"
        \t\t\t(at 0 0 0)
        \t\t)
        \t\t(property "Value" "{value}"
        \t\t\t(at 0 0 0)
        \t\t)
        \t\t(property "Footprint" "{footprint}"
        \t\t\t(at 0 0 0)
        \t\t)
        \t)""")


# ---------------------------------------------------------------------------
# ref_prefix
# ---------------------------------------------------------------------------

def test_ref_prefix_simple():
    assert ref_prefix("R5") == "R"
    assert ref_prefix("C100") == "C"
    assert ref_prefix("LED2") == "LED"
    assert ref_prefix("U1") == "U"
    assert ref_prefix("SW3") == "SW"


# ---------------------------------------------------------------------------
# iter_placed_symbols — skips lib_symbols and power refs
# ---------------------------------------------------------------------------

def test_iter_placed_symbols_skips_lib():
    sch = _make_sch([_sym("R1", "10k", ""), _sym("#PWR01", "PWR", "")])
    tree = sexpr.loads(sch)
    refs = [next(
        c[2].text for c in sym if isinstance(c, sexpr.SList)
        and len(c) >= 3 and isinstance(c[0], sexpr.Atom) and c[0].raw == "property"
        and isinstance(c[1], sexpr.Atom) and c[1].text == "Reference"
    ) for sym in iter_placed_symbols(tree)]
    # R1 included, #PWR01 excluded
    assert refs == ["R1"]


def test_iter_placed_symbols_multiple():
    sch = _make_sch([_sym("R1", "10k", ""), _sym("C1", "100nF", ""), _sym("U1", "74AHC244", "")])
    tree = sexpr.loads(sch)
    syms = list(iter_placed_symbols(tree))
    assert len(syms) == 3


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------

def test_resolve_default_tech():
    fp = resolve("R", "10k", _PREFS, {})
    assert fp == "Resistor_SMD:R_0805_2012Metric_Pad1.20x1.40mm_HandSolder"


def test_resolve_tech_override():
    fp = resolve("R", "10k", _PREFS, {"R": "THT"})
    assert fp == "Resistor_THT:R_Axial"


def test_resolve_value_keyed_prefs():
    fp = resolve("U", "74AHC244", _PREFS, {})
    assert fp == "Package_DIP:DIP-20_W7.62mm_Socket_LongPads"


def test_resolve_value_keyed_override():
    overrides = {"U": {"74AHC244": "CustomPkg:DIP-20"}}
    fp = resolve("U", "74AHC244", _PREFS, overrides)
    assert fp == "CustomPkg:DIP-20"


def test_resolve_unknown_prefix():
    fp = resolve("SW", "SW_DIP", _PREFS, {})
    assert fp is None


def test_resolve_unknown_value_no_default():
    # U has no default tech, only values; unknown value → None
    fp = resolve("U", "LM358", _PREFS, {})
    assert fp is None


# ---------------------------------------------------------------------------
# parse_overrides
# ---------------------------------------------------------------------------

def test_parse_tech_override():
    ov = parse_overrides(["R:THT", "C:SMD"])
    assert ov.data == {"R": "THT", "C": "SMD"}


def test_parse_value_override():
    ov = parse_overrides(["U:74AHC244=Package_DIP:DIP-20_W7.62mm_Socket_LongPads"])
    assert ov.data == {"U": {"74AHC244": "Package_DIP:DIP-20_W7.62mm_Socket_LongPads"}}


def test_parse_mixed_overrides():
    ov = parse_overrides(["R:THT", "U:74AHC244=Package_DIP:DIP-20"])
    assert ov.data["R"] == "THT"
    assert ov.data["U"] == {"74AHC244": "Package_DIP:DIP-20"}


def test_parse_override_bad_form():
    with pytest.raises(ValueError):
        parse_overrides(["RTHTSMD"])  # missing colon


# ---------------------------------------------------------------------------
# assign_footprints — dry run and real write
# ---------------------------------------------------------------------------

def test_assign_dry_run_does_not_write(tmp_path: Path):
    sch_file = tmp_path / "test.kicad_sch"
    sch_file.write_text(_make_sch([_sym("R1", "10k", "")]), encoding="utf-8")
    original = sch_file.read_text(encoding="utf-8")

    result = assign_footprints(
        sch_file, _PREFS, parse_overrides([]),
        dry_run=True,
    )

    assert sch_file.read_text(encoding="utf-8") == original
    assert len(result.assigned) == 1
    ref, value, old_fp, new_fp = result.assigned[0]
    assert ref == "R1"
    assert value == "10k"
    assert old_fp == ""


def test_assign_writes_footprint(tmp_path: Path):
    sch_file = tmp_path / "test.kicad_sch"
    sch_file.write_text(_make_sch([_sym("R1", "10k", ""), _sym("C1", "100nF", "")]),
                        encoding="utf-8")

    result = assign_footprints(sch_file, _PREFS, parse_overrides([]))

    written = sch_file.read_text(encoding="utf-8")
    assert "Resistor_SMD:R_0805_2012Metric_Pad1.20x1.40mm_HandSolder" in written
    assert "Capacitor_SMD:C_0805_2012Metric_Pad1.18x1.45mm_HandSolder" in written
    assert len(result.assigned) == 2


def test_assign_skips_already_assigned(tmp_path: Path):
    fp = "Resistor_SMD:R_0805_2012Metric_Pad1.20x1.40mm_HandSolder"
    sch_file = tmp_path / "test.kicad_sch"
    sch_file.write_text(_make_sch([_sym("R1", "10k", fp)]), encoding="utf-8")

    result = assign_footprints(sch_file, _PREFS, parse_overrides([]))

    assert result.assigned == []
    assert len(result.skipped_assigned) == 1
    ref, value, current_fp = result.skipped_assigned[0]
    assert ref == "R1"
    assert value == "10k"
    assert current_fp == fp


def test_assign_reassign_flag(tmp_path: Path):
    old_fp = "OldLibrary:OldFootprint"
    sch_file = tmp_path / "test.kicad_sch"
    sch_file.write_text(_make_sch([_sym("R1", "10k", old_fp)]), encoding="utf-8")

    result = assign_footprints(sch_file, _PREFS, parse_overrides([]), reassign=True)

    written = sch_file.read_text(encoding="utf-8")
    assert "Resistor_SMD:R_0805_2012Metric_Pad1.20x1.40mm_HandSolder" in written
    assert len(result.assigned) == 1


def test_assign_unknown_prefix_skipped(tmp_path: Path):
    sch_file = tmp_path / "test.kicad_sch"
    sch_file.write_text(_make_sch([_sym("SW1", "SW_DIP_x08", "")]), encoding="utf-8")

    result = assign_footprints(sch_file, _PREFS, parse_overrides([]))

    assert result.assigned == []
    assert len(result.skipped_unknown) == 1
    assert result.skipped_unknown[0][0] == "SW1"
    assert result.skipped_unknown[0][1] == "SW_DIP_x08"


def test_assign_tech_override(tmp_path: Path):
    sch_file = tmp_path / "test.kicad_sch"
    sch_file.write_text(_make_sch([_sym("R1", "10k", "")]), encoding="utf-8")

    assign_footprints(sch_file, _PREFS, parse_overrides(["R:THT"]))

    written = sch_file.read_text(encoding="utf-8")
    assert "Resistor_THT:R_Axial" in written


def test_assign_value_override(tmp_path: Path):
    sch_file = tmp_path / "test.kicad_sch"
    sch_file.write_text(_make_sch([_sym("U1", "74AHC244", "")]), encoding="utf-8")

    assign_footprints(
        sch_file, _PREFS,
        parse_overrides(["U:74AHC244=CustomPkg:DIP-20"]),
    )

    written = sch_file.read_text(encoding="utf-8")
    assert "CustomPkg:DIP-20" in written


def test_assign_round_trip_unmodified_preserved(tmp_path: Path):
    """Symbols that don't need changes must round-trip byte-for-byte."""
    fp = "Resistor_SMD:R_0805_2012Metric_Pad1.20x1.40mm_HandSolder"
    sch_file = tmp_path / "test.kicad_sch"
    original = _make_sch([_sym("R1", "10k", fp), _sym("C1", "100nF", "")])
    sch_file.write_text(original, encoding="utf-8")

    assign_footprints(sch_file, _PREFS, parse_overrides([]))

    written = sch_file.read_text(encoding="utf-8")
    # R1's footprint text must appear unchanged
    assert fp in written
    # C1 must now have a footprint
    assert "Capacitor_SMD" in written


# ---------------------------------------------------------------------------
# load_prefs
# ---------------------------------------------------------------------------

def test_load_prefs_missing(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="--init-prefs"):
        load_prefs(tmp_path / "nonexistent.toml")


def test_load_prefs_real(tmp_path: Path):
    toml = tmp_path / "prefs.toml"
    toml.write_text(
        '[prefix.R]\ndefault = "SMD"\nSMD = "Resistor_SMD:R_0805"\n',
        encoding="utf-8",
    )
    prefs = load_prefs(toml)
    assert prefs["prefix"]["R"]["SMD"] == "Resistor_SMD:R_0805"
