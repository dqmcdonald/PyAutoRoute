"""Assign KiCad footprints to unassigned schematic symbols from a preference database.

Reads a ``.kicad_sch`` file, finds placed symbols whose ``Footprint`` property is
empty, looks up the matching footprint string from a TOML preference file, and
writes the result back — round-trip-safe (only changed nodes are re-formatted).

CLI entry point: ``pyautoroute-assign`` (see ``main``).

Preference file (``~/.config/pyautoroute/footprint_prefs.toml``):

    [prefix.R]
    default = "SMD"
    SMD = "Resistor_SMD:R_0805_2012Metric_Pad1.20x1.40mm_HandSolder"
    THT = "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal"

    [prefix.U.values]
    "74AHC244" = "Package_DIP:DIP-20_W7.62mm_Socket_LongPads"

CLI override forms (positional args after the schematic path):

    R:THT                    tech-keyed  -- use THT footprint for all R
    U:74AHC244=Package_DIP:DIP-20_W7.62mm_Socket_LongPads   value-keyed
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from pyautoroute import sexpr
from pyautoroute.sexpr import Atom, SList

_DEFAULT_PREFS_PATH = Path.home() / ".config" / "pyautoroute" / "footprint_prefs.toml"
_DEFAULT_INDEX_PATH = Path.home() / ".config" / "pyautoroute" / "footprint_index.json"

# Regex to extract tags/descr from .kicad_mod files without full parse
_TAGS_RE = re.compile(r'\(tags\s+"((?:[^"\\]|\\.)*)"\s*\)')
_DESCR_RE = re.compile(r'\(descr\s+"((?:[^"\\]|\\.)*)"\s*\)')
# Resolve ${VAR} in KiCad URIs
_VAR_RE = re.compile(r'\$\{([^}]+)\}')

# Known absolute paths to the KiCad system footprint directory (macOS / Linux)
_KICAD_FP_DIR_CANDIDATES = [
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
    "/usr/share/kicad/footprints",
    "/usr/local/share/kicad/footprints",
]

# Library-name keywords that raise the score for a given reference prefix
_LIBRARY_AFFINITY: dict[str, list[str]] = {
    "R":   ["Resistor"],
    "RN":  ["Resistor", "Network"],
    "C":   ["Capacitor"],
    "CP":  ["Capacitor"],
    "L":   ["Inductor"],
    "FL":  ["Inductor", "Filter"],
    "D":   ["Diode"],
    "LED": ["LED"],
    "Q":   ["Transistor", "Package_TO_SOT"],
    "U":   ["Package_DIP", "Package_SO", "Package_QFP", "Package_QFN"],
    "J":   ["Connector"],
    "P":   ["Connector"],
    "CN":  ["Connector"],
    "SW":  ["Button_Switch"],
    "K":   ["Relay"],
    "Y":   ["Crystal", "Oscillator"],
    "X":   ["Crystal", "Oscillator"],
    "BT":  ["Battery"],
    "F":   ["Fuse"],
    "T":   ["Transformer"],
}

_DEFAULT_PREFS_CONTENT = """\
# pyautoroute footprint preference database
# Edit this file to match your preferred footprints for each component prefix.
# See: pyautoroute-assign --help

[defaults]
# Fallback technology when a prefix has no "default" key
technology = "SMD"

[prefix.R]
default = "SMD"
SMD = "Resistor_SMD:R_0805_2012Metric_Pad1.20x1.40mm_HandSolder"
THT = "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal"

[prefix.C]
default = "SMD"
SMD = "Capacitor_SMD:C_0805_2012Metric_Pad1.18x1.45mm_HandSolder"
THT = "Capacitor_THT:C_Disc_D5.0mm_W2.5mm_P5.00mm"

[prefix.L]
default = "SMD"
SMD = "Inductor_SMD:L_0805_2012Metric"
THT = "Inductor_THT:L_Axial_L5.3mm_D2.8mm_P10.16mm_Horizontal"

[prefix.D]
default = "SMD"
SMD = "Diode_SMD:D_0805_2012Metric_Pad1.15x1.40mm_HandSolder"
THT = "Diode_THT:D_DO-41_SOD81_P7.62mm_Horizontal"

[prefix.D.values]
# D refs with value "LED" use the LED footprint, not the diode footprint
"LED" = "LED_SMD:LED_0805_2012Metric_Pad1.15x1.40mm_HandSolder"

