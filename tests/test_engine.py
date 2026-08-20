import pytest

import coredb


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


def test_range_agg_aggregates_by_subject_for_a_reverse_pattern(db):
    # With the object bound and the subject wildcarded, every candidate
    # shares the same object_id - aggregating by object_id would collapse
    # NVIDIA's and AMD's day counts into one meaningless bucket instead of
    # keeping them separate by subject.
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01")
    db.retract_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-05")
    db.assert_fact("AMD", "SUPPLIED_BY", "TSMC", "2026-01-01")

    counts = db.range_agg((None, "SUPPLIED_BY", "TSMC"), "2026-01-01", "2026-01-10")
    assert counts == {"NVIDIA": 5, "AMD": 10}


def test_sync_snapshot_finds_currently_open_via_open_by_sp_idx(db):
    # Regression for the old O(all-history) _open_objects_for scan: after
    # several open/close cycles for unrelated objects, a fresh sync_snapshot
    # call must still resolve exactly the currently-open set.
    db.sync_snapshot("X", "REL", {"A": 1, "B": 1}, "2026-01-01")
    db.sync_snapshot("X", "REL", {"A": 1}, "2026-01-03")          # B closes
    result = db.sync_snapshot("X", "REL", {"A": 1, "C": 1}, "2026-01-05")  # C opens
    assert result["opened"] and not result["closed"]

    open_now = db.as_of(("X", "REL", None), "2026-01-05")
    assert sorted(v.object_id for v in open_now) == ["A", "C"]


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


def test_degree_counts_subject_and_object_side_without_double_counting(db):
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01")
    db.assert_fact("NVIDIA", "PARTNER_OF", "Microsoft", "2026-01-01")
    db.assert_fact("AMD", "SUPPLIED_BY", "TSMC", "2026-01-01")

    assert db.degree("NVIDIA", "2026-01-05") == 2.0   # both edges touch NVIDIA as subject
    assert db.degree("TSMC", "2026-01-05") == 2.0      # TSMC is the object of two edges

    # A self-loop (entity as both subject and object of one relationship)
    # must not be double-counted.
    db.assert_fact("Self", "SELF_REL", "Self", "2026-01-01")
    assert db.degree("Self", "2026-01-05") == 1.0


def test_weighted_degree_sums_confidence_treating_none_as_zero(db):
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01", confidence=0.9)
    db.assert_fact("NVIDIA", "PARTNER_OF", "Microsoft", "2026-01-01")  # no confidence -> 0.0
    assert db.degree("NVIDIA", "2026-01-05", weighted=True) == 0.9


def test_edge_weight_returns_none_when_not_open(db):
    assert db.edge_weight("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01") is None
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01", confidence=0.75)
    assert db.edge_weight("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-05") == 0.75
    db.retract_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-10")
    assert db.edge_weight("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-15") is None


def test_track_degree_and_edge_weight_point_sequences(db):
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01", confidence=0.5)
    db.assert_fact("NVIDIA", "PARTNER_OF", "Microsoft", "2026-01-03")

    degree_signal = db.track("degree", "NVIDIA", "2026-01-01", "2026-01-05")
    assert degree_signal.points == [
        ("2026-01-01", 1.0), ("2026-01-02", 1.0), ("2026-01-03", 2.0),
        ("2026-01-04", 2.0), ("2026-01-05", 2.0),
    ]

    weight_signal = db.track("edge_weight", ("NVIDIA", "SUPPLIED_BY", "TSMC"), "2026-01-01", "2026-01-03")
    assert weight_signal.points == [("2026-01-01", 0.5), ("2026-01-02", 0.5), ("2026-01-03", 0.5)]


def test_track_rejects_unknown_metric(db):
    with pytest.raises(coredb.ValidationError):
        db.track("eigenvector_centrality", "NVIDIA", "2026-01-01", "2026-01-05")


