"""CoreDB: an embedded bitemporal graph database with a temporal query DSL (TGQL)."""
import builtins
import json

from .engine import Database
from .errors import CoreDBError, SchemaVersionError, StorageError, ValidationError
from .model import Assertion, Entity, Relationship, RelationshipVersion
from .series import GraphDelta, GraphSeries
from .storage.lmdb_backend import LMDBStore


def open(path: str, map_size: int | None = None) -> Database:
    """Open (creating if needed) a CoreDB database at `path`. `map_size`
    overrides LMDB's default 1 GiB virtual address space - pass a larger
    value if you hit StorageError for a full map."""
    kwargs = {} if map_size is None else {"map_size": map_size}
    return Database(LMDBStore(path, **kwargs))


def restore(dump_path: str, db_path: str, map_size: int | None = None) -> Database:
    """Recreate a database at `db_path` by replaying a Database.dump() file
    through assert_fact/retract_fact. This restores the logical facts and
    their valid-time intervals; it does NOT preserve prior internal ids or
    system_from/system_to timestamps (those get replay-time values) - use
    it for disaster recovery or migrating across a schema change, not for
    byte-identical restoration."""
    db = open(db_path, map_size=map_size)
    with builtins.open(dump_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            db.assert_fact(record["subject_id"], record["predicate"], record["object_id"],
                            record["valid_from"], confidence=record.get("confidence"))
            if record.get("valid_to") is not None:
                db.retract_fact(record["subject_id"], record["predicate"], record["object_id"],
                                 record["valid_to"])
    return db


__all__ = [
    "open", "restore", "Database",
    "Entity", "Relationship", "RelationshipVersion", "Assertion",
    "GraphSeries", "GraphDelta",
    "CoreDBError", "ValidationError", "StorageError", "SchemaVersionError",
]
