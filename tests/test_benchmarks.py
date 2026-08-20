"""Smoke test for benchmarks/ - guards against the suite silently rotting
as the engine evolves. Runs at --quick sizes; asserts it completes without
raising and produces a result per registered benchmark. No timing
assertions here - wall-clock thresholds in a shared test environment are
flaky by nature, and that's not this test's job (benchmarks/README.md
covers how to actually read the numbers)."""
from benchmarks.bench_suite import BENCHMARKS, run_suite


def test_quick_suite_runs_and_returns_a_result_per_benchmark():
    results = run_suite(quick=True)
    assert len(results) == len(BENCHMARKS)
    for r in results:
        assert r.seconds >= 0
        assert r.name
