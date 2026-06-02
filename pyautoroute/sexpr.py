"""Round-trip-safe KiCad s-expression reader/writer.

KiCad `.kicad_pcb` files are s-expressions with a specific pretty-printing style:
tab indentation, a list whose items are all atoms on one line `(at 49.5 80)`, a
list containing sub-lists as a multi-line block with the leading atoms on the
opening line, and `(pts ...)` packing its `(xy ...)` children onto a single line.

Parsed atoms keep their exact source text (`Atom.raw`), so re-serializing an
unmodified tree reproduces the file byte-for-byte. New nodes built with the
`sym`/`string`/`number` helpers carry correctly-encoded `raw` text.
"""

from __future__ import annotations

from typing import Iterator, Union

Node = Union["Atom", "SList"]


class SList(list):
    """A list node that can remember the exact source text it was parsed from.

    When `span`/`src` are set, the serializer emits that slice verbatim, so any
    subtree we don't touch round-trips byte-for-byte regardless of KiCad's
    pretty-printing quirks (e.g. wrapping long atom lists). Mutating a node, or
    building a fresh one, leaves `span` unset so it is re-serialized generically.
    """

    src: str | None = None
    span: tuple[int, int] | None = None


class Atom:
    """A leaf token. `raw` is the exact source text (quotes/escapes preserved)."""

    __slots__ = ("raw",)

    def __init__(self, raw: str):
        self.raw = raw

    @property
    def is_string(self) -> bool:
        return len(self.raw) >= 2 and self.raw[0] == '"'

    @property
    def text(self) -> str:
        """Decoded value: an unquoted string, or the bare symbol/number text."""
        if self.is_string:
            return _unescape(self.raw[1:-1])
        return self.raw

    def as_float(self) -> float:
        """Parse the atom's raw text as a float.

        Returns:
            The numeric value of `raw` (raises `ValueError` if non-numeric).
        """
        return float(self.raw)

    def __repr__(self) -> str:
        return f"Atom({self.raw!r})"


def sym(s: str) -> Atom:
    """Build a bare symbol atom, e.g. ``sym('segment')`` -> ``segment``.

    Args:
        s: the symbol text, emitted verbatim (no quoting or escaping).

    Returns:
        An `Atom` whose `raw` is `s`.
    """
    return Atom(s)


def string(s: str) -> Atom:
    """Build a quoted-string atom with KiCad escaping, e.g. ``string('GND')`` -> ``"GND"``.

    Args:
        s: the decoded string value; backslashes and double quotes are escaped.

    Returns:
        An `Atom` whose `raw` is the quoted, escaped form of `s`.
    """
    return Atom('"' + _escape(s) + '"')


def number(x: float | int) -> Atom:
    """Build a numeric atom; whole values render without a decimal point.

    Args:
        x: the numeric value. Integers and whole-valued floats render as
            integers; other floats use the shortest round-trippable form.

    Returns:
        An `Atom` carrying the formatted number text.
    """
    if isinstance(x, int) or (isinstance(x, float) and x.is_integer()):
        return Atom(str(int(x)))
    return Atom(_fmt_float(x))


def _fmt_float(x: float) -> str:
    """Format a float as the shortest round-trippable text (KiCad trims zeros).

    Args:
        x: the value to format.
    """
    return repr(float(x))


def _escape(s: str) -> str:
    """Escape backslashes, double quotes, and control characters for a KiCad quoted string.

    Args:
        s: the raw string value to escape.
    """
    return (s.replace("\\", "\\\\")
             .replace('"', '\\"')
             .replace('\n', '\\n')
             .replace('\r', '\\r')
             .replace('\t', '\\t'))


