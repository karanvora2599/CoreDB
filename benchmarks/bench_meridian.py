"""Benchmarks over the generated Meridian dataset - the only suite here
that measures a graph nobody tuned for the measurement.

    python -m benchmarks.bench_meridian            # full
    python -m benchmarks.bench_meridian --quick     # skip the whole-graph rows

`bench_suite.py` builds each graph inline to isolate one cost (H=150
intervals, 100 nodes), and `bench_at_scale.py` builds a large but uniform
graph to answer one question about key size. Both are synthetic in shape as
well as content: every entity looks like every other. Meridian is different
in the way that matters for a query engine - ~64k versions and ~4.9k
entities spread across seven layers with wildly uneven degree, history depth
and churn cadence, so an index choice that only works on a uniform graph has
somewhere to fail.

What this is for: the rows below are the ones where cost depends on the
*shape* of the data rather than its size alone - a bound-subject scan versus
the documented full-table scan when neither subject nor object is bound, the
O(H + D) sweep versus the per-date reconstruction it replaced at a history
depth an inline fixture cannot reach, and the global centrality computations
that have no incremental form. See `Documentation/ARCHITECTURE.md`'s
Performance section for which of these are known-unoptimized by design.

Needs the dataset (`python -m tools.gen_test_dataset`); prints how to build
it and exits cleanly if it is missing, rather than failing.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coredb  # noqa: E402
from coredb.engine import SCHEMA_VERSION  # noqa: E402
from coredb.series import date_range  # noqa: E402

from .harness import bench, print_report  # noqa: E402

# A window inside the calendar, long enough for the sweep-vs-oracle gap to
# be unambiguous without making the oracle row dominate the whole run.
_WINDOW = ("2023-01-01", "2023-12-31")
_ORACLE_DATES = 120


@contextmanager
def meridian_db():
    """The dataset, opened from a throwaway copy.

    Copied for the same two reasons `tests/conftest.py` copies it: opening
    an LMDB environment grows its file to `map_size` immediately (so opening
    the shipped copy in place would inflate a 51 MB directory every run),
    and a benchmark must not mutate the shared dataset.
    """
    from tools.gen_test_dataset import MANIFEST_NAME, dataset_dir

    directory = dataset_dir()
    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.exists():
        raise SystemExit(
            f"No Meridian dataset at {directory}.\n"
            "Build it with:  python -m tools.gen_test_dataset"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise SystemExit(
            f"Dataset was built against engine schema_version="
            f"{manifest.get('schema_version')}, this checkout is at {SCHEMA_VERSION}.\n"
            "Rebuild it with:  python -m tools.gen_test_dataset --force"
        )

    staging = Path(tempfile.mkdtemp(prefix="coredb_meridian_bench_"))
    try:
        shutil.copytree(directory / manifest["files"]["db"], staging / "graph.db")
        db = coredb.open(str(staging / "graph.db"),
                          map_size=manifest["db"]["recommended_map_size"])
        try:
            yield db, manifest
        finally:
            db.close()
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ----------------------------------------------------------------------
# Pattern scans - the index-choice rows.
# ----------------------------------------------------------------------

def _scan_note(db, pattern, on_date):
    """`scanned` (index entries walked, each costing a version load) and
    `kept` (versions actually active on the date).

    Every row below reports both, because per-call time here is driven by
    `scanned`, not by which index served the pattern - `spo_idx` covers
    (subject, predicate) across *all* time, so a single-date query on a
    deep-history entity still walks its whole history and discards most of
    it. Comparing two scan rows without these counts compares fan-out and
    calls it an index result.
    """
    subject, predicate, obj = pattern
    if subject is not None:
        scanned = len(db.history((subject, predicate, obj)))
    elif obj is not None:
        scanned = len(db.history((None, predicate, obj)))
    else:
        scanned = db.stats()["versions"]
    return f"scanned~{scanned:,}, kept={len(db.as_of(pattern, on_date)):,}"


def bench_as_of_bound_subject_deep(db, gt, n=200):
    """Subject bound, on the entity with the deepest history in the
    dataset: served by an `spo_idx` prefix range, but the prefix spans
    every interval that entity ever had."""
    subject = gt["deep_history"]["entities"][0]
    pattern = (subject, "SUPPLIES", None)
    return bench("meridian.as_of (subject, deep H)", n,
                  lambda: [db.as_of(pattern, "2023-06-01") for _ in range(n)],
                  note=_scan_note(db, pattern, "2023-06-01"))


def bench_as_of_bound_subject_shallow(db, gt, n=2000):
    """The same index and the same query shape on a static-layer entity
    with almost no history - the contrast that isolates history depth from
    index choice."""
    pattern = ("FIRM_01500", "PRODUCES", None)
    return bench("meridian.as_of (subject, shallow H)", n,
                  lambda: [db.as_of(pattern, "2023-06-01") for _ in range(n)],
                  note=_scan_note(db, pattern, "2023-06-01"))


def bench_as_of_bound_object(db, gt, n=2000):
    """The reverse pattern, served by `ops_idx` - the index that exists so a
    bound object doesn't degrade to the full scan below."""
    pattern = (None, "SHIPS_VIA", "PORT_001")
    return bench("meridian.as_of (object bound)", n,
                  lambda: [db.as_of(pattern, "2023-06-01") for _ in range(n)],
                  note=_scan_note(db, pattern, "2023-06-01"))


