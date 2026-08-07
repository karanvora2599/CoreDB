# Query language

CoreDB's DSL (`coredb/query/grammar.lark`) is a small, purpose-built language for querying graph evolution — not an extension of Cypher or Gremlin. Every statement operates on a **pattern**: a `(subject, predicate, object)` triple where any position is either a literal identifier or a `?name` wildcard that gets captured as a variable binding in the results.

Run a query with `Database.execute(dsl_string)`. There's also a lower-level Python API (`db.as_of`, `db.history`, `db.diff`, `db.diff_delta`, `db.range_agg`, `db.as_known`, `db.series`) that the DSL compiles down to — use it directly if you don't need the DSL's string syntax.

## Patterns

```
(SUBJECT, PREDICATE, OBJECT)     # all bound - matches at most one relationship
(NVIDIA, ?p, ?o)                 # subject bound, predicate/object captured
(?s, SUPPLIED_BY, TSMC)          # object bound (reverse lookup), subject captured
```

Identifiers match `[A-Za-z_][A-Za-z0-9_]*`. Dates and datetimes are single-quoted strings (`'2026-01-01'`, `'2026-01-01T12:00:00'`).

## `MATCH ... AS OF ...`

The relationships matching a pattern that were active on a given date — works for any date, including ones never directly asserted (the answer is reconstructed from surrounding intervals, not a stored per-day row).

```
MATCH (NVIDIA, CO_OCCURS_WITH, ?o) AS OF '2026-01-05'
```

Optionally add `KNOWN BY '<datetime>'` for a **bitemporal** query — "what did the database believe as of `AS OF`, using only what it had actually recorded by `KNOWN BY`":

```
MATCH (NVIDIA, ?p, CHINA) AS OF '2026-01-01' KNOWN BY '2026-01-02T00:00:00'
```

**Result:** `list[dict]`, one dict per matching `RelationshipVersion` (see `DATA_MODEL.md`), plus a `"bindings"` key mapping each `?var` to its resolved value.

## `HISTORY ...`

Every interval a pattern has ever had, sorted chronologically. A relationship that closed and reopened returns multiple rows under the same `relationship_id`.

```
HISTORY (NVIDIA, SUPPLIED_BY, TSMC)
HISTORY (NVIDIA, SUPPLIED_BY, TSMC) BETWEEN '2026-01-01' AND '2026-06-30'
```

**Result:** `list[dict]`, same row shape as `MATCH`.

## `DIFF BETWEEN ... AND ...`

What changed between two dates, computed directly from the version log — not by comparing two `AS OF` snapshots, so it correctly catches a relationship that both opened and closed entirely inside the window.

```
DIFF BETWEEN '2026-01-01' AND '2026-01-10'                       # whole graph
DIFF BETWEEN '2026-01-01' AND '2026-01-10' FOR (NVIDIA, ?p, ?o)  # scoped to a pattern
```

**Result:** a single `dict` shaped like a `GraphDelta`:

```python
{
    "date_from": "2026-01-01", "date_to": "2026-01-10",
    "nodes_added": ["Apple"],       # net-new objects by date_to
    "nodes_removed": ["AMD"],       # objects gone by date_to
    "edges_opened": [...],          # RelationshipVersion rows that opened in the window
    "edges_closed": [...],          # ...that closed in the window
    "edges_persisted": [...],       # ...open across the entire window
}
```

`nodes_added`/`nodes_removed` are netted against churn: an object whose relationship both opened and closed inside the window (e.g. it left and rejoined) nets out to neither — it only shows up in `edges_opened`/`edges_closed`, not in either node list.

## `RANGE ... BETWEEN ... AND ...`

Day-count of each matching object's overlap with a date range — an object with several disjoint intervals in the window gets its day counts summed, not overwritten.

```
RANGE (NVIDIA, CO_OCCURS_WITH, ?o) BETWEEN '2026-01-01' AND '2026-01-31'
```

**Result:** `list[dict]` — `[{"object_id": "AI", "dayCount": 31}, ...]`.

## `SERIES ... BETWEEN ... AND ...`

The evolution-operator query: a **lazy view** over a pattern's history across an interval, stepped at a given resolution. Nothing is precomputed — each step is resolved with an `AS OF` call at iteration time.

```
SERIES (NVIDIA, CO_OCCURS_WITH, ?o) BETWEEN '2026-01-01' AND '2026-01-31'
SERIES (NVIDIA, CO_OCCURS_WITH, ?o) BETWEEN '2026-01-01' AND '2026-01-31' RESOLUTION '7d'
```

`RESOLUTION` defaults to `'1d'`; only `'<N>d'` (day-count) resolutions are supported today.

**Result:** `list[dict]` — one entry per step:

```python
[{"date": "2026-01-01", "facts": [...]}, {"date": "2026-01-08", "facts": [...]}, ...]
```

Via the Python API, `db.series(pattern, start, end, resolution_days=1)` returns a `GraphSeries` object directly, with `.at(date)` (single snapshot), `.diff(date_from=None, date_to=None)` (a `GraphDelta` over the series' own bounds by default), and `.dates()`/iteration.

## Result-shape summary

| Statement | Result |
|---|---|
| `MATCH` | `list[dict]` — matching `RelationshipVersion` rows |
| `HISTORY` | `list[dict]` — matching `RelationshipVersion` rows, chronological |
| `DIFF` | single `dict` — `GraphDelta` shape |
| `RANGE` | `list[dict]` — `{"object_id", "dayCount"}` |
| `SERIES` | `list[dict]` — `{"date", "facts": [...]}` per resolution step |
