"""Equivalence tests for coredb._native's detect_changepoints_indices - a
literal C++ port of coredb/signal.py's pure-Python binary segmentation.
Skips gracefully if no C++ toolchain built the extension (keeps this
optional acceleration from breaking installs/CI on a machine without one).
"""
import pytest

pytest.importorskip("coredb._native")

from coredb.signal import GraphSignal, detect_changepoints


def _make_points(values):
    return [(f"2020-01-{i + 1:02d}", v) for i, v in enumerate(values)]


CASES = {
    "clean_step": [1.0, 1.0, 1.0, 1.0, 10.0, 10.0, 10.0, 10.0],
    "flat": [5.0] * 12,
    "noisy_step": [1.0, 1.2, 0.9, 1.1, 9.8, 10.3, 9.9, 10.1, 10.0, 1.0, 0.8, 1.2],
    "two_steps": [1.0] * 6 + [5.0] * 6 + [9.0] * 6,
    "with_gaps": [1.0, 1.0, None, 1.0, 10.0, 10.0, None, 10.0, 10.0],
    "single_point_noise": [3.0, 3.1, 2.9, 3.2, 2.8, 8.0, 8.1, 7.9, 8.2, 7.8],
}


@pytest.mark.parametrize("name", CASES.keys())
def test_native_matches_python_for_each_case(name):
    points = _make_points(CASES[name])
    native = detect_changepoints(points, use_native=True)
    python = detect_changepoints(points, use_native=False)
    assert native == python


def test_native_matches_python_across_min_size_and_penalty():
    points = _make_points(CASES["two_steps"])
    for min_size in (1, 2, 3, 4):
        for penalty in (None, 0.0, 0.5, 5.0, 50.0):
            native = detect_changepoints(points, min_size=min_size, penalty=penalty, use_native=True)
            python = detect_changepoints(points, min_size=min_size, penalty=penalty, use_native=False)
            assert native == python, f"min_size={min_size} penalty={penalty}"


def test_use_native_true_without_extension_raises_clearly(monkeypatch):
    import coredb.signal as signal_mod
    monkeypatch.setattr(signal_mod, "_native", None)
    with pytest.raises(RuntimeError, match="coredb._native is not built"):
        detect_changepoints(_make_points(CASES["clean_step"]), use_native=True)


def test_graph_signal_changepoints_auto_detects_native():
    signal = GraphSignal(metric="degree", target="Hub", points=_make_points(CASES["clean_step"]))
    assert signal.changepoints() == detect_changepoints(signal.points, use_native=True)
