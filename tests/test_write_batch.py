import pytest

import coredb
from coredb.errors import StorageError


def test_write_batch_groups_multiple_mutations(db):
    with db.write_batch():
        db.assert_fact("A", "P", "O1", "2026-01-01")
        db.assert_fact("A", "P", "O2", "2026-01-01")
        db.retract_fact("A", "P", "O1", "2026-01-05")

    assert [v.object_id for v in db.as_of(("A", "P", None), "2026-01-01")] == ["O1", "O2"]
    assert db.as_of(("A", "P", "O1"), "2026-01-10") == []
    assert len(db.as_of(("A", "P", "O2"), "2026-01-10")) == 1


def test_write_batch_result_matches_non_batched_calls(tmp_path):
    # Same mutation sequence, one via write_batch, one without - must
    # produce identical persisted state (write_batch changes only
    # transaction grouping, not semantics).
    batched = coredb.open(str(tmp_path / "batched.db"))
    with batched.write_batch():
        batched.assert_fact("Hub", "TOUCHES", "A", "2026-01-01", confidence=0.5)
        batched.assert_fact("Hub", "TOUCHES", "B", "2026-01-03", confidence=0.7)
        batched.retract_fact("Hub", "TOUCHES", "A", "2026-01-10")
        batched.sync_snapshot("Hub", "PEER_OF", {"C": 0.9}, as_of_date="2026-01-05")

    plain = coredb.open(str(tmp_path / "plain.db"))
    plain.assert_fact("Hub", "TOUCHES", "A", "2026-01-01", confidence=0.5)
    plain.assert_fact("Hub", "TOUCHES", "B", "2026-01-03", confidence=0.7)
    plain.retract_fact("Hub", "TOUCHES", "A", "2026-01-10")
    plain.sync_snapshot("Hub", "PEER_OF", {"C": 0.9}, as_of_date="2026-01-05")

    for pattern in [("Hub", "TOUCHES", None), ("Hub", "PEER_OF", None)]:
        batched_history = [(v.object_id, v.valid_from, v.valid_to, v.confidence)
                            for v in batched.history(pattern)]
        plain_history = [(v.object_id, v.valid_from, v.valid_to, v.confidence)
                          for v in plain.history(pattern)]
        assert batched_history == plain_history

    batched.close()
    plain.close()


def test_write_batch_aborts_all_on_exception(db):
    db.assert_fact("Pre", "P", "O", "2026-01-01")
    with pytest.raises(ValueError):
        with db.write_batch():
            db.assert_fact("A", "P", "O", "2026-01-01")
            raise ValueError("boom")

    # The whole batch should have been rolled back - "A" was never committed.
    assert db.as_of(("A", "P", "O"), "2026-01-01") == []
    # Unrelated pre-existing data is untouched.
    assert len(db.as_of(("Pre", "P", "O"), "2026-01-01")) == 1


def test_write_batch_is_not_reentrant(db):
    with db.write_batch():
        with pytest.raises(StorageError, match="not reentrant"):
            with db.write_batch():
                pass


def test_write_batch_sync_snapshot_participates(db):
    with db.write_batch():
        db.sync_snapshot("Hub", "CO_OCCURS_WITH", {"X": 0.5, "Y": 0.6}, as_of_date="2026-01-01")
    assert {v.object_id for v in db.as_of(("Hub", "CO_OCCURS_WITH", None), "2026-01-01")} == {"X", "Y"}


def test_restore_chunks_across_multiple_batches(tmp_path):
    src = coredb.open(str(tmp_path / "src.db"))
    n = coredb._RESTORE_BATCH_SIZE + 250  # forces at least two write_batch chunks
    for i in range(n):
        src.assert_fact(f"S{i}", "LINK", f"O{i}", "2026-01-01")
    dump_path = str(tmp_path / "dump.jsonl")
    src.dump(dump_path)

    restored = coredb.restore(dump_path, str(tmp_path / "restored.db"))
    assert restored.stats()["relationships"] == n
    assert restored.stats() == src.stats()

    src.close()
    restored.close()
