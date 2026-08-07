"""GraphSeries and GraphDelta: the evolution-operator layer on top of the
engine's eager as_of/history/diff primitives.

A GraphSeries is a lazy view over a pattern's history across [start, end] -
it holds a reference back to the database and resolves snapshots/diffs on
demand rather than materializing every G_t up front. It has no dependency
on engine.py (the db it holds is duck-typed), so engine.py can freely
import GraphDelta/GraphSeries from here without a circular import.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class GraphDelta:
    """The structured result of diffing a pattern between two dates. Node
    sets are netted out against edge churn - an object whose relationship
    both opened and closed inside the window (or was already open before
    and after) doesn't appear in both nodes_added and nodes_removed."""
    date_from: str
    date_to: str
    nodes_added: list[str]
    nodes_removed: list[str]
    edges_opened: list
    edges_closed: list
    edges_persisted: list


class GraphSeries:
    """A lazy view over `pattern`'s relationship-version history across
    [start, end]. Nothing is computed at construction time."""

    def __init__(self, db, pattern: tuple, start: str, end: str, resolution_days: int = 1):
        self._db = db
        self.pattern = pattern
        self.start = start
        self.end = end
        self.resolution_days = resolution_days

    def at(self, on_date: str) -> list:
        """The pattern's matching versions active on `on_date`."""
        return self._db.as_of(self.pattern, on_date)

    def diff(self, date_from: str | None = None, date_to: str | None = None) -> GraphDelta:
        """The GraphDelta between two dates, defaulting to this series' own
        [start, end] bounds."""
        return self._db.diff_delta(self.pattern, date_from or self.start, date_to or self.end)

    def dates(self):
        """Yield 'YYYY-MM-DD' strings from start to end (inclusive), stepping
        by resolution_days."""
        d = datetime.strptime(self.start, "%Y-%m-%d").date()
        end_d = datetime.strptime(self.end, "%Y-%m-%d").date()
        step = timedelta(days=self.resolution_days)
        while d <= end_d:
            yield d.strftime("%Y-%m-%d")
            d += step

    def __iter__(self):
        """Yield (date, snapshot) pairs at `resolution_days` steps across
        [start, end] - each snapshot is resolved lazily via as_of() at
        iteration time, not precomputed."""
        for d in self.dates():
            yield d, self.at(d)
