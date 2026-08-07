"""Parses TGQL source text into ast_nodes via a Lark grammar + Transformer."""
from __future__ import annotations

from pathlib import Path

from lark import Lark, Transformer

from .ast_nodes import (
    AssertStatement, DiffQuery, HistoryQuery, MatchQuery, RangeQuery,
    RetractStatement, SeriesQuery, Term, WhereClause,
)

_GRAMMAR_PATH = Path(__file__).parent / "grammar.lark"


def _unquote(token) -> str:
    return str(token)[1:-1]


def _parse_resolution(token) -> int:
    """'7d' -> 7. Only day resolutions are supported for now."""
    text = _unquote(token)
    if not text.endswith("d"):
        raise ValueError(f"unsupported RESOLUTION unit: {text!r} (only '<N>d' is supported)")
    return int(text[:-1])


class _ASTBuilder(Transformer):
    def term(self, children):
        (tok,) = children
        text = str(tok)
        if text.startswith("?"):
            return Term(value=None, var_name=text[1:])
        return Term(value=text, var_name=None)

    def pattern(self, children):
        return tuple(children)

    def where_clause(self, children):
        comparator_tok, number_tok = children
        return WhereClause(field="confidence", comparator=str(comparator_tok), value=float(number_tok))

    def limit_clause(self, children):
        (number_tok,) = children
        return int(number_tok)

    def match_stmt(self, children):
        pattern = children[0]
        on_date = _unquote(children[1])
        known_by, where, limit = None, None, None
        for c in children[2:]:
            if isinstance(c, WhereClause):
                where = c
            elif isinstance(c, int):
                limit = c
            else:
                known_by = _unquote(c)
        return MatchQuery(pattern=pattern, on_date=on_date, known_by=known_by, where=where, limit=limit)

    def history_stmt(self, children):
        pattern = children[0]
        where, limit = None, None
        date_strings = []
        for c in children[1:]:
            if isinstance(c, WhereClause):
                where = c
            elif isinstance(c, int):
                limit = c
            else:
                date_strings.append(_unquote(c))
        start = date_strings[0] if len(date_strings) >= 1 else None
        end = date_strings[1] if len(date_strings) >= 2 else None
        return HistoryQuery(pattern=pattern, start=start, end=end, where=where, limit=limit)

    def diff_stmt(self, children):
        date_from = _unquote(children[0])
        date_to = _unquote(children[1])
        pattern = children[2] if len(children) > 2 else None
        return DiffQuery(date_from=date_from, date_to=date_to, pattern=pattern)

    def range_stmt(self, children):
        pattern = children[0]
        start = _unquote(children[1])
        end = _unquote(children[2])
        return RangeQuery(pattern=pattern, start=start, end=end)

    def series_stmt(self, children):
        pattern = children[0]
        start = _unquote(children[1])
        end = _unquote(children[2])
        resolution_days = _parse_resolution(children[3]) if len(children) > 3 else 1
        return SeriesQuery(pattern=pattern, start=start, end=end, resolution_days=resolution_days)

    def assert_stmt(self, children):
        pattern = children[0]
        valid_from = _unquote(children[1])
        confidence = float(children[2]) if len(children) > 2 else None
        return AssertStatement(pattern=pattern, valid_from=valid_from, confidence=confidence)

    def retract_stmt(self, children):
        pattern = children[0]
        valid_to = _unquote(children[1])
        return RetractStatement(pattern=pattern, valid_to=valid_to)

    def statement(self, children):
        return children[0]

    def start(self, children):
        return children[0]


_parser = Lark(_GRAMMAR_PATH.read_text(), parser="lalr")
_builder = _ASTBuilder()


def parse(source: str):
    tree = _parser.parse(source)
    return _builder.transform(tree)
