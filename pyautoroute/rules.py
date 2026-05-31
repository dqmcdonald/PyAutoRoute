"""Design rules read from a KiCad ``.kicad_pro`` project file.

Exposes per-net effective clearance / track width / via geometry (net-class
value floored by the board-wide minimums) plus a net-name -> net-class resolver
that honours explicit assignments and wildcard patterns. All distances are in
millimetres, matching the project file.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path

# KiCad's out-of-the-box defaults, used when a project file or field is absent.
_DEFAULT_CLEARANCE = 0.2
_DEFAULT_TRACK_WIDTH = 0.2
_DEFAULT_VIA_DIAMETER = 0.6
_DEFAULT_VIA_DRILL = 0.3
_DEFAULT_EDGE_CLEARANCE = 0.5
_DEFAULT_HOLE_TO_HOLE = 0.25


@dataclass(frozen=True)
class NetClass:
    name: str
    clearance: float
    track_width: float
    via_diameter: float
    via_drill: float


@dataclass
class DesignRules:
    classes: dict[str, NetClass]
    default_class: NetClass
    assignments: dict[str, str]          # explicit net name -> class name
    patterns: list[tuple[str, str]]      # (glob pattern, class name), ordered
    min_clearance: float
    min_track_width: float
    min_via_diameter: float
    min_via_drill: float
    min_copper_edge_clearance: float
    min_hole_to_hole: float

    def class_for(self, net_name: str) -> NetClass:
        """Resolve a net to its class.

        Resolution order: explicit assignment, then the first matching wildcard
        pattern, then the Default class. Results are memoised per net name: a
        `DesignRules` instance is immutable for the life of a run, so the same
        net always resolves to the same class, and the `fnmatch` pattern scan
        need only run once per distinct net.

        Args:
            net_name: the net name to resolve.

        Returns:
            The `NetClass` governing `net_name`.
        """
        cache = self.__dict__.get("_class_cache")
        if cache is None:
            cache = self.__dict__["_class_cache"] = {}
        cached = cache.get(net_name)
        if cached is not None:
            return cached
        cls_name = self.assignments.get(net_name)
        if cls_name is None:
            for pattern, name in self.patterns:
                if fnmatch.fnmatchcase(net_name, pattern):
                    cls_name = name
                    break
        if cls_name is not None and cls_name in self.classes:
            result = self.classes[cls_name]
        else:
            result = self.default_class
        cache[net_name] = result
        return result

    def clearance_for(self, net_name: str) -> float:
        """Effective clearance for a net (its class value floored by the board min).

        Args:
            net_name: the net name.

        Returns:
            The clearance in mm.
        """
        return max(self.class_for(net_name).clearance, self.min_clearance)

    def track_width_for(self, net_name: str) -> float:
        """Effective track width for a net (class value floored by the board min).

        Args:
            net_name: the net name.

        Returns:
            The track width in mm.
        """
        return max(self.class_for(net_name).track_width, self.min_track_width)

    def via_diameter_for(self, net_name: str) -> float:
        """Effective via diameter for a net (class value floored by the board min).

        Args:
            net_name: the net name.

        Returns:
            The via copper diameter in mm.
        """
        return max(self.class_for(net_name).via_diameter, self.min_via_diameter)

    def via_drill_for(self, net_name: str) -> float:
        """Effective via drill for a net (class value floored by the board min).

        Args:
            net_name: the net name.

        Returns:
            The via drill diameter in mm.
        """
        return max(self.class_for(net_name).via_drill, self.min_via_drill)

    def pair_clearance(self, net_a: str, net_b: str) -> float:
        """Clearance required between two different nets.

        KiCad uses the larger of the two nets' individual clearances.

        Args:
            net_a: one net name.
            net_b: the other net name.

        Returns:
            The required spacing in mm.
        """
        return max(self.clearance_for(net_a), self.clearance_for(net_b))


def _net_class_from_dict(d: dict) -> NetClass:
    """Build a `NetClass` from a ``.kicad_pro`` net-class dict, filling defaults.

    Args:
        d: the raw net-class mapping from the project file.

    Returns:
        The parsed `NetClass` (KiCad defaults for any missing field).
    """
    return NetClass(
        name=d.get("name", "Default"),
        clearance=float(d.get("clearance", _DEFAULT_CLEARANCE)),
        track_width=float(d.get("track_width", _DEFAULT_TRACK_WIDTH)),
        via_diameter=float(d.get("via_diameter", _DEFAULT_VIA_DIAMETER)),
        via_drill=float(d.get("via_drill", _DEFAULT_VIA_DRILL)),
    )


def default_rules() -> DesignRules:
    """Rules to use when no ``.kicad_pro`` is available."""
    default = NetClass(
        "Default", _DEFAULT_CLEARANCE, _DEFAULT_TRACK_WIDTH,
        _DEFAULT_VIA_DIAMETER, _DEFAULT_VIA_DRILL,
    )
    return DesignRules(
        classes={"Default": default},
        default_class=default,
        assignments={},
        patterns=[],
        min_clearance=0.0,
        min_track_width=_DEFAULT_TRACK_WIDTH,
        min_via_diameter=_DEFAULT_VIA_DIAMETER,
        min_via_drill=_DEFAULT_VIA_DRILL,
        min_copper_edge_clearance=_DEFAULT_EDGE_CLEARANCE,
        min_hole_to_hole=_DEFAULT_HOLE_TO_HOLE,
    )


def load_rules(pro_path: str | Path | None) -> DesignRules:
    """Parse a ``.kicad_pro`` file into `DesignRules`.

    Args:
        pro_path: path to the project ``.kicad_pro`` file, or ``None`` to use defaults.

    Returns:
        The parsed `DesignRules`, or `default_rules()` if the file is missing or None.
    """
    if pro_path is None:
        return default_rules()
    pro_path = Path(pro_path)
    if not pro_path.exists():
        return default_rules()

    data = json.loads(pro_path.read_text(encoding="utf-8"))
    net_settings = data.get("net_settings") or {}
    raw_classes = net_settings.get("classes") or []

    classes: dict[str, NetClass] = {}
    for c in raw_classes:
        nc = _net_class_from_dict(c)
        classes[nc.name] = nc
    if "Default" not in classes:
        classes["Default"] = NetClass(
            "Default", _DEFAULT_CLEARANCE, _DEFAULT_TRACK_WIDTH,
            _DEFAULT_VIA_DIAMETER, _DEFAULT_VIA_DRILL,
        )

    assignments_raw = net_settings.get("netclass_assignments") or {}
    assignments = {str(k): str(v) for k, v in assignments_raw.items()} \
        if isinstance(assignments_raw, dict) else {}

    patterns: list[tuple[str, str]] = []
    for p in net_settings.get("netclass_patterns") or []:
        pat, name = p.get("pattern"), p.get("netclass")
        if pat is not None and name is not None:
            patterns.append((str(pat), str(name)))

    rules = (data.get("board") or {}).get("design_settings", {}).get("rules", {})

    def rule(name: str, fallback: float) -> float:
        val = rules.get(name)
        return float(val) if val is not None else fallback

    return DesignRules(
        classes=classes,
        default_class=classes["Default"],
        assignments=assignments,
        patterns=patterns,
        min_clearance=rule("min_clearance", 0.0),
        min_track_width=rule("min_track_width", _DEFAULT_TRACK_WIDTH),
        min_via_diameter=rule("min_via_diameter", _DEFAULT_VIA_DIAMETER),
        min_via_drill=rule("min_through_hole_diameter", _DEFAULT_VIA_DRILL),
        min_copper_edge_clearance=rule("min_copper_edge_clearance", _DEFAULT_EDGE_CLEARANCE),
        min_hole_to_hole=rule("min_hole_to_hole", _DEFAULT_HOLE_TO_HOLE),
    )
