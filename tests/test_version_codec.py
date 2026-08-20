"""Round-trip correctness tests for coredb/storage/version_codec.py (the
"VersionCore" compact binary encoding, M10 Part 4) - this is the
correctness-critical layer under every read/write in engine.py, so it
gets its own direct coverage in addition to the full test suite
exercising it indirectly through every other test.
"""
import math

import pytest

from coredb.model import RelationshipVersion
from coredb.storage.version_codec import decode_version, encode_version


def _version(**overrides):
    defaults = dict(
        version_id=1, relationship_id=2, subject_id="NVIDIA", predicate="SUPPLIED_BY",
        object_id="TSMC", valid_from="2026-01-01", valid_to=None,
        system_from="2026-01-01T00:00:00.123456+00:00", system_to=None,
        last_confirmed="2026-01-01", confidence=0.9, properties={}, assertion_ids=[],
    )
    defaults.update(overrides)
    return RelationshipVersion(**defaults)


def test_round_trip_basic_open_version():
    v = _version()
    assert decode_version(encode_version(v)) == v


def test_round_trip_closed_version_with_all_fields():
    v = _version(
        valid_to="2026-06-15", system_to="2026-06-15T23:59:59.999999+00:00",
        properties={"nested": {"a": [1, 2, 3]}, "k": None}, assertion_ids=[10, 20, 30],
    )
    assert decode_version(encode_version(v)) == v


def test_round_trip_zero_confidence_distinct_from_none():
    zero = _version(confidence=0.0)
    none = _version(confidence=None)
    assert decode_version(encode_version(zero)).confidence == 0.0
    assert decode_version(encode_version(none)).confidence is None


def test_round_trip_empty_and_unicode_strings():
    v = _version(subject_id="", predicate="P", object_id="Unicode_éè_中文")
    assert decode_version(encode_version(v)) == v


def test_round_trip_extreme_dates():
    v = _version(valid_from="0001-01-01", valid_to="9999-12-31", last_confirmed="9999-12-31")
    assert decode_version(encode_version(v)) == v


def test_round_trip_microsecond_precision_timestamps():
    # Exercises the exact-integer (not float-seconds) timestamp codec path -
    # a float round-trip through `dt.timestamp() * 1e6` can lose precision
    # at present-day magnitudes; this must not.
    for micros in ["000001", "999999", "500000", "000000"]:
        ts = f"2026-03-14T09:26:53.{micros}+00:00" if micros != "000000" else "2026-03-14T09:26:53+00:00"
        v = _version(system_from=ts)
        assert decode_version(encode_version(v)).system_from == ts


def test_round_trip_large_ids_and_many_assertion_ids():
    v = _version(version_id=2**40, relationship_id=2**40 - 1, assertion_ids=list(range(500)))
    assert decode_version(encode_version(v)) == v


def test_round_trip_no_assertion_ids():
    v = _version(assertion_ids=[])
    decoded = decode_version(encode_version(v))
    assert decoded.assertion_ids == []
