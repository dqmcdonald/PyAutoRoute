"""Simulated-annealing optimisation over an initial routing.

Starting from a committed greedy routing, SA applies local moves — reroute one
connection, swap the routing order of two, or rip up a failed connection plus its
neighbours and retry — each evaluated incrementally against the live
``RoutingState`` (rip-up/reroute, never a full-board re-route). Worse moves are
accepted with Metropolis probability under a geometric cooling schedule; the
best-seen routing is kept and returned.

Energy E = wirelength + via_weight·(#vias) + unrouted_weight·(#unrouted).
DRC cleanliness is guaranteed by the router, so there is no violation term.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field

from . import netlist, router
from .router import RouteParams, RouteResult, RoutingState


@dataclass
class AnnealParams:
    iters: int | None = None
    time_budget: float | None = None
    t_start: float = 4.0
    t_end: float = 0.05
    unrouted_weight: float = 100.0
    rip_neighbours: int = 4
    seed: int = 0
    route_params: RouteParams = field(default_factory=lambda: RouteParams(max_expansions=400_000))


@dataclass
class AnnealResult:
    results: list[RouteResult | None]
    routed: int
    unrouted: int
    total_length: float
    total_vias: int
    iterations: int
    accepted: int
    start_energy: float
    best_energy: float


def _energy(results, via_weight: float, unrouted_weight: float) -> float:
    length = vias = 0.0
    unrouted = 0
    for r in results:
        if r is None:
            unrouted += 1
        else:
            length += r.length
            vias += r.vias
    return length + via_weight * vias + unrouted_weight * unrouted


def _aggregate(results):
    routed = sum(1 for r in results if r is not None)
    length = sum(r.length for r in results if r is not None)
    vias = sum(r.vias for r in results if r is not None)
    return routed, len(results) - routed, length, vias


def _centroid(conn):
    return ((conn.a.cx + conn.b.cx) / 2.0, (conn.a.cy + conn.b.cy) / 2.0)


class _Annealer:
    def __init__(self, state: RoutingState, connections, results, params: AnnealParams):
        self.state = state
        self.conns = connections
        self.results = results
        self.p = params
        self.rng = random.Random(params.seed)
        self.via_weight = params.route_params.via_cost

    def _route(self, idx):
        conn = self.conns[idx]
        grid = self.state.grid
        return router.route_connection(
            self.state, conn.net,
            grid.pad_access_nodes(conn.a), grid.pad_access_nodes(conn.b),
            self.p.route_params)

    def _apply(self, ripped: list[int], suborder: list[int]) -> dict:
        snapshot = {idx: self.results[idx] for idx in ripped}
        for idx in ripped:
            if self.results[idx] is not None:
                self.state.ripup(idx)
        for idx in suborder:
            res = self._route(idx)
            self.results[idx] = res
            if res is not None:
                self.state.commit(idx, res)
        return snapshot

    def _revert(self, snapshot: dict):
        for idx in snapshot:
            if self.results[idx] is not None:
                self.state.ripup(idx)
        for idx, old in snapshot.items():
            self.results[idx] = old
            if old is not None:
                self.state.commit(idx, old)

    def _propose(self) -> tuple[list[int], list[int]]:
        n = len(self.conns)
        unrouted = [i for i, r in enumerate(self.results) if r is None]
        if unrouted and self.rng.random() < 0.5:
            u = self.rng.choice(unrouted)
            cu = _centroid(self.conns[u])
            others = sorted(
                (i for i in range(n) if i != u and self.results[i] is not None),
                key=lambda i: math.dist(cu, _centroid(self.conns[i])))
            ripped = [u] + others[:self.p.rip_neighbours]
            return ripped, ripped                      # route the failed one first
        if n >= 2 and self.rng.random() < 0.4:
            i, j = self.rng.sample(range(n), 2)
            return [i, j], [j, i]                       # swap routing order
        i = self.rng.randrange(n)
        return [i], [i]                                 # reroute one

    def run(self, on_progress=None) -> AnnealResult:
        E = _energy(self.results, self.via_weight, self.p.unrouted_weight)
        start_E = E
        best_E = E
        best = list(self.results)
        accepted = 0

        total = self.p.iters if self.p.iters else 1_000_000
        t0 = time.time()
        ratio = self.p.t_end / self.p.t_start
        it = 0
        while True:
            if self.p.iters is not None and it >= self.p.iters:
                break
            if self.p.time_budget is not None and time.time() - t0 >= self.p.time_budget:
                break
            if self.p.iters is None and self.p.time_budget is None and it >= 200:
                break

            frac = (it / total) if self.p.time_budget is None else min(
                1.0, (time.time() - t0) / self.p.time_budget)
            T = self.p.t_start * (ratio ** frac)

            ripped, suborder = self._propose()
            snapshot = self._apply(ripped, suborder)
            E_new = _energy(self.results, self.via_weight, self.p.unrouted_weight)
            dE = E_new - E
            if dE <= 0 or self.rng.random() < math.exp(-dE / max(T, 1e-9)):
                E = E_new
                accepted += 1
                if E < best_E:
                    best_E = E
                    best = list(self.results)
            else:
                self._revert(snapshot)

            it += 1
            if on_progress is not None:
                routed = sum(1 for r in self.results if r is not None)
                on_progress(it, total, routed, len(self.results) - routed,
                            E, best_E, T)

        routed, unrouted, length, vias = _aggregate(best)
        return AnnealResult(best, routed, unrouted, length, vias,
                            it, accepted, start_E, best_E)


def anneal(state: RoutingState, connections, results, params: AnnealParams,
           on_progress=None) -> AnnealResult:
    """Optimise an already-committed routing in place; return the best seen."""
    return _Annealer(state, connections, results, params).run(on_progress)
