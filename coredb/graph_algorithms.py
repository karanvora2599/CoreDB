"""Pure graph algorithms operating on plain adjacency-list data (local
integer node indices - no LMDB/engine coupling). This is the computational
core behind engine.py's betweenness_all()/pagerank_all(): engine.py builds
the adjacency (it's the only place with a transaction), hands it here, and
this module dispatches to coredb._native when available, falling back to
the pure-Python implementation otherwise - the same optional-acceleration
pattern as coredb/signal.py's detect_changepoints(). Split out (rather than
living in engine.py) so the pure-Python fallback is directly testable
without a database, and serves as the correctness oracle for the native
path.
"""
from __future__ import annotations

from collections import deque

try:
    from . import _native
except ImportError:
    _native = None


def _brandes_betweenness_python(adjacency: list[list[int]], max_depth: int) -> list[float]:
    """Pure-Python Brandes' algorithm (unweighted, undirected, BFS bounded
    by max_depth) - the same algorithm engine.py's betweenness_all() used
    before the native port, kept as the fallback and correctness oracle."""
    n = len(adjacency)
    betweenness = [0.0] * n
    for s in range(n):
        stack: list[int] = []
        predecessors: list[list[int]] = [[] for _ in range(n)]
        sigma = [0] * n
        sigma[s] = 1
        dist = [-1] * n
        dist[s] = 0
        queue = deque([s])
        while queue:
            v = queue.popleft()
            stack.append(v)
            if dist[v] >= max_depth:
                continue
            for w in adjacency[v]:
                if dist[w] < 0:
                    dist[w] = dist[v] + 1
                    queue.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    predecessors[w].append(v)
        delta = [0.0] * n
        while stack:
            w = stack.pop()
            for v in predecessors[w]:
                delta[v] += (sigma[v] / sigma[w]) * (1 + delta[w])
            if w != s:
                betweenness[w] += delta[w]
    return betweenness


def betweenness_from_adjacency(adjacency: list[list[int]], max_depth: int,
                                use_native: bool | None = None) -> list[float]:
    """Raw per-node-index betweenness scores - not yet halved for the
    undirected double-count, that's the caller's job (matches
    engine.py's existing betweenness_all() contract). `use_native` forces
    one path or the other, for tests/benchmarks; leave None to
    auto-detect."""
    run_native = _native is not None if use_native is None else use_native
    if run_native:
        if _native is None:
            raise RuntimeError("use_native=True requested but coredb._native is not built")
        return list(_native.brandes_betweenness(adjacency, max_depth))
    return _brandes_betweenness_python(adjacency, max_depth)


def _pagerank_python(out_edges: list[list[int]], damping: float, max_iterations: int, tol: float) -> list[float]:
    """Pure-Python power-iteration PageRank - the same algorithm
    engine.py's pagerank_all() used before the native port. Dangling nodes
    redistribute their rank one node at a time in index order (not a
    "sum once" shortcut), matching the native port's operation sequence
    exactly."""
    n = len(out_edges)
    if n == 0:
        return []
    rank = [1.0 / n] * n
    for _ in range(max_iterations):
        new_rank = [(1.0 - damping) / n] * n
        for v in range(n):
            out = out_edges[v]
            if not out:
                share = damping * rank[v] / n
                for u in range(n):
                    new_rank[u] += share
            else:
                share = damping * rank[v] / len(out)
                for u in out:
                    new_rank[u] += share
        diff = sum(abs(new_rank[v] - rank[v]) for v in range(n))
        rank = new_rank
        if diff < tol:
            break
    return rank


def pagerank_from_adjacency(out_edges: list[list[int]], damping: float, max_iterations: int,
                             tol: float, use_native: bool | None = None) -> list[float]:
    """Raw per-node-index PageRank scores. `use_native` forces one path or
    the other, for tests/benchmarks; leave None to auto-detect."""
    run_native = _native is not None if use_native is None else use_native
    if run_native:
        if _native is None:
            raise RuntimeError("use_native=True requested but coredb._native is not built")
        return list(_native.pagerank(out_edges, damping, max_iterations, tol))
    return _pagerank_python(out_edges, damping, max_iterations, tol)
