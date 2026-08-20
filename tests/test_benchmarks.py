"""Smoke test for benchmarks/ - guards against the suite silently rotting
as the engine evolves. Runs at --quick sizes; asserts it completes without
raising and produces a result per registered benchmark. No timing
assertions here - wall-clock thresholds in a shared test environment are
flaky by nature, and that's not this test's job (benchmarks/README.md
covers how to actually read the numbers)."""
from benchmarks.bench_at_scale import bench_hot_path_as_of_at_scale, bench_ingest_at_scale
from benchmarks.bench_suite import BENCHMARKS, run_suite


def test_quick_suite_runs_and_returns_a_result_per_benchmark():
    results = run_suite(quick=True)
    assert len(results) == len(BENCHMARKS)
    for r in results:
        assert r.seconds >= 0
        assert r.name


def test_at_scale_benchmarks_run_at_small_sizes():
    # Tiny sizes here - this only guards against bench_at_scale.py rotting,
    # not a real at-scale measurement (see Documentation/ROADMAP.md's
    # Milestone 10 for the actual at-scale finding and how to reproduce it:
    # `python -m benchmarks.bench_at_scale`).
    ingest = bench_ingest_at_scale(n_facts=50, n_entities=10)
    assert ingest.seconds >= 0
    as_of = bench_hot_path_as_of_at_scale(n_facts=50, n_entities=10, n_lookups=5)
    assert as_of.seconds >= 0
