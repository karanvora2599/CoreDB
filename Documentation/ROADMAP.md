# Roadmap

CoreDB's guiding reframe: a normal graph database answers questions over `G`. A temporal graph database answers questions over `G(t)`. CoreDB is working toward answering questions over the evolution operator `𝒢[t0, t1]` itself — graph trajectory as a first-class, queryable thing, not just a timestamp filter on individual edges.

That's a large research-and-engineering direction. Each milestone below deliberately scopes a slice of it rather than building all of it in one pass.

## Milestone 1 — done

- Embedded engine on LMDB, no server/network dependency.
- Flat interval-valued facts: `(subject, predicate, object, valid_from, valid_to, observed_at, superseded_at, confidence)`, generalizing `Knowledge_Graph/backend/db.py`'s hardcoded NVIDIA-hub pattern to arbitrary triples.
- DSL: `MATCH ... AS OF`, `HISTORY`, `DIFF`, `RANGE`.

## Milestone 2 — done

- Data model split into `Entity` / `Relationship` (stable triple identity) / `RelationshipVersion` (one interval, bitemporal) / `Assertion` (one piece of evidence). `observed_at`/`superseded_at` renamed to `system_from`/`system_to` for symmetry with `valid_from`/`valid_to`.
- `GraphSeries` — a lazy view over a pattern's history across an interval; snapshots are resolved on demand, never precomputed.
- `GraphDelta` — `DIFF`'s result reshaped from a flat list of rows-with-status into a structured datatype (`nodes_added`/`nodes_removed`/`edges_opened`/`edges_closed`/`edges_persisted`), with node sets netted against churn.
- DSL: new `SERIES ... BETWEEN ... AND ... [RESOLUTION '<N>d']` statement.

## Milestone 3 — done

Hardening pass: TGQL grew a version number and a spec (`TGQL_SPEC.md`), reliability gaps were closed, and basic storage-management tooling was added.

- **TGQL v0.2**: `ASSERT`/`RETRACT` write statements (DSL is no longer read-only), `WHERE confidence <op> <value>` and `LIMIT <n>` on `MATCH`/`HISTORY`, line comments (`//`).
- **Reliability**: `coredb/errors.py` (`ValidationError`/`StorageError`/`SchemaVersionError`), input validation on every mutation method (rejects NUL bytes in identifiers, malformed dates, inverted intervals), a real fix for a latent bug where `sync_snapshot`'s confirm branch could move `last_confirmed` backwards on out-of-order dates, and `lmdb.MapFullError` wrapped into a clear `StorageError` instead of a raw LMDB exception.
- **Storage management**: schema-version marker with a loud `SchemaVersionError` on mismatch, `Database.stats()`, `Database.backup()` (compacted LMDB copy), `Database.dump()`/`coredb.restore()` (schema-independent JSON-lines migration path), `coredb.open(path, map_size=...)`.

Between M3 and M4, a codebase audit found and fixed three real bugs (not a milestone of its own, committed separately): `sync_snapshot`'s hot path rescanned a subject/predicate's entire history every call instead of just the currently-open subset (fixed with a new `open_by_sp_idx`); `range_agg` aggregated by `object_id` unconditionally, which was meaningless for a reverse pattern (fixed to aggregate by whichever side is the wildcard); and malformed TGQL raised raw `lark`/`ValueError` exceptions instead of a `coredb.errors` type (fixed with `QueryError`).

## Milestone 4 — done