def test_path_exists_direct_and_multi_hop(db):
    db.assert_fact("NVIDIA", "INVESTS_IN", "CoreWeave", "2024-01-01")
    db.assert_fact("CoreWeave", "SUPPLIES", "Microsoft", "2024-06-01")
    db.assert_fact("Microsoft", "PARTNER_OF", "OpenAI", "2024-01-01")

    # Not yet connected: the CoreWeave->Microsoft edge hasn't opened.
    assert db.path_exists("NVIDIA", "OpenAI", "2024-03-01") is None

    path = db.path_exists("NVIDIA", "OpenAI", "2024-07-01")
    assert [(v.subject_id, v.object_id) for v in path] == [
        ("NVIDIA", "CoreWeave"), ("CoreWeave", "Microsoft"), ("Microsoft", "OpenAI"),
    ]

    # A direct 1-hop path exists too.
    assert [(v.subject_id, v.object_id) for v in db.path_exists("NVIDIA", "CoreWeave", "2024-07-01")] == [
        ("NVIDIA", "CoreWeave"),
    ]


def test_path_exists_respects_max_depth(db):
    db.assert_fact("NVIDIA", "INVESTS_IN", "CoreWeave", "2024-01-01")
    db.assert_fact("CoreWeave", "SUPPLIES", "Microsoft", "2024-01-01")
    db.assert_fact("Microsoft", "PARTNER_OF", "OpenAI", "2024-01-01")

    assert db.path_exists("NVIDIA", "OpenAI", "2024-06-01", max_depth=2) is None
    assert db.path_exists("NVIDIA", "OpenAI", "2024-06-01", max_depth=3) is not None


def test_path_exists_finds_shortest_path(db):
    db.assert_fact("NVIDIA", "INVESTS_IN", "CoreWeave", "2024-01-01")
    db.assert_fact("CoreWeave", "SUPPLIES", "Microsoft", "2024-01-01")
    db.assert_fact("Microsoft", "PARTNER_OF", "OpenAI", "2024-01-01")
    # A direct 1-hop shortcut opens later.
    db.assert_fact("NVIDIA", "PARTNER_OF", "Microsoft", "2025-01-01")

    path = db.path_exists("NVIDIA", "OpenAI", "2025-02-01")
    assert [(v.subject_id, v.object_id) for v in path] == [
        ("NVIDIA", "Microsoft"), ("Microsoft", "OpenAI"),
    ]


def test_path_exists_trivial_for_same_entity(db):
    assert db.path_exists("NVIDIA", "NVIDIA", "2024-01-01") == []


def test_path_exists_rejects_max_depth_out_of_range(db):
    with pytest.raises(coredb.ValidationError):
        db.path_exists("A", "B", "2024-01-01", max_depth=0)
    with pytest.raises(coredb.ValidationError):
        db.path_exists("A", "B", "2024-01-01", max_depth=11)


def test_first_connected_finds_earliest_date_the_path_becomes_possible(db):
    db.assert_fact("NVIDIA", "INVESTS_IN", "CoreWeave", "2024-01-01")
    db.assert_fact("Microsoft", "PARTNER_OF", "OpenAI", "2024-01-01")
    # The connecting edge opens well after either endpoint's own first edge.
    db.assert_fact("CoreWeave", "SUPPLIES", "Microsoft", "2024-06-01")

    assert db.first_connected("NVIDIA", "OpenAI") == "2024-06-01"


def test_first_connected_returns_none_when_never_connected(db):
    db.assert_fact("NVIDIA", "INVESTS_IN", "CoreWeave", "2024-01-01")
    assert db.first_connected("NVIDIA", "OpenAI") is None


def test_path_history_shows_path_emerging(db):
    db.assert_fact("NVIDIA", "INVESTS_IN", "CoreWeave", "2024-01-01")
    db.assert_fact("Microsoft", "PARTNER_OF", "OpenAI", "2024-01-01")
    db.assert_fact("CoreWeave", "SUPPLIES", "Microsoft", "2024-06-01")

    points = db.path_history("NVIDIA", "OpenAI", "2024-01-01", "2024-07-01", resolution_days=180)
    assert [p["date"] for p in points] == ["2024-01-01", "2024-06-29"]
    assert points[0]["path"] is None
    assert len(points[1]["path"]) == 3


