"""Build the Meridian test dataset on disk.

    python -m tools.gen_test_dataset                      # full scale, default location
    python -m tools.gen_test_dataset --scale small        # ~1/10th, fast
    python -m tools.gen_test_dataset --out D:\\CoreBD_Test --force

The dataset is *generated*, never committed: it lives outside the repository
(default `D:\\CoreBD_Test` on Windows, `~/.cache/coredb-testdata` elsewhere)
and is reproducible byte-for-byte from `(spec_version, scale, seed)`, so
there is nothing to version-control except this generator.

Layout written to `--out`:

    manifest.json   the contract: layout, scale, checksums, engine stats,
                    and the planted ground truth the tests assert against
    events.jsonl    the canonical ingestion log (portable, engine-independent)
    facts.jsonl     Database.dump() of the result - the logical facts, in the
                    schema-independent form coredb.restore() replays
    graph.db/       a compacted LMDB database built by replaying events.jsonl
    README.md       the same orientation, for whoever finds the directory

`graph.db` is a build artifact, not the source of truth: `events.jsonl` is,
and the manifest records its SHA-256 so a stale or hand-edited database is
detected rather than silently trusted.

On the database's on-disk size: LMDB sizes its file to `map_size` the moment
an environment is opened (on Windows it is not sparse), so a database opened
with the 1 GiB default occupies 1 GiB regardless of content. This script
therefore builds into a throwaway staging environment and ships
`Database.backup()`'s compacted copy, and records `recommended_map_size` in
the manifest so consumers can open it without re-inflating the file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coredb  # noqa: E402
from coredb.engine import SCHEMA_VERSION  # noqa: E402
from tools.dataset_spec import SPEC_VERSION, DEFAULT_SEED, SCALES, build_events  # noqa: E402

MANIFEST_NAME = "manifest.json"
EVENTS_NAME = "events.jsonl"
FACTS_NAME = "facts.jsonl"
DB_NAME = "graph.db"

# One LMDB write transaction per this many events. A single transaction for
# the whole log would work but holds every dirty page until commit; chunking
# keeps peak memory bounded while still amortizing commit cost across
# thousands of writes (the reason write_batch() exists at all).
CHUNK = 4000

_STAGING_MAP_SIZE = 4 << 30   # generous: the uncompacted build carries churn overhead
_MIN_MAP_SIZE = 128 << 20
_MAP_SIZE_HEADROOM = 4        # compacted bytes * this = recommended map_size


def default_out_dir() -> Path:
    """`D:\\CoreBD_Test` where that drive exists (this project's usual box),
    otherwise a per-user cache directory - the tests locate the dataset via
    the same function, so the two never disagree."""
    if os.name == "nt" and Path("D:/").exists():
        return Path("D:/CoreBD_Test")
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "coredb-testdata"


def dataset_dir() -> Path:
    """The dataset location, `COREDB_TEST_DATASET` overriding the default."""
    override = os.environ.get("COREDB_TEST_DATASET")
    return Path(override) if override else default_out_dir()


@contextmanager
def _batch(db):
    """One write transaction for the enclosed block, where the engine
    supports it. `write_batch()` is recent; falling back to per-call
    transactions keeps this script working against an older engine instead
    of failing with an AttributeError."""
    write_batch = getattr(db, "write_batch", None)
    if write_batch is None:
        yield db
    else:
        with write_batch():
            yield db


def _apply(db, event: dict) -> None:
    op = event["op"]
    if op == "assert":
        db.assert_fact(event["s"], event["p"], event["o"], event["d"],
                        confidence=event.get("c"), sources=event.get("sources"))
    elif op == "retract":
        db.retract_fact(event["s"], event["p"], event["o"], event["d"])
    elif op == "sync":
        db.sync_snapshot(event["s"], event["p"], event["objects"], event["d"],
                          sources=event.get("sources"))
    else:
        raise ValueError(f"unknown event op {op!r}")


def load_events(db, events: list[dict], progress=None) -> None:
    """Replay `events` into `db` in chronological order, CHUNK per
    transaction."""
    for start in range(0, len(events), CHUNK):
        chunk = events[start:start + CHUNK]
        with _batch(db):
            for event in chunk:
                _apply(db, event)
        if progress:
            progress(min(start + CHUNK, len(events)), len(events))


def _write_events(path: Path, events: list[dict]) -> str:
    """Write the log and return its SHA-256. Separators are pinned so the
    checksum depends on the events, not on json's default spacing."""
    digest = hashlib.sha256()
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for event in events:
            line = json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n"
            f.write(line)
            digest.update(line.encode("utf-8"))
    return digest.hexdigest()


def _dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _round_up(n: int, multiple: int) -> int:
    return ((n + multiple - 1) // multiple) * multiple


def _event_op_counts(events: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for event in events:
        counts[event["op"]] = counts.get(event["op"], 0) + 1
    return counts


README_TEMPLATE = """# CoreDB test dataset - "{name}"

Generated, **not** version-controlled. Rebuild it with:

    python -m tools.gen_test_dataset --out "{out}" --scale {scale} --force

from the CoreDB repository. Output is deterministic in
`(spec_version={spec_version}, scale={scale}, seed={seed})`, so a rebuild
reproduces this directory byte for byte.

## Contents

| File | What it is |
|---|---|
| `manifest.json` | The contract: checksums, engine stats, planted ground truth |
| `events.jsonl` | Canonical ingestion log ({events:,} events) - the source of truth |
| `facts.jsonl` | `Database.dump()` of the result - replayable via `coredb.restore()` |
| `graph.db/` | Compacted LMDB database built from `events.jsonl` |

## Scale

{stats_table}

## Using it

`tests/test_large_dataset.py` picks this directory up automatically (or set
`COREDB_TEST_DATASET` to point elsewhere) and skips if it is missing. It
copies `graph.db` to a temporary directory before opening it, so this copy
stays pristine and stays compact - opening an LMDB environment in place
would immediately grow the file to `map_size`.
"""


def generate(out: Path, scale: str, seed: int, force: bool, quiet: bool = False) -> dict:
    def say(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    if out.exists() and any(out.iterdir()):
        if not force:
            raise SystemExit(
                f"{out} exists and is not empty - pass --force to rebuild it in place."
            )
        for child in out.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    out.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    say(f"building event log (scale={scale}, seed={seed}) ...")
    events, ground_truth = build_events(scale=scale, seed=seed)
    say(f"  {len(events):,} events")

    events_sha = _write_events(out / EVENTS_NAME, events)
    say(f"  wrote {EVENTS_NAME} ({(out / EVENTS_NAME).stat().st_size / 1e6:.1f} MB)")

    staging = Path(tempfile.mkdtemp(prefix="coredb_stage_", dir=str(out)))
    say("replaying into LMDB ...")
    db = coredb.open(str(staging), map_size=_STAGING_MAP_SIZE)
    try:
        def progress(done, total):
            say(f"  {done:,}/{total:,} events")
        load_events(db, events, progress=None if quiet else progress)
        stats = db.stats()
        db.dump(str(out / FACTS_NAME))
        db.backup(str(out / DB_NAME))
    finally:
        db.close()
    shutil.rmtree(staging, ignore_errors=True)

    db_bytes = _dir_size(out / DB_NAME)
    recommended = max(_MIN_MAP_SIZE, _round_up(db_bytes * _MAP_SIZE_HEADROOM, 64 << 20))
    elapsed = time.perf_counter() - t0

    manifest = {
        "name": "meridian-supply-network",
        "spec_version": SPEC_VERSION,
        "generator": "tools/gen_test_dataset.py",
        "scale": scale,
        "seed": seed,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "build_seconds": round(elapsed, 2),
        # The engine's on-disk schema at build time. `graph.db` is a raw
        # LMDB copy, so it is only readable by an engine at this exact
        # schema version - Database() raises SchemaVersionError otherwise.
        # Recording it lets a consumer say "rebuild the dataset" instead of
        # surfacing that as an unexplained failure.
        "schema_version": SCHEMA_VERSION,
        "files": {"events": EVENTS_NAME, "facts": FACTS_NAME, "db": DB_NAME},
        "events": {
            "count": len(events),
            "sha256": events_sha,
            "ops": _event_op_counts(events),
            "first_date": events[0]["d"],
            "last_date": events[-1]["d"],
        },
        # Engine-reported counts. Unlike `ground_truth` these are *not*
        # independent of the engine - they are a regression signal ("the
        # same log still produces the same shape"), not a correctness
        # oracle. Tests treat them accordingly.
        "stats": stats,
        "db": {
            "compacted_bytes": db_bytes,
            "recommended_map_size": recommended,
        },
        "ground_truth": ground_truth,
    }
    (out / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                      encoding="utf-8")

    stats_table = "\n".join(
        ["| Table | Entries |", "|---|---|"]
        + [f"| `{k}` | {v:,} |" for k, v in sorted(stats.items())]
        + [f"| *(compacted on disk)* | {db_bytes / 1e6:.1f} MB |"]
    )
    (out / "README.md").write_text(README_TEMPLATE.format(
        name=manifest["name"], out=out, scale=scale, seed=seed,
        spec_version=SPEC_VERSION, events=len(events), stats_table=stats_table,
    ), encoding="utf-8")

    say(f"\ndone in {elapsed:.1f}s -> {out}")
    for key, value in sorted(stats.items()):
        say(f"  {key:<16} {value:>9,}")
    say(f"  {'compacted db':<16} {db_bytes / 1e6:>8.1f} MB")
    return manifest


def main(argv=None) -> dict:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=None,
                        help=f"output directory (default: {default_out_dir()})")
    parser.add_argument("--scale", choices=sorted(SCALES), default="full")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--force", action="store_true", help="rebuild over an existing directory")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    return generate(args.out or default_out_dir(), args.scale, args.seed, args.force, args.quiet)


if __name__ == "__main__":
    main()
