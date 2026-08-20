"""Equivalence tests for coredb._native's brandes_betweenness/pagerank -
literal C++ ports of coredb/graph_algorithms.py's pure-Python
implementations (which are themselves what engine.py's betweenness_all()/
pagerank_all() used before the native port). Skips gracefully if no C++
toolchain built the extension.
"""
import random

import pytest

pytest.importorskip("coredb._native")

from coredb.graph_algorithms import betweenness_from_adjacency, pagerank_from_adjacency

# subject/predicate/object never appear here - these operate purely on
# local integer node indices, no graph domain content at all.
GRAPHS = {
    "path": [[1], [0, 2], [1]],
    "triangle": [[1, 2], [0, 2], [0, 1]],
    "star": [[1, 2, 3, 4], [0], [0], [0], [0]],
    "disconnected": [[1], [0], [3], [2], []],
    "self_referential_free": [[1, 2], [0], [0, 3], [2]],
}


def _random_graph(n, p, seed):
    rng = random.Random(seed)
    adjacency = [[] for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i != j and rng.random() < p:
                adjacency[i].append(j)
    return adjacency


@pytest.mark.parametrize("name", GRAPHS.keys())
def test_betweenness_native_matches_python(name):
    adjacency = GRAPHS[name]
    native = betweenness_from_adjacency(adjacency, max_depth=4, use_native=True)
    python = betweenness_from_adjacency(adjacency, max_depth=4, use_native=False)
    assert native == pytest.approx(python)


@pytest.mark.parametrize("name", GRAPHS.keys())
def test_pagerank_native_matches_python(name):
    adjacency = GRAPHS[name]
    native = pagerank_from_adjacency(adjacency, damping=0.85, max_iterations=100, tol=1e-6, use_native=True)
    python = pagerank_from_adjacency(adjacency, damping=0.85, max_iterations=100, tol=1e-6, use_native=False)
    assert native == pytest.approx(python)


def test_betweenness_native_matches_python_on_random_graphs():
    for seed in range(10):
        adjacency = _random_graph(n=15, p=0.25, seed=seed)
        native = betweenness_from_adjacency(adjacency, max_depth=3, use_native=True)
        python = betweenness_from_adjacency(adjacency, max_depth=3, use_native=False)
        assert native == pytest.approx(python), f"seed={seed}"


def test_pagerank_native_matches_python_on_random_graphs():
    for seed in range(10):
        adjacency = _random_graph(n=15, p=0.25, seed=seed)
        native = pagerank_from_adjacency(adjacency, damping=0.85, max_iterations=100, tol=1e-6, use_native=True)
        python = pagerank_from_adjacency(adjacency, damping=0.85, max_iterations=100, tol=1e-6, use_native=False)
        assert native == pytest.approx(python), f"seed={seed}"


def test_empty_graph():
    assert betweenness_from_adjacency([], max_depth=4, use_native=True) == []
    assert pagerank_from_adjacency([], damping=0.85, max_iterations=100, tol=1e-6, use_native=True) == []


def test_use_native_true_without_extension_raises_clearly(monkeypatch):
    import coredb.graph_algorithms as ga
    monkeypatch.setattr(ga, "_native", None)
    with pytest.raises(RuntimeError, match="coredb._native is not built"):
        betweenness_from_adjacency(GRAPHS["triangle"], max_depth=4, use_native=True)
    with pytest.raises(RuntimeError, match="coredb._native is not built"):
        pagerank_from_adjacency(GRAPHS["triangle"], damping=0.85, max_iterations=100, tol=1e-6, use_native=True)


def test_engine_betweenness_pagerank_end_to_end_use_native_by_default(db):
    db.assert_fact("A", "LINK", "B", "2026-01-01")
    db.assert_fact("B", "LINK", "C", "2026-01-01")
    import coredb.graph_algorithms as ga

    assert ga._native is not None, "expected the native extension to be built in this environment"
    scores = db.betweenness_all("2026-01-01")
    assert scores["B"] == 1.0
    assert scores["A"] == 0.0 and scores["C"] == 0.0

    ranks = db.pagerank_all("2026-01-01")
    assert set(ranks) == {"A", "B", "C"}
    assert abs(sum(ranks.values()) - 1.0) < 1e-6
