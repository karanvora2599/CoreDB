"""GraphSignal: a graph metric evaluated across time, turned into a plain
time series joinable with external (non-graph) data.

Like series.py, this module has no dependency on engine.py (the db it
would reference is never actually imported here - GraphSignal is a pure
datatype), keeping the "query/series/signal never import engine" direction
of the dependency graph intact.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GraphSignal:
    """A graph function evaluated at a sequence of dates: {(date, value)}."""
    metric: str
    target: str | tuple
    points: list[tuple[str, float | None]]

    def join(self, other: dict) -> list[tuple[str, float | None, float]]:
        """Inner join on date against a caller-supplied {date: value} series
        (e.g. external price/volatility data) - only dates present in both
        this signal and `other` survive, in this signal's date order."""
        return [(date, value, other[date]) for date, value in self.points if date in other]