- **`GraphSignal`** (`coredb/signal.py`) — a graph metric evaluated across time, turned into a plain `{(date, value)}` series. `GraphSignal.join(other: dict)` inner-joins against caller-supplied external data (e.g. price/volatility) by date.
- **`Database.degree()`/`weighted_degree` (via `weighted=True`)/`edge_weight()`** — the metrics honestly computable on the current single-hop model: how many relationships touch an entity (optionally confidence-weighted), and one specific relationship's confidence at a date. **Not** attempted: centrality-family metrics (betweenness, closeness, PageRank), since those need multi-hop traversal, which doesn't exist yet.
- **`Database.track(metric, target, start, end, resolution_days=1)`** — steps a metric across an interval into a `GraphSignal`.
- **TGQL v0.3**: `TRACK <METRIC>(<args>) BETWEEN ... AND ... [RESOLUTION '<N>d']`, metric name is a plain identifier (not a grammar keyword) so future metrics only need a registry entry.

## Milestone 5 — done

Multi-hop traversal, scoped to point-to-point path queries between two named entities (not general multi-hop pattern matching inside `MATCH`/`HISTORY`, which stays deferred).

- **`_neighbor_versions`** extracted from M4's `degree()` into a shared helper — every relationship touching one entity as of a date, deduplicated by `relationship_id`.
- **`Database.path_exists(subject, object_id, on_date, max_depth=4)`** — bounded breadth-first search, returns the shortest path or `None`. `max_depth` validated into `[1, 10]`.
- **`Database.first_connected(subject, object_id, start=None, end=None, max_depth=4)`** — earliest date two entities become connected, scanning `opened_time_idx`'s candidate dates chronologically (connectivity isn't monotonic, so no binary search).
- **`Database.path_history(subject, object_id, start, end, resolution_days=1, max_depth=4)`** — `path_exists` stepped across an interval.
- **TGQL v0.4**: `PATH (A, B) AS OF '<date>' [MAX_DEPTH <n>]`, `FIRST_CONNECTED (A, B) [BETWEEN ... AND ...] [MAX_DEPTH <n>]`, `PATH_HISTORY (A, B) BETWEEN ... AND ... [RESOLUTION '<N>d'] [MAX_DEPTH <n>]`.

## Milestone 6 — done

Centrality metrics, built directly on M5's BFS primitives.

