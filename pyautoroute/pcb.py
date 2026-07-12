"""Board model loaded from a ``.kicad_pcb`` s-expression, plus a routed-board
writer.

Parsing covers what the router needs: the copper layer stack, every pad with its
absolute position/rotation/shape, the per-footprint grouping (origin, lock state,
the ``Autoroute-overlap`` flag, and each pad's local offset, used by the optional
placement pass), the net-reference style (name-only as in KiCad 10, or a numbered
net table as in KiCad 6-9), existing tracks/vias/zones to treat as obstacles, the
free (dangling) vias to strip, and the Edge.Cuts outline shapes.

The writer clones the parsed tree, drops the free vias, appends freshly-built
``(segment ...)`` / ``(via ...)`` nodes, and serializes. Untouched subtrees keep
their source spans so the diff against the input is limited to the routing edits.
When the placement pass has moved footprints, `sync_tree_from_placement` rewrites
each moved footprint's ``(at ...)`` and regenerates the Edge.Cuts outline before
the write.
"""

from __future__ import annotations

import copy
import math
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import sexpr
from .sexpr import Atom, SList


# --- small s-expr accessors --------------------------------------------------

def child(node: SList, key: str) -> SList | None:
    """Return the first sub-list of `node` whose head symbol is `key`.

    Args:
        node: the parent list to search.
        key: the head symbol to match, e.g. ``"at"`` or ``"layers"``.

    Returns:
        The first matching child list, or `None` if there is none.
    """
    for it in node:
        if isinstance(it, SList) and sexpr.head_symbol(it) == key:
            return it
    return None


def children(node: SList, key: str):
    """Return every sub-list of `node` whose head symbol is `key`.

    Args:
        node: the parent list to search.
        key: the head symbol to match, e.g. ``"footprint"`` or ``"pad"``.

    Returns:
        A list of all matching child lists (possibly empty).
    """
    return [it for it in node if isinstance(it, SList) and sexpr.head_symbol(it) == key]


def atoms_after_head(node: SList) -> list[Atom]:
    """Return the atom tokens following a list's head symbol.

    Args:
        node: the list whose trailing atoms are wanted, e.g. ``(at 1 2 90)``.

    Returns:
        The `Atom` items after index 0 (sub-lists are skipped).
    """
    return [it for it in node[1:] if isinstance(it, Atom)]


def _is_numeric_atom(a: Atom) -> bool:
    try:
        float(a.raw)
        return True
    except ValueError:
        return False


def floats(node: SList | None) -> list[float]:
    """Read a list's trailing atoms as floats, e.g. ``(at 1 2)`` -> ``[1.0, 2.0]``.

    Args:
        node: the list to read, or `None`.

    Returns:
        The parsed floats, or ``[]`` when `node` is `None`.
    """
    if node is None:
        return []
    return [a.as_float() for a in atoms_after_head(node)]


def strings(node: SList | None) -> list[str]:
    """Read a list's trailing atoms as decoded strings.

    Args:
        node: the list to read, or `None`.

    Returns:
        The decoded text of each trailing atom, or ``[]`` when `node` is `None`.
    """
    if node is None:
        return []
    return [a.text for a in atoms_after_head(node)]


# --- geometry transform ------------------------------------------------------

def rotate(x: float, y: float, angle_deg: float) -> tuple[float, float]:
    """Rotate a point using KiCad's RotatePoint convention (matches pcbnew).

    Args:
        x: x offset from the rotation centre (mm).
        y: y offset from the rotation centre (mm).
        angle_deg: rotation angle in degrees (KiCad's sign convention).

    Returns:
        The rotated ``(x, y)`` offset.
    """
    a = math.radians(angle_deg)
    s, c = math.sin(a), math.cos(a)
    return (x * c + y * s, -x * s + y * c)


# --- model -------------------------------------------------------------------

@dataclass
class Pad:
    net: str
    pad_type: str            # smd | thru_hole | np_thru_hole | connect
    shape: str               # rect | roundrect | circle | oval | trapezoid | custom
    cx: float                # absolute centre (mm, KiCad coords, Y down)
    cy: float
    w: float
    h: float
    angle: float             # absolute rotation (deg)
    copper_layers: list[str]
    roundrect_rratio: float | None = None
    rect_delta: tuple[float, float] | None = None
    drill: float | None = None
    fp_ref: str = ""


@dataclass
class Via:
    cx: float
    cy: float
    size: float
    drill: float
    layers: tuple[str, str]
    net: str
    node: SList | None = None     # source node (so the writer can strip it)


@dataclass
class Segment:
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    layer: str
    net: str
    node: "SList | None" = None   # source node, so write_board can strip it


@dataclass
class OutlineShape:
    kind: str                 # poly | line | rect | arc | circle
    data: dict


@dataclass
class Footprint:
    """A placed footprint: its origin/rotation plus the data the placement pass
    needs to move it and keep its pads consistent.

    `local_offsets` holds, per entry in `pads`, the pad's ``(px, py)`` offset from
    the footprint origin and its rotation *relative to* the footprint, so a move
    recomputes each pad's absolute centre/angle (`sync_pads`). `at_node`/`fp_node`
    are the live source nodes the writer rewrites; `x0`/`y0`/`angle0` record the
    parsed pose so `moved` can tell whether the footprint was actually relocated.
    """

    ref: str
    x: float
    y: float
    angle: float
    locked: bool                                   # honoured as fixed by placement
    overlap_ok: bool                               # body may overlap others (e.g. a shield)
    pads: list[Pad]
    local_offsets: list[tuple[float, float, float]]
    at_node: SList
    fp_node: SList
    x0: float = 0.0
    y0: float = 0.0
    angle0: float = 0.0
    edge_affinity: str | None = None   # placement: pull to board edge; None | any | left | right | top | bottom
    uuid: str = ""                     # footprint's KiCad UUID (used to resolve native group membership)
    group_id: str | None = None        # UUID of the KiCad group this footprint belongs to, or None
    decouple_target: str | None = None  # decoupling cap: IC refdes it serves, "auto", or None

    @property
    def moved(self) -> bool:
        """Whether the footprint's pose differs from the parsed one."""
        return (abs(self.x - self.x0) > 1e-6 or abs(self.y - self.y0) > 1e-6
                or abs(self.angle - self.angle0) > 1e-6)

    def sync_pads(self) -> None:
        """Recompute each pad's absolute centre/rotation from the current pose.

        Applies the footprint's ``(x, y, angle)`` to every stored local offset so
        `Board.pads` reflects the footprint's position after a placement move.
        """
        for pad, (px, py, local_angle) in zip(self.pads, self.local_offsets):
            rx, ry = rotate(px, py, self.angle)
            pad.cx = self.x + rx
            pad.cy = self.y + ry
            pad.angle = local_angle + self.angle


@dataclass
class Stackup:
    """Substrate parameters parsed from the board's ``(setup (stackup ...))`` block.

    Used for differential-pair impedance estimates. Defaults match a standard
    1.6 mm two-layer FR4 board with 1 oz copper when no stackup is present.
    """
    copper_thickness: float = 0.035   # mm (1 oz Cu)
    dielectric_h: float = 1.6        # mm (core height)
    epsilon_r: float = 4.5           # relative permittivity (FR4)


@dataclass
class Board:
    tree: SList
    copper_layers: list[str]               # ordered; [0] is the preferred front
    pads: list[Pad]
    free_vias: list[Via]                   # dangling top-level vias (to strip)
    segments: list[Segment]
    zones: list[dict]
    outline: list[OutlineShape]
    numbered_nets: dict[int, str] = field(default_factory=dict)
    name_only_nets: bool = True
    footprints: list[Footprint] = field(default_factory=list)
    outline_synthesized: bool = False   # True when no Edge.Cuts found and a default was generated
    stackup: Stackup = field(default_factory=Stackup)

    @property
    def front_layer(self) -> str:
        """The preferred front copper layer (first in the stack), e.g. ``F.Cu``."""
        return self.copper_layers[0]

    @property
    def back_layer(self) -> str:
        """The back copper layer (last in the stack), e.g. ``B.Cu``."""
        return self.copper_layers[-1]

    def pads_by_net(self) -> dict[str, list[Pad]]:
        """Group the board's pads by net name.

        Returns:
            A mapping of net name -> pads on that net; pads with no net (``""``)
            are omitted.
        """
        out: dict[str, list[Pad]] = {}
        for p in self.pads:
            if p.net:
                out.setdefault(p.net, []).append(p)
        return out


# --- net reference parsing ---------------------------------------------------

def _net_name(net_node: SList | None, numbered: dict[int, str]) -> str:
    """Resolve a ``(net ...)`` reference to a net name across both file styles.

    Args:
        net_node: the ``(net ...)`` node, or `None` (treated as no net).
        numbered: the board's net-number -> name table (empty for name-only
            KiCad 10 files), used to resolve a bare numeric reference.

    Returns:
        The net name, or ``""`` when there is no net reference.
    """
    if net_node is None:
        return ""
    items = atoms_after_head(net_node)
    if not items:
        return ""
    if len(items) == 1:
        a = items[0]
        if a.is_string:
            return a.text                       # name-only: (net "GND")
        return numbered.get(int(a.as_float()), "")   # numbered: (net 3)
    # (net 3 "GND")
    return items[-1].text