[prefix.LED]
default = "SMD"
SMD = "LED_SMD:LED_0805_2012Metric_Pad1.15x1.40mm_HandSolder"
THT = "LED_THT:LED_D5.0mm"

[prefix.CP]
# Polarised capacitors (electrolytic / tantalum)
default = "THT"
THT = "Capacitor_THT:CP_Radial_D5.0mm_P2.00mm"
SMD = "Capacitor_Tantalum_SMD:CP_EIA-3216-18_Kemet-A"

[prefix.Q]
default = "THT"
THT = "Package_TO_SOT_THT:TO-92_Inline"
SMD = "Package_TO_SOT_SMD:SOT-23"

# Value-keyed rules: matched by the symbol's Value field.
# Add entries as you accumulate a library of known ICs.
[prefix.U.values]
"74AHC244" = "Package_DIP:DIP-20_W7.62mm_Socket_LongPads"
"74HC244"  = "Package_DIP:DIP-20_W7.62mm_Socket_LongPads"
"NE555"    = "Package_DIP:DIP-8_W7.62mm_Socket_LongPads"
"LM741"    = "Package_DIP:DIP-8_W7.62mm_Socket_LongPads"
"""


# ---------------------------------------------------------------------------
# Schematic parsing helpers
# ---------------------------------------------------------------------------

def load_schematic(path: Path) -> SList:
    """Parse a ``.kicad_sch`` file and return the root s-expression tree."""
    return sexpr.loads(path.read_text(encoding="utf-8"))


def _get_unit(sym: SList) -> int:
    """Return the (unit N) value from a placed symbol node, defaulting to 1."""
    for child in sym:
        if isinstance(child, SList) and len(child) >= 2:
            if isinstance(child[0], Atom) and child[0].raw == "unit":
                try:
                    return int(child[1].raw)
                except (ValueError, IndexError):
                    pass
    return 1


def iter_placed_symbols(tree: SList) -> Iterator[SList]:
    """Yield one placed symbol node per reference designator.

    Skips the lib_symbols definition block, power/flag refs (``#PWR*``,
    ``#FLG*``), and higher-numbered units of multi-unit symbols — so a
    four-gate IC with placements U1A/U1B/U1C/U1D is represented by the
    unit-1 instance only and receives a single footprint assignment.
    """
    # Collect all candidate (ref, unit, node) triples from the top-level tree
    candidates: list[tuple[str, int, SList]] = []
    min_unit: dict[str, int] = {}

    in_lib = False
    for child in tree:
        if not isinstance(child, SList):
            continue
        if not child or not isinstance(child[0], Atom):
            continue
        tag = child[0].raw
        if tag == "lib_symbols":
            in_lib = True
            continue
        if in_lib:
            in_lib = False
        if tag != "symbol":
            continue
        ref = _get_prop_value(child, "Reference")
        if ref is None or ref.startswith("#"):
            continue
        unit = _get_unit(child)
        candidates.append((ref, unit, child))
        if ref not in min_unit or unit < min_unit[ref]:
            min_unit[ref] = unit

    # Yield only the lowest-unit instance for each reference, in document order
    seen: set[str] = set()
    for ref, unit, sym in candidates:
        if unit == min_unit[ref] and ref not in seen:
            seen.add(ref)
            yield sym


def _get_prop_node(sym: SList, name: str) -> SList | None:
    """Return the ``(property "Name" ...)`` node, or None if absent."""
    for child in sym:
        if not isinstance(child, SList) or len(child) < 2:
            continue
        if not isinstance(child[0], Atom) or child[0].raw != "property":
            continue
        if not isinstance(child[1], Atom):
            continue
        if child[1].text == name:
            return child
    return None


def _get_prop_value(sym: SList, name: str) -> str | None:
    """Return the decoded value string for a named property, or None."""
    node = _get_prop_node(sym, name)
    if node is None or len(node) < 3:
        return None
    val = node[2]
    return val.text if isinstance(val, Atom) else None


def set_footprint(sym: SList, footprint: str) -> None:
    """Mutate the Footprint property of a placed symbol node in place.

    Clears the span on the property node and the symbol node so the serializer
    re-renders them rather than emitting the cached source bytes.
    """
    prop = _get_prop_node(sym, "Footprint")
    if prop is None:
        raise ValueError("symbol has no Footprint property")
    # Replace the value atom (index 2) with a new quoted-string atom
    prop[2] = sexpr.string(footprint)
    prop.span = None
    sym.span = None


# ---------------------------------------------------------------------------
# Reference prefix
# ---------------------------------------------------------------------------

_PREFIX_RE = re.compile(r"^([A-Za-z_]+)")


def ref_prefix(ref: str) -> str:
    """Extract the leading alpha prefix from a reference designator.

    Examples: ``'R5' -> 'R'``, ``'LED2' -> 'LED'``, ``'C100' -> 'C'``.
    """
    m = _PREFIX_RE.match(ref)
    return m.group(1) if m else ref


# ---------------------------------------------------------------------------
# Preference loading and resolution
# ---------------------------------------------------------------------------

def load_prefs(path: Path) -> dict:
    """Load the TOML preference file.

    Args:
        path: path to the ``.toml`` file.

    Returns:
        The parsed dict (``{defaults: {...}, prefix: {...}}``).

    Raises:
        FileNotFoundError: if the file does not exist (with a helpful message).
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Preference file not found: {path}\n"
            f"Run `pyautoroute-assign --init-prefs` to create it."
        )
    with open(path, "rb") as f:
        return tomllib.load(f)


