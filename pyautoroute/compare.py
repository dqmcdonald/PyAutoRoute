"""Compare routed boards from different sources (pyautoroute-compare CLI)."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .report import RoutingStats
    from .pcb import Board


@dataclass
class CompareResult:
    """Results of comparing 2-3 boards."""
    labels: list[str]
    stats: list[RoutingStats]
    excluded_nets: list[str]
    via_weight: float
    unrouted_weight: float
    design_warnings: list[str]


def compare(paths: list[str], *, pro: str | None = None, labels: list[str] | None = None,
            exclude: list[str] | None = None, via_weight: float = 2.0,
            unrouted_weight: float = 100.0) -> CompareResult:
    """Compare 2–3 routed boards of the same design.

    Args:
        paths: list of 2–3 `.kicad_pcb` file paths to compare.
        pro: optional `.kicad_pro` path for design rules (DRC). If omitted, auto-detects
            from first board's sibling or uses KiCad defaults.
        labels: optional list of board labels (e.g. ["PyAutoRoute", "Hand", "Tool X"]).
            If omitted, uses base filenames.
        exclude: optional list of net names to exclude from all boards (e.g. ["GND"]).
            Combined with auto-detected copper-pour nets.
        via_weight: cost coefficient for vias in scoring (default 2.0).
        unrouted_weight: cost coefficient for unrouted connections (default 100.0).

    Returns:
        A `CompareResult` with stats and metadata for each board.
    """
    from . import pcb, rules as rules_mod, report

    if len(paths) < 2 or len(paths) > 3:
        raise ValueError("Must provide 2–3 board paths")

    # Load boards
    boards = [pcb.load_board(p) for p in paths]

    # Load design rules from shared .kicad_pro
    pro_path = _resolve_pro(pro, paths)
    rules = rules_mod.load_rules(pro_path)

    # Collect copper-pour nets from all boards (union across all)
    pour_nets = set()
    for b in boards:
        pour_nets.update(pcb.zone_fill_nets(b))

    # Combine exclude sets: explicit excludes + auto-detected pour nets
    excl = sorted(pour_nets | set(exclude or []))

    # Score each board
    stats = [report.routing_stats(b, rules, exclude=excl) for b in boards]

    # Check design consistency
    warnings = _check_same_design(boards, stats, excl)

    return CompareResult(
        labels=labels or _auto_labels(paths),
        stats=stats,
        excluded_nets=excl,
        via_weight=via_weight,
        unrouted_weight=unrouted_weight,
        design_warnings=warnings,
    )


def _resolve_pro(pro: str | None, board_paths: list[str]) -> str | None:
    """Resolve the project file path.

    Tries (in order):
    1. The explicit ``--pro`` argument.
    2. Each board path with its suffix replaced by ``.kicad_pro``.
    3. Any ``.kicad_pro`` file in the same directory as the first board.
    Returns None if nothing is found (caller falls back to default rules).
    """
    if pro is not None:
        return pro
    # Try exact-name match for each supplied board path
    for bp in board_paths:
        candidate = Path(bp).with_suffix(".kicad_pro")
        if candidate.exists():
            return str(candidate)
    # Scan the directory of the first board for any .kicad_pro
    first_dir = Path(board_paths[0]).parent
    try:
        pros = list(first_dir.glob("*.kicad_pro"))
    except (OSError, ValueError):
        pros = []
    if len(pros) == 1:
        return str(pros[0])
    # Multiple .kicad_pro files — prefer the one whose stem matches any board stem
    board_stems = {Path(bp).stem for bp in board_paths}
    for candidate in pros:
        if candidate.stem in board_stems:
            return str(candidate)
    return str(pros[0]) if pros else None


def _auto_labels(paths: list[str]) -> list[str]:
    """Generate labels from board filenames."""
    return [Path(p).name for p in paths]


def _check_same_design(boards: list[Board], stats: list[RoutingStats], excl: list[str]) -> list[str]:
    """Check that all boards represent the same design.

    Returns a list of warning strings if there are inconsistencies (e.g. different
    connection counts, different nets). Non-fatal — the comparison proceeds but the
    warnings are reported to the user.
    """
    warnings = []

    # Check connection count consistency
    ref_total = stats[0].total
    for i, s in enumerate(stats[1:], 1):
        if s.total != ref_total:
            warnings.append(
                f"Board {i} has {s.total} connections (expected {ref_total}) — "
                f"different netlist?"
            )

    # Check net name consistency
    ref_nets = _get_board_nets(boards[0], excl)
    for i, b in enumerate(boards[1:], 1):
        board_nets = _get_board_nets(b, excl)
        if board_nets != ref_nets:
            added = board_nets - ref_nets
            missing = ref_nets - board_nets
            msg_parts = []
            if added:
                msg_parts.append(f"added {sorted(added)}")
            if missing:
                msg_parts.append(f"missing {sorted(missing)}")
            warnings.append(f"Board {i}: nets diverge ({'; '.join(msg_parts)})")

    return warnings


def _get_board_nets(board: Board, excl: list[str]) -> set[str]:
    """Get the set of non-excluded net names on a board."""
    excl_set = set(excl)
    nets = {p.net for p in board.pads if p.net not in excl_set}
    return nets


def _score(s: RoutingStats, via_weight: float, unrouted_weight: float) -> float:
    """Compute the headline score for a board."""
    return unrouted_weight * s.unrouted + s.length + via_weight * s.vias


def format_report(result: CompareResult) -> str:
    """Format the comparison result as a plain-text table."""
    scores = [_score(s, result.via_weight, result.unrouted_weight) for s in result.stats]
    ranked = _rank_boards(result, scores)

    lines = []
    lines.append("PyAutoRoute board comparison")
    n_boards = len(result.labels)
    n_conns = result.stats[0].total if result.stats else 0
    lines.append(f"  {n_boards} boards  ·  {n_conns} connections")
    if result.excluded_nets:
        lines.append(f"  ignored: {', '.join(result.excluded_nets)}  (copper-pour nets)")
    for warning in result.design_warnings:
        lines.append(f"  ⚠  {warning}")

    # ── Build metric rows as (name, [cell_value, ...], [is_best, ...]) ──────
    Row = tuple  # (metric_name: str, values: list[str], bests: list[bool])

    def _best_marker(bests: list[bool]) -> list[str]:
        return ["*" if b else " " for b in bests]

    rows: list[Row] = []

    # completion
    best_routed = max(s.routed for s in result.stats)
    rows.append(("completion", [
        f"{s.routed}/{s.total}  ({100*s.routed/s.total:.0f}%)" if s.total else "0/0"
        for s in result.stats
    ], [s.routed == best_routed for s in result.stats]))

    # DRC
    rows.append(("DRC", [
        f"{len(s.violations)} violation(s)" if s.violations else "clean"
        for s in result.stats
    ], [not s.violations for s in result.stats]))

    # wirelength
    best_len = min(s.length for s in result.stats)
    rows.append(("wirelength (mm)", [
        f"{s.length:.1f}" for s in result.stats
    ], [s.length == best_len for s in result.stats]))

    # directness
    directnesses = [
        s.length / s.ideal_length if s.ideal_length > 0 else None
        for s in result.stats
    ]
    best_dir = min((d for d in directnesses if d is not None), default=None)
    rows.append(("directness (×ideal)", [
        f"{d:.2f}×" if d is not None else "N/A" for d in directnesses
    ], [d is not None and d == best_dir for d in directnesses]))

    # vias (two sub-rows)
    best_vias = min(s.vias for s in result.stats)
    rows.append(("vias", [
        str(s.vias) for s in result.stats
    ], [s.vias == best_vias for s in result.stats]))
    rows.append(("  per connection", [
        f"{s.vias / s.total:.2f}" if s.total else "N/A"
        for s in result.stats
    ], [s.vias == best_vias for s in result.stats]))

    # score — DRC-dirty boards flagged with †
    best_score = min(scores)
    rows.append(("score", [
        f"{sc:.1f}" + (" †" if s.violations else "")
        for s, sc in zip(result.stats, scores)
    ], [sc == best_score and not s.violations
        for s, sc in zip(result.stats, scores)]))

    # ── Compute column widths from content ────────────────────────────────
    # Metric column: max metric name + 1 padding
    metric_w = max(len(r[0]) for r in rows) + 1

    # Data columns: max(label, widest cell value) + 2 padding; +2 for " *" marker
    col_ws = []
    for i, label in enumerate(result.labels):
        max_val = max(len(r[1][i]) for r in rows)
        col_ws.append(max(len(label), max_val) + 2)

    # ── Render ────────────────────────────────────────────────────────────
    def _hline(mid: str = "─") -> str:
        parts = ["─" * (metric_w + 1)]
        for w in col_ws:
            parts.append("─" * (w + 3))  # " │ " = 3 extra chars
        return "┼".join(parts)

    def _row(metric: str, vals: list[str], markers: list[str]) -> str:
        line = metric.ljust(metric_w)
        for val, mk, w in zip(vals, markers, col_ws):
            cell = f" {val.rjust(w)}{mk}"  # right-align value, marker on right
            line += f" │{cell}"
        return line

    # header
    lines.append("")
    header = " " * (metric_w + 1)
    for label, w in zip(result.labels, col_ws):
        header += f" │ {label.center(w)} "
    lines.append(header)
    lines.append(_hline())

    for metric, vals, bests in rows:
        lines.append(_row(metric, vals, _best_marker(bests)))

    lines.append(_hline())

    # footnotes
    if any(r[2].count(True) > 1 for r in rows):
        pass  # tied values all get * — no footnote needed
    if any(s.violations for s in result.stats):
        lines.append("  † board has DRC violations — excluded from ranking")

    # ranking + analysis
    lines.append("")
    lines.append("  ranking:  " + "   ".join(ranked))
    lines.append("")
    lines.append("  analysis:")
    for bullet in _generate_analysis(result, scores, ranked):
        lines.append(f"  - {bullet}")

    return "\n".join(lines)


def _layer_breakdown(result: CompareResult) -> dict[str, list[float]]:
    """Get segment lengths grouped by layer for each board.

    TODO: need board paths to compute this; CompareResult currently omits them.
    """
    # For now, return empty since we don't have board objects in CompareResult
    return {}


def _rank_boards(result: CompareResult, scores: list[float]) -> list[str]:
    """Generate ranking strings."""
    # Identify valid boards (no DRC violations)
    valid_scores = [
        (i, score) for i, (score, stats) in enumerate(zip(scores, result.stats))
        if not stats.violations
    ]

    if not valid_scores:
        return ["(all boards have DRC violations)"]

    # Sort by score (lower is better)
    valid_scores.sort(key=lambda x: x[1])

    ranking = []
    for rank, (board_idx, _) in enumerate(valid_scores, 1):
        marker = "*" if result.stats[board_idx].violations else ""
        ranking.append(f"{rank}. {result.labels[board_idx]}{marker}")

    # Mark boards with violations but in the ranking
    for i, stats in enumerate(result.stats):
        if stats.violations and i not in [idx for idx, _ in valid_scores]:
            ranking.append(f"–. {result.labels[i]}* (DRC violations)")

    return ranking


def _generate_analysis(result: CompareResult, scores: list[float], ranked: list[str]) -> list[str]:
    """Generate prose analysis of the comparison."""
    analysis = []

    # Find best/worst boards
    n_boards = len(result.stats)
    if n_boards == 2:
        a_idx, b_idx = 0, 1
        a_better = scores[a_idx] < scores[b_idx]
        winner = result.labels[a_idx if a_better else b_idx]
        delta = abs(scores[a_idx] - scores[b_idx])
        analysis.append(
            f"{winner} wins by {delta:.1f} points (better completion, directness, or fewer vias)."
        )
    else:
        # Find hand-routed or best board for comparison
        best_idx = min(range(n_boards), key=lambda i: scores[i])
        best_label = result.labels[best_idx]
        analysis.append(f"{best_label} is the best, with the lowest score.")

        for i, (label, score, stats) in enumerate(zip(result.labels, scores, result.stats)):
            if i == best_idx or stats.violations:
                continue
            delta = score - scores[best_idx]
            if delta > 0:
                analysis.append(
                    f"{label} scores {delta:.0f} points higher than {best_label} "
                    f"({stats.unrouted} unrouted, +{stats.length - result.stats[best_idx].length:.0f} mm)."
                )

    return analysis


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point for pyautoroute-compare."""
    parser = argparse.ArgumentParser(
        description="Compare routed boards from different sources",
        prog="pyautoroute-compare"
    )
    parser.add_argument("boards", nargs="+", metavar="BOARD",
                        help="Path(s) to .kicad_pcb file(s) to compare (2–3 boards)")
    parser.add_argument("--pro", metavar="FILE",
                        help="Project file (.kicad_pro) for design rules [auto-detected]")
    parser.add_argument("--label", action="append", dest="labels",
                        help="Label for each board (repeatable; matched by order)")
    parser.add_argument("--exclude-net", action="append", dest="exclude",
                        help="Net name(s) to exclude from comparison (repeatable)")
    parser.add_argument("--via-weight", type=float, default=2.0,
                        help="Via cost in scoring (default 2.0)")
    parser.add_argument("--unrouted-weight", type=float, default=100.0,
                        help="Unrouted connection cost in scoring (default 100.0)")

    args = parser.parse_args(argv)

    try:
        result = compare(
            args.boards,
            pro=args.pro,
            labels=args.labels,
            exclude=args.exclude,
            via_weight=args.via_weight,
            unrouted_weight=args.unrouted_weight,
        )
        print(format_report(result))
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
