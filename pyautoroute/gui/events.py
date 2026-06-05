"""Event dataclasses posted by the worker thread to the UI queue."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Phase:
    """A new pipeline phase has started (e.g. 'routing 94 connections')."""
    name: str


@dataclass
class Progress:
    """Per-iteration telemetry from placement, routing, or annealing."""
    kind: str       # "placing" | "routing" | "annealing"
    it: int
    total: int
    energy: float
    best: float
    temp: float
    accept: float   # fraction of recent moves accepted (0..1); 0 for greedy routing
    routed: int
    unrouted: int
    elapsed: float = 0.0   # seconds elapsed in current run (non-zero → use time display)
    budget: float = 0.0    # time budget in seconds


@dataclass
class BoardSnap:
    """A thread-safe snapshot of drawable board state.

    The board object is a copy made on the worker thread while it holds
    a consistent state, so the main thread can read it safely.
    """
    board: Any
    results: Any = None        # list[RouteResult|None] for in-progress routing
    grid: Any = None           # routing grid, required when results is set
    kind: str = "current"      # "current" | "best"


@dataclass
class Done:
    """The pipeline completed successfully."""
    out_path: str
    total: int
    routed: int
    unrouted: int
    length: float
    vias: int
    violations: list = field(default_factory=list)
    board: Any = None
    warnings: list = field(default_factory=list)  # non-fatal issues raised during the run


@dataclass
class SelfCheck:
    """In-progress DRC self-check result (violations count)."""
    violations: int


@dataclass
class Error:
    """An exception terminated the pipeline."""
    exc: BaseException
    tb: str = ""


def collect_issues(done: "Done") -> list[str]:
    """Gather every non-fatal issue from a finished run into display lines.

    Combines unrouted connections, DRC self-check violations (clearance and
    hole-to-hole, told apart by tuple shape), and the warnings collected during
    the run (mounting holes, ground plane, placement). Kept tkinter-free so it
    can be unit-tested and reused by the app's end-of-run summary dialog.

    Args:
        done: the `Done` event for the finished run.

    Returns:
        One human-readable line per issue (empty list when the run was clean).
    """
    issues: list[str] = []
    if done.unrouted > 0:
        issues.append(f"{done.unrouted} unrouted connection(s)")
    # violations is a combined list: clearance tuples are (layer, a, b, gap);
    # hole-to-hole tuples are (ref_a, ref_b, gap).
    clearance = [v for v in done.violations if len(v) == 4]
    holes = [v for v in done.violations if len(v) == 3]
    if clearance:
        issues.append(f"{len(clearance)} clearance violation(s) — "
                      "review the board in KiCad")
    if holes:
        issues.append(f"{len(holes)} hole-to-hole spacing violation(s)")
    issues.extend(done.warnings)
    return issues
