"""Walks a parsed AST node and executes it against a Database, returning
plain dict/list[dict] results - no HTTP/JSON-response shaping, since this
is a library concern, not a service concern.
"""
import operator

from ..errors import ValidationError
from .ast_nodes import (
    AssertStatement, ChangepointsQuery, DiffQuery, FirstConnectedQuery, HistoryQuery,
    MatchQuery, PathHistoryQuery, PathQuery, Pattern, RangeQuery, RetractStatement,
    SeriesQuery, TrackQuery, WhereClause, WhyChangedQuery,
)

_FIELD_BY_POSITION = ("subject_id", "predicate", "object_id")

_COMPARATORS = {
    ">": operator.gt, "<": operator.lt, ">=": operator.ge, "<=": operator.le,
    "=": operator.eq, "!=": operator.ne,
}


def _pattern_values(pattern: Pattern) -> tuple:
    return tuple(t.value for t in pattern)


def _require_literal_pattern(pattern: Pattern) -> tuple:
    """ASSERT/RETRACT/WHY_CHANGED each operate on one specific triple - '?'
    wildcards don't make sense there."""
    if any(term.is_var for term in pattern):
        raise ValidationError("this statement's pattern must be fully literal - '?' wildcards aren't allowed")
    return _pattern_values(pattern)


def _apply_where(versions: list, where: WhereClause | None) -> list:
    if where is None:
        return versions
    op = _COMPARATORS[where.comparator]
    return [v for v in versions if v.confidence is not None and op(v.confidence, where.value)]


def _apply_limit(items: list, limit: int | None) -> list:
    return items if limit is None else items[:limit]


def _bindings(version, pattern: Pattern) -> dict:
    bindings = {}
    for term, field_name in zip(pattern, _FIELD_BY_POSITION):
        if term.is_var:
            bindings[term.var_name] = getattr(version, field_name)
    return bindings


def _row(version, pattern: Pattern | None = None) -> dict:
    d = version.to_dict()
    if pattern is not None:
        d["bindings"] = _bindings(version, pattern)
    return d


def execute(db, ast) -> list[dict] | dict:
    if isinstance(ast, MatchQuery):
        pattern_vals = _pattern_values(ast.pattern)
        versions = (db.as_known(pattern_vals, ast.on_date, ast.known_by)
                    if ast.known_by is not None else db.as_of(pattern_vals, ast.on_date))
        versions = _apply_limit(_apply_where(versions, ast.where), ast.limit)
        return [_row(v, ast.pattern) for v in versions]

    if isinstance(ast, HistoryQuery):
        pattern_vals = _pattern_values(ast.pattern)
        versions = db.history(pattern_vals, ast.start, ast.end)
        versions = _apply_limit(_apply_where(versions, ast.where), ast.limit)
        return [_row(v, ast.pattern) for v in versions]

    if isinstance(ast, DiffQuery):
        pattern_vals = _pattern_values(ast.pattern) if ast.pattern is not None else None
        delta = db.diff_delta(pattern_vals, ast.date_from, ast.date_to)
        return {
            "date_from": delta.date_from,
            "date_to": delta.date_to,
            "nodes_added": delta.nodes_added,
            "nodes_removed": delta.nodes_removed,
            "edges_opened": [_row(v, ast.pattern) for v in delta.edges_opened],
            "edges_closed": [_row(v, ast.pattern) for v in delta.edges_closed],
            "edges_persisted": [_row(v, ast.pattern) for v in delta.edges_persisted],
        }

    if isinstance(ast, RangeQuery):
        subject, predicate, obj = _pattern_values(ast.pattern)
        day_counts = db.range_agg((subject, predicate, obj), ast.start, ast.end)
        # Mirror engine.range_agg's own key_field choice: aggregated by
        # subject_id for a reverse pattern (object bound, subject wildcard),
        # object_id otherwise - see that method's docstring for why.
        key_label = "subject_id" if subject is None and obj is not None else "object_id"
        return [{key_label: k, "dayCount": count} for k, count in day_counts.items()]

    if isinstance(ast, SeriesQuery):
        pattern_vals = _pattern_values(ast.pattern)
        gs = db.series(pattern_vals, ast.start, ast.end, ast.resolution_days)
        return [{"date": d, "facts": [_row(v, ast.pattern) for v in snapshot]} for d, snapshot in gs]

    if isinstance(ast, AssertStatement):
        subject, predicate, obj = _require_literal_pattern(ast.pattern)
        vid = db.assert_fact(subject, predicate, obj, ast.valid_from, confidence=ast.confidence)
        return {"version_id": vid}

    if isinstance(ast, RetractStatement):
        subject, predicate, obj = _require_literal_pattern(ast.pattern)
        vid = db.retract_fact(subject, predicate, obj, ast.valid_to)
        return {"version_id": vid}

    if isinstance(ast, TrackQuery):
        signal = db.track(ast.metric, ast.target, ast.start, ast.end,
                           ast.resolution_days, max_depth=ast.max_depth)
        return [{"date": d, "value": v} for d, v in signal.points]

    if isinstance(ast, PathQuery):
        path = db.path_exists(ast.a, ast.b, ast.on_date, max_depth=ast.max_depth)
        return {"connected": path is not None, "path": [v.to_dict() for v in path] if path is not None else None}

    if isinstance(ast, FirstConnectedQuery):
        date = db.first_connected(ast.a, ast.b, start=ast.start, end=ast.end, max_depth=ast.max_depth)
        return {"first_connected": date}

    if isinstance(ast, PathHistoryQuery):
        return db.path_history(ast.a, ast.b, ast.start, ast.end,
                                resolution_days=ast.resolution_days, max_depth=ast.max_depth)

    if isinstance(ast, WhyChangedQuery):
        subject, predicate, obj = _require_literal_pattern(ast.pattern)
        return db.why_changed(subject, predicate, obj, ast.date_from, ast.date_to)

    if isinstance(ast, ChangepointsQuery):
        dates = db.changepoints(ast.metric, ast.target, ast.start, ast.end,
                                 ast.resolution_days, max_depth=ast.max_depth)
        return {"changepoints": dates}

    raise TypeError(f"unknown AST node: {ast!r}")
