# GraphTSBench

A pure-Python benchmark suite over CoreDB's engine — ingest, `AS_OF`/`HISTORY`/`DIFF`, `TRACK` (degree/weighted_degree/edge_weight/betweenness), `SERIES`, `PATH`, `CHANGEPOINTS`, `WHY_CHANGED`, and `dump`/`restore`.

This exists to **gate optimization work**, not to produce a marketing number: before changing anything for performance reasons (algorithmic or, eventually, native), run this suite, make the change, run it again, and only keep the change if the numbers actually improved. No optimization should be justified by intuition alone when this suite can just be asked.

## Running

```bash
./.venv/Scripts/python -m benchmarks.run_all            # full sizes
./.venv/Scripts/python -m benchmarks.run_all --quick     # small sizes, fast dev-loop run
```

Each row reports wall-clock time and a throughput figure (`n` divided by seconds), where `n` is whatever unit that benchmark's docstring/note describes (facts ingested, resolution steps, etc.) — not directly comparable across rows.

## What `track_degree`/`track_weighted_degree`/`track_edge_weight`/`series_snapshot` measure

These four are the ones the interval-sweep rewrite (`coredb/engine.py`'s `_degree_track_points`/`_edge_weight_track_points`, `Database.series_snapshots`) targets: an entity or pattern with real churn history (`H` intervals) is queried across many resolution steps (`D` dates). Before the rewrite this cost `O(D × H)` (a fresh `as_of()`/`degree()` reconstruction per step); after, `O(H + D)` (one pass to build interval events, one sweep across the requested dates).

`track_betweenness` is included deliberately as a **negative** case — betweenness/closeness/PageRank are global per-date graph computations with no simple event-sweep, so `TRACK BETWEENNESS`/`TRACK PAGERANK` still cost `O(D × full-graph-computation)`. See `Documentation/ARCHITECTURE.md`'s Performance section.

## `bench_at_scale.py` — the deep, opt-in benchmark

```bash
./.venv/Scripts/python -m benchmarks.bench_at_scale
```

Not part of `run_all.py`'s default suite (too slow for a normal dev-loop run). Tests one specific question at a larger, more realistic scale (50,000 facts, long entity names, a small reused predicate vocabulary): does string-key size/comparison cost in `spo_idx`/`ops_idx`/`relationship_lookup` matter enough to justify integer-interned entity/predicate ids? As of M10 Part 5, the answer was no — see `Documentation/ARCHITECTURE.md`'s Performance section. Re-run this if that assumption needs revisiting (much larger graphs, a working set that exceeds RAM).

## Adding a benchmark

Add a `bench_*(quick: bool) -> BenchResult` function to `bench_suite.py` (using `harness.temp_db()`/`harness.bench()`) and append it to `BENCHMARKS`.
