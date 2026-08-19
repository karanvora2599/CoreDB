# Architecture

## Module layout

```
coredb/
├── __init__.py            # public API: coredb.open(path) -> Database
├── model.py                # Entity, Relationship, RelationshipVersion, Assertion, Source
├── errors.py                # CoreDBError, ValidationError, StorageError, SchemaVersionError, QueryError
├── engine.py                # Database: mutation + query + storage-management methods
├── series.py                # GraphSeries (lazy interval view), GraphDelta (structured diff)
├── storage/
│   ├── kvstore.py           # KVStore/Transaction interface - engine.py never talks to LMDB directly
│   ├── lmdb_backend.py       # LMDB implementation of KVStore
│   └── keys.py               # composite key encoding for LMDB's sorted byte-string keys
└── query/
    ├── grammar.lark          # Lark grammar for TGQL (coredb/query language)
    ├── ast_nodes.py           # MatchQuery, HistoryQuery, DiffQuery, RangeQuery, SeriesQuery, AssertStatement, RetractStatement
    ├── parser.py              # Lark parse tree -> AST
    └── executor.py            # AST -> engine.py calls -> plain dict/list[dict] results
```

`engine.py` is the only module that talks to `storage/`. `query/` never imports `engine.py` — `Database.execute()` imports `query.parser`/`query.executor` lazily, so the dependency only goes one direction (engine → query at call time, never query → engine at import time). This is why `engine.py` can freely import `GraphDelta`/`GraphSeries` from `series.py` without a circular import: `series.py` holds a duck-typed reference to the database rather than importing `engine.py`.

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

## Storage management

- **Schema versioning.** `Database` writes a `schema_version` marker into the `counters` table on first open. If an existing database's marker doesn't match the running code's expected version, `Database()` raises `SchemaVersionError` immediately rather than silently operating on a mismatched on-disk shape. Recovery path: `Database.dump()` with the old code version, `coredb.restore()` with the new one.
- **`Database.stats()`** — cheap entry counts per table via LMDB's native `stat()` (no manual iteration).
- **`Database.backup(path)`** — a compacted, self-contained copy via `env.copy(path, compact=True)`, safe to call on a live database. `path` is a directory: LMDB uses subdir mode by default, so a CoreDB database is a directory containing `data.mdb`/`lock.mdb`, not a single file.
- **`Database.dump(path)` / `coredb.restore(dump_path, db_path)`** — schema-independent JSON-lines export/import of the logical facts (not internal ids or system-time), replayed through `assert_fact`/`retract_fact`. This is the actual migration path across a schema change — a `backup()`'s raw LMDB bytes are still in the old schema and won't help after one.
- **`coredb.open(path, map_size=None)`** — `map_size` overrides LMDB's default 1 GiB virtual address space. Writes that would exceed it raise `StorageError` (wrapping LMDB's `MapFullError`) naming the fix; there's no automatic grow-and-retry, since that would require safely replaying an arbitrary caller-supplied transaction body, which isn't generalizable.

## Known limitations at this stage

- **Patterns with neither subject nor object bound** (only a predicate, or nothing at all) fall back to a full scan of the `versions` table. Fine at the current scale; would need a predicate-only index for large graphs.
- **Global (pattern-less) `DIFF`'s "persisted" set** is a full table scan — there's no interval index yet for "spans both dates" the way `opened_time_idx`/`closed_time_idx` cover the open/close cases.
- **Single-hop patterns only.** `(subject, predicate, object)` triples, not multi-hop traversal (`(a)-[]->(b)-[]->(c)`). See `ROADMAP.md`.
- **No automatic map-size growth or multi-process write coordination.** See "Concurrency" and "Storage management" above.