def _star_graph(db, hub, leaves, date="2026-01-01"):
    for leaf in leaves:
        db.assert_fact(hub, "CONNECTED_TO", leaf, date)


def test_closeness_star_graph_hand_verified_values(db):
    _star_graph(db, "Hub", ["L1", "L2", "L3", "L4"])
    # Hub: distance 1 to each of 4 leaves -> 4 * (1/1) = 4.
    assert db.closeness("Hub", "2026-01-05") == 4.0
    # L1: distance 1 to Hub, distance 2 to each of the other 3 leaves via Hub
    # -> 1/1 + 3*(1/2) = 2.5.
    assert db.closeness("L1", "2026-01-05") == 2.5


def test_closeness_respects_max_depth(db):
    db.assert_fact("A", "L", "B", "2026-01-01")
    db.assert_fact("B", "L", "C", "2026-01-01")
    db.assert_fact("C", "L", "D", "2026-01-01")

    assert db.closeness("A", "2026-01-05", max_depth=1) == 1.0            # only B
    assert db.closeness("A", "2026-01-05", max_depth=3) == 1 + 0.5 + 1 / 3  # B, C, D


def test_betweenness_path_graph_hand_verified_values(db):
    # Classic textbook case: on path A-B-C, B lies on the only shortest path
    # between A and C, so B's betweenness is 1.0 and A/C's are 0.
    db.assert_fact("A", "LINK", "B", "2026-01-01")
    db.assert_fact("B", "LINK", "C", "2026-01-01")

    result = db.betweenness_all("2026-01-05")
    assert result == {"A": 0.0, "B": 1.0, "C": 0.0}


def test_betweenness_single_entity_matches_all(db):
    db.assert_fact("A", "LINK", "B", "2026-01-01")
    db.assert_fact("B", "LINK", "C", "2026-01-01")

    all_scores = db.betweenness_all("2026-01-05")
    assert db.betweenness("B", "2026-01-05") == all_scores["B"]
    assert db.betweenness("A", "2026-01-05") == all_scores["A"]


def test_betweenness_multi_edge_between_same_pair_is_not_double_counted(db):
    # Two distinct relationships between the same pair (different
    # predicates) must count as one graph edge, not inflate shortest-path
    # counts in Brandes' algorithm.
    db.assert_fact("A", "SUPPLIES", "B", "2026-01-01")
    db.assert_fact("A", "PARTNERS_WITH", "B", "2026-01-01")
    db.assert_fact("B", "LINK", "C", "2026-01-01")

    result = db.betweenness_all("2026-01-05")
    assert result == {"A": 0.0, "B": 1.0, "C": 0.0}


def test_pagerank_hub_outranks_leaves(db):
    db.assert_fact("L1", "LINKS_TO", "Hub", "2026-01-01")
    db.assert_fact("L2", "LINKS_TO", "Hub", "2026-01-01")

    ranks = db.pagerank_all("2026-01-05")
    assert abs(sum(ranks.values()) - 1.0) < 1e-6
    assert ranks["Hub"] > ranks["L1"]
    assert ranks["Hub"] > ranks["L2"]


def test_pagerank_single_entity_matches_all(db):
    db.assert_fact("L1", "LINKS_TO", "Hub", "2026-01-01")

    all_ranks = db.pagerank_all("2026-01-05")
    assert db.pagerank("Hub", "2026-01-05") == all_ranks["Hub"]


def test_active_entity_ids_is_scoped_by_date(db):
    db.assert_fact("X", "REL", "Y", "2020-01-01")
    db.retract_fact("X", "REL", "Y", "2020-06-01")
    db.assert_fact("P", "REL", "Q", "2026-01-01")

    with db._store.txn() as t:
        active_2020 = db._active_entity_ids(t, "2020-03-01")
        active_2026 = db._active_entity_ids(t, "2026-01-05")
    assert active_2020 == {"X", "Y"}
    assert active_2026 == {"P", "Q"}


