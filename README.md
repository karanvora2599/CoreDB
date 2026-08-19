# CoreDB

CoreDB is an embedded **bitemporal graph database**: a graph whose edges have two independent time axes — *valid time* (when a relationship was true in the world) and *system time* (when this database learned about or closed it) — queried through **TGQL** (Temporal Graph Query Language), a custom query language, instead of one-off application code.

The core abstraction isn't "give me the graph at time `t`." It's the evolution of the graph over an interval — who a relationship connected, how it changed, and what the database *knew* at any point in the past. A normal graph database answers questions about `G`. CoreDB answers questions about `G(t)` and, increasingly, about the evolution operator over `[t0, t1]`.

This project generalizes a pattern first proven in [`Knowledge_Graph`](../Knowledge_Graph) (a bitemporal NVIDIA-news knowledge graph built on SQLite) into a reusable, domain-agnostic engine. Nothing in CoreDB's engine, storage, or query layers is specific to NVIDIA, news, or any other domain — `subject`/`predicate`/`object` are always caller-supplied strings.

## Status

- **Milestone 1** (done): embedded engine on LMDB, flat interval-valued facts, TGQL v0.1 (`MATCH`/`HISTORY`/`DIFF`/`RANGE`).
- **Milestone 2** (done): evidence-based data model (`Entity`/`Relationship`/`RelationshipVersion`/`Assertion`), `GraphSeries` (lazy interval view) and `GraphDelta` (structured diff datatype), TGQL `SERIES` statement.
- **Milestone 3** (done): TGQL v0.2 (`ASSERT`/`RETRACT` write statements, `WHERE`/`LIMIT` filtering, comments), input validation + a `coredb/errors.py` exception hierarchy, storage management (`stats`/`backup`/`dump`/`restore`, schema versioning, clear errors on a full LMDB map).
- **Milestone 4** (done): TGQL v0.3 (`TRACK`), turning a graph metric (`DEGREE`/`WEIGHTED_DEGREE`/`EDGE_WEIGHT`) into a `GraphSignal` time series joinable against external (non-graph) data.
- See [`Documentation/ROADMAP.md`](Documentation/ROADMAP.md) for what's deliberately deferred (centrality-family metrics, provenance queries, change-point detection, multi-hop traversal, a server mode).

## Install

```bash
python -m venv .venv
./.venv/Scripts/python -m pip install -e ".[dev]"   # Windows
# source .venv/bin/activate && pip install -e ".[dev]"  # macOS/Linux
```

Dependencies: [`lmdb`](https://pypi.org/project/lmdb/) (embedded storage engine) and [`lark`](https://pypi.org/project/lark/) (TGQL parser). No server, no network dependency — CoreDB is a linkable library, like SQLite.

## Quickstart

```python
import coredb

db = coredb.open("my_graph.db")

# Assert a relationship, valid starting on a date. Calling this again for
# the same (subject, predicate, object) confirms/extends the existing
# interval rather than creating a duplicate.
db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", valid_from="2026-01-01", confidence=0.9)

# Explicitly close a relationship.
db.retract_fact("NVIDIA", "SUPPLIED_BY", "TSMC", valid_to="2026-06-01")

# Bulk-sync "everything currently true" for one (subject, predicate) pair -
# opens new relationships, confirms existing ones, closes ones no longer
# present. Useful for periodic ingestion (e.g. one call per day).
db.sync_snapshot("NVIDIA", "CO_OCCURS_WITH", {"AI": 5, "Google": 2}, as_of_date="2026-01-01")

# Query and write via TGQL - patterns are (subject, predicate, object) with
# `?var` wildcards; ASSERT/RETRACT are the write statements.
db.execute("ASSERT (NVIDIA, SUPPLIED_BY, Samsung) VALID FROM '2026-02-01' CONFIDENCE 0.6")
db.execute("MATCH (NVIDIA, CO_OCCURS_WITH, ?o) AS OF '2026-01-01' WHERE confidence > 0.5")
db.execute("HISTORY (NVIDIA, CO_OCCURS_WITH, AI)")
db.execute("DIFF BETWEEN '2026-01-01' AND '2026-01-10' FOR (NVIDIA, ?p, ?o)")
db.execute("SERIES (NVIDIA, CO_OCCURS_WITH, ?o) BETWEEN '2026-01-01' AND '2026-01-10'")

# Turn a graph metric into a time series, and correlate it with external data.
db.execute("TRACK DEGREE(NVIDIA) BETWEEN '2026-01-01' AND '2026-01-10'")
signal = db.track("degree", "NVIDIA", "2026-01-01", "2026-01-10")
signal.join({"2026-01-05": 134.2})   # inner join against e.g. price data, by date

# Storage management.
db.stats()               # entry counts per table
db.backup("my_graph_backup.db")   # compacted, self-contained copy
db.dump("my_graph_dump.jsonl")    # schema-independent export

db.close()
```

Or use the Python API directly (`db.as_of(...)`, `db.history(...)`, `db.diff_delta(...)`, `db.series(...)`) without TGQL — see [`Documentation/QUERY_LANGUAGE.md`](Documentation/QUERY_LANGUAGE.md).

## Documentation

- [`Documentation/ARCHITECTURE.md`](Documentation/ARCHITECTURE.md) — module layout, storage engine choice, LMDB table/index design, concurrency and storage-management notes.
- [`Documentation/DATA_MODEL.md`](Documentation/DATA_MODEL.md) — `Entity`/`Relationship`/`RelationshipVersion`/`Assertion`, the bitemporal axes, a worked example.
- [`Documentation/TGQL_SPEC.md`](Documentation/TGQL_SPEC.md) — the full, versioned TGQL grammar reference.
- [`Documentation/QUERY_LANGUAGE.md`](Documentation/QUERY_LANGUAGE.md) — a short pointer into the spec, plus the Python-API equivalents.
- [`Documentation/ROADMAP.md`](Documentation/ROADMAP.md) — what's built, what's deliberately deferred, and why.

## Running tests

```bash
./.venv/Scripts/python -m pytest -v
```
