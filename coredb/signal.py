"""GraphSignal: a graph metric evaluated across time, turned into a plain
time series joinable with external (non-graph) data, plus change-point
detection over that series.

Like series.py, this module has no dependency on engine.py (the db it
would reference is never actually imported here - GraphSignal is a pure
datatype), keeping the "query/series/signal never import engine" direction
of the dependency graph intact.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

try:
    from . import _native
except ImportError:
    # No C++ toolchain at build time, or a pure-Python-only install -
    # detect_changepoints() below falls back to the pure-Python path
    # transparently. Optional acceleration, not a hard dependency - see
    # Documentation/ARCHITECTURE.md's Performance section.
    _native = None


def _segment_cost(values: list[float], start: int, end: int) -> float:
    """Residual sum of squared deviations from the mean, for values[start:end]."""
    n = end - start
    if n <= 0:
        return 0.0
    segment = values[start:end]
    mean = sum(segment) / n
    return sum((x - mean) ** 2 for x in segment)


def _best_split(values: list[float], start: int, end: int, min_size: int):
    """The split point in (start, end) that most reduces total cost, and
    the size of that reduction. Returns (None, 0.0) if no split of at least
    min_size on each side improves on leaving the segment whole."""
    base_cost = _segment_cost(values, start, end)
    best_gain = 0.0
    best_split = None
    for split in range(start + min_size, end - min_size + 1):
        cost = _segment_cost(values, start, split) + _segment_cost(values, split, end)
        gain = base_cost - cost
        if gain > best_gain:
            best_gain = gain
            best_split = split
    return best_split, best_gain


def _detect_changepoints_indices_python(values: list[float], min_size: int, penalty: float) -> list[int]:
    """Pure-Python index-space search - identical algorithm to the native
    coredb._native.detect_changepoints_indices, kept as the fallback (and
    as the correctness oracle in tests/test_native_segmentation.py)."""
    n = len(values)
    changepoints = []
    if n < 2 * min_size:
        return changepoints
    stack: list[tuple[int, int]] = [(0, n)]
    while stack:
        start, end = stack.pop()
        if end - start < 2 * min_size:
            continue
        split, gain = _best_split(values, start, end, min_size)
        if split is None or gain <= penalty:
            continue
        changepoints.append(split)
        stack.append((start, split))
        stack.append((split, end))
    return sorted(changepoints)


def detect_changepoints(points: list[tuple[str, float | None]], min_size: int = 2,
                         penalty: float | None = None, use_native: bool | None = None) -> list[str]:
    """Binary segmentation change-point detection (mean-shift, residual-
    sum-of-squares cost) over a GraphSignal's points - the basis of tools
    like `ruptures`' Binseg, not an ad hoc heuristic. Recursively finds the
    split that most reduces total cost, keeps it only if the gain exceeds
    `penalty`, and recurses into both halves. `None`-valued points (e.g.
    edge_weight when a relationship isn't open) are dropped first - that's
    "no data", not a real zero.

    `penalty` defaults to a standard BIC-style heuristic
    (`variance(values) * log(n)`) if not given; pass an explicit value to
    tune sensitivity (lower = more changepoints).

    The actual index-space search runs in coredb._native (a compiled
    pybind11 extension, a literal port of the same algorithm - not an
    approximation) when it's available, falling back to pure Python
    transparently otherwise; both produce identical results. `use_native`
    forces one path or the other (for tests/benchmarks comparing them
    directly) - leave it None to auto-detect.

    Returns the dates where a new regime starts, chronologically sorted.
    """
    dated_values = [(d, v) for d, v in points if v is not None]
    dates = [d for d, _ in dated_values]
    values = [v for _, v in dated_values]
    n = len(values)
    if n < 2 * min_size:
        return []

    if penalty is None:
        mean = sum(values) / n
        variance = sum((x - mean) ** 2 for x in values) / n
        penalty = variance * math.log(n) if variance > 0 else 0.0

    run_native = _native is not None if use_native is None else use_native
    if run_native:
        if _native is None:
            raise RuntimeError("use_native=True requested but coredb._native is not built")
        indices = _native.detect_changepoints_indices(values, min_size, penalty)
    else:
        indices = _detect_changepoints_indices_python(values, min_size, penalty)

    return sorted(dates[i] for i in indices)


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

    def changepoints(self, min_size: int = 2, penalty: float | None = None) -> list[str]:
        """The dates where this signal's underlying mean shifts
        significantly - see detect_changepoints()."""
        return detect_changepoints(self.points, min_size=min_size, penalty=penalty)
