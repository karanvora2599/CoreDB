"""GraphTSBench: the benchmark suite that gates future optimization work.

Each bench_* function seeds a fresh temp_db, times one operation, and
returns a BenchResult. `quick=True` uses small sizes for a fast dev-loop
run; `quick=False` (the default via run_all.py) uses larger sizes that
actually show scaling behavior - in particular track_degree/series_snapshot,
which are what the interval-sweep rewrite targets.
"""
from __future__ import annotations

import coredb.graph_algorithms as ga_mod
import coredb.signal as signal_mod

from .datasets import seed_churn_history, seed_entity_churn, seed_random_graph
from .harness import bench, temp_db


def bench_ingest(quick: bool):
    n = 200 if quick else 2000
    with temp_db() as db:
        def run():
            for i in range(n):
                db.assert_fact(f"S{i}", "LINK", f"O{i}", "2020-01-01")
        return bench("ingest.assert_fact", n, run)


def bench_ingest_batched(quick: bool):
    """Same workload as bench_ingest, isolating write_batch()'s effect -
    one LMDB transaction for the whole batch instead of one per assert_fact
    call. See ARCHITECTURE.md's Performance section (M10 remaining parts)."""
    n = 200 if quick else 2000
    with temp_db() as db:
        def run():
            with db.write_batch():
                for i in range(n):
                    db.assert_fact(f"S{i}", "LINK", f"O{i}", "2020-01-01")
        return bench("ingest.assert_fact (batched)", n, run)


def bench_as_of(quick: bool):
    n_entities, n_edges = (50, 100) if quick else (500, 2000)
    with temp_db() as db:
        seed_random_graph(db, n_entities, n_edges)
        return bench("read.as_of (pattern)", n_edges,
                      lambda: db.as_of(("Node0", "LINK", None), "2020-01-01"))


def bench_history(quick: bool):
    n_cycles = 20 if quick else 200
    with temp_db() as db:
        seed_churn_history(db, "Hub", "PEER_OF", "Leaf", n_cycles)
        return bench("read.history", n_cycles,
                      lambda: db.history(("Hub", "PEER_OF", "Leaf")))


def bench_diff_global(quick: bool):
    n_entities, n_edges = (50, 100) if quick else (500, 2000)
    with temp_db() as db:
        seed_random_graph(db, n_entities, n_edges)
        return bench("read.diff (global)", n_edges,
                      lambda: db.diff("2019-01-01", "2020-06-01"))


def bench_track_degree(quick: bool):
    """The benchmark the interval-sweep rewrite exists for: TRACK DEGREE
    over an entity with real churn history (H), across many resolution
    steps (D). Old cost: O(D x H). New cost: O(H + D)."""
    n_cycles, n_steps = (30, 60) if quick else (150, 365)
    with temp_db() as db:
        end = seed_entity_churn(db, "Hub", n_cycles)
        return bench("track.degree", n_steps,
                      lambda: db.track("degree", "Hub", "2020-01-01", end, resolution_days=1),
                      note=f"H={n_cycles} relationships, D~{n_steps} steps")


def bench_track_weighted_degree(quick: bool):
    n_cycles, n_steps = (30, 60) if quick else (150, 365)
    with temp_db() as db:
        end = seed_entity_churn(db, "Hub", n_cycles, weighted=True)
        return bench("track.weighted_degree", n_steps,
                      lambda: db.track("weighted_degree", "Hub", "2020-01-01", end, resolution_days=1),
                      note=f"H={n_cycles} relationships, D~{n_steps} steps")


def bench_track_edge_weight(quick: bool):
    n_cycles, n_steps = (30, 60) if quick else (150, 365)
    with temp_db() as db:
        end = seed_churn_history(db, "NVIDIA", "SUPPLIED_BY", "TSMC", n_cycles)
        target = ("NVIDIA", "SUPPLIED_BY", "TSMC")
        return bench("track.edge_weight", n_steps,
                      lambda: db.track("edge_weight", target, "2020-01-01", end, resolution_days=1),
                      note=f"H={n_cycles} intervals, D~{n_steps} steps")


def bench_track_betweenness(quick: bool):
    """Deliberately NOT optimized in this milestone - see
    Documentation/ARCHITECTURE.md's Performance section. Included so the
    suite documents (and can later prove an improvement to) this cost too."""
    n_entities, n_edges, n_steps = (30, 60, 10) if quick else (100, 300, 30)
    with temp_db() as db:
        seed_random_graph(db, n_entities, n_edges)
        return bench("track.betweenness (global, unoptimized)", n_steps,
                      lambda: db.track("betweenness", "Node0", "2020-01-01",
                                        f"2020-{1 + n_steps // 28:02d}-01", resolution_days=7, max_depth=3),
                      note=f"{n_entities} nodes, D~{n_steps} steps")


def _synthetic_adjacency(n: int, avg_degree: int, seed: int = 7):
    import random

    rng = random.Random(seed)
    adjacency = [[] for _ in range(n)]
    for i in range(n):
        targets = rng.sample(range(n), min(avg_degree, n - 1))
        adjacency[i] = [t for t in targets if t != i]
    return adjacency


def bench_betweenness_python(quick: bool):
    """Isolates JUST the Brandes computation (not the LMDB adjacency
    build) - the M10 Part 2 native target. See bench_betweenness_native."""
    n = 40 if quick else 150
    adjacency = _synthetic_adjacency(n, avg_degree=6)
    return bench("centrality.betweenness.python", n,
                  lambda: ga_mod.betweenness_from_adjacency(adjacency, max_depth=4, use_native=False))


