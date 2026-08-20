"""Timing/reporting utilities shared by every benchmark in bench_suite.py.

Deliberately not pytest-based: this measures wall-clock cost, not
correctness (tests/test_benchmarks.py covers "does it still run"). Kept
dependency-free (stdlib only) so it never needs anything beyond coredb's own
runtime dependencies.
"""
from __future__ import annotations

import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass

import coredb


@dataclass
class BenchResult:
    name: str
    n: int
    seconds: float
    note: str = ""

    @property
    def ops_per_sec(self) -> float | None:
        return self.n / self.seconds if self.seconds > 0 and self.n else None

    def __str__(self) -> str:
        rate = f"{self.ops_per_sec:,.1f} ops/s" if self.ops_per_sec else ""
        return f"{self.name:<28} n={self.n:<7} {self.seconds * 1000:>10.2f} ms  {rate:<16} {self.note}"


@contextmanager
def temp_db(**open_kwargs):
    """A throwaway CoreDB database in a temp directory, cleaned up on exit -
    mirrors tests/conftest.py's `db` fixture, but usable outside pytest."""
    d = tempfile.mkdtemp(prefix="coredb_bench_")
    database = coredb.open(d, **open_kwargs)
    try:
        yield database
    finally:
        database.close()
        shutil.rmtree(d, ignore_errors=True)


def bench(name: str, n: int, fn, *args, note: str = "", **kwargs) -> BenchResult:
    """Time one call to fn(*args, **kwargs); n is the "unit count" used to
    compute ops_per_sec (e.g. number of facts ingested, number of TRACK
    resolution steps) - callers decide what n means for their benchmark."""
    start = time.perf_counter()
    fn(*args, **kwargs)
    seconds = time.perf_counter() - start
    return BenchResult(name=name, n=n, seconds=seconds, note=note)


def print_report(results: list[BenchResult]) -> None:
    print(f"{'benchmark':<28} {'n':<9} {'time':>13}  {'throughput':<16} note")
    print("-" * 90)
    for r in results:
        print(r)
