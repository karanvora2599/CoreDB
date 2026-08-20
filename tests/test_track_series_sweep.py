"""Equivalence tests for the interval-sweep rewrite of TRACK (degree/
weighted_degree/edge_weight) and SERIES: the new O(H + D) sweep-based
implementations must produce results identical to the old O(D x H)
per-date reconstructions, which still exist as db.degree()/db.edge_weight()/
db.as_of() and serve as the ground-truth oracle here."""
import pytest

from coredb.series import date_range


def test_track_degree_matches_per_date_degree(db):
    # Multiple distinct relationships touching Hub, opening/closing/
    # reopening on different days (not the same-day boundary edge case).
    db.assert_fact("Hub", "TOUCHES", "A", "2020-01-01")
    db.retract_fact("Hub", "TOUCHES", "A", "2020-01-10")
    db.assert_fact("Hub", "TOUCHES", "A", "2020-01-15")
    db.assert_fact("Hub", "TOUCHES", "B", "2020-01-05", confidence=0.7)
    db.retract_fact("Hub", "TOUCHES", "B", "2020-01-20")
    db.assert_fact("Hub", "LINKED_WITH", "C", "2020-01-08")

    dates = list(date_range("2020-01-01", "2020-01-25"))
    expected = [(d, db.degree("Hub", d)) for d in dates]
    signal = db.track("degree", "Hub", "2020-01-01", "2020-01-25")
    assert signal.points == expected


def test_track_weighted_degree_matches_per_date_degree(db):
    db.assert_fact("Hub", "TOUCHES", "A", "2020-01-01", confidence=0.4)
    db.retract_fact("Hub", "TOUCHES", "A", "2020-01-10")
    db.assert_fact("Hub", "TOUCHES", "A", "2020-01-15", confidence=0.9)
    db.assert_fact("Hub", "TOUCHES", "B", "2020-01-05", confidence=0.7)

    dates = list(date_range("2020-01-01", "2020-01-20"))
    expected = [(d, db.degree("Hub", d, weighted=True)) for d in dates]
    signal = db.track("weighted_degree", "Hub", "2020-01-01", "2020-01-20")
    # Mathematically equal, not necessarily bit-identical: the sweep
    # accumulates running +=/-= adjustments while degree() re-sums from
    # scratch each call, and float addition isn't associative - the two
    # orderings can differ in the last bit (e.g. 1.1 - 0.4 != 0.7 exactly).
    assert [d for d, _ in signal.points] == [d for d, _ in expected]
    for (_, actual_v), (_, expected_v) in zip(signal.points, expected):
        assert actual_v == pytest.approx(expected_v)


def test_track_edge_weight_matches_per_date_edge_weight(db):
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2020-01-01", confidence=0.5)
    db.retract_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2020-01-10")
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2020-01-15", confidence=0.8)

    dates = list(date_range("2020-01-01", "2020-01-25"))
    expected = [(d, db.edge_weight("NVIDIA", "SUPPLIED_BY", "TSMC", d)) for d in dates]
    signal = db.track("edge_weight", ("NVIDIA", "SUPPLIED_BY", "TSMC"), "2020-01-01", "2020-01-25")
    assert signal.points == expected
    # A real gap (nothing open) must surface as None, not 0.0 or a stale value.
    assert dict(signal.points)["2020-01-12"] is None


def test_series_iteration_matches_per_date_as_of(db):
    db.assert_fact("Hub", "PEER_OF", "A", "2020-01-01")
    db.retract_fact("Hub", "PEER_OF", "A", "2020-01-10")
    db.assert_fact("Hub", "PEER_OF", "B", "2020-01-05")
    db.assert_fact("Hub", "PEER_OF", "C", "2020-01-12")

    pattern = ("Hub", "PEER_OF", None)
    dates = list(date_range("2020-01-01", "2020-01-20"))
    expected = [(d, {v.version_id for v in db.as_of(pattern, d)}) for d in dates]

    series = db.series(pattern, "2020-01-01", "2020-01-20")
    actual = [(d, {v.version_id for v in snapshot}) for d, snapshot in series]
    assert actual == expected


def test_track_degree_same_day_boundary_is_deterministic_not_a_crash(db):
    # retract then immediately reassert on the same date - valid_to is
    # inclusive, so both versions cover that boundary date even though
    # they were never simultaneously open in system time. The sweep must
    # resolve this deterministically (most-recently-opened wins), not
    # crash or silently double-count.
    db.assert_fact("Hub", "TOUCHES", "A", "2020-01-01", confidence=0.3)
    db.retract_fact("Hub", "TOUCHES", "A", "2020-01-10")
    db.assert_fact("Hub", "TOUCHES", "A", "2020-01-10", confidence=0.9)

    signal = db.track("weighted_degree", "Hub", "2020-01-01", "2020-01-15")
    points = dict(signal.points)
    # Exactly one relationship touches Hub throughout - degree must never
    # double-count it, even on the boundary date.
    degree_signal = db.track("degree", "Hub", "2020-01-01", "2020-01-15")
    assert all(v in (0.0, 1.0) for _, v in degree_signal.points)
    # The boundary date resolves to the newly (re)opened version's weight.
    assert points["2020-01-10"] == 0.9


def test_track_and_series_still_pass_dsl_layer(db):
    db.assert_fact("Hub", "TOUCHES", "A", "2020-01-01", confidence=0.6)
    result = db.execute("TRACK DEGREE(Hub) BETWEEN '2020-01-01' AND '2020-01-05'")
    expected_points = db.track("degree", "Hub", "2020-01-01", "2020-01-05").points
    assert [(r["date"], r["value"]) for r in result] == expected_points
    result = db.execute("SERIES (Hub, TOUCHES, ?o) BETWEEN '2020-01-01' AND '2020-01-05'")
    assert len(result) == 5
