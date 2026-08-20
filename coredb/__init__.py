"""CoreDB: an embedded bitemporal graph database with a temporal query DSL (TGQL)."""
import builtins
import json

from .engine import Database
from .errors import CoreDBError, QueryError, SchemaVersionError, StorageError, ValidationError
from .model import Assertion, Entity, Relationship, RelationshipVersion
from .series import GraphDelta, GraphSeries
from .storage.lmdb_backend import LMDBStore


def open(path: str, map_size: int | None = None) -> Database:
    """Open (creating if needed) a CoreDB database at `path`. `map_size`
    overrides LMDB's default 1 GiB virtual address space - pass a larger
    value if you hit StorageError for a full map."""
    kwargs = {} if map_size is None else {"map_size": map_size}
    return Database(LMDBStore(path, **kwargs))


_RESTORE_BATCH_SIZE = 5000


def restore(dump_path: str, db_path: str, map_size: int | None = None) -> Database:
    """Recreate a database at `db_path` by replaying a Database.dump() file
    through assert_fact/retract_fact. This restores the logical facts and
    their valid-time intervals; it does NOT preserve prior internal ids or
    system_from/system_to timestamps (those get replay-time values) - use
    it for disaster recovery or migrating across a schema change, not for
    byte-identical restoration.

    Replayed in write_batch() chunks of _RESTORE_BATCH_SIZE records each -
    not one giant transaction for the whole file (LMDB doesn't reclaim
    space from old MVCC snapshots until a transaction commits, so a single
    unbounded transaction would make a large restore's memory/map-size
    growth unbounded too) and not one transaction per record either (the
    inefficiency this batching fixes - see Documentation/ARCHITECTURE.md's
    Performance section)."""
    db = open(db_path, map_size=map_size)
    batch: list[dict] = []
    with builtins.open(dump_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            batch.append(json.loads(line))
            if len(batch) >= _RESTORE_BATCH_SIZE:
                _replay_batch(db, batch)
                batch = []
    if batch:
        _replay_batch(db, batch)
    return db


def _replay_batch(db: Database, records: list[dict]) -> None:
    with db.write_batch():
        for record in records:
            db.assert_fact(record["subject_id"], record["predicate"], record["object_id"],
                            record["valid_from"], confidence=record.get("confidence"))
            if record.get("valid_to") is not None:
                db.retract_fact(record["subject_id"], record["predicate"], record["object_id"],
                                 record["valid_to"])


__all__ = [
    "open", "restore", "Database",
    "Entity", "Relationship", "RelationshipVersion", "Assertion",
    "GraphSeries", "GraphDelta",
    "CoreDBError", "ValidationError", "StorageError", "SchemaVersionError", "QueryError",
]
