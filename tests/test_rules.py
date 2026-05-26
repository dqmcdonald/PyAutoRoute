"""Tests for pyautoroute.rules (.kicad_pro design-rule parsing)."""

from __future__ import annotations

import json
import pathlib

import pytest

from pyautoroute import rules

REPO = pathlib.Path(__file__).resolve().parent.parent
PRO = REPO / "TestProjects" / "Test1" / "Test1.kicad_pro"


def test_defaults_when_missing(tmp_path):
    r = rules.load_rules(tmp_path / "nope.kicad_pro")
    assert r.default_class.track_width == 0.2
    assert r.clearance_for("ANY") == 0.2
    assert r.min_copper_edge_clearance == 0.5


@pytest.mark.skipif(not PRO.exists(), reason="Test1 project not present")
def test_test1_rules():
    r = rules.load_rules(PRO)
    assert "Default" in r.classes
    assert r.clearance_for("GND") == 0.2
    assert r.track_width_for("GND") == 0.2
    assert r.via_diameter_for("GND") == 0.6
    assert r.via_drill_for("GND") == 0.3
    assert r.min_copper_edge_clearance == 0.5
    assert r.min_hole_to_hole == 0.25
    assert r.pair_clearance("GND", "5V") == 0.2


def test_pattern_and_assignment_resolution(tmp_path):
    pro = {
        "board": {"design_settings": {"rules": {"min_clearance": 0.1}}},
        "net_settings": {
            "classes": [
                {"name": "Default", "clearance": 0.2, "track_width": 0.2,
                 "via_diameter": 0.6, "via_drill": 0.3},
                {"name": "Power", "clearance": 0.4, "track_width": 0.5,
                 "via_diameter": 0.8, "via_drill": 0.4},
            ],
            "netclass_assignments": {"SPECIAL": "Power"},
            "netclass_patterns": [{"pattern": "/PWR*", "netclass": "Power"}],
        },
    }
    p = tmp_path / "p.kicad_pro"
    p.write_text(json.dumps(pro))
    r = rules.load_rules(p)
    assert r.class_for("SPECIAL").name == "Power"      # explicit assignment
    assert r.class_for("/PWR_5V").name == "Power"      # glob pattern
    assert r.class_for("DATA0").name == "Default"      # falls through
    assert r.track_width_for("/PWR_5V") == 0.5
