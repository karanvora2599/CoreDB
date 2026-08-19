import pytest

import coredb
from coredb.query.ast_nodes import (
    AssertStatement, DiffQuery, HistoryQuery, MatchQuery, RangeQuery, RetractStatement, SeriesQuery,
    TrackQuery,
)
from coredb.query.parser import parse


def test_parse_match_as_of():
    ast = parse("MATCH (NVIDIA, ?p, ?o) AS OF '2026-03-05'")
    assert isinstance(ast, MatchQuery)
    assert ast.pattern[0].value == "NVIDIA"
    assert ast.pattern[1].var_name == "p"
    assert ast.pattern[2].var_name == "o"
    assert ast.on_date == "2026-03-05"
    assert ast.known_by is None


def test_parse_match_as_of_known_by():
    ast = parse("MATCH (NVIDIA, ?p, ?o) AS OF '2026-03-05' KNOWN BY '2026-03-06T00:00:00'")
    assert ast.known_by == "2026-03-06T00:00:00"


def test_parse_history_with_and_without_range():
    ast = parse("HISTORY (NVIDIA, SUPPLIED_BY, TSMC)")
    assert isinstance(ast, HistoryQuery)
    assert ast.start is None and ast.end is None

    ast2 = parse("HISTORY (NVIDIA, SUPPLIED_BY, TSMC) BETWEEN '2026-01-01' AND '2026-06-30'")
    assert ast2.start == "2026-01-01" and ast2.end == "2026-06-30"


def test_parse_diff_with_and_without_pattern():
    ast = parse("DIFF BETWEEN '2026-01-08' AND '2026-01-13'")
    assert isinstance(ast, DiffQuery)
    assert ast.pattern is None

    ast2 = parse("DIFF BETWEEN '2026-01-08' AND '2026-01-13' FOR (NVIDIA, ?p, ?o)")
    assert ast2.pattern is not None
    assert ast2.pattern[0].value == "NVIDIA"


def test_parse_range():
    ast = parse("RANGE (NVIDIA, CO_OCCURS_WITH, ?o) BETWEEN '2026-01-01' AND '2026-01-31'")
    assert isinstance(ast, RangeQuery)
    assert ast.start == "2026-01-01" and ast.end == "2026-01-31"


def test_parse_series_default_and_explicit_resolution():
    ast = parse("SERIES (NVIDIA, CO_OCCURS_WITH, ?o) BETWEEN '2026-01-01' AND '2026-01-10'")
    assert isinstance(ast, SeriesQuery)
    assert ast.resolution_days == 1

    ast2 = parse("SERIES (NVIDIA, CO_OCCURS_WITH, ?o) BETWEEN '2026-01-01' AND '2026-01-10' RESOLUTION '5d'")
    assert ast2.resolution_days == 5


def test_parse_assert_and_retract():
    ast = parse("ASSERT (NVIDIA, SUPPLIED_BY, TSMC) VALID FROM '2026-01-01' CONFIDENCE 0.9")
    assert isinstance(ast, AssertStatement)
    assert ast.pattern[2].value == "TSMC"
    assert ast.valid_from == "2026-01-01"
    assert ast.confidence == 0.9

    ast2 = parse("ASSERT (NVIDIA, SUPPLIED_BY, TSMC) VALID FROM '2026-01-01'")
    assert ast2.confidence is None

    ast3 = parse("RETRACT (NVIDIA, SUPPLIED_BY, TSMC) VALID TO '2026-06-01'")
    assert isinstance(ast3, RetractStatement)
    assert ast3.valid_to == "2026-06-01"


def test_parse_where_and_limit_on_match_and_history():
    ast = parse("MATCH (NVIDIA, ?p, ?o) AS OF '2026-01-01' WHERE confidence > 0.5 LIMIT 3")
    assert ast.where.comparator == ">" and ast.where.value == 0.5
    assert ast.limit == 3

    ast2 = parse("HISTORY (NVIDIA, SUPPLIED_BY, TSMC) WHERE confidence >= 0.9")
    assert ast2.where.comparator == ">=" and ast2.where.value == 0.9
    assert ast2.limit is None