# --- layer helpers -----------------------------------------------------------

def _copper_layers(tree: SList) -> list[str]:
    """Extract the ordered copper-layer names from the board's ``(layers ...)``.

    Args:
        tree: the parsed board tree.

    Returns:
        Copper layer names (those ending in ``.Cu``) in board order; falls back
        to ``["F.Cu", "B.Cu"]`` if none are found.
    """
    layers_node = child(tree, "layers")
    out: list[str] = []
    if layers_node is None:
        return ["F.Cu", "B.Cu"]
    for entry in layers_node:
        if not isinstance(entry, SList):
            continue
        # (0 "F.Cu" signal ...)
        toks = [it for it in entry]
        if len(toks) >= 3 and isinstance(toks[1], Atom):
            name = toks[1].text
            if name.endswith(".Cu"):
                out.append(name)
    return out or ["F.Cu", "B.Cu"]


def _fab_layers(tree: SList) -> tuple[str, str]:
    """Return the front and back fabrication layer names from the board's layer table.

    Looks for layer entries whose canonical name contains ``Fab`` or
    ``Fabrication`` (case-insensitive). Falls back to ``"F.Fab"`` / ``"B.Fab"``
    when the board has no layer table or no matching entries.

    Args:
        tree: the parsed board tree.

    Returns:
        A ``(front_fab, back_fab)`` pair, e.g. ``("F.Fab", "B.Fab")``.
    """
    layers_node = child(tree, "layers")
    front = back = ""
    if layers_node is not None:
        for entry in layers_node:
            if not isinstance(entry, SList):
                continue
            toks = [it for it in entry]
            if len(toks) < 2 or not isinstance(toks[1], Atom):
                continue
            name = toks[1].text
            low = name.lower()
            if "fab" in low or "fabrication" in low:
                if low.startswith("f."):
                    front = name
                elif low.startswith("b."):
                    back = name
    return front or "F.Fab", back or "B.Fab"


def _silk_layers(tree: SList) -> tuple[str, str]:
    """Return the front and back silkscreen layer names from the board's layer table.

    Looks for layer entries whose canonical name contains ``SilkS`` or
    ``Silkscreen`` (case-insensitive). Falls back to ``"F.SilkS"`` / ``"B.SilkS"``
    when the board has no layer table or no matching entries.

    Args:
        tree: the parsed board tree.

    Returns:
        A ``(front_silk, back_silk)`` pair, e.g. ``("F.SilkS", "B.SilkS")``.
    """
    layers_node = child(tree, "layers")
    front = back = ""
    if layers_node is not None:
        for entry in layers_node:
            if not isinstance(entry, SList):
                continue
            toks = [it for it in entry]
            if len(toks) < 2 or not isinstance(toks[1], Atom):
                continue
            name = toks[1].text
            low = name.lower()
            if "silks" in low or "silkscreen" in low:
                if low.startswith("f."):
                    front = name
                elif low.startswith("b."):
                    back = name
    return front or "F.SilkS", back or "B.SilkS"


def _resolve_pad_layers(layer_strings: list[str], copper: list[str]) -> list[str]:
    """Resolve a pad's layer tokens to concrete copper-layer names.

    Args:
        layer_strings: the pad's raw ``(layers ...)`` tokens, which may include
            the wildcard ``"*.Cu"``.
        copper: the board's copper-layer names, used to expand ``"*.Cu"``.

    Returns:
        The copper layers the pad occupies (all of `copper` for a ``*.Cu`` pad).
    """
    out: list[str] = []
    for ls in layer_strings:
        if ls == "*.Cu":
            return list(copper)
        if ls.endswith(".Cu") and ls in copper:
            out.append(ls)
    return out


# --- pad parsing -------------------------------------------------------------

def _parse_pad(pad_node: SList, fx: float, fy: float, fa: float,
               copper: list[str], numbered: dict[int, str], fp_ref: str) -> Pad | None:
    """Parse one ``(pad ...)`` node into an absolute-positioned `Pad`.

    Args:
        pad_node: the ``(pad ...)`` s-expression.
        fx: the parent footprint's x origin (mm).
        fy: the parent footprint's y origin (mm).
        fa: the parent footprint's rotation (degrees), applied to the pad offset.
        copper: the board's copper-layer names (for layer resolution).
        numbered: the net-number -> name table (for net resolution).
        fp_ref: the parent footprint's reference designator, stored on the pad.

    Returns:
        The `Pad` with absolute centre/rotation, or `None` if the node lacks the
        expected number/type/shape header.
    """
    head = atoms_after_head(pad_node)
    # (pad "1" smd roundrect ...) -> [number, type, shape]
    if len(head) < 3:
        return None
    pad_type = head[1].raw
    shape = head[2].raw

    at = floats(child(pad_node, "at"))
    px, py = (at + [0.0, 0.0])[:2]
    pad_angle = at[2] if len(at) >= 3 else 0.0

    size = floats(child(pad_node, "size"))
    w, h = (size + [0.0, 0.0])[:2]

    rx, ry = rotate(px, py, fa)
    cx, cy = fx + rx, fy + ry

    layers = _resolve_pad_layers(strings(child(pad_node, "layers")), copper)

    rratio_node = child(pad_node, "roundrect_rratio")
    rratio = rratio_node[1].as_float() if rratio_node else None

    delta = floats(child(pad_node, "rect_delta"))
    rect_delta = (delta[0], delta[1]) if len(delta) >= 2 else None

    drill_node = child(pad_node, "drill")
    drill = None
    if drill_node is not None:
        # Skip non-numeric atoms (e.g. "oval" shape keyword) — KiCad emits
        # (drill d) for circular or (drill oval dx dy) for elongated drills.
        df = [a.as_float() for a in atoms_after_head(drill_node)
              if _is_numeric_atom(a)]
        if df:
            drill = df[-1]   # circular: df=[d]; oval: df=[dx, dy], use larger

    net = _net_name(child(pad_node, "net"), numbered)

    return Pad(
        net=net, pad_type=pad_type, shape=shape,
        cx=cx, cy=cy, w=w, h=h, angle=pad_angle,
        copper_layers=layers, roundrect_rratio=rratio,
        rect_delta=rect_delta, drill=drill, fp_ref=fp_ref,
    )


def _footprint_ref(fp_node: SList) -> str:
    """Read a footprint's reference designator from its ``Reference`` property.

    Args:
        fp_node: the ``(footprint ...)`` node.

    Returns:
        The reference (e.g. ``"R2"``), or ``""`` if absent.
    """
    for prop in children(fp_node, "property"):
        vals = atoms_after_head(prop)
        if vals and vals[0].text == "Reference" and len(vals) >= 2:
            return vals[1].text
    return ""


def _footprint_locked(fp_node: SList) -> bool:
    """Return whether a footprint is locked (a fixed position for placement).

    Handles both KiCad serialisations: a bare ``locked`` token among the
    footprint's children, or a ``(locked yes)`` sub-list.

    Args:
        fp_node: the ``(footprint ...)`` node.

    Returns:
        True if the footprint is locked.
    """
    for it in fp_node:
        if isinstance(it, Atom) and it.raw == "locked":
            return True
        if isinstance(it, SList) and sexpr.head_symbol(it) == "locked":
            vals = atoms_after_head(it)
            if vals and vals[0].text in ("yes", "true"):
                return True
    return False


def _footprint_overlap_ok(fp_node: SList) -> bool:
    """Return whether a footprint opts in to body overlap during placement.

    Reads the user-defined ``Autoroute-overlap`` property: a truthy value
    (``yes`` / ``true`` / ``1`` / ``overlap``, case-insensitive) means the
    footprint's body may overlap others — e.g. an Arduino shield sitting over the
    board it plugs into. Its pads are still kept clear of other copper.

    Args:
        fp_node: the ``(footprint ...)`` node.

    Returns:
        True if the ``Autoroute-overlap`` property is set truthy.
    """
    for prop in children(fp_node, "property"):
        vals = atoms_after_head(prop)
        if len(vals) >= 2 and vals[0].text == "Autoroute-overlap":
            return vals[1].text.strip().lower() in ("yes", "true", "1", "overlap")
    return False


_EDGE_SIDES = ("left", "right", "top", "bottom")


def _footprint_edge_affinity(fp_node: SList) -> str | None:
    """Return a footprint's board-edge placement affinity, or ``None``.

    Reads the user-defined ``Autoroute-edge`` property. Its value (case-insensitive)
    is the target side:

    - ``left`` / ``right`` / ``top`` / ``bottom`` → that side;
    - ``any`` (also an empty / ``yes`` / ``true`` value) → the nearest board edge.

    A flagged footprint is pulled to the boundary *and* oriented to lie flat
    against it during the optional placement pass — useful for connectors and
    similar parts that must sit on the board edge. An unrecognised value is
    ignored (returns ``None``).

    Args:
        fp_node: the ``(footprint ...)`` node.

    Returns:
        ``"any"``, a side name, or ``None`` if the property is absent or invalid.
    """
    for prop in children(fp_node, "property"):
        vals = atoms_after_head(prop)
        if len(vals) >= 2 and vals[0].text == "Autoroute-edge":
            val = vals[1].text.strip().lower()
            if val in _EDGE_SIDES:
                return val
            if val in ("", "any", "yes", "true"):
                return "any"
            return None
    return None


