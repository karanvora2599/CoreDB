"""LMDB implementation of the KVStore interface.

One LMDB environment holds several named sub-databases (dbi's), one per
logical table: relationships, relationship_lookup, versions, spo_idx,
ops_idx, open_idx, open_by_sp_idx, opened_time_idx, closed_time_idx,
assertions, assertions_by_version, entities, sources, counters. LMDB's
memory-mapped B+tree gives cheap sorted range iteration for free, which is
exactly what the temporal indexes need.
"""
import os
import struct

import lmdb

from ..errors import StorageError
from .kvstore import KVStore, Transaction

_MAP_FULL_MESSAGE = (
    "LMDB map is full - reopen the database with a larger map_size, "
    "e.g. coredb.open(path, map_size=<bytes>)."
)

TABLES = [
    "relationships",
    "relationship_lookup",
    "versions",
    "spo_idx",
    "ops_idx",
    "open_idx",
    "open_by_sp_idx",
    "opened_time_idx",
    "closed_time_idx",
    "assertions",
    "assertions_by_version",
    "entities",
    "sources",
    "counters",
]

_DEFAULT_MAP_SIZE = 1 << 30  # 1 GiB virtual address space; LMDB only uses what's written


class LMDBTransaction(Transaction):
    def __init__(self, env: "lmdb.Environment", dbis: dict, write: bool):
        self._txn = env.begin(write=write)
        self._dbis = dbis
        self._done = False

    def get(self, table: str, key: bytes) -> bytes | None:
        return self._txn.get(key, db=self._dbis[table])

    def put(self, table: str, key: bytes, value: bytes) -> None:
        try:
            self._txn.put(key, value, db=self._dbis[table])
        except lmdb.MapFullError as e:
            raise StorageError(_MAP_FULL_MESSAGE) from e

    def delete(self, table: str, key: bytes) -> None:
        self._txn.delete(key, db=self._dbis[table])

    def count(self, table: str) -> int:
        return self._txn.stat(db=self._dbis[table])["entries"]

    def range_iter(self, table: str, start: bytes, end: bytes):
        cursor = self._txn.cursor(db=self._dbis[table])
        if not cursor.set_range(start):
            return
        for key, value in cursor:
            if key >= end:
                break
            yield key, value

    def next_id(self, counter_name: str) -> int:
        raw = self._txn.get(counter_name.encode(), db=self._dbis["counters"])
        current = struct.unpack(">Q", raw)[0] if raw else 0
        nxt = current + 1
        self.put("counters", counter_name.encode(), struct.pack(">Q", nxt))
        return nxt

    def commit(self) -> None:
        if not self._done:
            try:
                self._txn.commit()
            except lmdb.MapFullError as e:
                raise StorageError(_MAP_FULL_MESSAGE) from e
            self._done = True

    def abort(self) -> None:
        if not self._done:
            self._txn.abort()
            self._done = True


class LMDBStore(KVStore):
    def __init__(self, path: str, map_size: int = _DEFAULT_MAP_SIZE):
        self._env = lmdb.open(path, map_size=map_size, max_dbs=len(TABLES) + 1)
        self._dbis = {name: self._env.open_db(name.encode()) for name in TABLES}

    def transaction(self, write: bool = False) -> LMDBTransaction:
        return LMDBTransaction(self._env, self._dbis, write=write)

    def close(self) -> None:
        self._env.close()

    def backup(self, path: str) -> None:
        """A compacted, self-contained copy of the current database state -
        safe to call while the database is open and in use. `path` is a
        directory (LMDB uses subdir mode: a database is a directory
        containing data.mdb/lock.mdb, not a single file) - it's created if
        missing, and must be empty."""
        os.makedirs(path, exist_ok=True)
        self._env.copy(path, compact=True)