def test_parse_ignores_line_comments():
    ast = parse("""
    // a leading comment
    MATCH (NVIDIA, ?p, ?o) AS OF '2026-01-01' // trailing comment
    """)
    assert isinstance(ast, MatchQuery)
    assert ast.on_date == "2026-01-01"


def test_parse_raises_query_error_on_malformed_syntax():
    with pytest.raises(coredb.QueryError):
        parse("NOT VALID TGQL AT ALL")


def test_parse_raises_query_error_on_unsupported_resolution_unit():
    with pytest.raises(coredb.QueryError):
        parse("SERIES (X, REL, ?o) BETWEEN '2026-01-01' AND '2026-01-10' RESOLUTION '5w'")


def test_parse_track_degree_and_weighted_degree():
    ast = parse("TRACK DEGREE(NVIDIA) BETWEEN '2026-01-01' AND '2026-01-10'")
    assert isinstance(ast, TrackQuery)
    assert ast.metric == "degree"
    assert ast.target == "NVIDIA"
    assert ast.start == "2026-01-01" and ast.end == "2026-01-10"
    assert ast.resolution_days == 1

    ast2 = parse("TRACK WEIGHTED_DEGREE(NVIDIA) BETWEEN '2026-01-01' AND '2026-01-10' RESOLUTION '7d'")
    assert ast2.metric == "weighted_degree"
    assert ast2.resolution_days == 7


def test_parse_track_edge_weight():
    ast = parse("TRACK EDGE_WEIGHT(NVIDIA, SUPPLIED_BY, TSMC) BETWEEN '2026-01-01' AND '2026-01-10'")
    assert ast.metric == "edge_weight"
    assert ast.target == ("NVIDIA", "SUPPLIED_BY", "TSMC")


def test_parse_track_rejects_unknown_metric_and_wrong_arity():
    with pytest.raises(coredb.QueryError):
        parse("TRACK BETWEENNESS(NVIDIA) BETWEEN '2026-01-01' AND '2026-01-10'")
    with pytest.raises(coredb.QueryError):
        parse("TRACK DEGREE(NVIDIA, SUPPLIED_BY, TSMC) BETWEEN '2026-01-01' AND '2026-01-10'")


def test_executor_match_matches_direct_engine_call(db):
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01")
    direct = db.as_of(("NVIDIA", "SUPPLIED_BY", None), "2026-01-05")
    rows = db.execute("MATCH (NVIDIA, SUPPLIED_BY, ?o) AS OF '2026-01-05'")
    assert len(rows) == len(direct) == 1
    assert rows[0]["bindings"]["o"] == "TSMC"
    assert rows[0]["object_id"] == direct[0].object_id


def test_executor_history_matches_direct_engine_call(db):
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01")
    db.retract_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-10")
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-02-01")

    direct = db.history(("NVIDIA", "SUPPLIED_BY", "TSMC"))
    rows = db.execute("HISTORY (NVIDIA, SUPPLIED_BY, TSMC)")
    assert [r["valid_from"] for r in rows] == [f.valid_from for f in direct]


def test_executor_diff_matches_direct_engine_call(db):
    db.sync_snapshot("NVIDIA", "CO_OCCURS_WITH", {"AI": 1, "Google": 1}, "2026-01-01")
    db.sync_snapshot("NVIDIA", "CO_OCCURS_WITH", {"AI": 1, "Samsung": 1}, "2026-01-03")

    delta = db.execute(
        "DIFF BETWEEN '2026-01-01' AND '2026-01-03' FOR (NVIDIA, CO_OCCURS_WITH, ?o)"
    )
    assert delta["nodes_added"] == ["Samsung"]
    assert delta["nodes_removed"] == ["Google"]
    assert {r["bindings"]["o"] for r in delta["edges_opened"]} == {"Samsung"}
    assert {r["bindings"]["o"] for r in delta["edges_closed"]} == {"Google"}
    assert {r["bindings"]["o"] for r in delta["edges_persisted"]} == {"AI"}


