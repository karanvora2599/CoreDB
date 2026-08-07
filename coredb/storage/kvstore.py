"""Storage-engine-agnostic interface. engine.py only talks to this, never to
LMDB directly, so a different embedded KV engine can be swapped in later
without touching graph/temporal logic.
"""
from abc import ABC, abstractmethod
from contextlib import contextmanager


class Transaction(ABC):
    """A read/write transaction scoped to one or more named tables."""

    @abstractmethod
    def get(self, table: str, key: bytes) -> bytes | None: ...

    @abstractmethod
    def put(self, table: str, key: bytes, value: bytes) -> None: ...

    @abstractmethod
    def delete(self, table: str, key: bytes) -> None: ...

    @abstractmethod
    def range_iter(self, table: str, start: bytes, end: bytes):
        """Yield (key, value) pairs for start <= key < end, in key order."""
        ...

    @abstractmethod
    def next_id(self, counter_name: str) -> int:
        """Atomically increment and return a named counter, scoped to this txn."""
        ...

    @abstractmethod
    def count(self, table: str) -> int:
        """Number of entries in `table`, without iterating them."""
        ...

    @abstractmethod
    def commit(self) -> None: ...

    @abstractmethod
    def abort(self) -> None: ...


class KVStore(ABC):
    @abstractmethod
    def transaction(self, write: bool = False) -> "Transaction": ...

    @abstractmethod
    def close(self) -> None: ...

    def backup(self, path: str) -> None:
        """Write a self-contained copy of this store to `path`. Optional -
        backends that can't support an online copy may leave this raising."""
        raise NotImplementedError(f"{type(self).__name__} does not support backup()")

    @contextmanager
    def txn(self, write: bool = False):
        t = self.transaction(write=write)
        try:
            yield t
            t.commit()
        except BaseException:
            t.abort()
            raise
