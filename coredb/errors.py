"""CoreDB's exception hierarchy - callers should catch these instead of raw
LMDB/struct/ValueError exceptions leaking out of internal implementation
details.
"""


class CoreDBError(Exception):
    """Base class for all CoreDB-raised errors."""


class ValidationError(CoreDBError):
    """Raised when caller-supplied data would corrupt or misrepresent the
    fact log - a NUL byte in an identifier, an unparseable date, or an
    interval whose valid_to precedes its valid_from."""


class StorageError(CoreDBError):
    """Raised when the underlying storage engine can't complete an
    operation - e.g. the LMDB map is full."""


class SchemaVersionError(StorageError):
    """Raised when an on-disk database's schema_version doesn't match the
    version this code expects. Use Database.dump()/coredb.restore() to
    migrate a database created by an older/newer version of CoreDB."""


class QueryError(CoreDBError):
    """Raised when TGQL source text fails to parse, or names an unsupported
    clause value (e.g. a RESOLUTION unit other than '<N>d')."""
