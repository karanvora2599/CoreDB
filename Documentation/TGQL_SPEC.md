# TGQL — Temporal Graph Query Language

TGQL is CoreDB's query language: a small, purpose-built grammar for querying and mutating graph evolution, not an extension of Cypher or Gremlin. It's versioned so it can grow deliberately — this document is the authoritative grammar reference (the source grammar lives in `coredb/query/grammar.lark`; this file explains it).

Run TGQL with `Database.execute(source: str)`. Every statement operates on a **pattern**: a `(subject, predicate, object)` triple where any position is a literal identifier or a `?name` wildcard captured as a variable binding in results.

Malformed source text (a syntax error, or an unsupported `RESOLUTION` unit) raises `coredb.QueryError`; write statements with an invalid triple (`ASSERT`/`RETRACT` with a `?` wildcard, or values that would fail `assert_fact`/`retract_fact`'s own validation) raise `coredb.ValidationError`.

```
(SUBJECT, PREDICATE, OBJECT)     # all bound
(NVIDIA, ?p, ?o)                 # subject bound, rest captured
(?s, SUPPLIED_BY, TSMC)          # object bound (reverse lookup)
```

Identifiers match `[A-Za-z_][A-Za-z0-9_]*`. Dates/datetimes are single-quoted strings. `// rest of line` is a comment, ignored anywhere.

## TGQL v0.1 (read-only queries)

### `MATCH ... AS OF '<date>' [KNOWN BY '<datetime>']`

Relationships matching a pattern active on a given date. `KNOWN BY` adds the bitemporal constraint: only use what the database had actually recorded by that wall-clock cutoff (avoids look-ahead bias).

```
MATCH (NVIDIA, CO_OCCURS_WITH, ?o) AS OF '2026-01-05'
MATCH (NVIDIA, ?p, CHINA) AS OF '2026-01-01' KNOWN BY '2026-01-02T00:00:00'
```

**Result:** `list[dict]` — one `RelationshipVersion` row per match, each with a `"bindings"` key mapping `?var` names to resolved values.

### `HISTORY ... [BETWEEN '<date>' AND '<date>']`

Every interval a pattern has ever had, chronologically sorted.

```
HISTORY (NVIDIA, SUPPLIED_BY, TSMC)
HISTORY (NVIDIA, SUPPLIED_BY, TSMC) BETWEEN '2026-01-01' AND '2026-06-30'
```

**Result:** `list[dict]`, same row shape as `MATCH`.

### `DIFF BETWEEN '<date>' AND '<date>' [FOR (...)]`

What changed between two dates, computed from the version log directly (catches relationships that both opened and closed inside the window).

```
DIFF BETWEEN '2026-01-01' AND '2026-01-10'
DIFF BETWEEN '2026-01-01' AND '2026-01-10' FOR (NVIDIA, ?p, ?o)
```

**Result:** a single `dict` — the `GraphDelta` shape: `date_from`, `date_to`, `nodes_added`, `nodes_removed` (netted against churn), `edges_opened`, `edges_closed`, `edges_persisted`.

### `RANGE ... BETWEEN '<date>' AND '<date>'`

Day-count of each matching object's overlap with a range, summing disjoint intervals per object.

```
RANGE (NVIDIA, CO_OCCURS_WITH, ?o) BETWEEN '2026-01-01' AND '2026-01-31'
```

**Result:** `list[dict]` — `{"object_id": ..., "dayCount": ...}`.

### `SERIES ... BETWEEN '<date>' AND '<date>' [RESOLUTION '<N>d']`

A lazy view over a pattern's history, stepped at a resolution (default `'1d'`; only day-count resolutions supported).

```
SERIES (NVIDIA, CO_OCCURS_WITH, ?o) BETWEEN '2026-01-01' AND '2026-01-31' RESOLUTION '7d'
```

**Result:** `list[dict]` — `[{"date": ..., "facts": [...]}, ...]`.

## TGQL v0.2 (write statements + filtering)

### `ASSERT (...) VALID FROM '<date>' [CONFIDENCE <number>]`

Opens (or confirms, if already open) an interval — the DSL entry point for `Database.assert_fact`. The pattern must be **fully literal**; `?` wildcards aren't allowed in a write statement.

```
ASSERT (NVIDIA, SUPPLIED_BY, TSMC) VALID FROM '2026-01-01' CONFIDENCE 0.9
```

**Result:** `{"version_id": <int>}`.

### `RETRACT (...) VALID TO '<date>'`

Closes the open interval for a triple — the DSL entry point for `Database.retract_fact`. Also requires a fully literal pattern.

```
RETRACT (NVIDIA, SUPPLIED_BY, TSMC) VALID TO '2026-06-01'
```

**Result:** `{"version_id": <int or None>}` (`None` if nothing was open).

### `... WHERE confidence <comparator> <number>`

Optional trailing filter on `MATCH` and `HISTORY` only. `<comparator>` is one of `> < >= <= = !=`. Only the `confidence` field is filterable in v0.2; rows with `confidence = None` never match a `WHERE` clause (there's nothing to compare).

```
MATCH (NVIDIA, ?p, ?o) AS OF '2026-01-01' WHERE confidence > 0.5
HISTORY (NVIDIA, SUPPLIED_BY, ?o) WHERE confidence >= 0.9
```

### `... LIMIT <n>`

Optional trailing clause on `MATCH` and `HISTORY` only, applied after `WHERE`. Simple list truncation — no `OFFSET`/pagination yet.

```
MATCH (NVIDIA, ?p, ?o) AS OF '2026-01-01' WHERE confidence > 0.5 LIMIT 10
```

## TGQL v0.3 (graph metrics as time series)

### `TRACK <METRIC>(<args>) BETWEEN '<date>' AND '<date>' [RESOLUTION '<N>d']`

Evaluates a graph metric at each resolution step across an interval, turning it into a plain time series (a `GraphSignal` under the hood — see `Database.track()`/`coredb/signal.py`). `<METRIC>` is a plain identifier (not a fixed grammar keyword), so future metrics only need a registry entry, not a grammar change.

Supported metrics — what's honestly computable on the current single-hop model (see "Not yet supported" for what isn't):

| Metric | Args | Meaning |
|---|---|---|
| `DEGREE(entity)` | 1 | Count of relationships touching `entity` (as subject or object), deduplicated by relationship |
| `WEIGHTED_DEGREE(entity)` | 1 | Same, summing `confidence` instead of counting (`None` treated as `0.0`) |
| `EDGE_WEIGHT(subject, predicate, object)` | 3 | The confidence of one specific relationship, or `null` when it isn't open |

```
TRACK DEGREE(NVIDIA) BETWEEN '2026-01-01' AND '2026-01-31'
TRACK WEIGHTED_DEGREE(NVIDIA) BETWEEN '2026-01-01' AND '2026-01-31' RESOLUTION '7d'
TRACK EDGE_WEIGHT(NVIDIA, SUPPLIED_BY, TSMC) BETWEEN '2026-01-01' AND '2026-01-31'
```

An unknown metric name or wrong argument count raises `coredb.QueryError`.

**Result:** `list[dict]` — `[{"date": ..., "value": ...}, ...]`.

Joining a signal against external (non-graph) data — e.g. correlating `DEGREE` against a price series — is Python-API-only in v0.3: `db.track(...)` returns a `GraphSignal`, and `GraphSignal.join(other: dict)` does an inner join on date. This isn't expressible in TGQL's string syntax since it needs caller-supplied data, the same reason `ASSERT ... SOURCE` stays Python-only.

## TGQL v0.4 (point-to-point path queries)

These trace a path between two **explicitly named entities** via breadth-first search over edges active at a date, bounded by `MAX_DEPTH` hops (default `4`, must be in `[1, 10]`). This is **not** general multi-hop pattern matching inside `MATCH`/`HISTORY` (see "Not yet supported") — `A`/`B` here are always concrete entities, never `?` wildcards, and there's no way to match an arbitrary-length chain as part of a larger pattern.

### `PATH (A, B) AS OF '<date>' [MAX_DEPTH <n>]`

Whether `A` and `B` are connected on a given date, and the shortest path if so.

```
PATH (NVIDIA, OpenAI) AS OF '2026-01-05'
PATH (NVIDIA, OpenAI) AS OF '2026-01-05' MAX_DEPTH 2
```

**Result:** `{"connected": bool, "path": [<version dict>, ...] | None}`.

### `FIRST_CONNECTED (A, B) [BETWEEN '<date>' AND '<date>'] [MAX_DEPTH <n>]`

The earliest date `A` and `B` become connected. Scans chronologically through every date some relationship opened (bounded by `BETWEEN` if given) — connectivity isn't monotonic (edges can close again), so this can't be a binary search.

```
FIRST_CONNECTED (NVIDIA, OpenAI)
FIRST_CONNECTED (NVIDIA, OpenAI) BETWEEN '2024-01-01' AND '2025-01-01' MAX_DEPTH 3
```

**Result:** `{"first_connected": <date> | None}`.

### `PATH_HISTORY (A, B) BETWEEN '<date>' AND '<date>' [RESOLUTION '<N>d'] [MAX_DEPTH <n>]`

`PATH` stepped across an interval — how the path between two entities emerges, changes, or disappears over time.

```
PATH_HISTORY (NVIDIA, OpenAI) BETWEEN '2024-01-01' AND '2025-01-01' RESOLUTION '30d'
```

**Result:** `list[dict]` — `[{"date": ..., "path": [<version dict>, ...] | None}, ...]`.

## Not yet supported

These are deliberately out of scope so far — see `Documentation/ROADMAP.md` for the fuller picture of what's deferred and why:

- **General multi-hop pattern matching** inside `MATCH`/`HISTORY` (`(a)-[]->(b)-[]->(c)` chains with wildcards at each hop) — v0.4's `PATH`/`FIRST_CONNECTED`/`PATH_HISTORY` are point-to-point queries between two named entities, not a path-pattern grammar.
- **Centrality-family metrics** (betweenness, closeness, PageRank) for `TRACK` — v0.4 adds the BFS traversal primitive these need, but the metrics themselves (which typically require many-source or all-pairs traversal, not single-path BFS) aren't implemented yet.
- `WHERE`/`LIMIT` on `DIFF`, `RANGE`, `SERIES`, `TRACK`, `PATH`, `FIRST_CONNECTED`, or `PATH_HISTORY` — only `MATCH`/`HISTORY` support them.
- `WHERE` on any field other than `confidence` — no query planner support yet for filtering on `properties` or other fields.
- `ASSERT ... SOURCE '<url>'` — attaching provenance from the DSL. Sources are still Python-API-only (`sources=[...]` on `assert_fact`/`sync_snapshot`).
- `GraphSignal.join()` from TGQL — see the `TRACK` section above.
- `ORDER BY` — `HISTORY` is always chronological; there's no way to sort `MATCH` results yet.

## Version history

- **v0.1** — `MATCH`/`HISTORY`/`DIFF`/`RANGE`/`SERIES`, read-only.
- **v0.2** — `ASSERT`/`RETRACT` write statements, `WHERE`/`LIMIT` on `MATCH`/`HISTORY`, line comments.
- **v0.3** — `TRACK` (`DEGREE`/`WEIGHTED_DEGREE`/`EDGE_WEIGHT`), turning a graph metric into a time series.
- **v0.4** — `PATH`/`FIRST_CONNECTED`/`PATH_HISTORY`, point-to-point multi-hop traversal via bounded BFS.
