import coredb


def test_stats_counts_entries(db):
    db.assert_fact("A", "P", "O1", "2026-01-01")
    db.assert_fact("A", "P", "O2", "2026-01-01")
    stats = db.stats()
    assert stats["relationships"] == 2
    assert stats["versions"] == 2
    assert stats["entities"] == 3  # A, O1, O2


def test_backup_produces_independent_copy(tmp_path):
    src_path = str(tmp_path / "src.db")
    src = coredb.open(src_path)
    src.assert_fact("A", "P", "O", "2026-01-01")

    backup_path = str(tmp_path / "backup.db")
    src.backup(backup_path)

    backup = coredb.open(backup_path)
    assert backup.stats() == src.stats()
    assert [v.object_id for v in backup.history(("A", "P", None))] == ["O"]

    # Independent: further writes to src shouldn't appear in the backup.
    src.assert_fact("A", "P", "O2", "2026-01-02")
    assert backup.stats() != src.stats()

    src.close()
    backup.close()


def test_dump_and_restore_round_trip(tmp_path):
    src = coredb.open(str(tmp_path / "src.db"))
    src.assert_fact("A", "P", "O", "2026-01-01", confidence=0.7)
    src.retract_fact("A", "P", "O", "2026-01-05")
    src.assert_fact("A", "P", "O", "2026-02-01", confidence=0.9)  # reopened interval

    dump_path = str(tmp_path / "dump.jsonl")
    src.dump(dump_path)

    restored = coredb.restore(dump_path, str(tmp_path / "restored.db"))
    history = restored.history(("A", "P", "O"))
    assert [(v.valid_from, v.valid_to, v.confidence) for v in history] == [
        ("2026-01-01", "2026-01-05", 0.7),
        ("2026-02-01", None, 0.9),
    ]

    src.close()
    restored.close()
