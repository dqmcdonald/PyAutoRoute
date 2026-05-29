"""Simulated-annealing optimisation over an initial routing.

Starting from a committed greedy routing, SA applies local moves — rip up a
connection (failed *or* already routed) plus its nearest neighbours and reroute
the freed cluster, swap the routing order of two, or reroute one — each evaluated
incrementally against the live ``RoutingState`` (rip-up/reroute, never a
full-board re-route). The cluster rip-and-reroute is what shortens an
already-complete board: freeing a local group and re-routing it in a fresh order
lets a connection claim a more direct path than it won during the original
sequential pass. Worse moves are accepted with Metropolis probability under a
geometric cooling schedule; the best-seen routing is kept and returned.

Energy E = wirelength + via_weight·(#vias) + unrouted_weight·(#unrouted).
DRC cleanliness is guaranteed by the router, so there is no violation term.
"""

from __future__ import annotations

import math
import random
import time
from collections import deque
from dataclasses import dataclass, field

from . import netlist, router
from .router import RouteParams, RouteResult, RoutingState

# Window (in iterations) over which the live acceptance ratio is measured. A
# recent rate tracks how the cooling schedule bites — it falls towards zero as
# T cools — which the cumulative accepted/iters figure (kept in AnnealResult)
# masks because it is dominated by the hot start.
_ACCEPT_WINDOW = 100


@dataclass
class AnnealParams:
    iters: int | None = None
    time_budget: float | None = None
    t_start: float = 4.0
    t_end: float = 0.05
    unrouted_weight: float = 100.0
    rip_neighbours: int = 4
    seed: int = 0
    snapshots: int = 0          # number of board snapshots to emit across the run
    route_params: RouteParams = field(default_factory=lambda: RouteParams(max_expansions=400_000))
    # Early-termination (stall detection): if the windowed accept ratio stays
    # below `stall_ratio` for `stall_patience` consecutive full accept-windows,
    # the run stops early. Disabled when `stall_patience <= 0` or
    # `stall_ratio <= 0` (the default), so the full budget is honoured.
    stall_ratio: float = 0.02
    stall_patience: int = 0


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
    """Compute the optimisation energy of a routing.

    Args:
        results: per-connection `RouteResult` or `None` (unrouted).
        via_weight: mm-equivalent cost per via.
        unrouted_weight: mm-equivalent penalty per unrouted connection.

    Returns:
        ``wirelength + via_weight*vias + unrouted_weight*unrouted``.
    """
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
    """Summarise a routing's totals.

    Args:
        results: per-connection `RouteResult` or `None`.

    Returns:
        ``(routed, unrouted, total_length, total_vias)``.
    """
    routed = sum(1 for r in results if r is not None)
    length = sum(r.length for r in results if r is not None)
    vias = sum(r.vias for r in results if r is not None)
    return routed, len(results) - routed, length, vias


def _centroid(conn):
    """Midpoint of a connection's two pad centres.

    Args:
        conn: the `pyautoroute.netlist.Connection`.

    Returns:
        The ``(x, y)`` midpoint in board mm.
    """
    return ((conn.a.cx + conn.b.cx) / 2.0, (conn.a.cy + conn.b.cy) / 2.0)