def bench_as_of_predicate_only(db, gt, n=1):
    """Neither subject nor object bound: a documented full scan of every
    version in the database. Included precisely because it is the known
    weak case - the row to watch if a predicate index is ever added."""
    pattern = (None, "SUPPLIES", None)
    return bench("meridian.as_of (predicate only, FULL SCAN)", n,
                  lambda: db.as_of(pattern, "2023-06-01"),
                  note=_scan_note(db, pattern, "2023-06-01"))


def bench_history_deep(db, gt, n=100):
    """One entity's whole interval history, at a depth (H) no inline
    fixture reaches."""
    subject = gt["deep_history"]["entities"][0]
    return bench("meridian.history (deep subject)", n,
                  lambda: [db.history((subject, None, None)) for _ in range(n)])


def bench_diff_scoped(db, gt, n=500):
    entity = gt["shift"]["entity"]
    return bench("meridian.diff (scoped pattern)", n,
                  lambda: [db.diff_delta((entity, None, None), "2023-01-01", "2023-12-31")
                           for _ in range(n)])


def bench_diff_global(db, gt, n=5):
    """Pattern-less DIFF: the time indexes cover opened/closed, but
    "persisted across both dates" has no interval index, so it falls back to
    a full version scan."""
    return bench("meridian.diff (global, FULL SCAN)", n,
                  lambda: [db.diff("2023-06-01", "2023-07-01") for _ in range(n)])


# ----------------------------------------------------------------------
# The interval sweep, against the reconstruction it replaced.
# ----------------------------------------------------------------------

def bench_track_degree(db, gt, n=365):
    subject = gt["deep_history"]["entities"][0]
    depth = len(db.history((subject, None, None)))
    return bench("meridian.track.degree (sweep)", n,
                  lambda: db.track("degree", subject, *_WINDOW),
                  note=f"H={depth} intervals, D={n} steps")


def bench_track_degree_oracle(db, gt, n=_ORACLE_DATES):
    """The pre-M9 shape: one `degree()` reconstruction per date, each an
    O(H) scan. Kept as a benchmark, not just a test oracle, so the sweep's
    win stays a measured number at real history depth rather than a claim
    inherited from the small fixture."""
    subject = gt["deep_history"]["entities"][0]
    dates = list(date_range(*_WINDOW))[:n]
    depth = len(db.history((subject, None, None)))
    return bench("meridian.track.degree (per-date oracle)", n,
                  lambda: [db.degree(subject, d) for d in dates],
                  note=f"H={depth} intervals, D={n} steps")


def bench_series_iterate(db, gt, n=365):
    subject = gt["deep_history"]["entities"][0]
    return bench("meridian.series.iterate", n,
                  lambda: list(db.series((subject, "SUPPLIES", None), *_WINDOW)))


def bench_changepoints(db, gt, n=261):
    """`TRACK` plus binary segmentation over the whole five-year calendar at
    weekly resolution - the full CHANGEPOINTS path, native search included
    when the extension is built."""
    subject = gt["deep_history"]["entities"][0]
    start, end = gt["calendar"]["start"], gt["calendar"]["end"]
    return bench("meridian.changepoints.degree", n,
                  lambda: db.changepoints("degree", subject, start, end, 7),
                  note=f"weekly over {start}..{end}")