- **`closeness(entity_id, on_date, max_depth=4)`** — harmonic closeness (`sum(1/distance)`), not the classical `(n-1)/sum(distances)` formula, which is undefined/misleading for a graph that may be disconnected or depth-bounded (both always true here). One bounded BFS per call via the new `_bfs_distances` primitive.
- **`betweenness_all(on_date, max_depth=4)`/`betweenness(entity_id, ...)`** — Brandes' algorithm (unweighted, undirected) over every active entity (`_active_entity_ids`, a new full-`versions`-scan primitive), each source's BFS bounded by `max_depth`. `_neighbor_node_ids` (deduplicated by the *other node*, not by `relationship_id`) fixes a real multi-edge double-counting bug the naive approach would have had. A global computation — `betweenness_all` exposes the whole-graph result directly since the singular `betweenness()` wrapper would otherwise recompute it per call.
- **`pagerank_all(on_date, damping=0.85, ...)`/`pagerank(entity_id, ...)`** — standard power-iteration PageRank over directed out-edges (`_out_neighbors`), same global-computation shape as betweenness.
- **TGQL v0.5**: `TRACK CLOSENESS/BETWEENNESS/PAGERANK(entity) BETWEEN ... AND ... [MAX_DEPTH <n>]` — no new grammar statement needed (`TRACK` already accepted any metric identifier by M4's design), just new `_METRIC_ARITY` registry entries and an optional `MAX_DEPTH` clause added to `track_stmt`.

## Milestone 7 — done

Provenance, built on the `Assertion`/`Source` chain the data model has captured since M2.

- **`Database.assertions_for_version(version_id)`** — every `Assertion` backing one version, chronological.
- **`Database.why_changed(subject, predicate, object_id, date_from, date_to)`** — combines `diff()`'s interval-level classification (`"opened"`/`"closed"`/`"churned"`/`"persisted"`/`"no_relationship"`, `"churned"` being new — both opened and closed within the window) with the evidence trail: every assertion across this triple's history whose `event_time` (valid time, not `ingested_at`/system time) falls in the window, resolved to its `Source` via a new `_load_source` full-scan primitive.
- **TGQL v0.6**: `WHY_CHANGED (...) BETWEEN ... AND ...`, reusing the `pattern` grammar (fully literal, like `ASSERT`/`RETRACT`).

## Milestone 8 — done

Change-point detection, closing out "the full architecture" (complete the evolution algebra, embedded-library scope).

- **`coredb/signal.py`: `detect_changepoints(points, min_size=2, penalty=None)`** — binary segmentation with a residual-sum-of-squares cost function, the basis of tools like `ruptures`' `Binseg`, not an ad hoc heuristic. Recursively finds the split that most reduces total cost, keeps it only if the gain exceeds `penalty` (defaults to a standard BIC-style heuristic, `variance(values) * log(n)`), and recurses into both halves. `None`-valued points (e.g. `EDGE_WEIGHT` gaps) are dropped first as "no data," not a real zero.
- **`GraphSignal.changepoints(...)`** — thin wrapper; **`Database.changepoints(metric, target, start, end, ...)`** — `track()` + `.changepoints()` combined, the path most callers want.
- **TGQL v0.7**: `CHANGEPOINTS <METRIC>(<args>) BETWEEN ... AND ... [RESOLUTION '<N>d'] [MAX_DEPTH <n>]` — same metric registry and arity checking as `TRACK`; the shared "metric call" grammar/parsing (`metric_call` rule) was factored out of `track_stmt` into something both `TRACK` and `CHANGEPOINTS` reuse, rather than duplicating the identifier/string/max-depth bucketing logic a second time.

**This closes the four-milestone "complete the evolution algebra" arc** (M5 traversal → M6 centrality → M7 provenance → M8 change-point detection), all as an embedded library per the locked-in scope. What's left is everything in "Explicitly deferred" below — real ideas, but outside that scope (a bigger grammar undertaking, a genuinely different deployment model, or infrastructure work like an optimizer/benchmark suite).

## Milestone 9 — done

`GraphTSBench` (`benchmarks/`) plus a real fix the benchmark's own numbers justified — not a new query surface, a performance/infrastructure pass prompted by an external technical review of the engine's cost profile.

- **`benchmarks/`** — a pure-Python benchmark suite (`harness.py`/`datasets.py`/`bench_suite.py`/`run_all.py`, `python -m benchmarks.run_all [--quick]`) covering ingest, `AS_OF`/`HISTORY`/`DIFF`, `TRACK` (degree/weighted_degree/edge_weight/betweenness), `SERIES`, `PATH`, `CHANGEPOINTS`, `WHY_CHANGED`, and `dump`/`restore`. Exists specifically to gate future optimization work — the "Explicitly deferred" bullet below is now built, and the intent (measure before optimizing, keep a change only if the numbers improved) carries forward to any future work, including the native-code path suggested during the review that prompted this.
- **Interval-sweep rewrite of `TRACK`/`SERIES`** — `degree`/`weighted_degree`/`edge_weight` and `SERIES` iteration previously reconstructed a snapshot via `as_of()`/`degree()` once per resolution step: O(D × H) for D resolution steps and H = a pattern's/entity's total history depth. Rewritten to fetch a pattern's/entity's full history once, build open/close events from each interval's `valid_from`/`valid_to`, and sweep a running total (`degree`/`weighted_degree`) or active-version set (`SERIES`) forward across the requested dates in one pass: O(H + D). Measured on `benchmarks/run_all.py`'s default sizes (H=150, D=365): `TRACK DEGREE` 6.09s → 28ms (≈214×), `TRACK EDGE_WEIGHT` 6.03s → 6.5ms (≈924×), `SERIES` iteration 6.33s → 9.6ms (≈659×). `TRACK CLOSENESS`/`BETWEENNESS`/`PAGERANK` are unchanged — deliberately out of scope, see `ARCHITECTURE.md`'s Performance section.
- The equivalence between the old and new implementations is tested directly (`tests/test_track_series_sweep.py`): the old single-date methods (`db.degree()`, `db.edge_weight()`, `db.as_of()`) still exist and serve as the oracle.

## Explicitly deferred (not built yet)

These come from a broader architectural vision shared during design discussion, but fall outside "complete the evolution algebra while staying an embedded library" — deferred for scoping reasons, not rejected:

- **General multi-hop pattern matching** inside `MATCH`/`HISTORY` (`(a)-[*1..2]-(b)` chains with wildcards at each hop) — M5 added point-to-point traversal between two named entities, not a path-pattern grammar.
- **Motif evolution** — tracking when a structural pattern (not just a single edge) emerges, dissolves, or recurs.
- **Streaming / continuous queries** — `SUBSCRIBE TO CHANGES(...)` over live ingestion, unifying historical and future-facing queries in one model.
- **Query optimizer** — right now every query method picks its own access path in Python (bound subject → `spo_idx`, bound object → `ops_idx`, neither → full scan; BFS has no traversal-specific index either). A real operator tree with cost-based planning is future work, not attempted yet.
- **Server / wire protocol / PostgreSQL** — CoreDB stays an embedded library. A client-server mode is a deliberate non-goal until multi-process/multi-user access is actually needed.
- **Automatic map-size growth and multi-process write coordination** — `MapFullError` is caught and explained (M3), but growing the map automatically would require safely replaying an arbitrary caller-supplied transaction, which isn't generalizable; multi-process writers are untested.
- **`WHERE` beyond `confidence`, and on `DIFF`/`RANGE`/`SERIES`/`TRACK`/`PATH`/`FIRST_CONNECTED`/`PATH_HISTORY`/`WHY_CHANGED`/`CHANGEPOINTS`** — see `TGQL_SPEC.md`'s "Not yet supported" section.
- **`assertions_for_version()` from TGQL** — version ids are internal; `WHY_CHANGED` is the entity/triple-oriented surface for provenance.
- **`ASSERT ... SOURCE '<url>'`** — provenance attachment from TGQL; sources remain Python-API-only for now.
- **`GraphSignal.join()` from TGQL** — joining a `TRACK` signal against external data needs caller-supplied data, so it stays Python-API-only, same reasoning as `ASSERT ... SOURCE`.
- **`min_size`/`penalty` tuning for `CHANGEPOINTS` from TGQL** — sensitivity tuning is Python-API-only (`db.changepoints(..., min_size=..., penalty=...)`); TGQL always uses the auto-computed default.
- **Change-point detection over `GraphSeries` directly** (structural regime shifts, not just a numeric metric's) — M8 only detects changepoints in a `GraphSignal` (a metric already reduced to numbers); detecting structural change in the graph itself, without picking a metric first, is a different and harder problem, not attempted.
- **Native (C++) implementation of hot paths** — proposed during an external technical review (compact binary `RelationshipVersion` encoding, native `TemporalScanner`/`TemporalKernel` functions releasing the GIL, integer-interned entity/predicate ids, covering indexes, a batch write API). M9's benchmark suite is explicitly the gate for this: no native optimization should land without `benchmarks/` numbers proving it actually helps.
- **Incremental/native `TRACK CLOSENESS`/`BETWEENNESS`/`PAGERANK`** — M9's interval sweep only applies to degree-family metrics and `SERIES`; global per-date graph algorithms have no simple event-sweep equivalent (see `ARCHITECTURE.md`'s Performance section) and would need real incremental-graph-algorithm work, not a straightforward refactor.

## Known limitations to revisit as scale increases

See `ARCHITECTURE.md`'s "Known limitations" section — predicate-only pattern scans, global `DIFF`'s persisted-set full scan, single-hop-only `MATCH`/`HISTORY` patterns, and unindexed BFS traversal cost are the concrete things that would need indexing/design work before this handles a large graph.