class _Annealer:
    def __init__(self, state: RoutingState, connections, results, params: AnnealParams):
        """Set up the annealer over an already-committed routing.

        Args:
            state: the live routing state (occupancy), mutated in place.
            connections: the full connection list.
            results: the current per-connection results; mutated during the run.
            params: the annealing parameters.
        """
        self.state = state
        self.conns = connections
        self.results = results
        self.p = params
        self.rng = random.Random(params.seed)
        self.via_weight = params.route_params.via_cost

    def _route(self, idx):
        """Route connection `idx` against the current state.

        Args:
            idx: the connection index.

        Returns:
            Its `RouteResult`, or `None` if unroutable now.
        """
        conn = self.conns[idx]
        grid = self.state.grid
        return router.route_connection(
            self.state, conn.net,
            grid.pad_access_nodes(conn.a), grid.pad_access_nodes(conn.b),
            self.p.route_params,
            src_xy=(conn.a.cx, conn.a.cy), dst_xy=(conn.b.cx, conn.b.cy))

    def _apply(self, ripped: list[int], suborder: list[int]) -> dict:
        """Rip up `ripped`, then re-route `suborder`, committing successes.

        Args:
            ripped: connection indices to rip up first.
            suborder: connection indices to re-route, in order (same set as
                `ripped`).

        Returns:
            A snapshot ``{idx: previous_result}`` for `_revert`.
        """
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
        """Undo an `_apply`, restoring the pre-move results.

        Args:
            snapshot: the ``{idx: previous_result}`` returned by `_apply`.
        """
        for idx in snapshot:
            if self.results[idx] is not None:
                self.state.ripup(idx)
        for idx, old in snapshot.items():
            self.results[idx] = old
            if old is not None:
                self.state.commit(idx, old)

    def _rip_cluster(self, seed: int, shuffle: bool) -> tuple[list[int], list[int]]:
        """Rip the seed connection plus its nearest routed neighbours and reroute
        the cluster. Ripping the whole cluster before re-routing frees the local
        space, so a connection routed first can take a more direct path than it
        held in the original sequential routing. ``shuffle`` randomises the
        re-route order (for optimising routed nets); otherwise the seed is routed
        first (to give a previously-failed net priority)."""
        cs = _centroid(self.conns[seed])
        neighbours = sorted(
            (i for i, r in enumerate(self.results) if r is not None and i != seed),
            key=lambda i: math.dist(cs, _centroid(self.conns[i])))
        cluster = [seed] + neighbours[:self.p.rip_neighbours]
        order = list(cluster)
        if shuffle:
            self.rng.shuffle(order)
        return cluster, order

    def _propose(self) -> tuple[list[int], list[int]]:
        """Pick the next move.

        Returns:
            ``(ripped, suborder)`` — the indices to rip up and the order to
            re-route them in. See the module docstring for the move mix.
        """
        n = len(self.conns)
        unrouted = [i for i, r in enumerate(self.results) if r is None]
        if unrouted and self.rng.random() < 0.5:
            return self._rip_cluster(self.rng.choice(unrouted), shuffle=False)

        routed = [i for i, r in enumerate(self.results) if r is not None]
        r = self.rng.random()
        if routed and r < 0.7:
            # rip a routed cluster + reroute in a fresh order to shorten the wiring
            return self._rip_cluster(self.rng.choice(routed), shuffle=True)
        if n >= 2 and r < 0.9:
            i, j = self.rng.sample(range(n), 2)
            return [i, j], [j, i]                       # swap routing order
        i = self.rng.randrange(n)
        return [i], [i]                                 # reroute one

    def run(self, on_progress=None, on_snapshot=None, cancel=None) -> AnnealResult:
        """Run the annealing loop and return the best routing seen.

        Args:
            on_progress: optional callback ``(it, total, routed, unrouted,
                energy, best, temp, accept)`` invoked each iteration, where
                ``accept`` is the fraction of moves accepted over the last
                ``_ACCEPT_WINDOW`` iterations.
            on_snapshot: optional callback ``(k, n, results)`` fired
                ``params.snapshots`` times across the run (see `anneal`).
            cancel: optional `threading.Event`; when set, the loop stops early
                and the best routing found so far is returned (for a GUI Stop
                button).

        Returns:
            The `AnnealResult` with the best routing and run statistics.
        """
        E = _energy(self.results, self.via_weight, self.p.unrouted_weight)
        start_E = E
        best_E = E
        best = list(self.results)
        accepted = 0
        recent = deque(maxlen=_ACCEPT_WINDOW)   # 1/0 per recent move, for the live ratio

        # Stall detection: count consecutive completed accept-windows whose
        # acceptance ratio stayed below `stall_ratio`; break after
        # `stall_patience` of them. Disabled when either knob is non-positive.
        stall_on = self.p.stall_patience > 0 and self.p.stall_ratio > 0.0
        stall_count = 0
        window_seen = 0

        total = self.p.iters if self.p.iters else 1_000_000
        t0 = time.time()
        ratio = self.p.t_end / self.p.t_start
        n_snap = self.p.snapshots if on_snapshot else 0
        next_snap = 1
        it = 0
        while True:
            if cancel is not None and cancel.is_set():
                break
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
            accept = dE <= 0 or self.rng.random() < math.exp(-dE / max(T, 1e-9))
            if accept:
                E = E_new
                accepted += 1
                if E < best_E:
                    best_E = E
                    best = list(self.results)
            else:
                self._revert(snapshot)
            recent.append(1 if accept else 0)

            it += 1
            window_seen += 1
            if on_progress is not None:
                routed = sum(1 for r in self.results if r is not None)
                on_progress(it, total, routed, len(self.results) - routed,
                            E, best_E, T, sum(recent) / len(recent))
            # emit intermediate snapshots as the run crosses k/N of its progress;
            # the final k=N snapshot is taken after the loop on the best routing.
            while n_snap and next_snap < n_snap and frac >= next_snap / n_snap:
                on_snapshot(next_snap, n_snap, self.results)
                next_snap += 1

            if stall_on and window_seen >= _ACCEPT_WINDOW:
                if sum(recent) / len(recent) < self.p.stall_ratio:
                    stall_count += 1
                    if stall_count >= self.p.stall_patience:
                        break
                else:
                    stall_count = 0
                window_seen = 0

        while n_snap and next_snap <= n_snap:
            on_snapshot(next_snap, n_snap, best)
            next_snap += 1

        routed, unrouted, length, vias = _aggregate(best)
        return AnnealResult(best, routed, unrouted, length, vias,
                            it, accepted, start_E, best_E)


def anneal(state: RoutingState, connections, results, params: AnnealParams,
           on_progress=None, on_snapshot=None, cancel=None) -> AnnealResult:
    """Optimise an already-committed routing in place; return the best seen.

    Args:
        state: the live routing state (occupancy), mutated in place.
        connections: the full connection list.
        results: the current per-connection results to optimise from.
        params: the annealing parameters (budget, schedule, weights, snapshots).
        on_progress: optional per-iteration progress callback (see
            `_Annealer.run`).
        on_snapshot: optional snapshot callback. When `params.snapshots` is set
            and this is given, it is invoked ``params.snapshots`` times across
            the run as ``on_snapshot(k, n, results)`` — intermediate calls
            capture the live routing as the run crosses each ``k/n`` of its
            progress, and the final call captures the best routing found. Useful
            for visualising how annealing improves the board.
        cancel: optional `threading.Event`; when set, the run stops early and
            returns the best routing found so far.

    Returns:
        The `AnnealResult` with the best routing and run statistics.
    """
    return _Annealer(state, connections, results, params).run(
        on_progress, on_snapshot, cancel)