# ----------------------------------------------------------------------
# Traversal and provenance.
# ----------------------------------------------------------------------

def bench_path_exists(db, gt, n=500):
    chain = gt["chain"]
    return bench("meridian.path_exists (4 hops)", n,
                  lambda: [db.path_exists(chain["nodes"][0], chain["nodes"][-1], "2022-04-01")
                           for _ in range(n)])


def bench_first_connected(db, gt, n=10):
    """Chronological scan over every date some relationship opened in the
    window, calling `path_exists` at each - connectivity isn't monotonic, so
    this can't binary-search."""
    chain = gt["chain"]
    window = chain["first_connected_window"]
    return bench("meridian.first_connected (windowed)", n,
                  lambda: [db.first_connected(chain["nodes"][0], chain["nodes"][-1],
                                               window[0], window[1]) for _ in range(n)])


def bench_closeness_hub(db, gt, n=100):
    hub = gt["hub"]
    return bench("meridian.closeness (hub)", n,
                  lambda: [db.closeness(hub["entity"], hub["on_date"]) for _ in range(n)],
                  note=f"{hub['degree']} peers at distance 1")


def bench_why_changed(db, gt, n=100):
    subject, predicate, obj = gt["churn"]["triple"]
    start, end = gt["churn"]["intervals"][0][0], gt["churn"]["intervals"][-1][1]
    return bench("meridian.why_changed", n,
                  lambda: [db.why_changed(subject, predicate, obj, start, end) for _ in range(n)])


# ----------------------------------------------------------------------
# Whole-graph work - no incremental form, full cost per call.
# ----------------------------------------------------------------------

def bench_pagerank_all(db, gt, n=1):
    return bench("meridian.pagerank_all (whole graph)", n,
                  lambda: db.pagerank_all("2023-06-01"),
                  note="power iteration over every active entity")


def bench_betweenness_all(db, gt, n=1):
    return bench("meridian.betweenness_all (whole graph)", n,
                  lambda: db.betweenness_all("2023-06-01", max_depth=3),
                  note="Brandes, max_depth=3")


def bench_dump(db, gt):
    """Full version scan plus serialization - the migration path's cost."""
    target = Path(tempfile.mkdtemp(prefix="coredb_dump_bench_")) / "facts.jsonl"
    count = db.stats()["versions"]
    try:
        return bench("meridian.dump (all versions)", count,
                      lambda: db.dump(str(target)))
    finally:
        shutil.rmtree(target.parent, ignore_errors=True)


# Ordered cheapest-first so a --quick run reads top to bottom. The bool is
# "run this even in --quick mode".
MERIDIAN_BENCHMARKS = [
    (bench_as_of_bound_subject_deep, True),
    (bench_as_of_bound_subject_shallow, True),
    (bench_as_of_bound_object, True),
    (bench_as_of_predicate_only, True),
    (bench_history_deep, True),
    (bench_diff_scoped, True),
    (bench_diff_global, True),
    (bench_track_degree, True),
    (bench_track_degree_oracle, False),
    (bench_series_iterate, True),
    (bench_changepoints, True),
    (bench_path_exists, True),
    (bench_first_connected, True),
    (bench_closeness_hub, True),
    (bench_why_changed, True),
    (bench_dump, True),
    (bench_pagerank_all, False),
    (bench_betweenness_all, False),
]


def run_meridian(quick: bool = False) -> list:
    results = []
    with meridian_db() as (db, manifest):
        stats = manifest["stats"]
        print(f"Meridian dataset: {stats['versions']:,} versions, "
              f"{stats['relationships']:,} relationships, {stats['entities']:,} entities, "
              f"{stats['assertions']:,} assertions "
              f"({manifest['db']['compacted_bytes'] / 1e6:.1f} MB compacted)\n")
        for fn, in_quick in MERIDIAN_BENCHMARKS:
            if quick and not in_quick:
                continue
            results.append(fn(db, manifest["ground_truth"]))
    return results


def main(argv=None) -> list:
    parser = argparse.ArgumentParser(description="CoreDB benchmarks over the Meridian dataset")
    parser.add_argument("--quick", action="store_true",
                        help="skip the whole-graph and per-date-oracle rows")
    args = parser.parse_args(argv)
    results = run_meridian(quick=args.quick)
    print_report(results)
    return results


if __name__ == "__main__":
    main()
