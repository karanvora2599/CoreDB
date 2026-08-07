# Data model

CoreDB's model is four cooperating concepts (`coredb/model.py`), not one flat temporal-edge record. Splitting them apart is what lets a relationship's strength be a *derived, evidence-backed* value instead of a single scalar some caller silently overwrites.

## The four types

### `Entity`

A stable node identity.

```python
Entity(entity_id="NVIDIA", type=None, attrs={}, first_seen="2026-01-01", last_seen="2026-01-10")
```

`first_seen`/`last_seen` are maintained automatically every time the entity appears as a subject or object in `assert_fact`/`sync_snapshot` — including on confirmation, not just first creation.

### `Relationship`

A stable identity for one `(subject, predicate, object)` triple. Created once, on first use, and never changes afterwards — even if the relationship closes and reopens later.

```python
Relationship(relationship_id=4, subject_id="Fleetwood_Mac", predicate="HAS_MEMBER",
             object_id="Lindsey_Buckingham", created_at="2026-08-07T00:52:15.009407+00:00")
```

This is what lets two disjoint tenures of the same triple be recognized as *the same logical relationship* rather than two unrelated facts — e.g. a band member who leaves and rejoins gets one `relationship_id` across both intervals, with two different `version_id`s.

### `RelationshipVersion`

One interval of a relationship — the unit `MATCH`/`HISTORY`/`DIFF`/`RANGE`/`SERIES` actually return.

```python
RelationshipVersion(
    version_id=5, relationship_id=4,
    subject_id="Fleetwood_Mac", predicate="HAS_MEMBER", object_id="Lindsey_Buckingham",
    valid_from="1997-01-01", valid_to=None,              # valid time
    system_from="2026-08-07T00:52:15+00:00", system_to=None,  # system time
    last_confirmed="1997-01-01", confidence=None, properties={}, assertion_ids=[],
)
```

Two independent time axes, matching the bitemporal pattern proven in `Knowledge_Graph/backend/db.py`:

- **Valid time** (`valid_from`/`valid_to`) — when the relationship was true *in the world*. `valid_to=None` means still open.
- **System time** (`system_from`/`system_to`) — when *this database* recorded or closed the version. Renamed from Milestone 1's `observed_at`/`superseded_at` for symmetry with `valid_from`/`valid_to`; same semantics.

These axes are independent on purpose. A news article published today can assert that something became true a month ago — `valid_from` is a month back, `system_from` is today. This is what makes `AS KNOWN BY` queries meaningful (see below): "what did the database believe as of a past point in wall-clock time" is a different question from "what do we now believe was true on a past date," and conflating the two axes creates look-ahead bias.

`properties: dict` is open for arbitrary derived attributes beyond `confidence` — reserved for future use (e.g. a computed edge weight), not populated by the engine yet.

### `Assertion`

One piece of evidence backing a `RelationshipVersion`. `assert_fact`/`sync_snapshot`'s `sources` parameter (a list of URLs or `{"url": ..., "title": ..., "domain": ..., "published_at": ...}` dicts) produces one `Assertion` per source.

```python
Assertion(
    assertion_id=2, relationship_id=4, version_id=5, source_id=1,
    event_time="1997-01-01", published_at=None, ingested_at="2026-08-07T00:52:15+00:00",
    polarity=1, confidence=0.9, payload={},
)
```

`polarity` (+1/-1) is reserved for future use (a source contradicting/retracting a relationship rather than supporting it) — the engine always writes `polarity=1` today. A version's `assertion_ids` list is how you'd trace a relationship's confidence back to the documents that produced it (full provenance querying — `WHY_CHANGED`-style — is deferred; see `ROADMAP.md`).

## Worked example

```python
db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", valid_from="2026-01-01", confidence=0.8,
                sources=["https://example.com/article-a"])
```

This creates, in order:
1. A `Relationship` for `(NVIDIA, SUPPLIED_BY, TSMC)` if one doesn't already exist (`relationship_lookup` table).
2. A `RelationshipVersion` with `valid_from="2026-01-01"`, `valid_to=None`, `system_from=<now>`.
3. An `Assertion` for `https://example.com/article-a`, linked to that version.
4. `Entity` records for `NVIDIA` and `TSMC` (or updates `last_seen` if they already exist).

Calling `assert_fact` again for the same triple with a later `valid_from` does **not** create a new version — it confirms the existing open one (`last_confirmed` advances, `confidence` updates if given, a new `Assertion` is added) so history isn't fragmented into one row per confirmation.

## Bulk ingestion: `sync_snapshot`

For periodic ingestion (e.g. "here's everything true about NVIDIA's co-occurrences today"), `sync_snapshot(subject, predicate, objects_now_true, as_of_date, sources=None)` diffs the given object set against what's currently open for that `(subject, predicate)` pair:

- Objects not previously open → opened as new versions.
- Objects already open → confirmed (`last_confirmed` bumped).
- Previously-open objects missing from `objects_now_true` → closed, **at their `last_confirmed` date**, not the day they were noticed missing. This means an ingestion gap (no data for a day or two) doesn't fabricate a false closure — a relationship open before the gap and still open after it reads as one continuous interval spanning the gap.

This is the generalized form of `Knowledge_Graph/backend/db.py`'s `ingest_daily_snapshot`, minus the hardcoded `NVIDIA`/`CO_OCCURS_WITH` hub — any `(subject, predicate)` pair works.
