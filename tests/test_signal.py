from coredb.signal import GraphSignal


def test_join_is_an_inner_join_on_date():
    signal = GraphSignal(
        metric="degree", target="NVIDIA",
        points=[("2026-01-01", 1.0), ("2026-01-02", 2.0), ("2026-01-03", 2.0)],
    )
    external = {"2026-01-02": 100.5, "2026-01-04": 999.0}  # 01-01/01-03 missing, 01-04 not in signal

    joined = signal.join(external)
    assert joined == [("2026-01-02", 2.0, 100.5)]


def test_join_preserves_signal_date_order():
    signal = GraphSignal(
        metric="degree", target="NVIDIA",
        points=[("2026-01-01", 1.0), ("2026-01-02", 2.0), ("2026-01-03", 3.0)],
    )
    external = {"2026-01-03": 30.0, "2026-01-01": 10.0}

    joined = signal.join(external)
    assert [date for date, _, _ in joined] == ["2026-01-01", "2026-01-03"]


def test_join_with_no_overlap_returns_empty():
    signal = GraphSignal(metric="degree", target="NVIDIA", points=[("2026-01-01", 1.0)])
    assert signal.join({"2099-01-01": 5.0}) == []