def bench_betweenness_native(quick: bool):
    n = 40 if quick else 150
    adjacency = _synthetic_adjacency(n, avg_degree=6)
    if ga_mod._native is None:
        return bench("centrality.betweenness.native (not built)", n, lambda: None)
    return bench("centrality.betweenness.native", n,
                  lambda: ga_mod.betweenness_from_adjacency(adjacency, max_depth=4, use_native=True))


def bench_pagerank_python(quick: bool):
    n = 40 if quick else 150
    adjacency = _synthetic_adjacency(n, avg_degree=6)
    return bench("centrality.pagerank.python", n,
                  lambda: ga_mod.pagerank_from_adjacency(adjacency, 0.85, 100, 1e-6, use_native=False))


def bench_pagerank_native(quick: bool):
    n = 40 if quick else 150
    adjacency = _synthetic_adjacency(n, avg_degree=6)
    if ga_mod._native is None:
        return bench("centrality.pagerank.native (not built)", n, lambda: None)
    return bench("centrality.pagerank.native", n,
                  lambda: ga_mod.pagerank_from_adjacency(adjacency, 0.85, 100, 1e-6, use_native=True))


def bench_series_snapshot(quick: bool):
    n_cycles, n_steps = (30, 60) if quick else (150, 365)
    with temp_db() as db:
        end = seed_entity_churn(db, "Hub", n_cycles)
        series = db.series(("Hub", "TOUCHES", None), "2020-01-01", end, resolution_days=1)
        return bench("series.iterate", n_steps, lambda: list(series),
                     note=f"H={n_cycles} relationships, D~{n_steps} steps")


def bench_path_exists(quick: bool):
    n_entities, n_edges = (30, 60) if quick else (100, 400)
    with temp_db() as db:
        seed_random_graph(db, n_entities, n_edges)
        return bench("path.path_exists", n_edges,
                      lambda: db.path_exists("Node0", f"Node{n_entities - 1}", "2020-01-01", max_depth=6))


def bench_changepoints(quick: bool):
    n_cycles, n_steps = (30, 60) if quick else (150, 365)
    with temp_db() as db:
        end = seed_entity_churn(db, "Hub", n_cycles)
        return bench("changepoints.degree", n_steps,
                      lambda: db.changepoints("degree", "Hub", "2020-01-01", end, resolution_days=1),
                      note=f"H={n_cycles} relationships, D~{n_steps} steps")


def _synthetic_signal_points(n: int):
    """A signal with real regime shifts every ~n/6 points, not noise -
    exercises the segmentation search's actual split-finding work rather
    than short-circuiting on an all-flat or trivial signal."""
    points = []
    for i in range(n):
        regime = (i * 6) // n
        value = float(regime * 7 + (i % 3))
        points.append((f"2020-{1 + i // 28:02d}-{1 + i % 28:02d}", value))
    return points


def bench_segmentation_python(quick: bool):
    """Isolates JUST the binary-segmentation search cost (not track()'s,
    already fixed in M9) - the native pilot's actual target. See
    bench_segmentation_native for the same signal through coredb._native."""
    n = 120 if quick else 365
    points = _synthetic_signal_points(n)
    return bench("segmentation.python", n,
                  lambda: signal_mod.detect_changepoints(points, use_native=False))


def bench_segmentation_native(quick: bool):
    n = 120 if quick else 365
    points = _synthetic_signal_points(n)
    if signal_mod._native is None:
        return bench("segmentation.native (not built)", n, lambda: None)
    return bench("segmentation.native", n,
                  lambda: signal_mod.detect_changepoints(points, use_native=True))


def bench_why_changed(quick: bool):
    n_cycles = 20 if quick else 100
    with temp_db() as db:
        end = seed_churn_history(db, "Hub", "PEER_OF", "Leaf", n_cycles)
        return bench("provenance.why_changed", n_cycles,
                      lambda: db.why_changed("Hub", "PEER_OF", "Leaf", "2020-01-01", end))


def bench_dump_restore(quick: bool):
    import os
    import shutil
    import tempfile

    import coredb

    n_edges = 200 if quick else 2000
    work_dir = tempfile.mkdtemp(prefix="coredb_bench_restore_")
    try:
        dump_path = os.path.join(work_dir, "dump.jsonl")
        restore_dir = os.path.join(work_dir, "restored")
        with temp_db() as db:
            seed_random_graph(db, n_edges // 2, n_edges)
            db.dump(dump_path)
        return bench("storage.restore", n_edges, lambda: coredb.restore(dump_path, restore_dir).close())
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


BENCHMARKS = [
    bench_ingest,
    bench_ingest_batched,
    bench_as_of,
    bench_history,
    bench_diff_global,
    bench_track_degree,
    bench_track_weighted_degree,
    bench_track_edge_weight,
    bench_track_betweenness,
    bench_betweenness_python,
    bench_betweenness_native,
    bench_pagerank_python,
    bench_pagerank_native,
    bench_series_snapshot,
    bench_path_exists,
    bench_changepoints,
    bench_segmentation_python,
    bench_segmentation_native,
    bench_why_changed,
    bench_dump_restore,
]


def run_suite(quick: bool = False) -> list:
    return [fn(quick) for fn in BENCHMARKS]
