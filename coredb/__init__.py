"""CoreDB: an embedded bitemporal graph database with a temporal query DSL."""
from .engine import Database
from .model import Assertion, Entity, Relationship, RelationshipVersion
from .series import GraphDelta, GraphSeries
from .storage.lmdb_backend import LMDBStore


def open(path: str) -> Database:
    """Open (creating if needed) a CoreDB database at `path`."""
    return Database(LMDBStore(path))


__all__ = [
    "open", "Database",
    "Entity", "Relationship", "RelationshipVersion", "Assertion",
    "GraphSeries", "GraphDelta",
]