def test_track_centrality_metrics_via_track(db):
    db.assert_fact("L1", "LINKS_TO", "Hub", "2026-01-01")
    db.assert_fact("L2", "LINKS_TO", "Hub", "2026-01-01")
    db.assert_fact("Hub", "CONNECTED_TO", "L1", "2026-01-01")
    db.assert_fact("Hub", "CONNECTED_TO", "L2", "2026-01-01")

    closeness_signal = db.track("closeness", "Hub", "2026-01-01", "2026-01-03")
    assert closeness_signal.points[0] == ("2026-01-01", db.closeness("Hub", "2026-01-01"))

    betweenness_signal = db.track("betweenness", "Hub", "2026-01-01", "2026-01-03")
    assert betweenness_signal.points[0] == ("2026-01-01", db.betweenness("Hub", "2026-01-01"))

    pagerank_signal = db.track("pagerank", "Hub", "2026-01-01", "2026-01-03")
    assert pagerank_signal.points[0] == ("2026-01-01", db.pagerank("Hub", "2026-01-01"))


def test_assertions_for_version_chronological_order(db):
    vid = db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01",
                          confidence=0.6, sources=["https://example.com/late", "https://example.com/early"])
    assertions = db.assertions_for_version(vid)
    assert len(assertions) == 2
    assert assertions == sorted(assertions, key=lambda a: a.ingested_at)


def test_why_changed_status_opened(db):
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-03-01")
    result = db.why_changed("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01", "2026-06-01")
    assert result["status"] == "opened"


def test_why_changed_status_closed(db):
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2025-01-01")
    db.retract_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-03-01")
    result = db.why_changed("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01", "2026-06-01")
    assert result["status"] == "closed"


def test_why_changed_status_persisted(db):
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2025-01-01")
    result = db.why_changed("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01", "2026-06-01")
    assert result["status"] == "persisted"


def test_why_changed_status_churned(db):
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01")
    db.retract_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-03-01")
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-04-01")
    result = db.why_changed("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01", "2026-06-01")
    assert result["status"] == "churned"


def test_why_changed_status_no_relationship(db):
    result = db.why_changed("X", "Y", "Z", "2026-01-01", "2026-06-01")
    assert result["status"] == "no_relationship"
    assert result["assertions"] == []


def test_why_changed_filters_assertions_by_event_time_window(db):
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01", confidence=0.6,
                    sources=["https://example.com/a"])
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-03-01", confidence=0.9,
                    sources=["https://example.com/b"])

    both = db.why_changed("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01", "2026-06-01")
    assert len(both["assertions"]) == 2

    narrow = db.why_changed("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01", "2026-01-15")
    assert len(narrow["assertions"]) == 1
    assert narrow["assertions"][0]["assertion"]["confidence"] == 0.6
    assert narrow["assertions"][0]["source"]["url"] == "https://example.com/a"


def test_why_changed_with_no_sources_given_has_no_provenance(db):
    # No sources -> _create_assertions never runs, so assertion_ids stays
    # empty. why_changed should report an empty evidence trail, not crash.
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01", confidence=0.6)
    result = db.why_changed("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01", "2026-06-01")
    assert result["assertions"] == []


def test_database_changepoints_matches_track_then_changepoints(db):
    db.sync_snapshot("NVIDIA", "PARTNER_OF", {"P1": 1}, "2026-01-01")
    db.sync_snapshot("NVIDIA", "PARTNER_OF", {"P1": 1}, "2026-01-08")
    db.sync_snapshot("NVIDIA", "PARTNER_OF", {"P1": 1, "P2": 1, "P3": 1, "P4": 1, "P5": 1}, "2026-01-15")
    db.sync_snapshot("NVIDIA", "PARTNER_OF", {"P1": 1, "P2": 1, "P3": 1, "P4": 1, "P5": 1}, "2026-01-22")

    direct = db.track("degree", "NVIDIA", "2026-01-01", "2026-01-22", resolution_days=7).changepoints()
    via_method = db.changepoints("degree", "NVIDIA", "2026-01-01", "2026-01-22", resolution_days=7)
    assert via_method == direct == ["2026-01-15"]
