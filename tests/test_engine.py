def test_assert_fact_opens_interval(db):
    vid = db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01", confidence=0.9)
    facts = db.as_of(("NVIDIA", "SUPPLIED_BY", "TSMC"), "2026-01-01")
    assert len(facts) == 1
    assert facts[0].version_id == vid
    assert facts[0].valid_to is None
    assert facts[0].confidence == 0.9


def test_assert_fact_reopen_reuses_same_relationship_id(db):
    # Closing and reopening the same triple should produce two different
    # versions under the same stable relationship_id, not an unrelated one.
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01")
    first = db.history(("NVIDIA", "SUPPLIED_BY", "TSMC"))[0]
    db.retract_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-05")
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-02-01")
    versions = db.history(("NVIDIA", "SUPPLIED_BY", "TSMC"))
    assert len(versions) == 2
    assert versions[0].relationship_id == first.relationship_id == versions[1].relationship_id
    assert versions[0].version_id != versions[1].version_id


def test_assert_fact_confirms_existing_open_instead_of_duplicating(db):
    fid1 = db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01", confidence=0.5)
    fid2 = db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-05", confidence=0.9)
    assert fid1 == fid2
    facts = db.history(("NVIDIA", "SUPPLIED_BY", "TSMC"))
    assert len(facts) == 1
    assert facts[0].last_confirmed == "2026-01-05"
    assert facts[0].confidence == 0.9


def test_retract_fact_closes_interval(db):
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01")
    fid = db.retract_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-10")
    assert fid is not None
    assert db.as_of(("NVIDIA", "SUPPLIED_BY", "TSMC"), "2026-01-15") == []
    facts = db.as_of(("NVIDIA", "SUPPLIED_BY", "TSMC"), "2026-01-10")
    assert len(facts) == 1
    assert facts[0].valid_to == "2026-01-10"


def test_retract_fact_with_nothing_open_returns_none(db):
    assert db.retract_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-10") is None


def test_sync_snapshot_open_close_confirm(db):
    r1 = db.sync_snapshot("NVIDIA", "CO_OCCURS_WITH", {"AI": 5, "Google": 2}, "2026-01-01")
    assert set(r1["opened"]) and not r1["closed"] and not r1["confirmed"]

    r2 = db.sync_snapshot("NVIDIA", "CO_OCCURS_WITH", {"AI": 6, "Samsung": 1}, "2026-01-03")
    assert len(r2["opened"]) == 1   # Samsung
    assert len(r2["closed"]) == 1   # Google
    assert len(r2["confirmed"]) == 1  # AI

    google = db.history(("NVIDIA", "CO_OCCURS_WITH", "Google"))[0]
    assert google.valid_from == "2026-01-01"
    assert google.valid_to == "2026-01-01"  # closed at last_confirmed, not the day it disappeared

    ai = db.history(("NVIDIA", "CO_OCCURS_WITH", "AI"))[0]
    assert ai.valid_to is None
    assert ai.last_confirmed == "2026-01-03"


def test_as_of_bridges_an_ingestion_gap(db):
    # A fact open before a gap (no data for 01-02) and still open after it
    # should read as continuously active on the gap day itself.
    db.sync_snapshot("NVIDIA", "CO_OCCURS_WITH", {"AI": 5}, "2026-01-01")
    db.sync_snapshot("NVIDIA", "CO_OCCURS_WITH", {"AI": 5}, "2026-01-03")
    facts = db.as_of(("NVIDIA", "CO_OCCURS_WITH", "AI"), "2026-01-02")
    assert len(facts) == 1
    assert facts[0].valid_to is None


def test_as_of_excludes_before_valid_from_and_after_valid_to(db):
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-05")
    db.retract_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-10")
    assert db.as_of(("NVIDIA", "SUPPLIED_BY", "TSMC"), "2026-01-04") == []
    assert len(db.as_of(("NVIDIA", "SUPPLIED_BY", "TSMC"), "2026-01-05")) == 1
    assert len(db.as_of(("NVIDIA", "SUPPLIED_BY", "TSMC"), "2026-01-10")) == 1
    assert db.as_of(("NVIDIA", "SUPPLIED_BY", "TSMC"), "2026-01-11") == []


