"""Compact binary encoding for RelationshipVersion - the "VersionCore"
format an external technical review proposed (M10 Part 4): a packed
struct instead of JSON for the versions table specifically, the highest-
volume table and the one on the hot path of every scan/BFS in engine.py.
Other tables (relationships/assertions/entities/sources) stay JSON -
lower volume, not measured as a problem, no reason to touch them.

Layout: a fixed-width header (all little-endian, no padding) of ids as
int64, dates as int32 proleptic-Gregorian day ordinals
(date.toordinal()), system timestamps as int64 microseconds since the
Unix epoch (exact integer arithmetic via timedelta, not float seconds -
avoids float64's precision limit at microsecond resolution for
present-day timestamps), and confidence as float64 with NaN as the
"None" sentinel - followed by a variable-length section for the
caller-supplied strings, the properties dict (still JSON - arbitrary
nested structure, not worth a hand-rolled schema), and the assertion-id
list.

Schema-versioned: SCHEMA_VERSION in engine.py was bumped when this format
was introduced, so an old JSON-encoded database raises SchemaVersionError
on open rather than being misread - Database.dump()/coredb.restore() is
the migration path, same as any other schema change.
"""
from __future__ import annotations

import json
import math
import struct
from datetime import date, datetime, timedelta, timezone

from ..model import RelationshipVersion

# version_id, relationship_id, valid_from, valid_to, system_from, system_to,
# last_confirmed, confidence, subject_len, predicate_len, object_len,
# properties_len, n_assertion_ids
_HEADER = struct.Struct("<qqiiqqidHHHII")
_NO_DATE = -(2**31)
_NO_TIMESTAMP = -(2**63)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _date_to_int(d: str) -> int:
    return date.fromisoformat(d).toordinal()


def _int_to_date(n: int) -> str:
    return date.fromordinal(n).isoformat()


def _timestamp_to_int(ts: str) -> int:
    delta = datetime.fromisoformat(ts) - _EPOCH
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _int_to_timestamp(n: int) -> str:
    return (_EPOCH + timedelta(microseconds=n)).isoformat()


def encode_version(v: RelationshipVersion) -> bytes:
    subject_b = v.subject_id.encode("utf-8")
    predicate_b = v.predicate.encode("utf-8")
    object_b = v.object_id.encode("utf-8")
    properties_b = json.dumps(v.properties).encode("utf-8")

    header = _HEADER.pack(
        v.version_id,
        v.relationship_id,
        _date_to_int(v.valid_from),
        _date_to_int(v.valid_to) if v.valid_to is not None else _NO_DATE,
        _timestamp_to_int(v.system_from),
        _timestamp_to_int(v.system_to) if v.system_to is not None else _NO_TIMESTAMP,
        _date_to_int(v.last_confirmed),
        v.confidence if v.confidence is not None else math.nan,
        len(subject_b), len(predicate_b), len(object_b),
        len(properties_b), len(v.assertion_ids),
    )
    assertion_ids_b = struct.pack(f"<{len(v.assertion_ids)}q", *v.assertion_ids)
    return header + subject_b + predicate_b + object_b + properties_b + assertion_ids_b


def decode_version(raw: bytes) -> RelationshipVersion:
    (version_id, relationship_id, valid_from_i, valid_to_i, system_from_i, system_to_i,
     last_confirmed_i, confidence, subject_len, predicate_len, object_len,
     properties_len, n_assertion_ids) = _HEADER.unpack_from(raw, 0)

    offset = _HEADER.size
    subject_id = raw[offset:offset + subject_len].decode("utf-8")
    offset += subject_len
    predicate = raw[offset:offset + predicate_len].decode("utf-8")
    offset += predicate_len
    object_id = raw[offset:offset + object_len].decode("utf-8")
    offset += object_len
    properties = json.loads(raw[offset:offset + properties_len].decode("utf-8"))
    offset += properties_len
    assertion_ids = list(struct.unpack_from(f"<{n_assertion_ids}q", raw, offset))

    return RelationshipVersion(
        version_id=version_id,
        relationship_id=relationship_id,
        subject_id=subject_id,
        predicate=predicate,
        object_id=object_id,
        valid_from=_int_to_date(valid_from_i),
        valid_to=_int_to_date(valid_to_i) if valid_to_i != _NO_DATE else None,
        system_from=_int_to_timestamp(system_from_i),
        system_to=_int_to_timestamp(system_to_i) if system_to_i != _NO_TIMESTAMP else None,
        last_confirmed=_int_to_date(last_confirmed_i),
        confidence=None if math.isnan(confidence) else confidence,
        properties=properties,
        assertion_ids=assertion_ids,
    )
