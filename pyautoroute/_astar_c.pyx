# cython: boundscheck=False, wraparound=False, cdivision=True, language_level=3
"""Cython core of the A* maze search (optional fast path for ``router.astar``).

This module reimplements the inner search loop of :func:`pyautoroute.router.astar`
in Cython for a 5-20x speedup over the optimised Python version, while producing
**bit-for-bit identical** paths and costs. It is an *optional* extension: when it
is not built, ``router`` falls back transparently to the pure-Python search.

The function operates on the same precomputed structures the Python search builds
(a per-net boolean free mask, an octile heuristic field, the via-stencil and
via-layer neighbour lists), so the two implementations share their inputs exactly
and only the heap-driven expansion loop differs. Search states use the identical
integer packing as the Python version
(``((layer*ny + row)*nx + col)*9 + dir+1``) and the same ``(f, counter)`` heap
ordering, so tie-breaking — and therefore the chosen path — matches.
"""

import numpy as np
cimport numpy as cnp
from libc.math cimport sqrt, INFINITY
from libc.stdlib cimport malloc, free as cfree, realloc

cnp.import_array()

cdef double SQRT2 = sqrt(2.0)

# 8 compass directions (matches router._DIRS), indexed 0..7.
cdef int[8] DX = [1, 1, 0, -1, -1, -1, 0, 1]
cdef int[8] DY = [0, 1, 1, 1, 0, -1, -1, -1]


cdef inline int turn_units(int a, int b) nogil:
    """45-degree turn magnitude (0-4) between two directions; 0 if either < 0."""
    cdef int d
    if a < 0 or b < 0:
        return 0
    d = a - b
    if d < 0:
        d = -d
    if 8 - d < d:
        return 8 - d
    return d


# --- binary min-heap of (f, counter, state, layer, col, row, dir) -------------
# Ordered by (f, counter) to match Python's heapq tuple ordering exactly.

cdef struct HeapItem:
    double f
    long long counter
    long long state
    int li
    int c
    int r
    int pdir


cdef struct Heap:
    HeapItem* data
    Py_ssize_t size
    Py_ssize_t cap


cdef inline int heap_less(HeapItem* a, HeapItem* b) nogil:
    if a.f < b.f:
        return 1
    if a.f > b.f:
        return 0
    return a.counter < b.counter


cdef inline bint heap_push(Heap* h, HeapItem it) noexcept nogil:
    """Push onto the heap. Returns False (leaving `h` untouched) if growing the
    backing array fails; the caller (which holds the GIL) must then raise —
    `realloc` returning NULL on allocation failure previously fell through to
    a NULL-pointer write below instead."""
    cdef Py_ssize_t i, parent, new_cap
    cdef HeapItem tmp
    cdef HeapItem* new_data
    if h.size >= h.cap:
        new_cap = h.cap * 2
        new_data = <HeapItem*>realloc(h.data, new_cap * sizeof(HeapItem))
        if new_data == NULL:
            return False
        h.data = new_data
        h.cap = new_cap
    h.data[h.size] = it
    i = h.size
    h.size += 1
    while i > 0:
        parent = (i - 1) >> 1
        if heap_less(&h.data[i], &h.data[parent]):
            tmp = h.data[i]
            h.data[i] = h.data[parent]
            h.data[parent] = tmp
            i = parent
        else:
            break
    return True


cdef inline HeapItem heap_pop(Heap* h) noexcept nogil:
    cdef HeapItem top = h.data[0]
    cdef HeapItem tmp
    cdef Py_ssize_t i, left, right, smallest
    h.size -= 1
    h.data[0] = h.data[h.size]
    i = 0
    while True:
        left = 2 * i + 1
        right = 2 * i + 2
        smallest = i
        if left < h.size and heap_less(&h.data[left], &h.data[smallest]):
            smallest = left
        if right < h.size and heap_less(&h.data[right], &h.data[smallest]):
            smallest = right
        if smallest == i:
            break
        tmp = h.data[i]
        h.data[i] = h.data[smallest]
        h.data[smallest] = tmp
        i = smallest
    return top