def test_diff_catches_facts_opened_and_closed_entirely_inside_window(db):
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "Ephemeral", "2026-01-03")
    db.retract_fact("NVIDIA", "SUPPLIED_BY", "Ephemeral", "2026-01-05")

    result = db.diff("2026-01-01", "2026-01-10", pattern=("NVIDIA", "SUPPLIED_BY", "Ephemeral"))
    assert any(f.object_id == "Ephemeral" for f in result["opened"])
    assert any(f.object_id == "Ephemeral" for f in result["closed"])
    assert result["persisted"] == []


def test_diff_global_scan_without_pattern(db):
    db.sync_snapshot("NVIDIA", "CO_OCCURS_WITH", {"AI": 1, "Google": 1}, "2026-01-01")
    db.sync_snapshot("NVIDIA", "CO_OCCURS_WITH", {"AI": 1, "Samsung": 1}, "2026-01-03")
    result = db.diff("2026-01-01", "2026-01-03")
    opened_objects = {f.object_id for f in result["opened"]}
    closed_objects = {f.object_id for f in result["closed"]}
    persisted_objects = {f.object_id for f in result["persisted"]}
    assert opened_objects == {"Samsung"}
    assert closed_objects == {"Google"}
    assert persisted_objects == {"AI"}


def test_range_agg_sums_disjoint_intervals(db):
    # AI appears, drops off, then reappears - two disjoint intervals inside
    # the window should be summed into one day count, not overwritten.
    db.assert_fact("NVIDIA", "CO_OCCURS_WITH", "AI", "2026-01-01")
    db.retract_fact("NVIDIA", "CO_OCCURS_WITH", "AI", "2026-01-02")
    db.assert_fact("NVIDIA", "CO_OCCURS_WITH", "AI", "2026-01-05")
    db.retract_fact("NVIDIA", "CO_OCCURS_WITH", "AI", "2026-01-06")

    counts = db.range_agg(("NVIDIA", "CO_OCCURS_WITH", None), "2026-01-01", "2026-01-10")
    assert counts["AI"] == 4  # (01-01..01-02) + (01-05..01-06) = 2 + 2


def _set_version_times(db, version_id, system_from=None, system_to=None):
    # Real datetime.now() calls a few lines apart can land on the same tick
    # at this clock's resolution, which would make these tests flaky - drive
    # system_from/system_to directly instead of racing the wall clock.
    with db._store.txn(write=True) as t:
        version = db._load_version(t, version_id)
        if system_from is not None:
            version.system_from = system_from
        if system_to is not None:
            version.system_to = system_to
        db._store_version(t, version)


def test_as_known_excludes_facts_not_yet_observed(db):
    vid = db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01")
    _set_version_times(db, vid, system_from="2026-01-02T00:00:00+00:00")

    assert db.as_known(("NVIDIA", "SUPPLIED_BY", "TSMC"), "2026-01-01", "2026-01-01T00:00:00+00:00") == []
    assert len(db.as_known(("NVIDIA", "SUPPLIED_BY", "TSMC"), "2026-01-01", "2026-01-03T00:00:00+00:00")) == 1


def test_as_known_excludes_facts_already_superseded_by_cutoff(db):
    vid = db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01")
    db.retract_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-05")
    _set_version_times(db, vid, system_from="2026-01-01T00:00:00+00:00", system_to="2026-01-06T00:00:00+00:00")

    assert len(db.as_known(("NVIDIA", "SUPPLIED_BY", "TSMC"), "2026-01-01", "2026-01-05T00:00:00+00:00")) == 1
    assert db.as_known(("NVIDIA", "SUPPLIED_BY", "TSMC"), "2026-01-01", "2026-01-07T00:00:00+00:00") == []