def _footprint_decouple(fp_node: SList) -> str | None:
    """Return a decoupling cap's associated-IC target, or ``None``.

    Reads the user-defined ``Autoroute-decouple`` property. Its value is the
    reference designator of the IC this decoupling cap serves (e.g. ``U3``), so
    the placement pass keeps the cap next to that IC; the special value ``auto``
    asks placement to find the IC by searching the cap's shared power/ground nets
    (`pyautoroute.netlist.resolve_decoupling_ic`). An absent or empty value means
    the footprint is not marked as a decoupling cap.

    Args:
        fp_node: the ``(footprint ...)`` node.

    Returns:
        The target refdes, the string ``"auto"``, or ``None``.
    """
    for prop in children(fp_node, "property"):
        vals = atoms_after_head(prop)
        if len(vals) >= 2 and vals[0].text == "Autoroute-decouple":
            v = vals[1].text.strip()
            return v or None
    return None


def _footprint_uuid(fp_node: SList) -> str:
    """Return the footprint's KiCad UUID, or ``""`` if absent.

    Args:
        fp_node: the ``(footprint ...)`` node.

    Returns:
        The UUID string (e.g. ``"0d488f2c-a923-4fda-b2f9-73d85518f74e"``).
    """
    for it in fp_node:
        if isinstance(it, SList) and sexpr.head_symbol(it) == "uuid":
            vals = atoms_after_head(it)
            if vals:
                return vals[0].text
    return ""


def _parse_groups(tree: SList) -> dict[str, str]:
    """Build a footprint-UUID → group-UUID map from top-level ``(group ...)`` nodes.

    KiCad 6+ serialises native groups as top-level ``(group "" (uuid ...) (members
    uuid1 uuid2 ...))`` nodes alongside footprints. Returns a dict mapping each
    member UUID to the UUID of the group it belongs to, so callers can annotate
    footprints after parsing them.

    Args:
        tree: the root board s-expression tree.

    Returns:
        A ``{member_uuid: group_uuid}`` mapping; empty when no groups exist.
    """
    result: dict[str, str] = {}
    for node in tree:
        if not (isinstance(node, SList) and sexpr.head_symbol(node) == "group"):
            continue
        group_uuid = ""
        for child_node in node:
            if isinstance(child_node, SList) and sexpr.head_symbol(child_node) == "uuid":
                vals = atoms_after_head(child_node)
                if vals:
                    group_uuid = vals[0].text
            elif isinstance(child_node, SList) and sexpr.head_symbol(child_node) == "members":
                for member in atoms_after_head(child_node):
                    if group_uuid:
                        result[member.text] = group_uuid
    return result


# --- outline parsing ---------------------------------------------------------

def _parse_outline(tree: SList) -> list[OutlineShape]:
    """Collect the board-edge graphic shapes from the ``Edge.Cuts`` layer.

    Args:
        tree: the parsed board tree.

    Returns:
        One `OutlineShape` per ``gr_poly`` / ``gr_line`` / ``gr_rect`` /
        ``gr_arc`` / ``gr_circle`` found on ``Edge.Cuts``.
    """
    shapes: list[OutlineShape] = []
    for it in tree:
        if not isinstance(it, SList):
            continue
        head = sexpr.head_symbol(it)
        if head not in ("gr_poly", "gr_line", "gr_rect", "gr_arc", "gr_circle"):
            continue
        layer = strings(child(it, "layer"))
        if not layer or layer[0] != "Edge.Cuts":
            continue
        if head == "gr_poly":
            pts = _pts(child(it, "pts"))
            shapes.append(OutlineShape("poly", {"pts": pts}))
        elif head == "gr_line":
            shapes.append(OutlineShape("line", {
                "start": tuple(floats(child(it, "start"))),
                "end": tuple(floats(child(it, "end"))),
            }))
        elif head == "gr_rect":
            shapes.append(OutlineShape("rect", {
                "start": tuple(floats(child(it, "start"))),
                "end": tuple(floats(child(it, "end"))),
            }))
        elif head == "gr_arc":
            shapes.append(OutlineShape("arc", {
                "start": tuple(floats(child(it, "start"))),
                "mid": tuple(floats(child(it, "mid"))),
                "end": tuple(floats(child(it, "end"))),
            }))
        elif head == "gr_circle":
            shapes.append(OutlineShape("circle", {
                "center": tuple(floats(child(it, "center"))),
                "end": tuple(floats(child(it, "end"))),
            }))
    return shapes


def _pts(pts_node: SList | None) -> list[tuple[float, float]]:
    """Extract ``(xy x y)`` coordinate pairs from a ``(pts ...)`` node.

    Args:
        pts_node: the ``(pts ...)`` node, or `None`.

    Returns:
        The ``(x, y)`` points in order, or ``[]`` when `pts_node` is `None`.
    """
    if pts_node is None:
        return []
    out = []
    for xy in pts_node:
        if isinstance(xy, SList) and sexpr.head_symbol(xy) == "xy":
            f = floats(xy)
            if len(f) >= 2:
                out.append((f[0], f[1]))
    return out


# --- top-level via / segment / zone parsing ----------------------------------

def _parse_free_vias(tree: SList, numbered: dict[int, str]) -> list[Via]:
    """Parse the top-level (dangling) ``(via ...)`` nodes, keeping their source.

    Args:
        tree: the parsed board tree.
        numbered: the net-number -> name table (for net resolution).

    Returns:
        One `Via` per top-level via, each retaining its source node so the
        writer can strip it.
    """
    out = []
    for it in children(tree, "via"):
        at = floats(child(it, "at"))
        size = floats(child(it, "size"))
        drill = floats(child(it, "drill"))
        lays = strings(child(it, "layers"))
        out.append(Via(
            cx=at[0] if at else 0.0, cy=at[1] if len(at) > 1 else 0.0,
            size=size[0] if size else 0.6,
            drill=drill[0] if drill else 0.3,
            layers=(lays[0], lays[-1]) if lays else ("F.Cu", "B.Cu"),
            net=_net_name(child(it, "net"), numbered),
            node=it,
        ))
    return out


def _parse_segments(tree: SList, numbered: dict[int, str]) -> list[Segment]:
    """Parse the existing ``(segment ...)`` tracks (routing obstacles).

    Args:
        tree: the parsed board tree.
        numbered: the net-number -> name table (for net resolution).

    Returns:
        One `Segment` per track segment with valid start/end points.
    """
    out = []
    for it in children(tree, "segment"):
        s = floats(child(it, "start"))
        e = floats(child(it, "end"))
        w = floats(child(it, "width"))
        lay = strings(child(it, "layer"))
        if len(s) >= 2 and len(e) >= 2:
            out.append(Segment(
                x1=s[0], y1=s[1], x2=e[0], y2=e[1],
                width=w[0] if w else 0.2,
                layer=lay[0] if lay else "F.Cu",
                net=_net_name(child(it, "net"), numbered),
                node=it,
            ))
    return out


def _parse_zones(tree: SList, numbered: dict[int, str]) -> list[dict]:
    """Parse copper ``(zone ...)`` regions into ``{net, layers, polygon, fill_enabled}`` dicts.

    Args:
        tree: the parsed board tree.
        numbered: the net-number -> name table (for net resolution).

    Returns:
        One dict per zone with its net name, layer list, outline points, and
        whether the zone has an active copper fill (``fill_enabled``).
    """
    out = []
    for it in children(tree, "zone"):
        fill_node = child(it, "fill")
        fill_enabled = False
        if fill_node is not None:
            a = atoms_after_head(fill_node)
            fill_enabled = bool(a) and a[0].text == "yes"
        out.append({
            "net": _net_name(child(it, "net_name"), numbered)
                   or _net_name(child(it, "net"), numbered),
            "layers": strings(child(it, "layers")) or strings(child(it, "layer")),
            "polygon": _pts(child(child(it, "polygon"), "pts") if child(it, "polygon") else None),
            "fill_enabled": fill_enabled,
            "node": it,
        })
    return out


def _numbered_net_table(tree: SList) -> dict[int, str]:
    """Read the top-level ``(net N "name")`` declarations (KiCad 6-9).

    Args:
        tree: the parsed board tree.

    Returns:
        A net-number -> name mapping; empty for name-only (KiCad 10) files.
    """
    table: dict[int, str] = {}
    for it in children(tree, "net"):
        a = atoms_after_head(it)
        if len(a) >= 2 and not a[0].is_string:
            table[int(a[0].as_float())] = a[1].text
    return table


# --- public API --------------------------------------------------------------

def _parse_stackup(tree: SList) -> Stackup:
    """Extract substrate parameters from the board's stackup block.

    Looks for the first dielectric layer (type ``core`` or ``prepreg``) to get
    the height and permittivity, and the first copper layer for thickness.
    Falls back to FR4 defaults when the block is absent or fields are missing.

    Args:
        tree: the root s-expression of the ``.kicad_pcb`` file.

    Returns:
        A `Stackup` with parsed (or default) values.
    """
    result = Stackup()
    setup = child(tree, "setup")
    if setup is None:
        return result
    stackup_node = child(setup, "stackup")
    if stackup_node is None:
        return result

    for layer_node in children(stackup_node, "layer"):
        type_node = child(layer_node, "type")
        if type_node is None:
            continue
        type_atoms = atoms_after_head(type_node)
        if not type_atoms:
            continue
        layer_type = type_atoms[0].text.strip('"')

        thickness_node = child(layer_node, "thickness")
        thickness = floats(thickness_node)

        if layer_type == "copper" and thickness:
            result.copper_thickness = thickness[0]
        elif layer_type in ("core", "prepreg") and thickness:
            result.dielectric_h = thickness[0]
            er_node = child(layer_node, "epsilon_r")
            er_vals = floats(er_node)
            if er_vals:
                result.epsilon_r = er_vals[0]
            break   # use the first dielectric layer only

    return result


