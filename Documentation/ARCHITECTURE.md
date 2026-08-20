# Architecture

## Module layout

```
coredb/
├── __init__.py            # public API: coredb.open(path) -> Database
├── model.py                # Entity, Relationship, RelationshipVersion, Assertion, Source
├── errors.py                # CoreDBError, ValidationError, StorageError, SchemaVersionError, QueryError
├── engine.py                # Database: mutation + query + storage-management methods
├── series.py                # GraphSeries (lazy interval view), GraphDelta (structured diff), date_range()
├── signal.py                # GraphSignal (metric evaluated across time, joinable against external data), detect_changepoints() (binary segmentation)
├── storage/
│   ├── kvstore.py           # KVStore/Transaction interface - engine.py never talks to LMDB directly
│   ├── lmdb_backend.py       # LMDB implementation of KVStore
│   └── keys.py               # composite key encoding for LMDB's sorted byte-string keys
└── query/
    ├── grammar.lark          # Lark grammar for TGQL (coredb/query language)
    ├── ast_nodes.py           # MatchQuery, HistoryQuery, DiffQuery, RangeQuery, SeriesQuery, AssertStatement, RetractStatement, TrackQuery/ChangepointsQuery (degree/closeness/betweenness/pagerank/...), PathQuery, FirstConnectedQuery, PathHistoryQuery, WhyChangedQuery
    ├── parser.py              # Lark parse tree -> AST
    └── executor.py            # AST -> engine.py calls -> plain dict/list[dict] results

benchmarks/                     # GraphTSBench - not part of the coredb package, see "Performance" below
├── harness.py                 # BenchResult, temp_db(), bench(), print_report()
├── datasets.py                 # synthetic graph/churn-history generators
├── bench_suite.py              # the benchmark definitions
└── run_all.py                  # CLI entrypoint: python -m benchmarks.run_all [--quick]
```

`engine.py` is the only module that talks to `storage/`. `query/` never imports `engine.py` — `Database.execute()` imports `query.parser`/`query.executor` lazily, so the dependency only goes one direction (engine → query at call time, never query → engine at import time). This is why `engine.py` can freely import `GraphDelta`/`GraphSeries` from `series.py` (and `GraphSignal` from `signal.py`) without a circular import: neither module imports `engine.py` — the `db` each one holds is duck-typed.

## Storage engine: why LMDB