def test_history_sorted_and_filtered_by_range(db):
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-05")
    db.retract_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-10")
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-02-01")

    full = db.history(("NVIDIA", "SUPPLIED_BY", "TSMC"))
    assert [f.valid_from for f in full] == ["2026-01-05", "2026-02-01"]

    scoped = db.history(("NVIDIA", "SUPPLIED_BY", "TSMC"), start="2026-01-20", end="2026-02-28")
    assert [f.valid_from for f in scoped] == ["2026-02-01"]


def test_reverse_pattern_lookup_by_object(db):
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01")
    db.assert_fact("AMD", "SUPPLIED_BY", "TSMC", "2026-01-01")
    facts = db.as_of((None, "SUPPLIED_BY", "TSMC"), "2026-01-02")
    subjects = {f.subject_id for f in facts}
    assert subjects == {"NVIDIA", "AMD"}


def test_entity_first_and_last_seen_tracked(db):
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-05")
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-10")
    ent = db.get_entity("NVIDIA")
    assert ent.first_seen == "2026-01-05"
    assert ent.last_seen == "2026-01-10"


def test_assert_fact_with_sources_creates_assertions(db):
    vid = db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01", confidence=0.8,
                          sources=["https://example.com/a", {"url": "https://example.com/b", "title": "B"}])
    version = db.history(("NVIDIA", "SUPPLIED_BY", "TSMC"))[0]
    assert version.version_id == vid
    assert len(version.assertion_ids) == 2


def test_diff_delta_nets_out_churned_object(db):
    # An object whose relationship both closes and reopens inside the
    # window (like RTX leaving and rejoining) shouldn't show up as both
    # added and removed - it nets out to neither.
    db.sync_snapshot("NVIDIA", "CO_OCCURS_WITH", {"AMD": 1, "AI": 1, "RTX": 1}, "2026-01-01")
    db.sync_snapshot("NVIDIA", "CO_OCCURS_WITH", {"AI": 1, "RTX": 1, "Apple": 1}, "2026-01-03")
    db.sync_snapshot("NVIDIA", "CO_OCCURS_WITH", {"AI": 1, "Apple": 1}, "2026-01-04")
    db.sync_snapshot("NVIDIA", "CO_OCCURS_WITH", {"AI": 1, "Apple": 1, "RTX": 1}, "2026-01-05")

    delta = db.diff_delta(("NVIDIA", "CO_OCCURS_WITH", None), "2026-01-01", "2026-01-05")
    assert delta.nodes_added == ["Apple"]
    assert delta.nodes_removed == ["AMD"]
    edge_opened_objects = [v.object_id for v in delta.edges_opened]
    edge_closed_objects = [v.object_id for v in delta.edges_closed]
    assert sorted(edge_opened_objects) == ["Apple", "RTX"]
    assert sorted(edge_closed_objects) == ["AMD", "RTX"]


def test_graph_series_at_and_iteration(db):
    db.sync_snapshot("NVIDIA", "CO_OCCURS_WITH", {"AI": 1}, "2026-01-01")
    db.sync_snapshot("NVIDIA", "CO_OCCURS_WITH", {"AI": 1, "AMD": 1}, "2026-01-03")

    gs = db.series(("NVIDIA", "CO_OCCURS_WITH", None), "2026-01-01", "2026-01-03")
    assert {v.object_id for v in gs.at("2026-01-01")} == {"AI"}
    assert {v.object_id for v in gs.at("2026-01-03")} == {"AI", "AMD"}

    steps = list(gs)
    assert [d for d, _ in steps] == ["2026-01-01", "2026-01-02", "2026-01-03"]

    delta = gs.diff()
    assert delta.nodes_added == ["AMD"]


def test_graph_series_resolution_days(db):
    db.sync_snapshot("NVIDIA", "CO_OCCURS_WITH", {"AI": 1}, "2026-01-01")
    gs = db.series(("NVIDIA", "CO_OCCURS_WITH", None), "2026-01-01", "2026-01-10", resolution_days=5)
    assert list(gs.dates()) == ["2026-01-01", "2026-01-06"]
