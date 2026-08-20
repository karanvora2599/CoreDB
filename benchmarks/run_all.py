"""CLI entrypoint for GraphTSBench.

    python -m benchmarks.run_all            # full sizes
    python -m benchmarks.run_all --quick     # small sizes, fast dev-loop run
"""
from __future__ import annotations

import argparse

from .bench_suite import run_suite
from .harness import print_report


def main(argv=None) -> list:
    parser = argparse.ArgumentParser(description="CoreDB benchmark suite (GraphTSBench)")
    parser.add_argument("--quick", action="store_true", help="use small sizes for a fast dev-loop run")
    args = parser.parse_args(argv)
    results = run_suite(quick=args.quick)
    print_report(results)
    return results


if __name__ == "__main__":
    main()
