"""Tokenizer/parser/serializer round-trip tests for pyautoroute.sexpr."""

from __future__ import annotations

import pathlib

import pytest

from pyautoroute import sexpr

REPO = pathlib.Path(__file__).resolve().parent.parent
TEST1 = REPO / "TestProjects" / "Test1" / "Test1.kicad_pcb"


def test_atom_helpers():
    assert sexpr.sym("segment").raw == "segment"
    assert sexpr.string("GND").raw == '"GND"'
    assert sexpr.string('a"b\\c').raw == '"a\\"b\\\\c"'
    assert sexpr.number(80).raw == "80"
    assert sexpr.number(49.5).raw == "49.5"
    assert sexpr.number(0.0).raw == "0"
    assert sexpr.number(180.0).raw == "180"


def test_atom_decoding():
    assert sexpr.Atom('"F.Cu"').text == "F.Cu"
    assert sexpr.Atom('"a\\"b"').text == 'a"b'
    assert sexpr.Atom("49.5").as_float() == 49.5
    assert sexpr.Atom('"F.Cu"').is_string
    assert not sexpr.Atom("smd").is_string


def test_parse_basic_structure():
    tree = sexpr.loads('(kicad_pcb (version 20260206) (at 49.5 80))')
    assert sexpr.head_symbol(tree) == "kicad_pcb"
    assert sexpr.head_symbol(tree[1]) == "version"
    assert tree[1][1].as_float() == 20260206
    assert tree[2][1].raw == "49.5"


def test_loads_stray_closing_paren_raises_value_error():
    """A stray ')' with no open list to close must raise the documented
    ValueError, not an IndexError from popping an empty parse stack."""
    with pytest.raises(ValueError):
        sexpr.loads('(kicad_pcb (version 1)))')


def test_loads_unclosed_list_raises_value_error():
    with pytest.raises(ValueError):
        sexpr.loads('(kicad_pcb (version 1)')


def test_roundtrip_small_inline():
    text = "(size 1.2 1.4)"
    assert sexpr.dumps(sexpr.loads(text)) == text


def test_roundtrip_pts_packing():
    text = '(gr_poly\n\t(pts\n\t\t(xy 38 36.5) (xy 78 36.5)\n\t)\n)'
    assert sexpr.dumps(sexpr.loads(text)) == text


def test_string_with_spaces_preserved():
    text = '(property "Description" "A long value")'
    tree = sexpr.loads(text)
    assert tree[2].text == "A long value"
    assert sexpr.dumps(tree) == text


def _structural_eq(a, b) -> bool:
    if isinstance(a, sexpr.Atom) and isinstance(b, sexpr.Atom):
        return a.raw == b.raw
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_structural_eq(x, y) for x, y in zip(a, b))
    return False


@pytest.mark.skipif(not TEST1.exists(), reason="Test1 board not present")
def test_roundtrip_test1_byte_identical():
    original = TEST1.read_text()
    tree = sexpr.loads(original)
    assert sexpr.dump_file(tree) == original


@pytest.mark.skipif(not TEST1.exists(), reason="Test1 board not present")
def test_generic_serializer_is_structurally_faithful():
    # Spans give byte-identity for free; this proves the generic formatter
    # (used for nodes we build) reproduces the structure exactly.
    original = TEST1.read_text()
    tree = sexpr.loads(original)
    regen = sexpr.dumps(tree, verbatim=False)
    assert _structural_eq(sexpr.loads(regen), tree)
