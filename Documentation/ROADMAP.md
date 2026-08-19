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

## Explicitly deferred (not built yet)

These come from a broader architectural vision shared during design discussion. They're real, well-motivated ideas — deferred for scoping reasons, not rejected:

- **Centrality-family `TRACK` metrics** (betweenness, closeness, PageRank) — depend on multi-hop traversal (below); `DEGREE`/`WEIGHTED_DEGREE`/`EDGE_WEIGHT` (M4) are what's honestly computable without it.
- **Provenance / `WHY_CHANGED`** — tracing a relationship's confidence change back through its `Assertion` records to the original sources. The data model already captures what's needed (`assertion_ids`, `source_id`, `event_time`/`published_at`/`ingested_at`); the query surface to walk that lineage isn't built.
- **Change-point detection** — native `CHANGEPOINTS` operator over a `GraphSeries`/`GraphSignal` to surface structural or metric regime shifts, instead of requiring a separate offline ML job.
- **Multi-hop traversal** — patterns are single-hop `(subject, predicate, object)` triples today, matching the star-topology scope of the original `Knowledge_Graph` app (generalized beyond its hardcoded hub, but not generalized to path queries like `(a)-[*1..2]-(b)`). Temporal path queries (`PATH HISTORY`, `FIRST_CONNECTED`, `PATH_STABILITY`) and centrality-family `TRACK` metrics both depend on this landing first.
- **Motif evolution** — tracking when a structural pattern (not just a single edge) emerges, dissolves, or recurs.
- **Streaming / continuous queries** — `SUBSCRIBE TO CHANGES(...)` over live ingestion, unifying historical and future-facing queries in one model.
- **Query optimizer** — right now every query method picks its own access path in Python (bound subject → `spo_idx`, bound object → `ops_idx`, neither → full scan). A real operator tree with cost-based planning (e.g. choosing between reconstructing two snapshots vs. a direct delta scan) is future work, not attempted yet.
- **Server / wire protocol / PostgreSQL** — CoreDB stays an embedded library for now. A client-server mode (whether on top of LMDB or a different backend) is a deliberate non-goal until multi-process/multi-user access is actually needed.
- **Automatic map-size growth and multi-process write coordination** — `MapFullError` is caught and explained (M3), but growing the map automatically would require safely replaying an arbitrary caller-supplied transaction, which isn't generalizable; multi-process writers are untested.
- **`WHERE` beyond `confidence`, and on `DIFF`/`RANGE`/`SERIES`/`TRACK`** — see `TGQL_SPEC.md`'s "Not yet supported" section.
- **`ASSERT ... SOURCE '<url>'`** — provenance attachment from TGQL; sources remain Python-API-only for now.
- **`GraphSignal.join()` from TGQL** — joining a `TRACK` signal against external data needs caller-supplied data, so it stays Python-API-only, same reasoning as `ASSERT ... SOURCE`.
- **Benchmark suite** — a dedicated benchmark (`GraphTSBench`-style: `AS_OF`, bitemporal reconstruction, `HISTORY`, `DIFF`, `TRACK`, change-point, provenance, cross-modal joins) to measure latency/storage/reconstruction cost as the engine matures.

## Known limitations to revisit as scale increases

See `ARCHITECTURE.md`'s "Known limitations" section — predicate-only pattern scans, global `DIFF`'s persisted-set full scan, and single-hop-only patterns are the concrete things that would need indexing/design work before this handles a large graph.
