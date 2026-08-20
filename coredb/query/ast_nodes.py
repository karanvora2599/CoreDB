"""AST produced by parser.py from the DSL grammar, consumed by executor.py."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Term:
    """A pattern position: a bound literal, or a `?name` variable to project."""
    value: str | None
    var_name: str | None = None

    @property
    def is_var(self) -> bool:
        return self.var_name is not None


Pattern = tuple[Term, Term, Term]


@dataclass
class WhereClause:
    """`WHERE confidence <comparator> <value>` - the only supported field
    is confidence for now (TGQL v0.2)."""
    field: str
    comparator: str
    value: float


@dataclass
class MatchQuery:
    pattern: Pattern
    on_date: str
    known_by: str | None = None
    where: WhereClause | None = None
    limit: int | None = None


@dataclass
class HistoryQuery:
    pattern: Pattern
    start: str | None = None
    end: str | None = None
    where: WhereClause | None = None
    limit: int | None = None


@dataclass
class DiffQuery:
    date_from: str
    date_to: str
    pattern: Pattern | None = None


@dataclass
class RangeQuery:
    pattern: Pattern
    start: str
    end: str


@dataclass
class SeriesQuery:
    pattern: Pattern
    start: str
    end: str
    resolution_days: int = 1


@dataclass
class AssertStatement:
    pattern: Pattern
    valid_from: str
    confidence: float | None = None


@dataclass
class RetractStatement:
    pattern: Pattern
    valid_to: str


@dataclass
class TrackQuery:
    metric: str                  # "degree" | "weighted_degree" | "edge_weight" | "closeness" | "betweenness" | "pagerank"
    target: str | tuple          # entity_id, or (subject, predicate, object) for edge_weight
    start: str
    end: str
    resolution_days: int = 1
    max_depth: int = 4           # only used by closeness/betweenness; harmless otherwise


@dataclass
class PathQuery:
    a: str
    b: str
    on_date: str
    max_depth: int = 4


@dataclass
class FirstConnectedQuery:
    a: str
    b: str
    start: str | None = None
    end: str | None = None
    max_depth: int = 4


@dataclass
class PathHistoryQuery:
    a: str
    b: str
    start: str
    end: str
    resolution_days: int = 1
    max_depth: int = 4


@dataclass
class WhyChangedQuery:
    pattern: Pattern
    date_from: str
    date_to: str


@dataclass
class ChangepointsQuery:
    metric: str
    target: str | tuple
    start: str
    end: str
    resolution_days: int = 1
    max_depth: int = 4