def _unescape(s: str) -> str:
    """Reverse `_escape`: interpret C-style backslash escapes in a quoted-string body.

    Args:
        s: the inner text of a quoted atom (without surrounding quotes).
    """
    _ESC = {'n': '\n', 'r': '\r', 't': '\t', '"': '"', '\\': '\\'}
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            out.append(_ESC.get(s[i + 1], s[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


# --- tokenizer ---------------------------------------------------------------

def _tokenize(text: str) -> Iterator[tuple[str, int, int]]:
    """Split s-expression source into tokens, preserving source offsets.

    Args:
        text: the full s-expression source.

    Yields:
        ``(token_text, start, end)`` for each paren, quoted string, or bare
        atom, where `end` is exclusive.
    """
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "(" or c == ")":
            yield c, i, i + 1
            i += 1
            continue
        if c == '"':
            start = i
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    i += 1
                    break
                i += 1
            yield text[start:i], start, i
            continue
        # bare atom: run until whitespace or paren or quote
        start = i
        while i < n and text[i] not in ' \t\r\n()"':
            i += 1
        yield text[start:i], start, i


# --- parser ------------------------------------------------------------------

def loads(text: str) -> SList:
    """Parse one top-level s-expression into a nested tree of Atom/SList nodes.

    Each SList records its source span so an unmodified subtree serializes back
    to its exact original bytes.

    Args:
        text: the s-expression source (e.g. the contents of a `.kicad_pcb`).

    Returns:
        The root `SList`.

    Raises:
        ValueError: on unbalanced parentheses, a stray atom, or empty input.
    """
    stack: list[tuple[SList, int]] = []
    root: SList | None = None
    for tok, start, end in _tokenize(text):
        if tok == "(":
            new = SList()
            if stack:
                stack[-1][0].append(new)
            stack.append((new, start))
        elif tok == ")":
            node, open_at = stack.pop()
            node.src = text
            node.span = (open_at, end)
            if not stack:
                root = node
        else:
            if not stack:
                raise ValueError(f"atom {tok!r} outside any list")
            stack[-1][0].append(Atom(tok))
    if stack:
        raise ValueError("unbalanced parentheses: unclosed list")
    if root is None:
        raise ValueError("no s-expression found")
    return root


# --- serializer --------------------------------------------------------------

def head_symbol(node: Node) -> str | None:
    """Return the leading symbol of a list node, e.g. ``'pad'`` for ``(pad ...)``.

    Args:
        node: any node; only a non-empty list with an `Atom` head qualifies.

    Returns:
        The head atom's `raw` text, or `None` for atoms / empty / atomless-head
        lists.
    """
    if isinstance(node, list) and node and isinstance(node[0], Atom):
        return node[0].raw
    return None


def _is_atom(node: Node) -> bool:
    """Return whether `node` is an `Atom` (leaf) rather than an `SList`.

    Args:
        node: the node to test.
    """
    return isinstance(node, Atom)


def dumps(node: Node, indent: str = "", verbatim: bool = True) -> str:
    """Serialize a node using KiCad's formatting style (no trailing newline).

    Args:
        node: the `Atom` or `SList` to serialize.
        indent: the indent prefix for this node's lines (grows by a tab per
            level during recursion).
        verbatim: when True, an `SList` that still carries its source span is
            emitted byte-for-byte; pass False to force generic formatting (used
            to exercise the formatter and to re-render mutated subtrees).

    Returns:
        The serialized text, without a trailing newline.
    """
    if _is_atom(node):
        return node.raw

    if verbatim and isinstance(node, SList) and node.span is not None and node.src is not None:
        return node.src[node.span[0]:node.span[1]]

    # all-atom list -> single line
    if all(_is_atom(it) for it in node):
        return "(" + " ".join(it.raw for it in node) + ")"

    child_indent = indent + "\t"

    # (pts ...) packs its child lists onto one indented line
    if head_symbol(node) == "pts":
        inner = " ".join(dumps(it, child_indent, verbatim) for it in node[1:])
        return "(pts\n" + child_indent + inner + "\n" + indent + ")"

    # leading atoms stay on the opening line; list children each get a line
    first_list = next(
        (i for i, it in enumerate(node) if isinstance(it, list)), len(node)
    )
    head = " ".join(it.raw for it in node[:first_list])
    out = "(" + head
    for it in node[first_list:]:
        out += "\n" + child_indent + dumps(it, child_indent, verbatim)
    out += "\n" + indent + ")"
    return out


def dump_file(node: Node) -> str:
    """Serialize a top-level node as a complete file (with trailing newline).

    Args:
        node: the root node to serialize.

    Returns:
        The full file text, including the trailing newline KiCad expects.
    """
    return dumps(node) + "\n"
