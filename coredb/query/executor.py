"""Walks a parsed AST node and executes it against a Database, returning
plain dict/list[dict] results - no HTTP/JSON-response shaping, since this
is a library concern, not a service concern.
"""
from .ast_nodes import DiffQuery, HistoryQuery, MatchQuery, Pattern, RangeQuery, SeriesQuery

_FIELD_BY_POSITION = ("subject_id", "predicate", "object_id")


def _pattern_values(pattern: Pattern) -> tuple:
    return tuple(t.value for t in pattern)


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
        return [_row(v, ast.pattern) for v in versions]

    if isinstance(ast, HistoryQuery):
        pattern_vals = _pattern_values(ast.pattern)
        versions = db.history(pattern_vals, ast.start, ast.end)
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
        pattern_vals = _pattern_values(ast.pattern)
        day_counts = db.range_agg(pattern_vals, ast.start, ast.end)
        return [{"object_id": obj, "dayCount": count} for obj, count in day_counts.items()]

    if isinstance(ast, SeriesQuery):
        pattern_vals = _pattern_values(ast.pattern)
        gs = db.series(pattern_vals, ast.start, ast.end, ast.resolution_days)
        return [{"date": d, "facts": [_row(v, ast.pattern) for v in snapshot]} for d, snapshot in gs]

    raise TypeError(f"unknown AST node: {ast!r}")
