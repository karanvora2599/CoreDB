"""Replays a small multi-day scenario shaped like Knowledge_Graph/backend/db.py's
NVIDIA-hub ingestion (ingest_daily_snapshot -> get_facts_as_of/get_graph_diff)
through CoreDB's generalized sync_snapshot/as_of/diff, to confirm the
generalization (arbitrary subject/predicate instead of a hardcoded hub)
didn't lose the bitemporal semantics already proven there:
  - relationships not seen before are opened
  - ones no longer present are closed at the last day they were confirmed,
    not the day they disappeared
  - a relationship can close and reopen as two distinct intervals
  - diff scans the fact log directly, so an object can legitimately appear
    in both 'opened' and 'closed' if it has two different intervals
"""

HUB = "NVIDIA"
PRED = "CO_OCCURS_WITH"


def _ingest(db, day, objects):
    return db.sync_snapshot(HUB, PRED, {o: 1.0 for o in objects}, day)


def test_hub_scenario_matches_documented_db_py_semantics(db):
    _ingest(db, "2026-01-01", {"AMD", "AI", "RTX"})
    _ingest(db, "2026-01-02", {"AMD", "AI", "RTX"})       # all confirmed
    _ingest(db, "2026-01-03", {"AI", "RTX", "Apple"})      # AMD drops, Apple opens
    _ingest(db, "2026-01-04", {"AI", "Apple"})             # RTX drops
    _ingest(db, "2026-01-05", {"AI", "Apple", "RTX"})      # RTX reopens as a new interval

    # AMD: closed at the last day it was actually confirmed (01-02), not the
    # day it disappeared (01-03) - the exact behavior db.py's docstring calls out.
    amd_history = db.history((HUB, PRED, "AMD"))
    assert len(amd_history) == 1
    assert amd_history[0].valid_from == "2026-01-01"
    assert amd_history[0].valid_to == "2026-01-02"

    # RTX: two disjoint intervals, not one merged/overwritten fact.
    rtx_history = db.history((HUB, PRED, "RTX"))
    assert len(rtx_history) == 2
    assert rtx_history[0].valid_from == "2026-01-01" and rtx_history[0].valid_to == "2026-01-03"
    assert rtx_history[1].valid_from == "2026-01-05" and rtx_history[1].valid_to is None

    # AI never drops - one continuously-open interval across all five days.
    ai_history = db.history((HUB, PRED, "AI"))
    assert len(ai_history) == 1
    assert ai_history[0].valid_to is None

    result = db.diff("2026-01-01", "2026-01-05", pattern=(HUB, PRED, None))
    opened_objects = [f.object_id for f in result["opened"]]
    closed_objects = [f.object_id for f in result["closed"]]
    persisted_objects = [f.object_id for f in result["persisted"]]

    assert sorted(opened_objects) == ["Apple", "RTX"]
    assert sorted(closed_objects) == ["AMD", "RTX"]
    assert persisted_objects == ["AI"]

    # as_of a date never separately ingested still reflects the graph as it
    # stood - AS OF is a derived view, not a per-day stored row.
    facts_mid = db.as_of((HUB, PRED, None), "2026-01-03")
    assert {f.object_id for f in facts_mid} == {"AI", "RTX", "Apple"}