def resolve(prefix: str, value: str, prefs: dict, overrides: dict) -> str | None:
    """Look up the footprint string for a (prefix, value) pair.

    Resolution order:

    1. CLI value-keyed override: ``overrides[prefix][value]`` (exact footprint string)
    2. Prefs value-keyed rule: ``prefs['prefix'][prefix]['values'][value]``
    3. CLI tech override: ``overrides[prefix]`` as a tech key into ``prefs['prefix'][prefix]``
    4. Prefs default tech: ``prefs['prefix'][prefix]['default']`` → tech footprint
    5. Global default tech (``prefs['defaults']['technology']``) → tech footprint

    Args:
        prefix: the reference prefix, e.g. ``'R'``, ``'LED'``.
        value: the symbol's Value field, e.g. ``'10k'``, ``'74AHC244'``.
        prefs: the loaded TOML preference dict.
        overrides: parsed CLI overrides — either ``{prefix: tech_str}`` for
            tech-keyed, or ``{prefix: {value_str: footprint_str}}`` for
            value-keyed.

    Returns:
        The resolved footprint string, or ``None`` if unresolvable.
    """
    prefix_overrides = overrides.get(prefix, {})
    prefix_prefs = prefs.get("prefix", {}).get(prefix, {})

    # 1. CLI value-keyed override
    if isinstance(prefix_overrides, dict):
        if value in prefix_overrides:
            return prefix_overrides[value]
    elif isinstance(prefix_overrides, str):
        # Tech override present; still check value-keyed prefs first
        pass

    # 2. Prefs value-keyed rule
    values_map = prefix_prefs.get("values", {})
    if value in values_map:
        return values_map[value]

    # 3. CLI tech override
    if isinstance(prefix_overrides, str):
        tech = prefix_overrides
        fp = prefix_prefs.get(tech)
        if fp and isinstance(fp, str):
            return fp

    # 4 & 5. Default tech from prefs or global
    if not prefix_prefs:
        return None
    tech = prefix_prefs.get("default") or prefs.get("defaults", {}).get("technology", "SMD")
    fp = prefix_prefs.get(tech)
    return fp if isinstance(fp, str) else None


# ---------------------------------------------------------------------------
# Override parsing
# ---------------------------------------------------------------------------

@dataclass
class ParsedOverrides:
    """Decoded CLI override arguments."""
    # tech-keyed: prefix -> tech string (e.g. 'R' -> 'THT')
    # value-keyed: prefix -> {value -> footprint} (e.g. 'U' -> {'74AHC244': 'Pkg...'})
    data: dict = field(default_factory=dict)

    def get(self, prefix: str, default=None):
        return self.data.get(prefix, default)