def astar(cnp.uint8_t[:, :, ::1] free_mask,
          double[:, ::1] hfield,
          object sources,
          object targets,
          object via_stencil,
          object via_layers,
          double pitch,
          double via_cost,
          double bend45, double bend90, double bend135, double bend180,
          double back_layer_penalty,
          long long max_expansions):
    """Run the A* search and return the node path, or ``None``.

    All occupancy / heuristic structures are precomputed by the caller so this
    matches the Python search bit-for-bit; only the heap loop runs here.

    Args:
        free_mask: boolean ``(n_layers, ny, nx)`` mask, True where the routing
            net may occupy a node (C-contiguous ``uint8``).
        hfield: octile-distance-to-target field ``(ny, nx)`` in mm.
        sources: list of ``(layer, col, row)`` start nodes.
        targets: list of ``(layer, col, row)`` goal nodes.
        via_stencil: list of ``(dc, dr)`` offsets the via clearance disk covers.
        via_layers: list (indexed by layer) of tuples of other layer indices a
            via may jump to from that layer.
        pitch: grid pitch in mm.
        via_cost: mm-equivalent penalty for a layer change.
        bend45, bend90, bend135, bend180: bend penalties indexed by turn units.
        back_layer_penalty: mm per step on a non-front (layer != 0) layer.
        max_expansions: search budget; returns ``None`` once exceeded.

    Returns:
        The ``(layer, col, row)`` path from a source to a target, deduplicated,
        or ``None`` if no target is reachable within the budget.
    """
    cdef int n_layers = free_mask.shape[0]
    cdef int ny = free_mask.shape[1]
    cdef int nx = free_mask.shape[2]
    cdef int _D = 9
    cdef long long LROW = nx * _D
    cdef long long n_states = <long long>n_layers * ny * LROW + 1

    if not sources or not targets:
        return None

    # bend penalty lookup (turn units 0..4)
    cdef double[5] bend
    bend[0] = 0.0
    bend[1] = bend45
    bend[2] = bend90
    bend[3] = bend135
    bend[4] = bend180

    # --- via stencil as C arrays ---
    cdef int n_sten = len(via_stencil)
    cdef int* sten_dc = <int*>malloc(n_sten * sizeof(int))
    cdef int* sten_dr = <int*>malloc(n_sten * sizeof(int))
    cdef int k
    for k in range(n_sten):
        sten_dc[k] = via_stencil[k][0]
        sten_dr[k] = via_stencil[k][1]

    # --- via layer neighbours flattened: offsets + values ---
    cdef int* via_off = <int*>malloc((n_layers + 1) * sizeof(int))
    cdef int total_via = 0
    for k in range(n_layers):
        via_off[k] = total_via
        total_via += len(via_layers[k])
    via_off[n_layers] = total_via
    cdef int* via_val = <int*>malloc((total_via if total_via else 1) * sizeof(int))
    cdef int j, idx2
    idx2 = 0
    for k in range(n_layers):
        for j in range(len(via_layers[k])):
            via_val[idx2] = via_layers[k][j]
            idx2 += 1

    # --- gscore / came as dict (sparse, like Python) ---
    cdef dict gscore = {}
    cdef dict came = {}        # state -> (pred_state_or_None, (li, c, r))

    # --- target membership set (encoded ignoring dir) ---
    # target_set holds (li, c, r) tuples; we test the node tuple directly.
    cdef set target_set = set()
    for t in targets:
        target_set.add((t[0], t[1], t[2]))

    # --- heap ---
    cdef Heap heap
    heap.cap = 1024
    heap.size = 0
    heap.data = <HeapItem*>malloc(heap.cap * sizeof(HeapItem))
    if heap.data == NULL:
        cfree(sten_dc)
        cfree(sten_dr)
        cfree(via_off)
        cfree(via_val)
        raise MemoryError("out of memory allocating the A* search heap")

    cdef long long counter = 0
    cdef HeapItem it
    cdef int li, c, r, pdir, di, nc, nr, nli
    cdef long long s, ns
    cdef double g, f, cost, step, ng, hval
    cdef bint diagonal
    cdef long long expansions = 0
    cdef object result = None

    try:
        for src in sources:
            li = src[0]; c = src[1]; r = src[2]
            if not (0 <= c < nx and 0 <= r < ny and free_mask[li, r, c]):
                continue
            # encode: ((li*ny + r)*nx + c)*9 + (dir+1); for dir=-1 -> +0
            s = ((<long long>li * ny + r) * nx + c) * _D + 0
            gscore[s] = 0.0
            came[s] = (None, (li, c, r))
            it.f = hfield[r, c]
            it.counter = counter; counter += 1
            it.state = s
            it.li = li; it.c = c; it.r = r; it.pdir = -1
            if not heap_push(&heap, it):
                raise MemoryError("out of memory growing the A* search heap")

        while heap.size > 0:
            it = heap_pop(&heap)
            f = it.f; s = it.state
            li = it.li; c = it.c; r = it.r; pdir = it.pdir
            g = gscore[s]
            if f > g + hfield[r, c] + 1e-9:
                continue
            if (li, c, r) in target_set:
                result = _reconstruct(came, s)
                break

            expansions += 1
            if expansions > max_expansions:
                result = None
                break

            for di in range(8):
                nc = c + DX[di]
                nr = r + DY[di]
                if not (0 <= nc < nx and 0 <= nr < ny and free_mask[li, nr, nc]):
                    continue
                diagonal = DX[di] != 0 and DY[di] != 0
                if diagonal:
                    # corner-cut prevention: both orthogonal neighbours free
                    if not (0 <= c + DX[di] < nx and 0 <= r < ny
                            and free_mask[li, r, c + DX[di]]):
                        continue
                    if not (0 <= c < nx and 0 <= r + DY[di] < ny
                            and free_mask[li, r + DY[di], c]):
                        continue
                if diagonal:
                    step = pitch * SQRT2
                else:
                    step = pitch
                cost = step + bend[turn_units(pdir, di)]
                if li != 0:
                    cost += back_layer_penalty
                ng = g + cost
                ns = ((<long long>li * ny + nr) * nx + nc) * _D + (di + 1)
                if ng < <double>gscore.get(ns, INFINITY):
                    gscore[ns] = ng
                    came[ns] = (s, (li, nc, nr))
                    it.f = ng + hfield[nr, nc]
                    it.counter = counter; counter += 1
                    it.state = ns
                    it.li = li; it.c = nc; it.r = nr; it.pdir = di
                    if not heap_push(&heap, it):
                        raise MemoryError("out of memory growing the A* search heap")

            if n_layers > 1 and _can_via(free_mask, sten_dc, sten_dr, n_sten,
                                         n_layers, nx, ny, c, r):
                ng = g + via_cost
                hval = hfield[r, c]
                for k in range(via_off[li], via_off[li + 1]):
                    nli = via_val[k]
                    ns = ((<long long>nli * ny + r) * nx + c) * _D + 0
                    if ng < <double>gscore.get(ns, INFINITY):
                        gscore[ns] = ng
                        came[ns] = (s, (nli, c, r))
                        it.f = ng + hval
                        it.counter = counter; counter += 1
                        it.state = ns
                        it.li = nli; it.c = c; it.r = r; it.pdir = -1
                        if not heap_push(&heap, it):
                            raise MemoryError("out of memory growing the A* search heap")
    finally:
        cfree(heap.data)
        cfree(sten_dc)
        cfree(sten_dr)
        cfree(via_off)
        cfree(via_val)

    return result


cdef bint _can_via(cnp.uint8_t[:, :, ::1] free_mask,
                   int* sten_dc, int* sten_dr, int n_sten,
                   int n_layers, int nx, int ny, int c, int r) noexcept nogil:
    cdef int k, li, cc, rr
    for k in range(n_sten):
        cc = c + sten_dc[k]
        rr = r + sten_dr[k]
        if not (0 <= cc < nx and 0 <= rr < ny):
            return False
        for li in range(n_layers):
            if not free_mask[li, rr, cc]:
                return False
    return True


cdef _reconstruct(dict came, long long st):
    """Rebuild the node path from the came-from map (matches router._reconstruct)."""
    cdef list out = []
    cdef object cur = st
    cdef tuple entry
    while cur is not None:
        entry = came[cur]
        out.append(entry[1])
        cur = entry[0]
    out.reverse()
    cdef list dedup = [out[0]]
    cdef object last = out[0]
    cdef int i
    cdef object node
    for i in range(1, len(out)):
        node = out[i]
        if node != last:
            dedup.append(node)
            last = node
    return dedup
