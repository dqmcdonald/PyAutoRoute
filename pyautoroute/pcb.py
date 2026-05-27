"""Board model loaded from a ``.kicad_pcb`` s-expression, plus a routed-board
writer.

Parsing covers what the router needs: the copper layer stack, every pad with its
absolute position/rotation/shape, the net-reference style (name-only as in KiCad
10, or a numbered net table as in KiCad 6-9), existing tracks/vias/zones to treat
as obstacles, the free (dangling) vias to strip, and the Edge.Cuts outline shapes.

The writer clones the parsed tree, drops the free vias, appends freshly-built
``(segment ...)`` / ``(via ...)`` nodes, and serializes. Untouched subtrees keep
their source spans so the diff against the input is limited to the routing edits.
"""

from __future__ import annotations

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


@dataclass
class OutlineShape:
    kind: str                 # poly | line | rect | arc | circle
    data: dict


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
        df = floats(drill_node)
        if df:
            drill = df[-1]   # (drill d) or (drill oval dx dy)

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
            ))
    return out


def _parse_zones(tree: SList, numbered: dict[int, str]) -> list[dict]:
    """Parse copper ``(zone ...)`` regions into ``{net, layers, polygon}`` dicts.

    Args:
        tree: the parsed board tree.
        numbered: the net-number -> name table (for net resolution).

    Returns:
        One dict per zone with its net name, layer list, and outline points.
    """
    out = []
    for it in children(tree, "zone"):
        out.append({
            "net": _net_name(child(it, "net_name"), numbered)
                   or _net_name(child(it, "net"), numbered),
            "layers": strings(child(it, "layers")) or strings(child(it, "layer")),
            "polygon": _pts(child(child(it, "polygon"), "pts") if child(it, "polygon") else None),
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
    tree = sexpr.loads(Path(pcb_path).read_text())
    copper = _copper_layers(tree)
    numbered = _numbered_net_table(tree)
    name_only = len(numbered) == 0

    pads: list[Pad] = []
    for fp in children(tree, "footprint"):
        at = floats(child(fp, "at"))
        fx, fy = (at + [0.0, 0.0])[:2]
        fa = at[2] if len(at) >= 3 else 0.0
        ref = _footprint_ref(fp)
        for pad_node in children(fp, "pad"):
            p = _parse_pad(pad_node, fx, fy, fa, copper, numbered, ref)
            if p is not None:
                pads.append(p)

    return Board(
        tree=tree,
        copper_layers=copper,
        pads=pads,
        free_vias=_parse_free_vias(tree, numbered),
        segments=_parse_segments(tree, numbered),
        zones=_parse_zones(tree, numbered),
        outline=_parse_outline(tree),
        numbered_nets=numbered,
        name_only_nets=name_only,
    )


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


def write_board(board: Board, out_path: str | Path,
                new_nodes: list[SList] | None = None,
                strip_free_vias: bool = True) -> None:
    """Serialize a routed copy: drop free vias, append new segment/via nodes.

    Clones the parsed tree (untouched subtrees keep their source spans, so the
    diff against the input stays limited to the routing edits).

    Args:
        board: the source board whose tree is cloned.
        out_path: destination path for the routed ``.kicad_pcb``.
        new_nodes: freshly-built ``(segment ...)`` / ``(via ...)`` nodes to
            append (from `make_segment` / `make_via`); `None` for none.
        strip_free_vias: when True, omit the board's dangling free vias from the
            output.
    """
    strip_ids = {id(v.node) for v in board.free_vias} if strip_free_vias else set()
    new_root = SList()
    for ch in board.tree:
        if isinstance(ch, SList) and id(ch) in strip_ids:
            continue
        new_root.append(ch)
    for node in (new_nodes or []):
        new_root.append(node)
    Path(out_path).write_text(sexpr.dump_file(new_root))