def parse_overrides(args: list[str]) -> ParsedOverrides:
    """Parse positional override arguments into a structured dict.

    Two forms are accepted:

    - ``PREFIX:TECH`` — tech-keyed, e.g. ``R:THT``
    - ``PREFIX:VALUE=FOOTPRINT`` — value-keyed, e.g.
      ``U:74AHC244=Package_DIP:DIP-20_W7.62mm_Socket_LongPads``

    The split on the first ``=`` distinguishes the two forms; footprint library
    names contain colons but never ``=``.

    Args:
        args: list of raw string args (e.g. ``['R:THT', 'U:74AHC244=Pkg...']``).

    Returns:
        A ``ParsedOverrides`` object.

    Raises:
        ValueError: if an arg cannot be parsed.
    """
    result: dict = {}
    for arg in args:
        if "=" in arg:
            left, footprint = arg.split("=", 1)
            if ":" not in left:
                raise ValueError(f"Invalid value-keyed override (expected PREFIX:VALUE=FP): {arg!r}")
            prefix, val = left.split(":", 1)
            prefix, val = prefix.strip(), val.strip()
            if prefix not in result:
                result[prefix] = {}
            elif isinstance(result[prefix], str):
                raise ValueError(f"Cannot mix tech and value overrides for prefix {prefix!r}")
            result[prefix][val] = footprint.strip()
        else:
            if ":" not in arg:
                raise ValueError(f"Invalid override (expected PREFIX:TECH or PREFIX:VALUE=FP): {arg!r}")
            prefix, tech = arg.split(":", 1)
            prefix, tech = prefix.strip(), tech.strip()
            if prefix in result and isinstance(result[prefix], dict):
                raise ValueError(f"Cannot mix tech and value overrides for prefix {prefix!r}")
            result[prefix] = tech
    return ParsedOverrides(data=result)


# ---------------------------------------------------------------------------
# Footprint library index
# ---------------------------------------------------------------------------

def _lib_child_value(node: SList, key: str) -> str:
    """Return the value atom of a ``(key "value")`` child inside a lib node."""
    for child in node:
        if isinstance(child, SList) and len(child) >= 2:
            if isinstance(child[0], Atom) and child[0].text == key:
                if isinstance(child[1], Atom):
                    return child[1].text
    return ""


def _resolve_uri(uri: str, extra_vars: dict[str, str]) -> str:
    """Expand ``${VAR}`` placeholders in a KiCad library URI."""
    env = {**os.environ, **extra_vars}
    return _VAR_RE.sub(lambda m: env.get(m.group(1), m.group(0)), uri)