def load_board(pcb_path: str | Path) -> Board:
    """Parse a ``.kicad_pcb`` file into a `Board` model.

    Reads the s-expression directly (no `pcbnew`) and collects the copper stack,
    every pad with its absolute position/rotation/shape, existing tracks/vias/
    zones, the free (dangling) vias, the net-reference style, and the Edge.Cuts
    outline.

    Args:
        pcb_path: path to the ``.kicad_pcb`` file.

    Returns:
        The populated `Board`.
    """
    tree = sexpr.loads(Path(pcb_path).read_text(encoding="utf-8"))
    copper = _copper_layers(tree)
    numbered = _numbered_net_table(tree)
    name_only = len(numbered) == 0

    groups = _parse_groups(tree)
    pads: list[Pad] = []
    footprints: list[Footprint] = []
    for fp in children(tree, "footprint"):
        at_node = child(fp, "at")
        at = floats(at_node)
        fx, fy = (at + [0.0, 0.0])[:2]
        fa = at[2] if len(at) >= 3 else 0.0
        ref = _footprint_ref(fp)
        fp_uuid = _footprint_uuid(fp)
        fp_pads: list[Pad] = []
        local_offsets: list[tuple[float, float, float]] = []
        for pad_node in children(fp, "pad"):
            p = _parse_pad(pad_node, fx, fy, fa, copper, numbered, ref)
            if p is None:
                continue
            pads.append(p)
            fp_pads.append(p)
            pat = floats(child(pad_node, "at"))
            px, py = (pat + [0.0, 0.0])[:2]
            pad_angle = pat[2] if len(pat) >= 3 else 0.0
            local_offsets.append((px, py, pad_angle - fa))
        if at_node is not None:
            footprints.append(Footprint(
                ref=ref, x=fx, y=fy, angle=fa,
                locked=_footprint_locked(fp), overlap_ok=_footprint_overlap_ok(fp),
                edge_affinity=_footprint_edge_affinity(fp),
                decouple_target=_footprint_decouple(fp),
                pads=fp_pads, local_offsets=local_offsets,
                at_node=at_node, fp_node=fp,
                x0=fx, y0=fy, angle0=fa,
                uuid=fp_uuid, group_id=groups.get(fp_uuid),
            ))

    board = Board(
        tree=tree,
        copper_layers=copper,
        pads=pads,
        free_vias=_parse_free_vias(tree, numbered),
        segments=_parse_segments(tree, numbered),
        zones=_parse_zones(tree, numbered),
        outline=_parse_outline(tree),
        numbered_nets=numbered,
        name_only_nets=name_only,
        footprints=footprints,
        stackup=_parse_stackup(tree),
    )
    ensure_outline(board)
    return board


def zone_fill_nets(board: Board) -> set[str]:
    """Return the net names that have at least one active copper-fill zone.

    Args:
        board: the loaded board.

    Returns:
        A set of net name strings (e.g. ``{"GND"}``).  Empty when no filled
        zones exist.
    """
    return {z["net"] for z in board.zones
            if z.get("fill_enabled") and z.get("net")}


def _locate_kicad_cli() -> str | None:
    """Find the ``kicad-cli`` binary on ``PATH`` or at common macOS install paths.

    Returns:
        The executable path, or `None` if it can't be found.
    """
    import shutil

    kicad_cli = shutil.which("kicad-cli")
    if kicad_cli is None:
        for candidate in [
            "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
            "/Applications/Kicad9/KiCad.app/Contents/MacOS/kicad-cli",
        ]:
            if Path(candidate).exists():
                kicad_cli = candidate
                break
    return kicad_cli


