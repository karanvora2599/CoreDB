from coredb.signal import GraphSignal, detect_changepoints


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


def _dated(values):
    return [(f"2026-01-{i + 1:02d}", v) for i, v in enumerate(values)]


def test_detect_changepoints_finds_a_clean_step():
    points = _dated([1, 1, 1, 1, 10, 10, 10, 10])
    # Robust by construction: both child segments are internally constant
    # after the first split, so their own best-split gain is exactly 0,
    # guaranteeing no further splits regardless of the penalty used.
    assert detect_changepoints(points) == ["2026-01-05"]


def test_detect_changepoints_flat_series_has_none():
    points = _dated([5, 5, 5, 5, 5, 5])
    assert detect_changepoints(points) == []


def test_detect_changepoints_skips_none_values():
    points = [
        ("2026-01-01", 1.0), ("2026-01-02", None), ("2026-01-03", 1.0), ("2026-01-04", None),
        ("2026-01-05", 10.0), ("2026-01-06", 10.0), ("2026-01-07", 10.0), ("2026-01-08", 10.0),
    ]
    assert detect_changepoints(points) == ["2026-01-05"]


def test_detect_changepoints_too_few_points_returns_empty():
    assert detect_changepoints(_dated([1, 2, 3]), min_size=2) == []


def test_graph_signal_changepoints_matches_detect_changepoints():
    points = _dated([1, 1, 1, 1, 10, 10, 10, 10])
    signal = GraphSignal(metric="degree", target="NVIDIA", points=points)
    assert signal.changepoints() == detect_changepoints(points)