def _collect_pretty_dirs(
    table_path: Path,
    extra_vars: dict[str, str],
    visited: set[str] | None = None,
) -> list[Path]:
    """Recursively follow an fp-lib-table and return all .pretty library paths.

    ``type "Table"`` entries redirect to another fp-lib-table file (KiCad's
    indirection for the system library); ``type "KiCad"`` entries are actual
    ``.pretty`` directories.  A visited-set prevents cycles.
    """
    if visited is None:
        visited = set()
    key = str(table_path.resolve())
    if key in visited or not table_path.exists():
        return []
    visited.add(key)

    try:
        tree = sexpr.loads(table_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    dirs: list[Path] = []
    for child in tree:
        if not isinstance(child, SList) or not child:
            continue
        if not isinstance(child[0], Atom) or child[0].text != "lib":
            continue
        lib_type = _lib_child_value(child, "type")
        uri = _resolve_uri(_lib_child_value(child, "uri"), extra_vars)
        if lib_type == "Table":
            dirs.extend(_collect_pretty_dirs(Path(uri), extra_vars, visited))
        elif lib_type == "KiCad":
            p = Path(uri)
            if p.is_dir():
                dirs.append(p)
    return dirs


def _find_user_fp_lib_table() -> Path | None:
    """Find the most recent KiCad user fp-lib-table on this machine."""
    patterns = [
        str(Path.home() / "Library" / "Preferences" / "kicad" / "*" / "fp-lib-table"),
        str(Path.home() / ".config" / "kicad" / "*" / "fp-lib-table"),
        str(Path.home() / "AppData" / "Roaming" / "kicad" / "*" / "fp-lib-table"),
    ]
    candidates = [Path(p) for pat in patterns for p in glob.glob(pat)]
    if not candidates:
        return None

    def _ver(p: Path) -> float:
        try:
            return float(p.parent.name)
        except ValueError:
            return 0.0

    return max(candidates, key=_ver)


def _extra_vars_for_table(table_path: Path) -> dict[str, str]:
    """Build ``${KICHADn_FOOTPRINT_DIR}`` substitutions for a lib-table version."""
    extra: dict[str, str] = {}
    for candidate in _KICAD_FP_DIR_CANDIDATES:
        if Path(candidate).is_dir():
            try:
                major = table_path.parent.name.split(".")[0]
                extra[f"KICAD{major}_FOOTPRINT_DIR"] = candidate
            except (IndexError, ValueError):
                pass
            extra["KICAD_FOOTPRINT_DIR"] = candidate
            break
    return extra


def _extract_tags_descr(path: Path) -> tuple[str, str]:
    """Quickly pull tags and descr from the header of a ``.kicad_mod`` file.

    Both fields always appear in the first ~1 KB so we read only that slice
    rather than parsing the full (potentially large) geometry file.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(1024)
    except OSError:
        return "", ""
    tags_m = _TAGS_RE.search(head)
    descr_m = _DESCR_RE.search(head)
    return (
        tags_m.group(1).lower() if tags_m else "",
        descr_m.group(1).lower() if descr_m else "",
    )


def build_index(lib_table: Path | None = None) -> dict:
    """Scan all KiCad footprint libraries and return a searchable index dict.

    Args:
        lib_table: path to the user fp-lib-table; auto-detected if ``None``.

    Returns:
        A dict with keys ``version``, ``built``, ``lib_table``, ``entries``
        (list of ``[fp_string, tags, descr]`` triples, all lower-cased for
        search).

    Raises:
        FileNotFoundError: if no KiCad fp-lib-table can be located.
    """
    if lib_table is None:
        lib_table = _find_user_fp_lib_table()
    if lib_table is None:
        raise FileNotFoundError(
            "Could not find a KiCad fp-lib-table. "
            "Use --lib-table PATH to specify one."
        )

    extra_vars = _extra_vars_for_table(lib_table)
    pretty_dirs = _collect_pretty_dirs(lib_table, extra_vars)

    entries: list[list[str]] = []
    for pretty_dir in pretty_dirs:
        lib_name = pretty_dir.stem
        for mod_file in sorted(pretty_dir.glob("*.kicad_mod")):
            tags, descr = _extract_tags_descr(mod_file)
            entries.append([f"{lib_name}:{mod_file.stem}", tags, descr])

    return {
        "version": 1,
        "built": datetime.now(timezone.utc).isoformat(),
        "lib_table": str(lib_table),
        "entries": entries,
    }


def save_index(index: dict, path: Path) -> None:
    """Write the footprint index to a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")


def load_index(path: Path) -> dict | None:
    """Load the footprint index, returning ``None`` if missing or corrupt."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _query_tokens(value: str) -> list[str]:
    """Tokenise a Value string for footprint search.

    Splits on ``_``, ``-``, and spaces; also strips leading ``x`` from
    hex-style counts (``x08`` → ``08`` and ``8``) so patterns like
    ``SW_DIP_x08`` match footprint names containing ``8``.

    Additionally splits mixed alpha-numeric parts on digit/alpha boundaries
    so IC part numbers like ``74HC14`` yield sub-tokens ``['74', 'HC', '14']``,
    allowing the pin-count digit to match package names like ``DIP-14``.
    """
    tokens: list[str] = []
    seen: set[str] = set()

    def _add(t: str) -> None:
        if t and t not in seen:
            seen.add(t)
            tokens.append(t)

    for part in re.split(r"[_\-\s]+", value):
        if not part:
            continue
        _add(part)
        if part.startswith("x") and part[1:].isdigit():
            stripped = part[1:].lstrip("0") or "0"
            _add(part[1:])
            _add(stripped)
        elif part.isdigit():
            _add(str(int(part)))
        else:
            # Split on alpha-numeric boundaries: "74HC14" -> ["74", "14"] (numeric sub-tokens
            # kept for pin-count matching; alpha sub-tokens require >= 3 chars to avoid
            # false positives from 2-letter abbreviations like "HC" matching "SwitchCraft").
            for sub in re.findall(r'[A-Za-z]+|\d+', part):
                if sub.isdigit():
                    _add(sub)
                    _add(str(int(sub)))
                elif len(sub) >= 3:
                    _add(sub)
    return tokens


def _score(fp_str: str, tags: str, descr: str,
           query_tokens: list[str], affinity_kws: list[str]) -> int:
    """Score a footprint entry against query tokens and library affinity."""
    lib, _, fp_name = fp_str.partition(":")
    fp_lower = fp_name.lower()
    lib_lower = lib.lower()
    score = 0
    for tok in query_tokens:
        t = tok.lower()
        if t.isdigit():
            # Require non-digit boundary so "14" matches "DIP-14" but not "9774140360".
            # Also count only the best-field match to avoid inflating scores when the
            # package code happens to appear in name, tags, AND descr simultaneously.
            # Also exclude '.' as a preceding char so "555" in "1.555mm" doesn't match
            pat = re.compile(r'(?<![0-9.])' + re.escape(t) + r'(?!\d)')
            if pat.search(fp_lower):
                score += 3
            elif pat.search(tags):
                score += 2
            elif pat.search(descr):
                score += 1
        else:
            if t in fp_lower:
                score += 3
            if t in tags:
                score += 2
            if t in descr:
                score += 1
    for i, kw in enumerate(affinity_kws):
        if kw.lower() in lib_lower:
            # First affinity keyword is the preferred package family (+3);
            # later entries are acceptable alternatives (+2).
            score += 3 if i == 0 else 2
            break
    return score


def suggest_footprints(
    prefix: str, value: str, index: dict, n: int = 3
) -> list[str]:
    """Return up to ``n`` suggested footprint strings for a (prefix, value) pair.

    Args:
        prefix: reference prefix, e.g. ``'SW'``.
        value: symbol Value field, e.g. ``'SW_DIP_x08'``.
        index: loaded index dict from :func:`load_index`.
        n: maximum number of suggestions to return.

    Returns:
        List of ``LibraryName:FootprintName`` strings, best match first.
    """
    tokens = _query_tokens(value)
    if not tokens:
        return []
    affinity = _LIBRARY_AFFINITY.get(prefix, [])
    scored = [
        (_score(e[0], e[1], e[2], tokens, affinity), e[0])
        for e in index.get("entries", [])
    ]
    scored.sort(key=lambda x: -x[0])
    return [fp for s, fp in scored[:n] if s > 0]


# ---------------------------------------------------------------------------
# Main assignment logic
# ---------------------------------------------------------------------------

@dataclass
class AssignResult:
    """Summary of a footprint assignment run."""
    # (ref, value, old_fp, new_fp) — old_fp is "" when the symbol was unassigned
    assigned: list[tuple[str, str, str, str]] = field(default_factory=list)
    # (ref, value, current_fp) — skipped because already assigned and --all not set
    skipped_assigned: list[tuple[str, str, str]] = field(default_factory=list)
    # (ref, value) — skipped because no preference for this prefix
    skipped_unknown: list[tuple[str, str]] = field(default_factory=list)


def assign_footprints(
    sch_path: Path,
    prefs: dict,
    overrides: ParsedOverrides,
    *,
    reassign: bool = False,
    dry_run: bool = False,
) -> AssignResult:
    """Assign footprints to unassigned (or all) symbols in a schematic.

    Args:
        sch_path: path to the ``.kicad_sch`` file to modify.
        prefs: loaded preference dict from :func:`load_prefs`.
        overrides: parsed CLI overrides from :func:`parse_overrides`.
        reassign: when True, also replace already-assigned footprints.
        dry_run: when True, compute changes but do not write the file.

    Returns:
        An :class:`AssignResult` describing what was done.
    """
    tree = load_schematic(sch_path)
    result = AssignResult()

    for sym in iter_placed_symbols(tree):
        ref = _get_prop_value(sym, "Reference") or "?"
        value = _get_prop_value(sym, "Value") or ""
        current_fp = _get_prop_value(sym, "Footprint") or ""

        if current_fp and not reassign:
            result.skipped_assigned.append((ref, value, current_fp))
            continue

        prefix = ref_prefix(ref)
        footprint = resolve(prefix, value, prefs, overrides.data)

        if footprint is None:
            result.skipped_unknown.append((ref, value))
            continue

        if not dry_run:
            set_footprint(sym, footprint)
        result.assigned.append((ref, value, current_fp, footprint))

    if not dry_run and result.assigned:
        # Write back: fresh root forces re-serialization; unmodified children
        # keep their cached source spans so the diff is minimal.
        new_root = SList()
        for ch in tree:
            new_root.append(ch)
        sch_path.write_text(sexpr.dump_file(new_root), encoding="utf-8")

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _init_prefs(prefs_path: Path) -> None:
    if prefs_path.exists():
        print(f"Preference file already exists: {prefs_path}")
        print("Remove it first if you want to reset to defaults.")
        sys.exit(1)
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    prefs_path.write_text(_DEFAULT_PREFS_CONTENT, encoding="utf-8")
    print(f"Created: {prefs_path}")
    print("Edit this file to match your preferred footprints.")


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``pyautoroute-assign``."""
    parser = argparse.ArgumentParser(
        prog="pyautoroute-assign",
        description=(
            "Assign footprints to unassigned KiCad schematic symbols "
            "based on a preference database."
        ),
        epilog=(
            "OVERRIDE forms:\n"
            "  PREFIX:TECH             e.g. R:THT  C:SMD\n"
            "  PREFIX:VALUE=FOOTPRINT  e.g. 'U:74AHC244=Package_DIP:DIP-20_W7.62mm_Socket_LongPads'\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "schematic", nargs="?", metavar="SCHEMATIC.kicad_sch",
        help="schematic file to process",
    )
    parser.add_argument(
        "overrides", nargs="*", metavar="OVERRIDE",
        help="per-invocation overrides (PREFIX:TECH or PREFIX:VALUE=FOOTPRINT)",
    )
    parser.add_argument(
        "--all", "-a", dest="reassign", action="store_true",
        help="also re-assign already-assigned footprints",
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true",
        help="show what would change without writing the file",
    )
    parser.add_argument(
        "--prefs", metavar="PATH", type=Path, default=_DEFAULT_PREFS_PATH,
        help=f"preference file (default: {_DEFAULT_PREFS_PATH})",
    )
    parser.add_argument(
        "--init-prefs", action="store_true",
        help="write a default preference file and exit",
    )
    parser.add_argument(
        "--index", metavar="PATH", type=Path, default=_DEFAULT_INDEX_PATH,
        help=f"footprint index file (default: {_DEFAULT_INDEX_PATH})",
    )
    parser.add_argument(
        "--rebuild-index", action="store_true",
        help="scan KiCad footprint libraries and rebuild the search index, then exit",
    )
    parser.add_argument(
        "--lib-table", metavar="PATH", type=Path, default=None,
        help="KiCad fp-lib-table to use when building the index (auto-detected if omitted)",
    )
    parser.add_argument(
        "--suggest", metavar="N", type=int, default=3,
        help="number of library suggestions for unrecognised prefixes (0 to disable; "
             "requires --rebuild-index to have been run first; default: 3)",
    )

    args = parser.parse_args(argv)

    if args.init_prefs:
        _init_prefs(args.prefs)
        return

    if args.rebuild_index:
        print("Scanning KiCad footprint libraries…", end=" ", flush=True)
        try:
            index = build_index(lib_table=args.lib_table)
        except FileNotFoundError as e:
            print(f"\nerror: {e}", file=sys.stderr)
            sys.exit(1)
        save_index(index, args.index)
        n = len(index["entries"])
        print(f"{n} footprints indexed → {args.index}")
        return

    if not args.schematic:
        parser.error("SCHEMATIC.kicad_sch is required")

    sch_path = Path(args.schematic)
    if not sch_path.exists():
        print(f"error: file not found: {sch_path}", file=sys.stderr)
        sys.exit(1)

    try:
        overrides = parse_overrides(args.overrides)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        prefs = load_prefs(args.prefs)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    index = load_index(args.index) if args.suggest > 0 else None

    result = assign_footprints(
        sch_path, prefs, overrides,
        reassign=args.reassign,
        dry_run=args.dry_run,
    )

    tag = sch_path.name
    n_assigned = len(result.assigned)
    n_unknown = len(result.skipped_unknown)
    n_already = len(result.skipped_assigned)
    dry_tag = " [dry run]" if args.dry_run else ""

    print(f"{tag}{dry_tag} — {n_assigned} assigned, "
          f"{n_unknown} skipped (unknown prefix), "
          f"{n_already} skipped (already assigned)")

    for ref, value, old_fp, new_fp in result.assigned:
        if old_fp:
            print(f"  {ref:<6}  [{value}]  {old_fp}  →  {new_fp}")
        else:
            print(f"  {ref:<6}  [{value}]  →  {new_fp}")

    if result.skipped_assigned:
        print("  already assigned (use --all to reassign):")
        for ref, value, current_fp in result.skipped_assigned:
            print(f"    {ref:<6}  [{value}]  {current_fp}")

    if result.skipped_unknown:
        hint = "" if index else "  (run --rebuild-index to enable suggestions)"
        print(f"  no preference for:{hint}")
        for ref, value in result.skipped_unknown:
            print(f"    {ref:<6}  [{value}]")
            if index:
                suggestions = suggest_footprints(
                    ref_prefix(ref), value, index, args.suggest
                )
                for fp in suggestions:
                    print(f"             → {fp}")
