import pytest

import coredb


def test_assert_fact_rejects_nul_byte_in_identifier(db):
    with pytest.raises(coredb.ValidationError):
        db.assert_fact("A\x00B", "P", "O", "2026-01-01")


def test_assert_fact_rejects_empty_identifier(db):
    with pytest.raises(coredb.ValidationError):
        db.assert_fact("", "P", "O", "2026-01-01")


def test_assert_fact_rejects_malformed_date(db):
    with pytest.raises(coredb.ValidationError):
        db.assert_fact("A", "P", "O", "not-a-date")


def test_retract_fact_rejects_malformed_date(db):
    db.assert_fact("A", "P", "O", "2026-01-01")
    with pytest.raises(coredb.ValidationError):
        db.retract_fact("A", "P", "O", "not-a-date")


def test_sync_snapshot_rejects_nul_byte_in_object(db):
    with pytest.raises(coredb.ValidationError):
        db.sync_snapshot("A", "P", {"bad\x00object": 1}, "2026-01-01")


def test_retract_fact_rejects_inverted_interval(db):
    db.assert_fact("A", "P", "O", "2026-01-10")
    with pytest.raises(coredb.ValidationError):
        db.retract_fact("A", "P", "O", "2026-01-01")  # before valid_from


def test_sync_snapshot_confirm_does_not_move_last_confirmed_backwards(db):
    # Calling sync_snapshot with an earlier as_of_date than a prior call
    # must not move last_confirmed backwards below valid_from - that would
    # let a later close silently create an inverted interval.
    db.sync_snapshot("A", "P", {"O": 1}, "2026-01-10")
    db.sync_snapshot("A", "P", {"O": 1}, "2026-01-05")  # out of order
    version = db.history(("A", "P", "O"))[0]
    assert version.last_confirmed == "2026-01-10"


def test_map_full_raises_storage_error(tmp_path):
    tiny = coredb.open(str(tmp_path / "tiny.db"), map_size=50_000)
    with pytest.raises(coredb.StorageError):
        for i in range(2000):
            tiny.assert_fact(f"S{i}", "P", f"O{i}", "2026-01-01")
    tiny.close()


def test_schema_version_mismatch_raises(tmp_path):
    path = str(tmp_path / "mismatched.db")
    db = coredb.open(path)
    db.close()

    import lmdb
    env = lmdb.open(path, max_dbs=20)
    dbi = env.open_db(b"counters")
    with env.begin(write=True) as t:
        t.put(b"__schema_version__", b"999", db=dbi)
    env.close()

    with pytest.raises(coredb.SchemaVersionError):
        coredb.open(path)
