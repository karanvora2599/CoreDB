"""LMDB implementation of the KVStore interface.

One LMDB environment holds several named sub-databases (dbi's), one per
logical table: relationships, relationship_lookup, versions, spo_idx,
ops_idx, open_idx, opened_time_idx, closed_time_idx, assertions,
assertions_by_version, entities, sources, counters. LMDB's memory-mapped
B+tree gives cheap sorted range iteration for free, which is exactly what
the temporal indexes need.
"""
import struct

import lmdb

from .kvstore import KVStore, Transaction

TABLES = [
    "relationships",
    "relationship_lookup",
    "versions",
    "spo_idx",
    "ops_idx",
    "open_idx",
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
        self._txn.put(key, value, db=self._dbis[table])

    def delete(self, table: str, key: bytes) -> None:
        self._txn.delete(key, db=self._dbis[table])

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
        self._txn.put(counter_name.encode(), struct.pack(">Q", nxt), db=self._dbis["counters"])
        return nxt

    def commit(self) -> None:
        if not self._done:
            self._txn.commit()
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