def try_refill_zones(board_path: Path) -> bool:
    """Attempt to refill copper zones in *board_path* using ``kicad-cli``.

    Locates ``kicad-cli`` from ``PATH`` or common macOS install locations,
    then runs ``kicad-cli pcb drc --refill-zones --save-board``.

    Args:
        board_path: path to the ``.kicad_pcb`` file to refill in-place.

    Returns:
        ``True`` if ``kicad-cli`` ran and exited 0; ``False`` otherwise
        (tool not found, non-zero exit, or any exception).
    """
    import subprocess

    kicad_cli = _locate_kicad_cli()
    if kicad_cli is None:
        return False
    try:
        result = subprocess.run(
            [kicad_cli, "pcb", "drc",
             "--refill-zones", "--save-board",
             "--output", "/dev/null",
             str(board_path)],
            capture_output=True, text=True, timeout=120, check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


@dataclass
class DrcViolation:
    """One violation from ``kicad-cli``'s real DRC report (see `run_kicad_cli_drc`)."""
    severity: str    # "error", "warning", "exclusion", ... (kicad-cli's own label)
    kind: str        # kicad-cli's rule id, e.g. "clearance", "silk_over_copper"
    description: str


def run_kicad_cli_drc(board_path: str | Path) -> list[DrcViolation] | None:
    """Run ``kicad-cli``'s real DRC on *board_path* and parse its JSON report.

    This is a best-effort ground-truth check layered on top of — not
    replacing — the fast in-repo self-check (`geometry.clearance_violations` /
    `geometry.drill_violations`): it papers over gaps the local checker
    doesn't model (courtyard overlap, silkscreen-over-pad, zone-fill rule
    violations, and anything else KiCad's own DRC covers) using the same
    ``kicad-cli`` discovery this module already relies on for zone refill.

    Only the ``violations`` section of the report is read — schematic-parity
    and unconnected-item checks require a linked schematic/netlist this tool
    doesn't manage, and aren't requested (``--schematic-parity`` is never
    passed), so those sections should be absent regardless.

    Args:
        board_path: path to the ``.kicad_pcb`` file to check.

    Returns:
        A list of `DrcViolation` (empty means clean), or `None` if
        ``kicad-cli`` isn't available, the run failed, or its report
        couldn't be parsed. Callers must treat `None` as "unavailable", not
        "clean" — it is never returned alongside partial results.
    """
    import json
    import subprocess
    import tempfile

    kicad_cli = _locate_kicad_cli()
    if kicad_cli is None:
        return None

    with tempfile.TemporaryDirectory() as tmp:
        report_path = Path(tmp) / "drc.json"
        try:
            subprocess.run(
                [kicad_cli, "pcb", "drc",
                 "--format", "json",
                 "--output", str(report_path),
                 str(board_path)],
                capture_output=True, text=True, timeout=120, check=False,
            )
            if not report_path.exists():
                return None
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    return _parse_kicad_drc_report(report)


def _parse_kicad_drc_report(report: dict) -> list[DrcViolation]:
    """Flatten a ``kicad-cli`` JSON DRC report into `DrcViolation`s.

    Defensive by design: the exact report schema has drifted across KiCad
    versions, so every field is read with a fallback and a malformed entry is
    skipped rather than raising — a parsing hiccup should never take down an
    otherwise-successful routing run.

    Args:
        report: the parsed JSON DRC report (``kicad-cli pcb drc --format json``).

    Returns:
        One `DrcViolation` per entry in the report's ``violations`` list.
    """
    out: list[DrcViolation] = []
    if not isinstance(report, dict):
        return out
    for v in report.get("violations", None) or []:
        if not isinstance(v, dict):
            continue
        out.append(DrcViolation(
            severity=str(v.get("severity", "error")),
            kind=str(v.get("type", "unknown")),
            description=str(v.get("description", "")),
        ))
    return out


# --- node builders for the writer --------------------------------------------

def _net_ref_node(board: Board, net: str) -> SList:
    """Build a ``(net ...)`` node in the board's own reference style.

    Args:
        board: the board, providing the net style and number table.
        net: the net name to reference.

    Returns:
        A ``(net "GND")`` node for name-only boards, or ``(net <code>)`` for
        numbered boards.
    """
    node = SList([sexpr.sym("net")])
    if board.name_only_nets:
        node.append(sexpr.string(net))
    else:
        code = next((n for n, name in board.numbered_nets.items() if name == net), 0)
        node.append(sexpr.number(code))
    return node


def _xy_node(head: str, x: float, y: float) -> SList:
    """Build a two-coordinate node such as ``(start x y)`` or ``(at x y)``.

    Args:
        head: the leading symbol, e.g. ``"start"``, ``"end"``, or ``"at"``.
        x: the x coordinate (mm).
        y: the y coordinate (mm).

    Returns:
        The ``(head x y)`` node.
    """
    return SList([sexpr.sym(head), sexpr.number(x), sexpr.number(y)])


def make_segment(board: Board, x1, y1, x2, y2, width, layer, net) -> SList:
    """Build a ``(segment ...)`` node for a routed track.

    Args:
        board: the board (for the net-reference style).
        x1: start x (mm).
        y1: start y (mm).
        x2: end x (mm).
        y2: end y (mm).
        width: track width (mm).
        layer: copper layer name, e.g. ``"F.Cu"``.
        net: the segment's net name.

    Returns:
        A ``(segment ...)`` node with a fresh uuid.
    """
    return SList([
        sexpr.sym("segment"),
        _xy_node("start", x1, y1),
        _xy_node("end", x2, y2),
        SList([sexpr.sym("width"), sexpr.number(width)]),
        SList([sexpr.sym("layer"), sexpr.string(layer)]),
        _net_ref_node(board, net),
        SList([sexpr.sym("uuid"), sexpr.string(str(_uuid.uuid4()))]),
    ])


def make_via(board: Board, x, y, size, drill, layer_a, layer_b, net) -> SList:
    """Build a ``(via ...)`` node for a layer transition.

    Args:
        board: the board (for the net-reference style).
        x: via centre x (mm).
        y: via centre y (mm).
        size: via copper diameter (mm).
        drill: via drill diameter (mm).
        layer_a: one connected copper layer, e.g. ``"F.Cu"``.
        layer_b: the other connected copper layer, e.g. ``"B.Cu"``.
        net: the via's net name.

    Returns:
        A ``(via ...)`` node with a fresh uuid.
    """
    return SList([
        sexpr.sym("via"),
        _xy_node("at", x, y),
        SList([sexpr.sym("size"), sexpr.number(size)]),
        SList([sexpr.sym("drill"), sexpr.number(drill)]),
        SList([sexpr.sym("layers"), sexpr.string(layer_a), sexpr.string(layer_b)]),
        _net_ref_node(board, net),
        SList([sexpr.sym("uuid"), sexpr.string(str(_uuid.uuid4()))]),
    ])


def make_zone_node(board: Board, layer: str, net: str,
                   pts: list[tuple[float, float]], *,
                   clearance: float = 0.5,
                   min_thickness: float = 0.25) -> SList:
    """Build a ``(zone ...)`` copper-pour boundary node (no filled_polygon — KiCad fills it).

    Args:
        board: the board (for net-reference style).
        layer: the zone layer (e.g. "B.Cu", "F.Cu").
        net: the net name (e.g. "GND").
        pts: list of (x, y) boundary vertices (exterior ring; drop repeated close point).
        clearance: thermal-relief clearance and connect_pads clearance (mm).
        min_thickness: minimum copper strand thickness (mm).

    Returns:
        A ``(zone ...)`` node with net, layer, hatch, fill, and polygon outline.
        The ``(filled_polygon ...)`` is omitted — KiCad adds it on refill.
    """
    # Build (pts (xy x y) ...)
    pts_node = SList([sexpr.sym("pts")])
    for x, y in pts:
        pts_node.append(SList([sexpr.sym("xy"), sexpr.number(x), sexpr.number(y)]))

    # Thermal bridge spokes must be at least as wide as min_thickness, otherwise
    # KiCad's fill algorithm silently drops the thermal reliefs for affected pads.
    thermal_bridge = max(clearance, min_thickness)

    # Zones need (net_name "GND") alongside (net <code>) on numbered-net boards;
    # KiCad's fill engine uses both to match the zone to its net.
    zone_items: list = [
        sexpr.sym("zone"),
        _net_ref_node(board, net),
    ]
    if not board.name_only_nets:
        zone_items.append(SList([sexpr.sym("net_name"), sexpr.string(net)]))
    zone_items += [
        SList([sexpr.sym("layer"), sexpr.string(layer)]),
        SList([sexpr.sym("uuid"), sexpr.string(str(__import__("uuid").uuid4()))]),
        SList([sexpr.sym("hatch"), sexpr.sym("edge"), sexpr.number(0.5)]),
        SList([
            sexpr.sym("connect_pads"),
            SList([sexpr.sym("clearance"), sexpr.number(clearance)])
        ]),
        SList([sexpr.sym("min_thickness"), sexpr.number(min_thickness)]),
        SList([
            sexpr.sym("fill"), sexpr.sym("yes"),
            SList([sexpr.sym("thermal_gap"), sexpr.number(clearance)]),
            SList([sexpr.sym("thermal_bridge_width"), sexpr.number(thermal_bridge)]),
            SList([sexpr.sym("island_removal_mode"), sexpr.number(0)])
        ]),
        SList([sexpr.sym("polygon"), pts_node]),
    ]
    return SList(zone_items)


def make_edge_rect(x1: float, y1: float, x2: float, y2: float,
                   width: float = 0.05) -> SList:
    """Build a ``(gr_rect ...)`` board-outline node on ``Edge.Cuts``.

    Args:
        x1: a corner x (mm).
        y1: a corner y (mm).
        x2: the opposite corner x (mm).
        y2: the opposite corner y (mm).
        width: the edge stroke width (mm).

    Returns:
        A ``(gr_rect ...)`` node on ``Edge.Cuts`` with a fresh uuid.
    """
    return SList([
        sexpr.sym("gr_rect"),
        _xy_node("start", x1, y1),
        _xy_node("end", x2, y2),
        SList([sexpr.sym("stroke"),
               SList([sexpr.sym("width"), sexpr.number(width)]),
               SList([sexpr.sym("type"), sexpr.sym("solid")])]),
        SList([sexpr.sym("fill"), sexpr.sym("no")]),
        SList([sexpr.sym("layer"), sexpr.string("Edge.Cuts")]),
        SList([sexpr.sym("uuid"), sexpr.string(str(_uuid.uuid4()))]),
    ])


def make_npth(x: float, y: float, drill: float, *, ref: str = "MH1") -> SList:
    """Build an NPTH mounting-hole ``(footprint ...)`` node.

    The footprint carries a single non-plated through-hole pad with no copper
    annular ring (``size == drill``) on every copper layer, matching KiCad's
    ``MountingHole`` library style. It has no net, and is excluded from the BoM
    and position files (``(attr ...)``). Because the pad round-trips through
    `load_board` as an `np_thru_hole` `Pad` with a `drill`, the hole is picked up
    by `pyautoroute.geometry.board_drills` / `board_obstacles` on reload.

    Args:
        x: hole centre x (mm).
        y: hole centre y (mm).
        drill: drill diameter (mm).
        ref: reference designator for the footprint (e.g. ``"MH1"``).

    Returns:
        A ``(footprint ...)`` node with a fresh uuid, ready for
        `write_board(..., new_nodes=...)`.
    """
    def _u() -> SList:
        return SList([sexpr.sym("uuid"), sexpr.string(str(_uuid.uuid4()))])

    pad = SList([
        sexpr.sym("pad"),
        sexpr.string(""), sexpr.sym("np_thru_hole"), sexpr.sym("circle"),
        _xy_node("at", 0.0, 0.0),
        SList([sexpr.sym("size"), sexpr.number(drill), sexpr.number(drill)]),
        SList([sexpr.sym("drill"), sexpr.number(drill)]),
        SList([sexpr.sym("layers"), sexpr.string("*.Cu"), sexpr.string("*.Mask")]),
        _u(),
    ])
    return SList([
        sexpr.sym("footprint"),
        sexpr.string("MountingHole"),
        SList([sexpr.sym("layer"), sexpr.string("F.Cu")]),
        _u(),
        _xy_node("at", x, y),
        SList([sexpr.sym("attr"),
               sexpr.sym("exclude_from_pos_files"),
               sexpr.sym("exclude_from_bom")]),
        SList([sexpr.sym("property"), sexpr.string("Reference"), sexpr.string(ref),
               _xy_node("at", 0.0, 0.0),
               SList([sexpr.sym("layer"), sexpr.string("F.SilkS")]),
               _u()]),
        pad,
    ])


def _pad_half_extent(pad: Pad) -> float:
    """Rotation-independent half-extent of a pad (half its bounding diagonal).

    Args:
        pad: the pad whose width/height bound the extent.

    Returns:
        ``hypot(w, h) / 2`` — an upper bound on the pad's reach from its centre at
        any rotation, used to size the regenerated board outline conservatively.
    """
    return 0.5 * math.hypot(pad.w, pad.h)


def pad_bounding_outline(pads: list[Pad], margin: float = 2.0) -> list[OutlineShape]:
    """Build a single-rectangle outline bounding all *pads*, grown by *margin*.

    Each pad contributes its rotation-independent half-extent (`_pad_half_extent`)
    so the rectangle conservatively covers every pad at any rotation.

    Args:
        pads: the pads to bound (must be non-empty).
        margin: extra space (mm) added around the pads.

    Returns:
        A one-element list holding the bounding `OutlineShape` rectangle.
    """
    x0 = min(p.cx - _pad_half_extent(p) for p in pads) - margin
    y0 = min(p.cy - _pad_half_extent(p) for p in pads) - margin
    x1 = max(p.cx + _pad_half_extent(p) for p in pads) + margin
    y1 = max(p.cy + _pad_half_extent(p) for p in pads) + margin
    return [OutlineShape("rect", {"start": (x0, y0), "end": (x1, y1)})]


def apply_placement(board: Board, margin: float = 2.0,
                    keep_outline: bool = False) -> bool:
    """Push the footprints' current poses into the model for routing.

    Recomputes every pad's absolute centre/rotation from its footprint pose
    (`Footprint.sync_pads`) and, by default, replaces `Board.outline` with a single
    rectangle bounding all pads (grown by `margin`). Call after the placement pass
    and before building the routing grid; the grid and router then see the new
    layout.

    With `keep_outline` and a real (non-synthesised) Edge.Cuts present, the parsed
    outline is left untouched instead — the placement was contained within it — so
    routing uses the board's existing shape.

    Args:
        board: the board to update in place.
        margin: extra space (mm) added around the pads when sizing the outline.
        keep_outline: keep the board's existing Edge.Cuts instead of regenerating
            it (only honoured when a closed, non-synthesised outline exists).

    Returns:
        True if the existing outline was kept; False if a bounding rectangle was
        (re)generated (including the `keep_outline` fall-back when there is no
        real outline to keep).
    """
    for fp in board.footprints:
        fp.sync_pads()
    if not board.pads:
        return False
    if keep_outline and board.outline and not board.outline_synthesized:
        return True                       # keep the existing Edge.Cuts
    board.outline = pad_bounding_outline(board.pads, margin)
    return False


def ensure_outline(board: Board, margin: float = 2.0) -> bool:
    """Synthesize a default Edge.Cuts outline if the board has none.

    When a board file has no shapes on the ``Edge.Cuts`` layer, this function
    derives a rectangle from the pad extents (grown by *margin* mm) and both
    sets ``board.outline`` and appends the matching ``(gr_rect ...)`` node to
    ``board.tree`` so the outline is written to the output file.

    Args:
        board: the board to update in place.
        margin: extra space (mm) added around the pad bounding box.

    Returns:
        True if an outline already existed (no change), False if a default was
        synthesised.
    """
    if board.outline:
        return True
    if not board.pads:
        return True   # nothing to bound; leave outline empty
    x0 = min(p.cx - _pad_half_extent(p) for p in board.pads) - margin
    y0 = min(p.cy - _pad_half_extent(p) for p in board.pads) - margin
    x1 = max(p.cx + _pad_half_extent(p) for p in board.pads) + margin
    y1 = max(p.cy + _pad_half_extent(p) for p in board.pads) + margin
    board.outline = [OutlineShape("rect", {"start": (x0, y0), "end": (x1, y1)})]
    board.tree.append(make_edge_rect(x0, y0, x1, y1))
    board.outline_synthesized = True
    return False


def _is_edge_graphic(node) -> bool:
    """Return whether a top-level node is an ``Edge.Cuts`` graphic shape.

    Args:
        node: a child of the board tree.

    Returns:
        True for a ``gr_*`` shape whose ``(layer ...)`` is ``Edge.Cuts``.
    """
    if not isinstance(node, SList):
        return False
    if sexpr.head_symbol(node) not in ("gr_poly", "gr_line", "gr_rect", "gr_arc", "gr_circle"):
        return False
    layer = strings(child(node, "layer"))
    return bool(layer) and layer[0] == "Edge.Cuts"


def _rotate_pad_nodes(fp_node: SList, delta: float) -> None:
    """Add `delta` degrees to every pad's absolute ``(at)`` angle in a footprint.

    KiCad pad ``(at px py angle)`` stores ``px``/``py`` as the footprint-local
    (pre-rotation) offset and ``angle`` as the *absolute* pad orientation. When a
    footprint is rotated by `delta`, only the angle changes (the local offset is
    unchanged), so this rewrites each pad's ``(at)`` angle and clears the pad
    node's span so the change is serialised rather than emitted verbatim.

    Args:
        fp_node: the ``(footprint ...)`` node whose pads to re-angle.
        delta: the footprint's rotation change (degrees).
    """
    for pad in children(fp_node, "pad"):
        at = child(pad, "at")
        vals = floats(at)
        if len(vals) < 2:
            continue
        px, py = vals[0], vals[1]
        old_angle = vals[2] if len(vals) >= 3 else 0.0
        new_at = _xy_node("at", px, py)
        new_at.append(sexpr.number((old_angle + delta) % 360.0))
        pad.span = None
        for i, ch in enumerate(pad):
            if ch is at:
                pad[i] = new_at
                break


def _rotate_text_nodes(fp_node: SList, delta: float) -> None:
    """Add `delta` degrees to every text node's absolute angle in a footprint.

    KiCad 7+ stores ``fp_text`` and ``property`` text orientations absolutely
    (already incorporating the footprint rotation), the same convention as pad
    angles.  When a footprint is rotated by `delta`, those angles must be
    updated so KiCad renders the text correctly after reloading.

    Args:
        fp_node: the ``(footprint ...)`` node whose text nodes to re-angle.
        delta: the footprint's rotation change (degrees).
    """
    for tag in ("fp_text", "property"):
        for txt in children(fp_node, tag):
            at = child(txt, "at")
            vals = floats(at)
            if len(vals) < 2:
                continue
            lx, ly = vals[0], vals[1]
            old_angle = vals[2] if len(vals) >= 3 else 0.0
            new_at = _xy_node("at", lx, ly)
            new_at.append(sexpr.number((old_angle + delta) % 360.0))
            txt.span = None
            for i, ch in enumerate(txt):
                if ch is at:
                    txt[i] = new_at
                    break


def _move_gr_text(text_node: SList, old_cx: float, old_cy: float,
                  new_cx: float, new_cy: float, angle_delta: float) -> None:
    """Translate and rotate a top-level gr_text node to follow its group.

    Reads the text's current ``(at x y [angle])`` values, treats them as being
    expressed relative to ``(old_cx, old_cy)``, rotates by ``angle_delta``, then
    places the result relative to ``(new_cx, new_cy)``.  The node's span is
    cleared so the serialiser re-emits the updated position.

    Args:
        text_node: the ``(gr_text ...)`` s-expression node to transform (mutated).
        old_cx: group centroid x before movement (from ``fp.x0``).
        old_cy: group centroid y before movement (from ``fp.y0``).
        new_cx: group centroid x after movement (from ``fp.x``).
        new_cy: group centroid y after movement (from ``fp.y``).
        angle_delta: total rotation applied to the group (degrees).
    """
    old_at = child(text_node, "at")
    if old_at is None:
        return
    vals = floats(old_at)
    if len(vals) < 2:
        return
    tx, ty = vals[0], vals[1]
    text_angle = vals[2] if len(vals) >= 3 else 0.0

    rel_x = tx - old_cx
    rel_y = ty - old_cy
    if abs(angle_delta) > 1e-6:
        rel_x, rel_y = rotate(rel_x, rel_y, angle_delta)
    new_x = new_cx + rel_x
    new_y = new_cy + rel_y
    new_angle = (text_angle + angle_delta) % 360.0

    new_at = SList([sexpr.sym("at"), sexpr.number(new_x), sexpr.number(new_y)])
    if abs(new_angle) > 1e-6:
        new_at.append(sexpr.number(new_angle))

    text_node.span = None
    for i, ch in enumerate(text_node):
        if ch is old_at:
            text_node[i] = new_at
            break


def sync_tree_from_placement(board: Board, edge_width: float = 0.05,
                             keep_outline: bool = False) -> None:
    """Rewrite the board tree to match the placement result, in place.

    For each footprint that actually moved, clears its node's source span (so it
    re-serialises from structure rather than verbatim) and replaces the ``(at ...)``
    child with the new pose — children keep their own spans, so the only textual
    diff is the footprint's ``(at)`` line. Top-level ``gr_text`` items that belong
    to the same KiCad group as a moved footprint are transformed by the same
    translation and rotation so they travel with their group. Replaces every
    ``Edge.Cuts`` graphic with a single ``gr_rect`` matching `Board.outline` (set
    by `apply_placement`) — unless `keep_outline`, in which case the existing
    Edge.Cuts is left as-is and only the footprint poses are rewritten.

    Args:
        board: the board whose tree is mutated (and is then ready for
            `write_board`).
        edge_width: stroke width (mm) for the regenerated outline rectangle.
        keep_outline: leave the board's existing Edge.Cuts untouched (pair with
            `apply_placement(..., keep_outline=True)`).
    """
    for fp in board.footprints:
        if not fp.moved:
            continue
        fp.fp_node.span = None
        new_at = SList([sexpr.sym("at"), sexpr.number(fp.x), sexpr.number(fp.y)])
        if abs(fp.angle) > 1e-9 or len(fp.at_node) > 3:
            new_at.append(sexpr.number(fp.angle))
        for i, ch in enumerate(fp.fp_node):
            if ch is fp.at_node:
                fp.fp_node[i] = new_at
                break
        fp.at_node = new_at
        # When the footprint was rotated, propagate the rotation into each pad's
        # (at) angle. KiCad stores pad angles *absolutely* (already including the
        # footprint rotation), and pad nodes are otherwise emitted verbatim from
        # their source span, so without this a rotated footprint's pads keep their
        # old orientation on reload — which mis-orients rectangular/oval pads and
        # fails DRC.
        delta = fp.angle - fp.angle0
        if abs(delta) > 1e-9:
            _rotate_pad_nodes(fp.fp_node, delta)
            _rotate_text_nodes(fp.fp_node, delta)

    # Move top-level gr_text items that share a KiCad group with moved footprints.
    # The text's current (at ...) values are its original parsed positions (x0/y0),
    # so we compute the full transformation from original centroid to final centroid.
    _sync_group_text(board)

    if keep_outline:
        return                            # leave the existing Edge.Cuts untouched
    rect = next((s for s in board.outline if s.kind == "rect"), None)
    if rect is None:
        return
    board.tree[:] = [ch for ch in board.tree if not _is_edge_graphic(ch)]
    (rx0, ry0), (rx1, ry1) = rect.data["start"], rect.data["end"]
    board.tree.append(make_edge_rect(rx0, ry0, rx1, ry1, edge_width))


def gr_text_group_fps(board: Board) -> dict[str, tuple[SList, list]]:
    """Return grouped top-level gr_text items and their associated footprints.

    Identifies every top-level ``gr_text`` that shares a KiCad group with at
    least one footprint, and returns the information needed to:

    * exclude those texts from fixed-obstacle lists in the placer (they travel
      with their footprint, not fixed at their original position);
    * extend the associated footprint's bounding box to cover the text during
      overlap scoring;
    * render grouped text at its live position during the GUI placement preview.

    The mapping is built from ``board.tree`` (always the original parsed state)
    and ``board.footprints`` (which may hold live placement poses).

    Args:
        board: the board to query.

    Returns:
        dict mapping each grouped gr_text's UUID to a
        ``(text_node, footprint_list)`` tuple, where *footprint_list* contains
        every `Footprint` that shares the same KiCad group.
    """
    fp_by_uuid = {fp.uuid: fp for fp in board.footprints if fp.uuid}
    if not fp_by_uuid:
        return {}

    # Parse group_uuid -> [member_uuids] from the board tree.
    group_members: dict[str, list[str]] = {}
    for node in board.tree:
        if not (isinstance(node, SList) and sexpr.head_symbol(node) == "group"):
            continue
        gid = ""
        members: list[str] = []
        for ch in node:
            if isinstance(ch, SList) and sexpr.head_symbol(ch) == "uuid":
                vals = atoms_after_head(ch)
                if vals:
                    gid = vals[0].text
            elif isinstance(ch, SList) and sexpr.head_symbol(ch) == "members":
                members = [a.text for a in atoms_after_head(ch)]
        if gid and members:
            group_members[gid] = members

    # Build uuid -> gr_text node.
    gr_text_nodes: dict[str, SList] = {}
    for node in board.tree:
        if isinstance(node, SList) and sexpr.head_symbol(node) == "gr_text":
            uuid_node = child(node, "uuid")
            if uuid_node:
                vals = atoms_after_head(uuid_node)
                if vals:
                    gr_text_nodes[vals[0].text] = node

    if not gr_text_nodes:
        return {}

    result: dict[str, tuple[SList, list]] = {}
    for gid, members in group_members.items():
        fps_in_group = [fp_by_uuid[uid] for uid in members if uid in fp_by_uuid]
        if not fps_in_group:
            continue
        for uid in members:
            if uid in gr_text_nodes:
                result[uid] = (gr_text_nodes[uid], fps_in_group)
    return result


def _sync_group_text(board: Board) -> None:
    """Move top-level gr_text nodes that belong to groups containing moved footprints.

    Called by `sync_tree_from_placement` after footprint positions have been
    written.  Uses ``fp.x0/y0/angle0`` (the original parsed pose) as the
    reference centroid so the transformation is always computed from the
    original board file — it is therefore idempotent with respect to how many
    scatter passes preceded placement.

    Args:
        board: the board whose tree is mutated in place.
    """
    grouped = gr_text_group_fps(board)
    if not grouped:
        return

    for text_uuid, (text_node, fps) in grouped.items():
        if not any(fp.moved for fp in fps):
            continue
        old_cx = sum(fp.x0 for fp in fps) / len(fps)
        old_cy = sum(fp.y0 for fp in fps) / len(fps)
        new_cx = sum(fp.x for fp in fps) / len(fps)
        new_cy = sum(fp.y for fp in fps) / len(fps)
        angle_delta = fps[0].angle - fps[0].angle0
        _move_gr_text(text_node, old_cx, old_cy, new_cx, new_cy, angle_delta)


def stamp_comment(board: Board, text: str) -> None:
    """Write *text* into the first empty title-block comment slot (1–9).

    If the board's ``(title_block ...)`` node has no ``(comment N ...)``
    children, slot 1 is used.  The first slot whose current value is the
    empty string ``""`` is chosen; if all nine are non-empty the call is a
    no-op (existing comments are not overwritten).

    Args:
        board: the board to update in place (its tree is mutated).
        text: the comment text to write (plain string; will be quoted).
    """
    tb = child(board.tree, "title_block")
    if tb is None:
        tb = SList([sexpr.sym("title_block")])
        board.tree.append(tb)

    # Clear the verbatim source span so the serializer re-renders this node
    # (appending to a parsed SList doesn't auto-invalidate its span).
    tb.span = None

    # Collect existing comment slots: slot_num -> node
    existing: dict[int, SList] = {}
    for node in children(tb, "comment"):
        atoms = atoms_after_head(node)
        if atoms:
            try:
                existing[int(atoms[0].raw)] = node
            except (ValueError, IndexError):
                pass

    # Find the first empty slot in 1-9
    for slot in range(1, 10):
        if slot not in existing:
            tb.append(SList([sexpr.sym("comment"),
                             sexpr.number(slot),
                             sexpr.string(text)]))
            return
        # Check whether the existing comment is empty
        node = existing[slot]
        atoms = atoms_after_head(node)
        if len(atoms) >= 2 and atoms[1].text == "":
            node[node.index(atoms[1])] = sexpr.string(text)
            node.span = None  # this SList's span is now stale; force re-render
            return
    # All 9 slots occupied — leave unchanged


def move_values_to_silk(board: Board) -> int:
    """Move footprint ``Value`` text to the matching silkscreen layer.

    Scans every footprint in the board tree for ``(property "Value" ...)``
    nodes (KiCad 7+) and ``(fp_text value ...)`` nodes (KiCad 6 and earlier)
    whose ``(layer ...)`` is **not** already a silkscreen layer.  Each such
    node is reassigned to the silkscreen layer that matches the footprint's
    side (front or back).  Hidden text (``(hide yes)``) is moved too — the
    layer assignment is corrected regardless of visibility.

    The board's s-expression tree is mutated in place; call `write_board` (or
    `Path.write_text(sexpr.dump_file(...))`) to persist the changes.

    Args:
        board: the board to update in place.

    Returns:
        The number of text nodes whose layer was changed.
    """
    front_silk, back_silk = _silk_layers(board.tree)
    silk_names = {front_silk, back_silk,
                  "F.SilkS", "B.SilkS", "F.Silkscreen", "B.Silkscreen"}

    changed = 0
    for fp_node in children(board.tree, "footprint"):
        fp_layer_strs = strings(child(fp_node, "layer"))
        fp_layer = fp_layer_strs[0] if fp_layer_strs else "F.Cu"
        target_silk = back_silk if fp_layer.startswith("B.") else front_silk

        for prop_node in children(fp_node, "property"):
            atoms = atoms_after_head(prop_node)
            if not atoms or atoms[0].text != "Value":
                continue
            layer_node = child(prop_node, "layer")
            if layer_node is None:
                continue
            layer_atoms = atoms_after_head(layer_node)
            if not layer_atoms:
                continue
            if layer_atoms[0].text in silk_names:
                continue
            layer_node[layer_node.index(layer_atoms[0])] = sexpr.string(target_silk)
            layer_node.span = None
            prop_node.span = None
            fp_node.span = None
            changed += 1

        for txt_node in children(fp_node, "fp_text"):
            atoms = atoms_after_head(txt_node)
            if not atoms or atoms[0].text != "value":
                continue
            layer_node = child(txt_node, "layer")
            if layer_node is None:
                continue
            layer_atoms = atoms_after_head(layer_node)
            if not layer_atoms:
                continue
            if layer_atoms[0].text in silk_names:
                continue
            layer_node[layer_node.index(layer_atoms[0])] = sexpr.string(target_silk)
            layer_node.span = None
            txt_node.span = None
            fp_node.span = None
            changed += 1

    return changed


def move_refs_to_fab(board: Board) -> int:
    """Move footprint ``Reference`` text to the matching fabrication layer.

    Scans every footprint for ``(property "Reference" ...)`` nodes (KiCad 7+)
    and ``(fp_text reference ...)`` nodes (KiCad 6 and earlier) whose layer is
    **not** already a fabrication layer, and reassigns them to the fab layer
    matching the footprint's side.  Hidden text is moved too.

    Args:
        board: the board to update in place.

    Returns:
        The number of text nodes whose layer was changed.
    """
    front_fab, back_fab = _fab_layers(board.tree)
    fab_names = {front_fab, back_fab, "F.Fab", "B.Fab",
                 "F.Fabrication", "B.Fabrication"}

    changed = 0
    for fp_node in children(board.tree, "footprint"):
        fp_layer_strs = strings(child(fp_node, "layer"))
        fp_layer = fp_layer_strs[0] if fp_layer_strs else "F.Cu"
        target_fab = back_fab if fp_layer.startswith("B.") else front_fab

        for prop_node in children(fp_node, "property"):
            atoms = atoms_after_head(prop_node)
            if not atoms or atoms[0].text != "Reference":
                continue
            layer_node = child(prop_node, "layer")
            if layer_node is None:
                continue
            layer_atoms = atoms_after_head(layer_node)
            if not layer_atoms:
                continue
            if layer_atoms[0].text in fab_names:
                continue
            layer_node[layer_node.index(layer_atoms[0])] = sexpr.string(target_fab)
            layer_node.span = None
            prop_node.span = None
            fp_node.span = None
            changed += 1

        for txt_node in children(fp_node, "fp_text"):
            atoms = atoms_after_head(txt_node)
            if not atoms or atoms[0].text != "reference":
                continue
            layer_node = child(txt_node, "layer")
            if layer_node is None:
                continue
            layer_atoms = atoms_after_head(layer_node)
            if not layer_atoms:
                continue
            if layer_atoms[0].text in fab_names:
                continue
            layer_node[layer_node.index(layer_atoms[0])] = sexpr.string(target_fab)
            layer_node.span = None
            txt_node.span = None
            fp_node.span = None
            changed += 1

    return changed


def write_board(board: Board, out_path: str | Path,
                new_nodes: list[SList] | None = None,
                strip_free_vias: bool = True,
                strip_segments: bool = False,
                extra_strip_ids: set[int] | None = None) -> None:
    """Serialize a routed copy: drop free vias/segments, append new routing nodes.

    Clones the parsed tree (untouched subtrees keep their source spans, so the
    diff against the input stays limited to the routing edits).

    Args:
        board: the source board whose tree is cloned.
        out_path: destination path for the routed ``.kicad_pcb``.
        new_nodes: freshly-built ``(segment ...)`` / ``(via ...)`` nodes to
            append (from `make_segment` / `make_via`); `None` for none.
        strip_free_vias: when True, omit the board's dangling free vias from the
            output.
        strip_segments: when True, omit all existing ``(segment ...)`` tracks
            from the output (use for a clean re-route so tracks are not doubled).
        extra_strip_ids: additional ``id(node)`` values to omit, beyond those
            selected by ``strip_free_vias`` / ``strip_segments`` — used to
            remove individual free vias that are superseded by co-located vias
            in ``new_nodes`` (duplicate-via deduplication in preserve mode).
    """
    strip_ids: set[int] = set()
    if strip_free_vias:
        strip_ids.update(id(v.node) for v in board.free_vias if v.node is not None)
    if strip_segments:
        strip_ids.update(id(s.node) for s in board.segments if s.node is not None)
    if extra_strip_ids:
        strip_ids.update(extra_strip_ids)
    new_root = SList()
    for ch in board.tree:
        if isinstance(ch, SList) and id(ch) in strip_ids:
            continue
        new_root.append(ch)
    for node in (new_nodes or []):
        new_root.append(node)
    Path(out_path).write_text(sexpr.dump_file(new_root), encoding="utf-8")


# --- footprint constraint editor (GUI) ----------------------------------------


def footprint_bbox(fp: Footprint) -> tuple[float, float, float, float]:
    """Axis-aligned bounding box over a footprint's pads.

    Returns:
        ``(x0, y0, x1, y1)`` in board mm, or ``(0, 0, 0, 0)`` if no pads.
    """
    if not fp.pads:
        return (0.0, 0.0, 0.0, 0.0)
    he = lambda p: 0.5 * math.hypot(p.w, p.h)
    xs = [p.cx - he(p) for p in fp.pads] + [p.cx + he(p) for p in fp.pads]
    ys = [p.cy - he(p) for p in fp.pads] + [p.cy + he(p) for p in fp.pads]
    return min(xs), min(ys), max(xs), max(ys)


def footprint_at(board: Board, x: float, y: float) -> Footprint | None:
    """The footprint whose body box contains (x, y); the smallest if overlapping.

    Args:
        board: the board.
        x, y: board coordinates in mm.

    Returns:
        A `Footprint` whose bbox fully encloses (x, y), or `None` if no match.
        If multiple footprints overlap the point, returns the one with the
        smallest area (easiest to click precisely inside a dense group).
    """
    hits: list[tuple[float, Footprint]] = []
    for fp in board.footprints:
        if not fp.pads:
            continue
        x0, y0, x1, y1 = footprint_bbox(fp)
        if x0 <= x <= x1 and y0 <= y <= y1:
            area = (x1 - x0) * (y1 - y0)
            hits.append((area, fp))
    return min(hits, key=lambda t: t[0])[1] if hits else None


def _make_property_node(
    fp: Footprint, name: str, value: str
) -> SList:
    """Build a KiCad-valid hidden custom field for a footprint.

    Creates a full property node with placement, layer, hide, uuid, and effects.
    The font effects are borrowed from an existing property on the footprint
    if present; otherwise uses a default.

    Args:
        fp: the footprint.
        name: property name (e.g. "Autoroute-edge").
        value: property value (e.g. "left").

    Returns:
        A fresh `SList` (span=None, will re-serialize) ready to append to the
        footprint node. The structure is KiCad 7+ compliant.
    """
    uid = sexpr.string(str(_uuid.uuid4()))

    # Try to borrow font effects from an existing property. Deep-copy it: the
    # borrowed node would otherwise be aliased into two parents (the donor
    # property and this new one), so mutating/re-spanning one (e.g. via
    # `set_footprint_property`) would corrupt the other.
    font_effects = None
    for prop in children(fp.fp_node, "property"):
        fx = child(prop, "effects")
        if fx is not None:
            font_effects = copy.deepcopy(fx)
            break
    if font_effects is None:
        font_effects = SList(
            [
                sexpr.sym("effects"),
                SList([
                    sexpr.sym("font"),
                    SList([sexpr.sym("size"), sexpr.Atom("1"), sexpr.Atom("1")]),
                    SList([sexpr.sym("thickness"), sexpr.Atom("0.15")]),
                ]),
            ]
        )

    return SList(
        [
            sexpr.sym("property"),
            sexpr.string(name),
            sexpr.string(value),
            SList([sexpr.sym("at"), sexpr.Atom(str(fp.x)), sexpr.Atom(str(fp.y)),
                   sexpr.Atom(str(fp.angle))]),
            SList([sexpr.sym("layer"), sexpr.string("F.Fab")]),
            SList([sexpr.sym("hide"), sexpr.sym("yes")]),
            SList([sexpr.sym("uuid"), uid]),
            font_effects,
        ]
    )


def set_footprint_property(fp: Footprint, name: str, value: str | None) -> None:
    """Set/replace/remove a footprint custom property in the sexpr tree.

    Updates the footprint's in-memory `fp_node` (sexpr) so the change reflects
    in serialization; also sets `.span = None` on modified nodes to force
    re-serialization.

    Args:
        fp: the footprint whose property is being edited.
        name: property name (e.g. "Autoroute-overlap").
        value: property value, or `None` to remove the property.
    """
    fp_node = fp.fp_node
    existing = None
    for prop in children(fp_node, "property"):
        vals = atoms_after_head(prop)
        if vals and vals[0].text == name:
            existing = prop
            break
    if value is None:
        if existing is not None:
            fp_node.remove(existing)
            fp_node.span = None
        return
    if existing is not None:
        vals = atoms_after_head(existing)
        if len(vals) >= 2:
            existing[existing.index(vals[1])] = sexpr.string(value)
            existing.span = None
    else:
        fp_node.append(_make_property_node(fp, name, value))
    fp_node.span = None


def set_footprint_edge(fp: Footprint, side: str | None) -> None:
    """Set a footprint's edge-affinity constraint.

    Updates both the `Footprint.edge_affinity` field and the sexpr tree.

    Args:
        fp: the footprint.
        side: "left", "right", "top", "bottom", "any", or `None` (no constraint).
    """
    fp.edge_affinity = side
    set_footprint_property(
        fp, "Autoroute-edge", side if side is not None else None
    )


def set_footprint_overlap(fp: Footprint, on: bool) -> None:
    """Set a footprint's overlap-ok constraint.

    Updates both the `Footprint.overlap_ok` field and the sexpr tree.

    Args:
        fp: the footprint.
        on: whether the body may overlap other footprints.
    """
    fp.overlap_ok = on
    set_footprint_property(
        fp, "Autoroute-overlap", "yes" if on else None
    )


def set_footprint_decoupling(fp: Footprint, target: str | None) -> None:
    """Set/clear a footprint's decoupling-cap target.

    Updates both the `Footprint.decouple_target` field and the sexpr tree
    (``Autoroute-decouple`` property), so the change is persisted on the next
    `write_board`.

    Args:
        fp: the footprint.
        target: the associated IC's reference designator, ``"auto"`` (resolve by
            net search at placement time), or `None` to clear the mark.
    """
    fp.decouple_target = target
    set_footprint_property(fp, "Autoroute-decouple", target)


def set_footprint_locked(fp: Footprint, locked: bool) -> None:
    """Set a footprint's lock state.

    Updates both the `Footprint.locked` field and the sexpr tree, removing
    any existing lock node/atom and adding a fresh `(locked yes)` if locked.

    Args:
        fp: the footprint.
        locked: whether the footprint is fixed during placement.
    """
    fp.locked = locked
    node = fp.fp_node
    # Remove any existing lock nodes (bare atom or sublist).
    node[:] = [
        it
        for it in node
        if not (isinstance(it, Atom) and it.raw == "locked")
        and not (isinstance(it, SList) and sexpr.head_symbol(it) == "locked")
    ]
    if locked:
        node.insert(1, SList([sexpr.sym("locked"), sexpr.sym("yes")]))
    node.span = None
