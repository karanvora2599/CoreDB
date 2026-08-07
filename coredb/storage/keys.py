"""Composite key encoding for LMDB sub-databases.

LMDB keys are raw bytes compared lexicographically, so:
  - String components are NUL-joined (b"\\x00") - entity/predicate ids must
    not contain a NUL byte, which is true of any reasonable text identifier.
  - Dates ("YYYY-MM-DD") and ISO datetimes sort correctly as plain UTF-8
    bytes, so no extra encoding is needed for them.
  - Counter-based ids (relationship_id, version_id, assertion_id, source_id)
    are packed big-endian so numeric order == byte order, keeping
    per-triple history sorted chronologically-then-by-insertion-order even
    when several versions share the same valid_from.
"""
import struct

_SEP = b"\x00"

# A byte higher than any character used in identifiers/dates, so a prefix
# scan for "everything under key X" can be bounded by X + _HIGH.
_HIGH = b"\xff"


def encode_id(id_: int) -> bytes:
    return struct.pack(">Q", id_)


def decode_id(raw: bytes) -> int:
    return struct.unpack(">Q", raw)[0]


def _enc(*parts: str) -> bytes:
    return _SEP.join(p.encode("utf-8") for p in parts)


def spo_key(subject: str, predicate: str, valid_from: str, version_id: int) -> bytes:
    return _enc(subject, predicate, valid_from) + _SEP + encode_id(version_id)


def spo_prefix(subject: str, predicate: str | None = None) -> bytes:
    if predicate is None:
        return _enc(subject) + _SEP
    return _enc(subject, predicate) + _SEP


def ops_key(object_id: str, predicate: str, subject: str, valid_from: str, version_id: int) -> bytes:
    return _enc(object_id, predicate, subject, valid_from) + _SEP + encode_id(version_id)


def ops_prefix(object_id: str, predicate: str | None = None) -> bytes:
    if predicate is None:
        return _enc(object_id) + _SEP
    return _enc(object_id, predicate) + _SEP


def triple_key(subject: str, predicate: str, object_id: str) -> bytes:
    return _enc(subject, predicate, object_id)


def decode_triple_key(raw: bytes) -> tuple[str, str, str]:
    subject, predicate, object_id = raw.split(_SEP)
    return subject.decode("utf-8"), predicate.decode("utf-8"), object_id.decode("utf-8")


def time_key(date: str, version_id: int) -> bytes:
    return date.encode("utf-8") + _SEP + encode_id(version_id)


def assertion_by_version_key(version_id: int, assertion_id: int) -> bytes:
    return encode_id(version_id) + _SEP + encode_id(assertion_id)


def prefix_upper_bound(prefix: bytes) -> bytes:
    """Exclusive upper bound for iterating all keys starting with `prefix`."""
    return prefix + _HIGH
