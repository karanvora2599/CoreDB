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

## Explicitly deferred (not built yet)

These come from a broader architectural vision shared during design discussion. They're real, well-motivated ideas — deferred for scoping reasons, not rejected:

- **`TRACK` / `GraphSignal`** — turning an arbitrary graph function (degree, centrality, edge weight) into a time series joinable with external series (e.g. correlating structural graph change with market volatility). This is likely the next milestone after M2, since `GraphSeries` is the foundation it needs to sit on.
- **Provenance / `WHY_CHANGED`** — tracing a relationship's confidence change back through its `Assertion` records to the original sources. The data model (M2) already captures what's needed (`assertion_ids`, `source_id`, `event_time`/`published_at`/`ingested_at`); the query surface to walk that lineage isn't built.
- **Change-point detection** — native `CHANGEPOINTS` operator over a `GraphSeries` to surface structural regime shifts, instead of requiring a separate offline ML job.
- **Multi-hop traversal** — patterns are single-hop `(subject, predicate, object)` triples today, matching the star-topology scope of the original `Knowledge_Graph` app (generalized beyond its hardcoded hub, but not generalized to path queries like `(a)-[*1..2]-(b)`). Temporal path queries (`PATH HISTORY`, `FIRST_CONNECTED`, `PATH_STABILITY`) depend on this.
- **Motif evolution** — tracking when a structural pattern (not just a single edge) emerges, dissolves, or recurs.
- **Streaming / continuous queries** — `SUBSCRIBE TO CHANGES(...)` over live ingestion, unifying historical and future-facing queries in one model.
- **Query optimizer** — right now every query method picks its own access path in Python (bound subject → `spo_idx`, bound object → `ops_idx`, neither → full scan). A real operator tree with cost-based planning (e.g. choosing between reconstructing two snapshots vs. a direct delta scan) is future work, not attempted yet.
- **Server / wire protocol / PostgreSQL** — CoreDB stays an embedded library for now. A client-server mode (whether on top of LMDB or a different backend) is a deliberate non-goal until multi-process/multi-user access is actually needed.
- **Benchmark suite** — a dedicated benchmark (`GraphTSBench`-style: `AS_OF`, bitemporal reconstruction, `HISTORY`, `DIFF`, `TRACK`, change-point, provenance, cross-modal joins) to measure latency/storage/reconstruction cost as the engine matures.

## Known limitations to revisit as scale increases

See `ARCHITECTURE.md`'s "Known limitations" section — predicate-only pattern scans, global `DIFF`'s persisted-set full scan, and single-hop-only patterns are the concrete things that would need indexing/design work before this handles a large graph.