def test_executor_range_matches_direct_engine_call(db):
    db.sync_snapshot("NVIDIA", "CO_OCCURS_WITH", {"AI": 1}, "2026-01-01")
    db.sync_snapshot("NVIDIA", "CO_OCCURS_WITH", {"AI": 1}, "2026-01-03")

    direct = db.range_agg(("NVIDIA", "CO_OCCURS_WITH", None), "2026-01-01", "2026-01-03")
    rows = db.execute("RANGE (NVIDIA, CO_OCCURS_WITH, ?o) BETWEEN '2026-01-01' AND '2026-01-03'")
    assert {r["object_id"]: r["dayCount"] for r in rows} == direct


def test_executor_range_labels_reverse_pattern_by_subject(db):
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01")
    db.assert_fact("AMD", "SUPPLIED_BY", "TSMC", "2026-01-01")

    rows = db.execute("RANGE (?s, SUPPLIED_BY, TSMC) BETWEEN '2026-01-01' AND '2026-01-05'")
    assert all("subject_id" in r and "object_id" not in r for r in rows)
    assert {r["subject_id"] for r in rows} == {"NVIDIA", "AMD"}


def test_executor_series_matches_direct_engine_call(db):
    db.sync_snapshot("NVIDIA", "CO_OCCURS_WITH", {"AI": 1}, "2026-01-01")
    db.sync_snapshot("NVIDIA", "CO_OCCURS_WITH", {"AI": 1, "AMD": 1}, "2026-01-03")

    rows = db.execute("SERIES (NVIDIA, CO_OCCURS_WITH, ?o) BETWEEN '2026-01-01' AND '2026-01-03'")
    assert [r["date"] for r in rows] == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert {f["bindings"]["o"] for f in rows[0]["facts"]} == {"AI"}
    assert {f["bindings"]["o"] for f in rows[2]["facts"]} == {"AI", "AMD"}


def test_executor_assert_and_retract_mutate_the_database(db):
    result = db.execute("ASSERT (NVIDIA, SUPPLIED_BY, TSMC) VALID FROM '2026-01-01' CONFIDENCE 0.9")
    assert "version_id" in result
    facts = db.as_of(("NVIDIA", "SUPPLIED_BY", "TSMC"), "2026-01-05")
    assert len(facts) == 1 and facts[0].confidence == 0.9

    db.execute("RETRACT (NVIDIA, SUPPLIED_BY, TSMC) VALID TO '2026-06-01'")
    assert db.as_of(("NVIDIA", "SUPPLIED_BY", "TSMC"), "2026-07-01") == []


def test_executor_assert_rejects_variable_pattern(db):
    with pytest.raises(coredb.ValidationError):
        db.execute("ASSERT (NVIDIA, ?p, TSMC) VALID FROM '2026-01-01'")


def test_executor_where_and_limit_filter_results(db):
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01", confidence=0.9)
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "Samsung", "2026-01-01", confidence=0.3)
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "Intel", "2026-01-01", confidence=0.95)

    rows = db.execute("HISTORY (NVIDIA, SUPPLIED_BY, ?o) WHERE confidence >= 0.5")
    assert {r["bindings"]["o"] for r in rows} == {"TSMC", "Intel"}

    limited = db.execute("HISTORY (NVIDIA, SUPPLIED_BY, ?o) LIMIT 1")
    assert len(limited) == 1


def test_executor_track_matches_direct_engine_call(db):
    db.assert_fact("NVIDIA", "SUPPLIED_BY", "TSMC", "2026-01-01", confidence=0.5)

    direct = db.track("degree", "NVIDIA", "2026-01-01", "2026-01-03")
    rows = db.execute("TRACK DEGREE(NVIDIA) BETWEEN '2026-01-01' AND '2026-01-03'")
    assert [(r["date"], r["value"]) for r in rows] == direct.points