CoreDB embeds [LMDB](https://www.lmdb.tech/doc/) rather than hand-rolling a custom on-disk format, and rather than starting on a client-server database like PostgreSQL:

- **Sorted range iteration is native.** LMDB is a memory-mapped B+tree with byte-string keys compared lexicographically. Every temporal index CoreDB needs (scan all versions for a subject+predicate prefix, scan all versions opened/closed in a date range) is a direct range scan — no secondary sort step.
- **It's already a proven, embedded engine.** The goal of "don't build a custom storage engine first" is satisfied by using an existing one, not by switching to a client-server database — CoreDB stays a linkable library with no network dependency, matching the "embedded library" decision for this stage.
- **Facts are append-heavy, not update-heavy.** LMDB's B+tree (vs. an LSM tree like RocksDB, tuned for high write throughput with background compaction) fits a workload where relationships are asserted once, confirmed occasionally, and closed once — not rewritten constantly.
- **Storage is behind an interface.** `coredb/storage/kvstore.py` defines `KVStore`/`Transaction` as an ABC; `engine.py` only calls that interface. Swapping in a different embedded engine later (or a client-server one, if CoreDB ever needs multi-process/multi-user access) means writing a new `storage/*_backend.py`, not touching the graph/temporal logic in `engine.py`.

## LMDB tables and what they power

One LMDB environment, one sub-database (dbi) per logical table:

| Table | Key → Value | Powers |
|---|---|---|
| `relationships` | `relationship_id` → `Relationship` JSON | Looking up a relationship's stable metadata |
| `relationship_lookup` | `subject\0predicate\0object` → `relationship_id` | Finding/creating the stable id for a triple (permanent once created) |
| `versions` | `version_id` → `RelationshipVersion` JSON | The primary interval store |
| `spo_idx` | `subject\0predicate\0valid_from\0version_id` → `version_id` | `MATCH`/`HISTORY` with a bound subject: `(NVIDIA, ?p, ?o)` |
| `ops_idx` | `object\0predicate\0subject\0valid_from\0version_id` → `version_id` | Reverse pattern lookups: `(?s, p, TSMC)` |
| `open_idx` | `relationship_id` → `version_id` | Fast "is this triple currently open" check, used by `assert_fact`/`retract_fact`/`sync_snapshot` |
| `open_by_sp_idx` | `subject\0predicate\0relationship_id` → `version_id` | `sync_snapshot`'s "what's currently open for this pair" scan — O(number open), not O(all history ever seen) |
| `opened_time_idx` | `valid_from\0version_id` → `version_id` | Global (pattern-less) `DIFF`'s "opened" scan |
| `closed_time_idx` | `valid_to\0version_id` → `version_id` | Global `DIFF`'s "closed" scan |
| `assertions` | `assertion_id` → `Assertion` JSON | Evidence records backing a version |
| `assertions_by_version` | `version_id\0assertion_id` → `1` | Listing/counting the assertions behind one version |
| `entities` | `entity_id` → `Entity` JSON | `first_seen`/`last_seen` tracking |
| `sources` | `url` → `Source` JSON | Deduplicating sources by URL |
| `counters` | counter name → `int` | Monotonic id generation (LMDB has no autoincrement) |

## Concurrency

LMDB serializes write transactions within one process: a second `env.begin(write=True)` blocks until the first commits (this is LMDB's own writer-mutex, not something CoreDB implements). Every mutation method (`assert_fact`, `retract_fact`, `sync_snapshot`) does its read-modify-write inside exactly one transaction, so multiple threads calling these concurrently in the same process serialize safely without any extra locking in CoreDB.

What's **not** covered: multiple *processes* writing to the same database (LMDB supports it at the storage level, but CoreDB hasn't been tested under it), and crash-mid-write recovery (LMDB's own durability guarantees apply, but CoreDB has no test coverage exercising an abrupt kill mid-transaction).

## Traversal

`PATH`/`FIRST_CONNECTED`/`PATH_HISTORY` (TGQL v0.4) are answered by breadth-first search, not a new index: `_neighbor_versions(t, entity_id, on_date)` fetches every relationship touching one entity as of a date by combining `_scan_candidates` on both `spo_idx` (entity as subject) and `ops_idx` (entity as object), deduplicated by `relationship_id` — the same neighbor-fetch `degree()` (TGQL v0.3) already needed, now shared between both. `path_exists` runs standard level-by-level BFS from that primitive, bounded by `max_depth` (validated into `[1, 10]` — unbounded depth risks exponential frontier blowup with no cost limiting). `first_connected` reuses `opened_time_idx` (no new index) to get every candidate date some relationship opened, then scans chronologically calling `path_exists` — connectivity isn't monotonic (edges close too), so this can't be a binary search.

Cost scales with branching factor (average degree) to the power of `max_depth`, with no traversal-specific index (like a precomputed reachability structure) yet — fine for the graphs this has been exercised on, a likely place to revisit if traversal becomes a bottleneck on a high-degree graph.

**Centrality** (TGQL v0.5) builds three more primitives on top: `_neighbor_node_ids` (like `_neighbor_versions` but deduplicated by the *other node* rather than by `relationship_id`, so two entities connected by more than one relationship count as one graph edge — the correctness fix `betweenness_all` needs, since Brandes' shortest-path counting would otherwise double-count a multi-edge pair); `_bfs_distances` (a full bounded-BFS distance map, generalizing `path_exists`'s single-target search); and `_active_entity_ids` (every entity with a relationship active on a date — the node set for the two genuinely global algorithms below, requiring a full `versions` scan since no "distinct entities as of a date" index exists, the same limitation `diff()`'s global branch already has).

- **`closeness`** — one bounded BFS (`_bfs_distances`) per call; as cheap as `degree`/`path_exists`.
- **`betweenness_all`/`pagerank_all`** — process every entity in `_active_entity_ids` in one pass (Brandes' algorithm; PageRank power iteration), regardless of which entity the caller actually wants. These are the most expensive operations in the engine so far, with no caching or incrementality — `TRACK BETWEENNESS`/`TRACK PAGERANK` across many resolution steps re-runs the full graph computation at every single step. `betweenness`/`pagerank` (singular) are convenience wrappers around these that extract one entity's score, each paying that same full cost per call — prefer the `_all` form directly when scoring more than one entity.

## Provenance

`WHY_CHANGED` (TGQL v0.6) walks the `Assertion`/`Source` chain the data model has captured since M2 but had no query surface for until now. `_load_source(t, source_id)` is a full scan of the `sources` table (keyed by `url` for `_find_or_create_source`'s dedup, not by `source_id`) — a one-off provenance lookup, not a hot path, the same tradeoff as `_active_entity_ids`/`diff()`'s global branch. `why_changed()`'s evidence-window filter uses each `Assertion`'s `event_time` (the valid-time date its claim pertains to — always equal to the `valid_from`/`as_of_date` passed to `assert_fact`/`sync_snapshot` at creation), not `ingested_at` (system time, when the assertion was recorded) — this keeps the evidence trail aligned with the `status` field, which is a valid-time classification via `diff()`.

## Change-point detection

`CHANGEPOINTS` (TGQL v0.7) is `TRACK` plus `coredb/signal.py`'s `detect_changepoints()`: binary segmentation with a residual-sum-of-squares cost function, recursively splitting a signal's points wherever the split reduces total cost by more than a penalty (a standard BIC-style default, `variance(values) * log(n)`, unless the caller overrides it). Cost is `O(n)` per candidate split point and there are `O(n)` candidates per segment, so one segmentation pass is `O(n^2)` in the worst case over `n` = the signal's point count — fine for the resolution-bounded series `TRACK`/`CHANGEPOINTS` actually produce (dozens to low hundreds of points, not the underlying graph's full history), but not something to run over an unbounded/very-fine-resolution series without expecting it to scale accordingly. No new engine-level state: `changepoints()` is exactly `track()` followed by `GraphSignal.changepoints()`, so it inherits `TRACK`'s own costs (including `BETWEENNESS`/`PAGERANK`'s per-step full-graph recomputation, above) on top of the segmentation itself.

## Performance

`benchmarks/` (`GraphTSBench`, `python -m benchmarks.run_all [--quick]`) is the suite that gates optimization work — a change is only kept if the numbers actually improve, not on intuition. It prompted (and then measured) one real fix so far:

- **`TRACK DEGREE`/`WEIGHTED_DEGREE`/`EDGE_WEIGHT` and `SERIES` iteration are O(H + D)**, not O(D × H) (H = a pattern's/entity's total history depth, D = the number of resolution steps). The old implementation called `degree()`/`edge_weight()`/`as_of()` fresh at every step, each an O(H) scan. `engine.py`'s `_degree_track_points`/`_edge_weight_track_points`/`series_snapshots` instead fetch the full history once, turn each interval into an open/close event at its `valid_from`/day-after-`valid_to`, sort, and sweep a running total (degree family) or active-version set (`SERIES`) forward across the requested dates in one pass. Measured on the benchmark's default sizes (H=150 relationships, D=365 daily steps): `TRACK DEGREE` 6.09s → 28ms, `TRACK EDGE_WEIGHT` 6.03s → 6.5ms, `SERIES` iteration 6.33s → 9.6ms. The old single-date methods (`degree()`, `edge_weight()`, `as_of()`) are unchanged and serve as the correctness oracle for `tests/test_track_series_sweep.py`.
  - **Same-day boundary case**: a `retract_fact(valid_to=d)` immediately followed by `assert_fact(valid_from=d)` on the same triple produces two versions whose valid-time intervals both cover `d` (`valid_to` is inclusive), even though they were never simultaneously open in system time. The old per-date dedup resolved this via list/dict iteration order — not a meaningful guarantee. The sweep resolves it deterministically (most-recently-opened version wins) — a documented, deliberate choice, not a silent behavior change.
- **`TRACK CLOSENESS`/`BETWEENNESS`/`PAGERANK` are unchanged** — still one full per-date computation per resolution step. Global graph algorithms have no simple event-sweep equivalent the way an additive metric like degree does; a real fix would need incremental-graph-algorithm work, deliberately out of scope for this pass (`benchmarks/`'s `track_betweenness` benchmark exists specifically to keep this cost visible, not to hide it).
- **`CHANGEPOINTS`' own cost can now dominate at large point counts.** `changepoints()` is `track()` + binary segmentation (`coredb/signal.py`); once `track()`'s cost dropped, the segmentation's own documented `O(n^2)` cost (n = point count) became the larger term at D=365 (`benchmarks/run_all.py`: `changepoints.degree` ≈2.35s of that run's ≈2.38s total is segmentation, not `track()`). Not addressed here — a real fix (e.g. a faster segmentation algorithm) is future work, not something the interval-sweep rewrite could touch.

## Storage management

- **Schema versioning.** `Database` writes a `schema_version` marker into the `counters` table on first open. If an existing database's marker doesn't match the running code's expected version, `Database()` raises `SchemaVersionError` immediately rather than silently operating on a mismatched on-disk shape. Recovery path: `Database.dump()` with the old code version, `coredb.restore()` with the new one.
- **`Database.stats()`** — cheap entry counts per table via LMDB's native `stat()` (no manual iteration).
- **`Database.backup(path)`** — a compacted, self-contained copy via `env.copy(path, compact=True)`, safe to call on a live database. `path` is a directory: LMDB uses subdir mode by default, so a CoreDB database is a directory containing `data.mdb`/`lock.mdb`, not a single file.
- **`Database.dump(path)` / `coredb.restore(dump_path, db_path)`** — schema-independent JSON-lines export/import of the logical facts (not internal ids or system-time), replayed through `assert_fact`/`retract_fact`. This is the actual migration path across a schema change — a `backup()`'s raw LMDB bytes are still in the old schema and won't help after one.
- **`coredb.open(path, map_size=None)`** — `map_size` overrides LMDB's default 1 GiB virtual address space. Writes that would exceed it raise `StorageError` (wrapping LMDB's `MapFullError`) naming the fix; there's no automatic grow-and-retry, since that would require safely replaying an arbitrary caller-supplied transaction body, which isn't generalizable.

## Known limitations at this stage

- **Patterns with neither subject nor object bound** (only a predicate, or nothing at all) fall back to a full scan of the `versions` table. Fine at the current scale; would need a predicate-only index for large graphs.
- **Global (pattern-less) `DIFF`'s "persisted" set** is a full table scan — there's no interval index yet for "spans both dates" the way `opened_time_idx`/`closed_time_idx` cover the open/close cases.
- **`MATCH`/`HISTORY` patterns are still single-hop only.** `(subject, predicate, object)` triples with no chain syntax — `PATH`/`FIRST_CONNECTED`/`PATH_HISTORY` (above) added point-to-point traversal between two named entities, not general multi-hop *pattern matching*. See `ROADMAP.md`.
- **No traversal-specific index.** BFS cost scales with branching factor × `max_depth`; see "Traversal" above.
- **No caching for `betweenness_all`/`pagerank_all`.** Both are full-graph computations with no incrementality; see "Traversal" above.
- **No automatic map-size growth or multi-process write coordination.** See "Concurrency" and "Storage management" above.
