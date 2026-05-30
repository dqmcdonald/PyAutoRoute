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

    if not paths or len(paths) > 3:
        raise ValueError("Must provide 2–3 board paths")

    # Load boards
    boards = [pcb.load_board(p) for p in paths]

    # Load design rules from shared .kicad_pro
    pro_path = _resolve_pro(pro, paths[0])
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


def _resolve_pro(pro: str | None, first_path: str) -> str | None:
    """Resolve the project file path.

    If `pro` is given, return it. Otherwise, try first_path's sibling `.kicad_pro`,
    then with the same stem. Return None if not found.
    """
    if pro is not None:
        return pro
    p = Path(first_path)
    with_suffix = p.with_suffix(".kicad_pro")
    if with_suffix.exists():
        return str(with_suffix)
    with_stem = p.with_name(p.stem + ".kicad_pro")
    if with_stem.exists():
        return str(with_stem)
    return None


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
    """Format the comparison result as a plain-text report."""
    lines = []
    lines.append("PyAutoRoute board comparison")

    # Header
    pro_label = "(no project file)"  # We don't have path info in CompareResult
    lines.append(f"  design:  {pro_label}  ({len(result.labels)} boards, "
                 f"{result.stats[0].total} connections)")

    if result.excluded_nets:
        lines.append(f"  ignored: {', '.join(result.excluded_nets)}  (copper-pour nets)")

    for warning in result.design_warnings:
        lines.append(f"  ⚠ {warning}")

    # Metrics table
    lines.append("")
    labels = result.labels
    n_boards = len(labels)

    # Column widths
    metric_width = 18
    col_width = 16

    # Header row
    header_parts = [""]  # metric column
    for label in labels:
        header_parts.append(label.ljust(col_width))
    lines.append("".join(header_parts))

    # Completion
    completion_line = ["completion".ljust(metric_width)]
    best_routed = max(s.routed for s in result.stats)
    for s in result.stats:
        pct = 100 * s.routed / s.total if s.total > 0 else 0
        marker = "[best]" if s.routed == best_routed else ""
        val = f"{s.routed}/{s.total} {pct:.0f}% {marker}".ljust(col_width)
        completion_line.append(val)
    lines.append("".join(completion_line))

    # DRC violations
    drc_line = ["DRC".ljust(metric_width)]
    for s in result.stats:
        if s.violations:
            val = f"{len(s.violations)} ✗".ljust(col_width)
        else:
            val = "clean".ljust(col_width)
        drc_line.append(val)
    lines.append("".join(drc_line))

    # Wirelength
    wirelength_line = ["wirelength (mm)".ljust(metric_width)]
    best_length = min(s.length for s in result.stats)
    for s in result.stats:
        marker = "[best]" if s.length == best_length else ""
        val = f"{s.length:.1f} {marker}".ljust(col_width)
        wirelength_line.append(val)
    lines.append("".join(wirelength_line))

    # Directness
    directness_line = ["directness (×ideal)".ljust(metric_width)]
    best_directness = min(
        s.length / s.ideal_length if s.ideal_length > 0 else float('inf')
        for s in result.stats
    )
    for s in result.stats:
        if s.ideal_length > 0:
            directness = s.length / s.ideal_length
            marker = "[best]" if directness == best_directness else ""
            val = f"{directness:.2f}× {marker}".ljust(col_width)
        else:
            val = "N/A".ljust(col_width)
        directness_line.append(val)
    lines.append("".join(directness_line))

    # Vias
    vias_line = ["vias".ljust(metric_width)]
    best_vias = min(s.vias for s in result.stats)
    for s in result.stats:
        per_conn = s.vias / s.total if s.total > 0 else 0
        marker = "[best]" if s.vias == best_vias else ""
        val = f"{s.vias} ({per_conn:.2f}/conn) {marker}".ljust(col_width)
        vias_line.append(val)
    lines.append("".join(vias_line))

    # Layers (length by layer)
    layers_by_name = _layer_breakdown(result)
    if layers_by_name:
        for layer_name in sorted(layers_by_name.keys()):
            layer_line = [f"  {layer_name}".ljust(metric_width)]
            for lengths in layers_by_name[layer_name]:
                val = f"{lengths:.0f} mm".ljust(col_width)
                layer_line.append(val)
            lines.append("".join(layer_line))

    # Score
    scores = [_score(s, result.via_weight, result.unrouted_weight) for s in result.stats]
    best_score = min(scores)
    score_line = ["score".ljust(metric_width)]
    for s, score in zip(result.stats, scores):
        # Flag boards with DRC violations
        drc_marker = " *" if s.violations else ""
        marker = "[best]" if score == best_score else ""
        val = f"{score:.1f}{drc_marker} {marker}".ljust(col_width)
        score_line.append(val)
    lines.append("".join(score_line))

    # Ranking
    lines.append("")
    ranked = _rank_boards(result, scores)
    ranking_str = "  ranking: " + "  ".join(ranked)
    lines.append(ranking_str)

    if result.design_warnings:
        lines.append("  (* board has design/netlist inconsistencies)")
    if any(s.violations for s in result.stats):
        lines.append("  (* board has DRC violations — excluded from ranking)")

    # Analysis
    lines.append("")
    lines.append("  analysis:")
    analysis = _generate_analysis(result, scores, ranked)
    for bullet in analysis:
        lines.append(f"  - {bullet}")

    return "\n".join(lines)


def _layer_breakdown(result: CompareResult) -> dict[str, list[float]]:
    """Get segment lengths grouped by layer for each board."""
    from . import pcb

    layers_by_board = []
    all_layers = set()

    for board_path, stats in zip([], result.stats):  # TODO: need board paths
        # For now, skip layer breakdown since we don't have board objects in CompareResult
        pass

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
