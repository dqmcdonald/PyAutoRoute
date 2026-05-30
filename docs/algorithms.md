# The algorithms, in plain language

This is a friendly, jargon-light tour of *how* PyAutoRoute decides where to put
copper. It complements [`architecture.md`](architecture.md), which is the precise
developer reference (data structures, formulas, file:line). Read this first if
you want the intuition; read that one when you need the details.

The job: you give PyAutoRoute a board with parts placed and nets assigned but no
wires. It has to draw wires (tracks) on two copper layers, front and back,
connecting everything that should be connected, without any two different nets
touching or coming too close.

It does this in a few stages. Here's what each one is doing and why.

---

## 1. The routing grid — turning the board into graph paper

Computers find paths much more easily on a grid than in continuous space, so the
first thing PyAutoRoute does is lay an invisible sheet of graph paper over the
board: a regular lattice of points (nodes), one stack per copper layer. A wire is
then just a path that hops from node to node.

Each node is marked as either **free**, **blocked** (a pad, the board edge, or
existing copper sits there), or **owned by a net** (you may route *that* net
through it, but no other).

The clever part is **clearance**. Two different nets must stay a minimum distance
apart. Rather than check distances during routing, PyAutoRoute *grows* every
obstacle by that distance up front (plus a small allowance for the grid's
coarseness) and blocks the nodes underneath. After that, any path the router
finds on the free nodes is automatically far enough from everything else — the
result is **"DRC-clean by construction"** (DRC = design-rule check). No wire ever
has to be checked and rejected afterwards.

> Analogy: instead of measuring, as you walk, whether you're too close to the
> walls, you paint a "keep-out" stripe along every wall first and then just stay
> off the paint.

---

## 2. Rats-nest decomposition — turning "nets" into simple A-to-B jobs

A net often connects more than two pads (think of a ground net touching dozens of
parts). The router, though, only knows how to connect **one point to one other
point**. So PyAutoRoute breaks each multi-pad net into a set of two-pin "connect
A to B" jobs.

Which pairs? It picks the set of connections that links all the net's pads with
the **least total length** — this is a classic **Minimum Spanning Tree (MST)**.
Picture the pads as towns and you want to lay the shortest total length of road
that still lets you drive between any two towns; you'd never build a redundant
road. The MST is exactly that shortest road network, and it's what the
on-screen "rats-nest" lines in a PCB tool show.

---

## 3. A\* — finding one good wire

Now the core: given graph paper and a single "connect A to B" job, find a good
path. PyAutoRoute uses **A\*** (pronounced "A-star"), the standard shortest-path
search used in everything from game AI to GPS navigation.

A\* explores outward from the start, always expanding the most promising node
next. "Most promising" = *cost so far* + *optimistic guess of the cost still to
go*. The guess (the **heuristic**) is the straight-line-ish distance to the
target — it never overestimates, which is what guarantees A\* returns a genuinely
shortest path rather than just *a* path.

What "shortest" means here isn't only length — the **cost model** encodes the
board's real priorities, in order:

1. **Length** — every step costs its true distance, so a 45° diagonal step
   (cost ≈ 1.41) genuinely beats going around two sides of a square (cost 2).
2. **Few bends** — turning corners adds a small penalty (sharper turn, bigger
   penalty), so wires come out straight and tidy with 45° corners rather than
   jagged staircases.
3. **Few vias** — switching from the front layer to the back needs a **via** (a
   plated hole), which costs a fixed penalty, so the router only changes layers
   when it really helps.
4. **Prefer the front** — a tiny per-step nudge so ties are broken in favour of
   the front layer.

> Analogy: A\* is a hiker heading for a peak they can see. They always step
> toward the most promising direction (progress made + distance still to go), and
> they'd rather walk a straight gentle path than zig-zag or climb over a ridge
> (a via) unless it's a real shortcut.

A net that can't be reached is simply **left unrouted and reported** — never
drawn as a short.

---

## 4. Simulated annealing — making the *whole* board better

A\* routes one wire optimally, but the *order* you route wires in matters: an
early wire can wall off a later one, forcing it into a long detour or leaving it
unroutable. There's no quick way to find the perfect order, so PyAutoRoute uses
**simulated annealing** to keep improving the whole arrangement.

Simulated annealing is borrowed from metallurgy: heat a metal and the atoms jiggle
freely; cool it *slowly* and they settle into a strong, low-energy crystal. The
algorithm mimics this. It defines an **energy** for the whole board —

> energy = total wire length + a penalty per via + a big penalty per unrouted net

— and tries random changes: rip up a cluster of wires and re-route them in a new
order, swap the order of two connections, and so on. After each change it checks
whether the energy went down.

The twist that stops it getting stuck: it doesn't *only* accept improvements.
Early on (when "hot") it will sometimes accept a change that makes things slightly
worse, which lets it climb out of a dead-end local arrangement. As it "cools",
it grows pickier and accepts only improvements, settling into a good solution. It
always remembers the best board it has seen and returns that.

> Analogy: shaking a tray of marbles to settle them into the lowest tray —
> shake hard at first to dislodge marbles stuck on bumps, then gently so they
> come to rest in the deepest pockets.

You control how long this runs with `--iters` (a number of attempts) or `--time`
(a wall-clock budget). With neither, the board is routed once in shortest-first
order and that's it.

---

## 5. Footprint placement — the same idea, applied to the parts (`--place`)

The experimental `--place` pass runs the *same* simulated-annealing idea one level
up: instead of moving wires, it moves the **parts**. Its energy rewards short
rats-nest connections, penalises parts overlapping, and penalises a sprawling
layout (to pull everything compact). Locked parts stay put; parts flagged
`Autoroute-overlap` may sit on top of others (e.g. a shield), and parts flagged
`Autoroute-edge` are pulled to the boundary and aligned flat against it. When it
finishes it draws a board outline around the result and hands off to routing as
usual.

---

## How the stages fit together

```
parse board ─▶ build grid (with clearance baked in)
                     │
   nets ─▶ rats-nest (MST) ─▶ list of "connect A to B" jobs
                     │
         ┌───────────┴───────────┐
         │  route each job (A*)   │ ◀── optional: simulated annealing
         └───────────┬───────────┘     keeps rerouting to lower the
                     │                  whole-board energy
              write DRC-clean board ─▶ self-check
```

Two ideas do most of the work: **bake clearance into the grid** so every result
is legal automatically, and **let A\* (one wire) and simulated annealing (the
whole board) cooperate** — A\* guarantees each wire is locally good, annealing
shuffles the global picture to escape bad local arrangements. For the exact cost
formulas, clearance margins, and data structures, see
[`architecture.md`](architecture.md).
