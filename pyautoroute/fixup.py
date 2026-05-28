"""Board fixup utilities: command-line tool for correcting common issues.

Currently supported operations:

``--values``
    Move footprint ``Value`` text to the appropriate silkscreen layer
    (``F.SilkS`` for front-side footprints, ``B.SilkS`` for back-side).
    KiCad often places value text on ``F.Fab``/``B.Fab`` by default; this
    puts it where it will appear on the physical board.

Usage::

    pyautoroute-fix --values board.kicad_pcb
    pyautoroute-fix --values board.kicad_pcb -o fixed.kicad_pcb
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pyautoroute-fix",
        description="Apply fixups to a KiCad PCB file.",
    )
    p.add_argument("input", metavar="BOARD.kicad_pcb",
                   help="Input board file.")
    p.add_argument("-o", "--output", metavar="OUT.kicad_pcb",
                   help="Output path (default: overwrite input).")
    p.add_argument("--values", action="store_true",
                   help="Move Value text to the appropriate silkscreen layer.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would change without writing the file.")
    return p


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.values:
        parser.error("No fixup selected. Add --values (or another fixup flag).")

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"error: file not found: {in_path}", file=sys.stderr)
        return 1

    out_path = Path(args.output) if args.output else in_path

    from pyautoroute import pcb

    board = pcb.load_board(in_path)
    total = 0

    if args.values:
        n = pcb.fix_value_layers(board)
        total += n
        if n:
            print(f"  values: moved {n} Value text node(s) to silkscreen.")
        else:
            print("  values: all Value text is already on a silkscreen layer.")

    if total == 0:
        print("No changes needed.")
        return 0

    if args.dry_run:
        print(f"Dry run: {total} change(s) would be written to {out_path}.")
        return 0

    from pyautoroute.sexpr import dump_file
    out_path.write_text(dump_file(board.tree))
    print(f"Written: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
