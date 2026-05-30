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


@dataclass
class Error:
    """An exception terminated the pipeline."""
    exc: BaseException
    tb: str = ""
